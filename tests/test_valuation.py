"""Tests for refresh_valuations, which talks to the module-level session."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import db as db_module
from app.config import Settings
from app.models import (
    Base,
    Event,
    Holding,
    HoldingStatus,
    HoldingValuation,
)
from app.portfolio import add_holding, refresh_valuations

NOW = datetime.now(timezone.utc)


@pytest.fixture
def bound_db(tmp_path, monkeypatch):
    """Point the app's global session factory at a throwaway database.

    `refresh_valuations` opens its own sessions via `session_scope`, so it can't
    take an injected session the way the pure functions can.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", Session)
    return Session


def settings(**kwargs) -> Settings:
    base = dict(ticketmaster_api_key="testkey", requests_per_second=0, market_dma_ids="324")
    base.update(kwargs)
    return Settings(**base)


TM_EVENT = {
    "id": "REMOTE1",
    "name": "Remote Show",
    "url": "https://www.ticketmaster.com/event/REMOTE1",
    "dates": {"start": {"dateTime": "2026-12-01T02:00:00Z"},
              "status": {"code": "onsale"}},
    "priceRanges": [{"min": 130.0, "max": 400.0, "currency": "USD"}],
    "_embedded": {
        "venues": [{"name": "Remote Arena", "city": {"name": "Portland"}}],
        "attractions": [{"id": "K9", "name": "Remote Artist"}],
    },
}


class TestLocalPricing:
    @pytest.mark.asyncio
    async def test_prefers_fresh_local_price_without_any_api_call(self, bound_db):
        with bound_db() as s:
            event = Event(
                source="ticketmaster",
                source_id="LOCAL1",
                name="Local Show",
                starts_at=NOW + timedelta(days=30),
                min_price=90.0,
                max_price=300.0,
                status_code="onsale",
                last_seen_at=NOW,
            )
            s.add(event)
            s.commit()
            h = add_holding(
                s,
                event_url="https://www.ticketmaster.com/event/LOCAL1",
                quantity=2,
                unit_cost=60.0,
                fees=20.0,
            )
            s.commit()
            holding_id = h.id

        # respx with no routes registered: any HTTP call raises, proving the
        # local path made none.
        with respx.mock(assert_all_called=False):
            count = await refresh_valuations(settings(), now=NOW)

        assert count == 1
        with bound_db() as s:
            v = s.scalar(select(HoldingValuation))
            assert v.unit_value == 90.0
            assert v.market_max_price == 300.0
            assert v.error is None
            # basis = (60*2 + 20)/2 = 70; (90-70)*2 = 40
            assert v.unrealized_pnl == pytest.approx(40.0)
            assert v.holding_id == holding_id

    @pytest.mark.asyncio
    @respx.mock
    async def test_stale_local_price_falls_back_to_the_api(self, bound_db):
        respx.get(
            "https://app.ticketmaster.com/discovery/v2/events/REMOTE1.json"
        ).mock(return_value=httpx.Response(200, json=TM_EVENT))

        with bound_db() as s:
            s.add(
                Event(
                    source="ticketmaster",
                    source_id="REMOTE1",
                    name="Remote Show",
                    starts_at=NOW + timedelta(days=90),
                    min_price=99.0,
                    status_code="onsale",
                    # Older than LOCAL_PRICE_MAX_AGE, so it must not be trusted.
                    last_seen_at=NOW - timedelta(days=3),
                )
            )
            s.commit()
            add_holding(
                s,
                event_url="https://www.ticketmaster.com/event/REMOTE1",
                quantity=1,
                unit_cost=100.0,
            )
            s.commit()

        await refresh_valuations(settings(), now=NOW)

        with bound_db() as s:
            v = s.scalar(select(HoldingValuation))
            # 130 from the API, not the stale 99 on the local row.
            assert v.unit_value == 130.0
            assert v.unrealized_pnl == pytest.approx(30.0)


class TestUnpriceableHoldings:
    @pytest.mark.asyncio
    async def test_manual_holding_records_an_explanatory_error(self, bound_db):
        with bound_db() as s:
            add_holding(s, event_name="Cash purchase", quantity=1, unit_cost=200.0)
            s.commit()

        count = await refresh_valuations(settings(ticketmaster_api_key=""), now=NOW)

        assert count == 1
        with bound_db() as s:
            v = s.scalar(select(HoldingValuation))
            assert v.unit_value is None
            assert "re-add with --url" in v.error

    @pytest.mark.asyncio
    @respx.mock
    async def test_failed_lookup_is_recorded_not_raised(self, bound_db):
        respx.get(
            "https://app.ticketmaster.com/discovery/v2/events/GONE123456.json"
        ).mock(return_value=httpx.Response(404, json={"errors": []}))

        with bound_db() as s:
            add_holding(
                s,
                event_url="https://www.ticketmaster.com/event/GONE123456",
                quantity=1,
                unit_cost=50.0,
            )
            s.commit()

        await refresh_valuations(settings(), now=NOW)

        with bound_db() as s:
            v = s.scalar(select(HoldingValuation))
            assert v.unit_value is None
            assert v.error == "source lookup failed"


class TestExpiry:
    @pytest.mark.asyncio
    async def test_past_event_retires_the_position(self, bound_db):
        with bound_db() as s:
            h = add_holding(s, event_name="Old Show", quantity=1, unit_cost=50.0)
            h.event_starts_at = NOW - timedelta(days=1)
            s.commit()
            holding_id = h.id

        count = await refresh_valuations(settings(), now=NOW)

        # Expired positions are retired, not valued.
        assert count == 0
        with bound_db() as s:
            assert s.get(Holding, holding_id).status == HoldingStatus.EXPIRED
            assert s.scalars(select(HoldingValuation)).all() == []

    @pytest.mark.asyncio
    async def test_sold_positions_are_skipped_entirely(self, bound_db):
        with bound_db() as s:
            h = add_holding(s, event_name="Sold Show", quantity=1, unit_cost=50.0)
            h.status = HoldingStatus.SOLD
            s.commit()

        assert await refresh_valuations(settings(), now=NOW) == 0
        with bound_db() as s:
            assert s.scalars(select(HoldingValuation)).all() == []

    @pytest.mark.asyncio
    async def test_no_holdings_is_a_clean_no_op(self, bound_db):
        assert await refresh_valuations(settings(), now=NOW) == 0
