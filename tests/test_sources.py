from __future__ import annotations

import httpx
import pytest
import respx

from app.sources.axs import AxsSource
from app.sources.base import detect_transfer_block
from app.sources.ticketmaster import TicketmasterSource, _price_span


class TestDetectTransferBlock:
    @pytest.mark.parametrize(
        "text",
        [
            "These tickets are non-transferable.",
            "Mobile only. NON TRANSFERABLE tickets.",
            "Resale is prohibited for this event.",
            "No resale permitted.",
            "Paperless ticketing in effect.",
            "Credit card entry required at the door.",
            "Face value exchange only.",
        ],
    )
    def test_flags_blocking_language(self, text):
        blocked, reason = detect_transfer_block(text)
        assert blocked, text
        assert reason

    @pytest.mark.parametrize(
        "text",
        [
            "Doors open at 7pm. All ages welcome.",
            "This is a general admission standing show.",
            "Tickets are transferable via the app.",
            "",
            None,
        ],
    )
    def test_allows_ordinary_prose(self, text):
        blocked, _ = detect_transfer_block(text)
        assert not blocked

    def test_scans_multiple_fields(self):
        blocked, _ = detect_transfer_block("Doors at 7", None, "No resale.")
        assert blocked


class TestPriceSpan:
    def test_none_when_absent(self):
        assert _price_span(None) == (None, None, None)
        assert _price_span([]) == (None, None, None)

    def test_widest_span_across_tiers(self):
        ranges = [
            {"min": 45.0, "max": 120.0, "currency": "USD"},
            {"min": 250.0, "max": 900.0, "currency": "USD"},
        ]
        assert _price_span(ranges) == (45.0, 900.0, "USD")

    def test_tolerates_partial_entries(self):
        ranges = [{"currency": "USD"}, {"min": 30.0, "currency": "USD"}]
        assert _price_span(ranges) == (30.0, 30.0, "USD")


TM_PAYLOAD = {
    "_embedded": {
        "events": [
            {
                "id": "vv1",
                "name": "Big Show",
                "url": "https://www.ticketmaster.com/event/vv1",
                "pleaseNote": "All sales final. Tickets are non-transferable.",
                "images": [
                    {"url": "https://img/wide.jpg", "ratio": "16_9", "width": 1024},
                    {"url": "https://img/sq.jpg", "ratio": "1_1", "width": 500},
                ],
                "dates": {
                    "start": {"dateTime": "2026-09-10T02:00:00Z"},
                    "status": {"code": "onsale"},
                },
                "sales": {
                    "public": {
                        "startDateTime": "2026-05-01T15:00:00Z",
                        "endDateTime": "2026-09-10T01:00:00Z",
                    }
                },
                "priceRanges": [{"min": 55.0, "max": 250.0, "currency": "USD"}],
                "_embedded": {
                    "venues": [
                        {
                            "name": "Paramount Theatre",
                            "capacity": "2807",
                            "city": {"name": "Seattle"},
                            "state": {"stateCode": "WA"},
                        }
                    ],
                    "attractions": [{"id": "K1", "name": "Big Show Artist"}],
                },
            },
            {
                "id": "vv2",
                "name": "Other Show",
                "dates": {"start": {"dateTime": "2026-09-12T02:00:00Z"},
                          "status": {"code": "onsale"}},
                "_embedded": {"attractions": [{"id": "K1", "name": "Big Show Artist"}]},
            },
        ]
    },
    "page": {"totalPages": 1, "totalElements": 2, "number": 0},
}

EMPTY_PAYLOAD = {"page": {"totalPages": 0, "totalElements": 0, "number": 0}}


@pytest.mark.asyncio
@respx.mock
async def test_ticketmaster_parses_and_annotates():
    route = respx.get("https://app.ticketmaster.com/discovery/v2/events.json")
    # First window returns data; every later window is empty.
    route.side_effect = [
        httpx.Response(200, json=TM_PAYLOAD),
        *[httpx.Response(200, json=EMPTY_PAYLOAD) for _ in range(40)],
    ]

    async with TicketmasterSource(
        api_key="k", dma_ids=["324"], lookahead_days=30, requests_per_second=0
    ) as tm:
        events = await tm.fetch_events()

    assert len(events) == 2
    big = next(e for e in events if e.source_id == "vv1")

    assert big.artist_name == "Big Show Artist"
    assert big.venue_name == "Paramount Theatre"
    assert big.venue_capacity == 2807
    assert big.city == "Seattle" and big.state == "WA"
    assert big.min_price == 55.0 and big.max_price == 250.0
    assert big.image_url == "https://img/wide.jpg"  # prefers 16:9
    assert big.starts_at is not None and big.starts_at.year == 2026
    assert big.onsale_starts_at is not None

    # Restriction prose is detected during annotation.
    assert big.extra["transfer_blocked"] is True
    assert "non-transferable" in big.extra["transfer_block_reason"]

    # Two dates by the same attraction in the same market.
    assert big.extra["tour_dates_in_market"] == 2
    assert big.extra["tour_dates_total"] == 2


@pytest.mark.asyncio
async def test_ticketmaster_without_key_returns_nothing():
    async with TicketmasterSource(api_key="", dma_ids=["324"]) as tm:
        assert await tm.fetch_events() == []


@pytest.mark.asyncio
@respx.mock
async def test_ticketmaster_survives_http_error():
    respx.get("https://app.ticketmaster.com/discovery/v2/events.json").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    async with TicketmasterSource(
        api_key="k", dma_ids=["324"], lookahead_days=30, requests_per_second=0
    ) as tm:
        assert await tm.fetch_events() == []


class TestAxsGuardrails:
    @pytest.mark.asyncio
    async def test_disabled_by_default(self):
        async with AxsSource() as axs:
            assert axs.enabled is False
            assert await axs.fetch_events() == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_respects_robots_disallow(self):
        respx.get("https://www.axs.com/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /")
        )
        async with AxsSource(enabled=True, requests_per_second=0) as axs:
            assert await axs.can_fetch("https://www.axs.com/events") is False
            assert await axs.fetch_events() == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_unreachable_robots_is_treated_as_disallow(self):
        respx.get("https://www.axs.com/robots.txt").mock(
            return_value=httpx.Response(503)
        )
        async with AxsSource(enabled=True, requests_per_second=0) as axs:
            assert await axs.can_fetch("https://www.axs.com/events") is False
