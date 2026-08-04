from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import (
    Base,
    Event,
    Holding,
    HoldingStatus,
    HoldingValuation,
)
from app.portfolio import (
    add_holding,
    build_report,
    mark_sold,
    render_report,
    sms_summary,
)
from app.sources.ticketmaster import event_id_from_url

NOW = datetime.now(timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        yield s


def value(session, holding_id: int, unit_value: float, days_ago: int = 0) -> None:
    session.add(
        HoldingValuation(
            holding_id=holding_id,
            captured_at=NOW - timedelta(days=days_ago),
            market_min_price=unit_value,
            market_max_price=unit_value * 3,
            unit_value=unit_value,
            status_code="onsale",
        )
    )
    session.commit()


class TestEventIdFromUrl:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.ticketmaster.com/event/0E006012ABCD1234", "0E006012ABCD1234"),
            (
                "https://www.ticketmaster.com/artist-tickets-seattle/event/1AfZk9a7b2",
                "1AfZk9a7b2",
            ),
            ("https://www.ticketmaster.com/event/vvG1zZ9abcd?x=1", "vvG1zZ9abcd"),
            ("https://www.ticketmaster.com/search?q=foo", None),
            ("not a url", None),
            (None, None),
        ],
    )
    def test_extraction(self, url, expected):
        assert event_id_from_url(url) == expected


class TestAddHolding:
    def test_records_cost_basis_with_fees_amortised(self, session):
        h = add_holding(
            session,
            event_url="https://www.ticketmaster.com/event/ABC123",
            quantity=2,
            unit_cost=100.0,
            fees=40.0,
        )
        session.commit()
        assert h.total_cost == 240.0
        assert h.cost_per_ticket == 120.0
        assert h.source_event_id == "ABC123"
        assert h.status == HoldingStatus.HELD

    def test_links_and_backfills_from_a_known_event(self, session):
        session.add(
            Event(
                source="ticketmaster",
                source_id="ABC123",
                name="Real Show",
                artist_name="Real Artist",
                venue_name="Paramount",
                city="Seattle",
                starts_at=NOW + timedelta(days=30),
                url="https://www.ticketmaster.com/event/ABC123",
            )
        )
        session.commit()

        h = add_holding(
            session, event_url="https://www.ticketmaster.com/event/ABC123",
            quantity=1, unit_cost=80.0,
        )
        session.commit()
        assert h.event_id is not None
        assert h.event_name == "Real Show"
        assert h.artist_name == "Real Artist"
        assert h.city == "Seattle"

    def test_manual_purchase_without_url(self, session):
        h = add_holding(session, event_name="Some Festival", quantity=3, unit_cost=210.0)
        session.commit()
        assert h.source == "manual"
        assert h.source_event_id is None
        assert h.event_name == "Some Festival"

    def test_requires_url_or_name(self, session):
        with pytest.raises(ValueError, match="need a Ticketmaster event URL"):
            add_holding(session, quantity=1, unit_cost=50.0)

    def test_rejects_bad_quantity_and_cost(self, session):
        with pytest.raises(ValueError, match="quantity must be positive"):
            add_holding(session, event_name="X", quantity=0, unit_cost=50.0)
        with pytest.raises(ValueError, match="cannot be negative"):
            add_holding(session, event_name="X", quantity=1, unit_cost=-5.0)


class TestMarkSold:
    def test_realized_pnl_nets_out_both_fee_legs(self, session):
        h = add_holding(session, event_name="X", quantity=2, unit_cost=100.0, fees=40.0)
        session.commit()
        mark_sold(session, h.id, sale_unit_price=180.0, sale_fees=50.0)
        session.commit()
        # cost 240, proceeds 2*180 - 50 = 310
        assert h.realized_pnl == pytest.approx(70.0)
        assert h.status == HoldingStatus.SOLD
        assert not h.is_open

    def test_realized_pnl_is_none_while_held(self, session):
        h = add_holding(session, event_name="X", quantity=1, unit_cost=100.0)
        session.commit()
        assert h.realized_pnl is None

    def test_loss_is_reported_negative(self, session):
        h = add_holding(session, event_name="X", quantity=1, unit_cost=200.0, fees=20.0)
        session.commit()
        mark_sold(session, h.id, sale_unit_price=90.0, sale_fees=10.0)
        session.commit()
        assert h.realized_pnl == pytest.approx(90.0 - 10.0 - 220.0)

    def test_unknown_id_raises(self, session):
        with pytest.raises(ValueError, match="no holding with id"):
            mark_sold(session, 999, sale_unit_price=10.0)


class TestBuildReport:
    def test_marks_position_and_computes_pnl(self, session):
        h = add_holding(session, event_name="X", quantity=2, unit_cost=100.0, fees=40.0)
        session.commit()
        value(session, h.id, unit_value=180.0)

        reports, totals = build_report(session)
        r = reports[0]
        assert r.unit_value == 180.0
        assert r.market_value == 360.0
        assert r.unrealized_pnl == pytest.approx(120.0)  # 360 - 240
        assert r.pnl_pct == pytest.approx(0.5)
        assert totals["positions"] == 1
        assert totals["tickets"] == 2
        assert totals["unrealized_pnl"] == pytest.approx(120.0)

    def test_day_change_uses_the_two_most_recent_marks(self, session):
        h = add_holding(session, event_name="X", quantity=1, unit_cost=100.0)
        session.commit()
        value(session, h.id, unit_value=100.0, days_ago=2)
        value(session, h.id, unit_value=120.0, days_ago=0)

        r = build_report(session)[0][0]
        assert r.day_change == pytest.approx(20.0)
        assert r.day_change_pct == pytest.approx(0.2)

    def test_first_mark_has_no_day_change(self, session):
        h = add_holding(session, event_name="X", quantity=1, unit_cost=100.0)
        session.commit()
        value(session, h.id, unit_value=110.0)
        r = build_report(session)[0][0]
        assert r.day_change is None and r.day_change_pct is None

    def test_unpriced_positions_are_flagged_not_counted(self, session):
        priced = add_holding(session, event_name="Priced", quantity=1, unit_cost=100.0)
        add_holding(session, event_name="Unpriced", quantity=1, unit_cost=500.0)
        session.commit()
        value(session, priced.id, unit_value=150.0)

        reports, totals = build_report(session)
        assert totals["positions"] == 2
        assert totals["unpriced"] == 1
        # The 500 basis of the unpriced position must not drag P/L negative.
        assert totals["priced_cost"] == pytest.approx(100.0)
        assert totals["unrealized_pnl"] == pytest.approx(50.0)
        assert totals["unrealized_pct"] == pytest.approx(0.5)
        assert any(r.stale for r in reports)

    def test_sold_positions_excluded_from_open_totals_but_add_to_realized(self, session):
        held = add_holding(session, event_name="Held", quantity=1, unit_cost=100.0)
        sold = add_holding(session, event_name="Sold", quantity=1, unit_cost=100.0)
        session.commit()
        value(session, held.id, unit_value=150.0)
        mark_sold(session, sold.id, sale_unit_price=200.0)
        session.commit()

        reports, totals = build_report(session, include_sold=False)
        assert totals["positions"] == 1
        assert [r.holding.event_name for r in reports] == ["Held"]
        assert totals["realized_pnl"] == pytest.approx(100.0)

    def test_include_sold_returns_both(self, session):
        a = add_holding(session, event_name="A", quantity=1, unit_cost=10.0)
        session.commit()
        mark_sold(session, a.id, sale_unit_price=20.0)
        add_holding(session, event_name="B", quantity=1, unit_cost=10.0)
        session.commit()
        reports, _ = build_report(session, include_sold=True)
        assert len(reports) == 2

    def test_biggest_movers_sort_first(self, session):
        calm = add_holding(session, event_name="Calm", quantity=1, unit_cost=100.0)
        wild = add_holding(session, event_name="Wild", quantity=1, unit_cost=100.0)
        session.commit()
        value(session, calm.id, unit_value=100.0, days_ago=2)
        value(session, calm.id, unit_value=102.0, days_ago=0)
        value(session, wild.id, unit_value=100.0, days_ago=2)
        value(session, wild.id, unit_value=160.0, days_ago=0)

        reports, _ = build_report(session)
        assert reports[0].holding.event_name == "Wild"

    def test_empty_portfolio_reports_nothing_rather_than_erroring(self, session):
        reports, totals = build_report(session)
        assert reports == []
        assert totals["positions"] == 0
        assert totals["total_cost"] == 0.0
        # Nothing priced means no P/L opinion — None, not a $0 that would read
        # as a flat position.
        assert totals["total_value"] is None
        assert totals["unrealized_pnl"] is None
        assert totals["unrealized_pct"] is None

    def test_all_unpriced_reports_no_value_rather_than_zero(self, session):
        add_holding(session, event_name="Unpriced", quantity=1, unit_cost=320.0)
        session.commit()
        _, totals = build_report(session)
        assert totals["total_cost"] == 320.0
        assert totals["priced_positions"] == 0
        # A $320 basis against a $0 value would look like a total loss.
        assert totals["total_value"] is None
        assert totals["unrealized_pnl"] is None


class TestReportRendering:
    def _seeded(self, session):
        h = add_holding(
            session,
            event_url="https://www.ticketmaster.com/event/ABC123",
            event_name="Phoebe Bridgers",
            quantity=2,
            unit_cost=90.0,
            fees=30.0,
            section="Orch",
            row="F",
        )
        h.event_starts_at = NOW + timedelta(days=25)
        h.venue_name = "Paramount"
        h.city = "Seattle"
        session.commit()
        value(session, h.id, unit_value=105.0, days_ago=2)
        value(session, h.id, unit_value=140.0, days_ago=0)
        return build_report(session)

    def test_text_and_html_carry_the_numbers(self, session):
        reports, totals = self._seeded(session)
        text, html = render_report(reports, totals, now=NOW)

        assert "Phoebe Bridgers" in text and "Phoebe Bridgers" in html
        # basis 210/2 = 105/ea, now 140/ea -> +70 total
        assert "$105" in text
        assert "$140" in html
        assert "stubhub.com" in html
        # The primary-vs-resale caveat must always ship with the numbers.
        assert "proxy" in html.lower()
        assert "PRIMARY" in text or "primary" in text

    def test_html_escapes_event_names(self, session):
        h = add_holding(
            session, event_name="<script>alert(1)</script>", quantity=1, unit_cost=10.0
        )
        session.commit()
        value(session, h.id, unit_value=20.0)
        reports, totals = build_report(session)
        _, html = render_report(reports, totals, now=NOW)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_negative_pnl_renders_with_a_minus_sign(self, session):
        h = add_holding(session, event_name="Dud", quantity=1, unit_cost=200.0)
        session.commit()
        value(session, h.id, unit_value=50.0)
        reports, totals = build_report(session)
        text, html = render_report(reports, totals, now=NOW)
        assert "-$150" in text
        assert "-$150" in html

    def test_sms_summary_is_compact_and_names_movers(self, session):
        reports, totals = self._seeded(session)
        movers = [r for r in reports if (r.day_change_pct or 0) >= 0.15]
        body = sms_summary(totals, movers)
        assert len(body) <= 1000
        assert "unrealized" in body
        assert "Phoebe Bridgers" in body


class TestValuationBookkeeping:
    def test_valuation_cascade_deletes_with_holding(self, session):
        h = add_holding(session, event_name="X", quantity=1, unit_cost=10.0)
        session.commit()
        value(session, h.id, unit_value=20.0)
        session.delete(h)
        session.commit()
        assert session.scalars(select(HoldingValuation)).all() == []

    def test_open_holdings_query_covers_held_and_listed(self, session):
        a = add_holding(session, event_name="A", quantity=1, unit_cost=10.0)
        b = add_holding(session, event_name="B", quantity=1, unit_cost=10.0)
        c = add_holding(session, event_name="C", quantity=1, unit_cost=10.0)
        session.commit()
        b.status = HoldingStatus.LISTED
        mark_sold(session, c.id, sale_unit_price=15.0)
        session.commit()

        open_ids = {
            h.id
            for h in session.scalars(
                select(Holding).where(
                    Holding.status.in_([HoldingStatus.HELD, HoldingStatus.LISTED])
                )
            ).all()
        }
        assert open_ids == {a.id, b.id}
