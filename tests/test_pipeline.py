"""End-to-end tests over an in-memory database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.alerts import render_email, select_candidates
from app.config import Settings
from app.ingest import rescore_all, upsert_events
from app.models import Alert, Artist, Base, Event, PriceSnapshot, Score
from app.sources.base import RawEvent

NOW = datetime.now(timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        yield s


def seed_scored_event(
    session,
    source_id: str = "e1",
    prices: tuple[float, ...] = (50.0, 58.0, 67.0, 78.0),
    popularity: int = 82,
    followers: int = 5_000_000,
    **event_kwargs,
) -> Event:
    """Create an event with enough real signal to clear MIN_CONFIDENCE.

    Selection tests care about filtering and ranking, not cold-start handling,
    so they need a position the scorer can actually form an opinion about:
    known artist metrics plus a price history spanning several days.
    """
    upsert_events(session, [raw(source_id=source_id, min_price=prices[-1], **event_kwargs)])
    session.flush()

    event = session.scalar(
        select(Event).where(Event.source_id == source_id)
    )
    artist = session.get(Artist, event.artist_id)
    artist.popularity = popularity
    artist.followers = followers

    session.query(PriceSnapshot).filter(
        PriceSnapshot.event_id == event.id
    ).delete()
    for i, price in enumerate(prices):
        session.add(
            PriceSnapshot(
                event_id=event.id,
                captured_at=NOW - timedelta(days=2 * (len(prices) - 1 - i)),
                min_price=price,
                max_price=price * 3,
                status_code="onsale",
            )
        )
    session.commit()
    return event


def raw(source_id="e1", min_price=50.0, **kwargs) -> RawEvent:
    defaults = dict(
        source="ticketmaster",
        source_id=source_id,
        name="Test Show",
        artist_name="Test Artist",
        venue_name="Paramount",
        venue_capacity=2800,
        city="Seattle",
        state="WA",
        dma_id="324",
        starts_at=NOW + timedelta(days=35),
        onsale_starts_at=NOW - timedelta(days=20),
        url="https://www.ticketmaster.com/event/e1",
        min_price=min_price,
        max_price=min_price * 3,
        currency="USD",
        status_code="onsale",
        extra={"tour_dates_total": 1, "tour_dates_in_market": 1,
               "transfer_blocked": False, "transfer_block_reason": None},
    )
    defaults.update(kwargs)
    return RawEvent(**defaults)


class TestUpsert:
    def test_creates_event_and_snapshot(self, session):
        created, updated = upsert_events(session, [raw()])
        session.commit()
        assert (created, updated) == (1, 0)
        assert session.scalar(select(Event)).name == "Test Show"
        assert len(session.scalars(select(PriceSnapshot)).all()) == 1

    def test_second_ingest_updates_rather_than_duplicates(self, session):
        upsert_events(session, [raw()])
        session.commit()
        created, updated = upsert_events(session, [raw()])
        session.commit()
        assert (created, updated) == (0, 1)
        assert len(session.scalars(select(Event)).all()) == 1

    def test_price_change_appends_a_snapshot(self, session):
        upsert_events(session, [raw(min_price=50.0)])
        session.commit()
        upsert_events(session, [raw(min_price=75.0)])
        session.commit()
        snaps = session.scalars(
            select(PriceSnapshot).order_by(PriceSnapshot.id)
        ).all()
        assert [s.min_price for s in snaps] == [50.0, 75.0]

    def test_unchanged_price_does_not_append(self, session):
        upsert_events(session, [raw(min_price=50.0)])
        session.commit()
        upsert_events(session, [raw(min_price=50.0)])
        session.commit()
        assert len(session.scalars(select(PriceSnapshot)).all()) == 1

    def test_artist_is_created_once_and_linked(self, session):
        upsert_events(session, [raw(source_id="a"), raw(source_id="b")])
        session.commit()
        events = session.scalars(select(Event)).all()
        artist_ids = {e.artist_id for e in events}
        assert len(artist_ids) == 1 and None not in artist_ids

    def test_transfer_block_is_persisted(self, session):
        blocked = raw(
            restrictions_text="Non-transferable.",
            extra={"tour_dates_total": 1, "tour_dates_in_market": 1,
                   "transfer_blocked": True,
                   "transfer_block_reason": "tickets marked non-transferable"},
        )
        upsert_events(session, [blocked])
        session.commit()
        event = session.scalar(select(Event))
        assert event.transfer_blocked is True
        assert event.transfer_block_reason


class TestRescoreAndSelect:
    def _settings(self, **kwargs) -> Settings:
        base = dict(
            alert_score_threshold=0.0,
            alert_cooldown_hours=48,
            max_alerts_per_email=10,
            email_dry_run=True,
        )
        base.update(kwargs)
        return Settings(**base)

    def test_rescore_persists_scores(self, session):
        upsert_events(session, [raw()])
        session.commit()
        rescore_all(session, now=NOW)
        session.commit()
        score = session.scalar(select(Score))
        assert score is not None
        assert 0.0 <= score.score <= 1.0
        assert "_reasons" in score.factors

    def test_past_events_are_not_rescored(self, session):
        upsert_events(session, [raw(starts_at=NOW - timedelta(days=5))])
        session.commit()
        assert rescore_all(session, now=NOW) == []

    def test_vetoed_events_are_excluded_from_candidates(self, session):
        seed_scored_event(session, source_id="good")
        seed_scored_event(
            session,
            source_id="bad",
            extra={"tour_dates_total": 1, "tour_dates_in_market": 1,
                   "transfer_blocked": True,
                   "transfer_block_reason": "resale prohibited"},
        )
        rescore_all(session, now=NOW)
        session.commit()

        candidates = select_candidates(session, self._settings(), now=NOW)
        assert {c.event.source_id for c in candidates} == {"good"}

    def test_only_the_latest_score_per_event_is_used(self, session):
        seed_scored_event(session)
        rescore_all(session, now=NOW - timedelta(hours=6))
        rescore_all(session, now=NOW)
        session.commit()

        candidates = select_candidates(session, self._settings(), now=NOW)
        assert len(candidates) == 1
        assert candidates[0].score.computed_at.replace(tzinfo=timezone.utc) == NOW

    def test_cooldown_suppresses_repeat_alerts(self, session):
        seed_scored_event(session)
        rescore_all(session, now=NOW)
        session.commit()

        settings = self._settings()
        assert len(select_candidates(session, settings, now=NOW)) == 1

        event = session.scalar(select(Event))
        session.add(Alert(event_id=event.id, score=0.9, sent_at=NOW - timedelta(hours=2)))
        session.commit()
        assert select_candidates(session, settings, now=NOW) == []

    def test_cooldown_expires(self, session):
        seed_scored_event(session)
        rescore_all(session, now=NOW)
        session.commit()

        event = session.scalar(select(Event))
        session.add(Alert(event_id=event.id, score=0.9, sent_at=NOW - timedelta(hours=99)))
        session.commit()
        assert len(select_candidates(session, self._settings(), now=NOW)) == 1

    def test_threshold_filters_low_scores(self, session):
        upsert_events(session, [raw()])
        session.commit()
        rescore_all(session, now=NOW)
        session.commit()
        strict = self._settings(alert_score_threshold=0.999)
        assert select_candidates(session, strict, now=NOW) == []

    def test_results_are_ranked_by_conviction(self, session):
        seed_scored_event(session, source_id="a", prices=(50.0, 60.0, 72.0, 88.0))
        seed_scored_event(session, source_id="b", prices=(90.0, 88.0, 86.0, 85.0),
                          popularity=45, followers=60_000)
        rescore_all(session, now=NOW)
        session.commit()
        candidates = select_candidates(session, self._settings(), now=NOW)
        ranked = [c.score.score * c.score.confidence for c in candidates]
        assert ranked == sorted(ranked, reverse=True)


class TestEmailRendering:
    def test_renders_both_parts_with_links(self, session):
        seed_scored_event(session)
        rescore_all(session, now=NOW)
        session.commit()
        candidates = select_candidates(
            session, Settings(alert_score_threshold=0.0), now=NOW
        )
        text, html = render_email(candidates, now=NOW)

        assert "Test Show" in text and "Test Show" in html
        assert "ticketmaster.com/event/e1" in html
        assert "stubhub.com" in html  # resale comps link
        assert "non-transferable" in html.lower()  # the disclaimer
        assert "Buy:" in text

    def test_escapes_event_names(self, session):
        seed_scored_event(session, name="Bad <script>alert(1)</script> Show")
        rescore_all(session, now=NOW)
        session.commit()
        candidates = select_candidates(
            session, Settings(alert_score_threshold=0.0), now=NOW
        )
        _, html = render_email(candidates, now=NOW)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html
