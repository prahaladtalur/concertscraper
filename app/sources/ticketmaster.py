"""Ticketmaster Discovery API v2 adapter.

This is an official, documented, free API (5,000 requests/day, ~5 req/sec).
Using it means we never scrape Ticketmaster HTML, never touch their bot
defences, and never violate their terms. Get a key at
https://developer.ticketmaster.com/.

Notable API quirks handled here:
  * Deep paging is capped: page * size must stay under 1000. We slice the
    date window instead of paging past the wall.
  * `priceRanges` is often absent before the public onsale, and sometimes
    contains several entries (standard / VIP). We take the widest span.
  * `dates.start.dateTime` is UTC; `localDate` alone shows up for TBA events.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from .base import RateLimiter, RawEvent, detect_transfer_block

log = logging.getLogger(__name__)

BASE_URL = "https://app.ticketmaster.com/discovery/v2"
PAGE_SIZE = 199  # max allowed is 199 when deep-paging
MAX_OFFSET = 1000  # API rejects page*size >= 1000

# Ticketmaster event URLs end in /event/<id>, e.g.
# https://www.ticketmaster.com/artist-tickets-seattle/event/0E006012ABCD1234
_EVENT_URL_RE = re.compile(r"/event/([0-9A-Za-z]{6,})", re.IGNORECASE)


def event_id_from_url(url: str | None) -> str | None:
    """Pull the Ticketmaster event id out of a pasted event URL."""
    match = _EVENT_URL_RE.search(url or "")
    return match.group(1) if match else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _price_span(price_ranges: list[dict] | None) -> tuple[float | None, float | None, str | None]:
    """Widest min/max across all listed price tiers.

    Tiers are sometimes partial (a min with no max, or vice versa), so each
    bound falls back to the other rather than reporting a half-open range the
    scoring layer would have to special-case.
    """
    if not price_ranges:
        return None, None, None
    mins = [p["min"] for p in price_ranges if isinstance(p.get("min"), (int, float))]
    maxs = [p["max"] for p in price_ranges if isinstance(p.get("max"), (int, float))]
    currency = next((p.get("currency") for p in price_ranges if p.get("currency")), None)

    low = min(mins) if mins else (min(maxs) if maxs else None)
    high = max(maxs) if maxs else (max(mins) if mins else None)
    return low, high, currency


class TicketmasterSource:
    name = "ticketmaster"

    def __init__(
        self,
        api_key: str,
        dma_ids: list[str],
        classification_name: str = "Music",
        lookahead_days: int = 240,
        requests_per_second: float = 4.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.dma_ids = dma_ids
        self.classification_name = classification_name
        self.lookahead_days = lookahead_days
        self._limiter = RateLimiter(requests_per_second)
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "TicketmasterSource":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- HTTP ------------------------------------------------------------
    async def _get(self, path: str, params: dict) -> dict:
        assert self._client is not None, "use as an async context manager"
        await self._limiter.acquire()
        params = {**params, "apikey": self.api_key}
        resp = await self._client.get(f"{BASE_URL}{path}", params=params)
        if resp.status_code == 429:
            log.warning("Ticketmaster rate limit hit; backing off")
            raise httpx.HTTPStatusError(
                "rate limited", request=resp.request, response=resp
            )
        resp.raise_for_status()
        return resp.json()

    # -- Fetching --------------------------------------------------------
    async def fetch_events(self) -> list[RawEvent]:
        if not self.api_key:
            log.warning("No Ticketmaster API key configured; skipping fetch")
            return []

        collected: dict[str, RawEvent] = {}
        now = datetime.now(timezone.utc)

        # Slice the lookahead into 30-day windows. Each window stays well
        # under the 1000-result deep-paging wall, so nothing gets silently
        # truncated the way a single wide query would.
        window = timedelta(days=30)
        cursor = now
        horizon = now + timedelta(days=self.lookahead_days)

        while cursor < horizon:
            window_end = min(cursor + window, horizon)
            for dma_id in self.dma_ids:
                events = await self._fetch_window(dma_id, cursor, window_end)
                for ev in events:
                    collected[ev.source_id] = ev
            cursor = window_end

        annotated = self._annotate_tour_context(list(collected.values()))
        log.info("Ticketmaster: %d unique events", len(annotated))
        return annotated

    async def fetch_event_by_id(self, source_id: str) -> RawEvent | None:
        """Look up a single event, regardless of configured markets.

        Holdings can be bought anywhere, so the daily portfolio refresh needs
        a path that doesn't depend on the event falling inside MARKET_DMA_IDS.
        """
        if not self.api_key:
            return None
        try:
            payload = await self._get(f"/events/{source_id}.json", {})
        except httpx.HTTPError as exc:
            log.warning("Ticketmaster lookup failed for %s: %s", source_id, exc)
            return None

        parsed = self._parse_event(payload, payload.get("dmaId") or "")
        if parsed is not None:
            # A single lookup has no tour context to compare against.
            parsed.extra.setdefault("tour_dates_total", 1)
            parsed.extra.setdefault("tour_dates_in_market", 1)
            blocked, reason = detect_transfer_block(parsed.restrictions_text)
            parsed.extra["transfer_blocked"] = blocked
            parsed.extra["transfer_block_reason"] = reason
        return parsed

    async def _fetch_window(
        self, dma_id: str, start: datetime, end: datetime
    ) -> list[RawEvent]:
        out: list[RawEvent] = []
        page = 0
        while page * PAGE_SIZE < MAX_OFFSET:
            params = {
                "dmaId": dma_id,
                "classificationName": self.classification_name,
                "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "size": PAGE_SIZE,
                "page": page,
                "sort": "date,asc",
            }
            try:
                payload = await self._get("/events.json", params)
            except httpx.HTTPError as exc:
                log.error("Ticketmaster window fetch failed (dma=%s): %s", dma_id, exc)
                break

            events = payload.get("_embedded", {}).get("events", [])
            for raw in events:
                parsed = self._parse_event(raw, dma_id)
                if parsed is not None:
                    out.append(parsed)

            page_info = payload.get("page", {})
            if page + 1 >= page_info.get("totalPages", 0):
                break
            page += 1
        return out

    def _parse_event(self, raw: dict, dma_id: str) -> RawEvent | None:
        source_id = raw.get("id")
        if not source_id:
            return None

        dates = raw.get("dates", {})
        sales = raw.get("sales", {}).get("public", {})
        embedded = raw.get("_embedded", {})
        venues = embedded.get("venues") or [{}]
        venue = venues[0]
        attractions = embedded.get("attractions") or []

        min_price, max_price, currency = _price_span(raw.get("priceRanges"))

        restriction_parts = [
            raw.get("pleaseNote"),
            raw.get("info"),
            raw.get("accessibility", {}).get("info")
            if isinstance(raw.get("accessibility"), dict)
            else None,
        ]
        restrictions_text = "\n".join(p for p in restriction_parts if p) or None

        images = raw.get("images") or []
        # Prefer a wide 16:9 image for the email card.
        image_url = None
        for img in sorted(images, key=lambda i: -(i.get("width") or 0)):
            if img.get("ratio") == "16_9":
                image_url = img.get("url")
                break
        if image_url is None and images:
            image_url = images[0].get("url")

        return RawEvent(
            source=self.name,
            source_id=source_id,
            name=raw.get("name") or "(untitled)",
            artist_name=(attractions[0].get("name") if attractions else None),
            venue_name=venue.get("name"),
            venue_capacity=(
                int(venue["capacity"]) if str(venue.get("capacity", "")).isdigit() else None
            ),
            city=(venue.get("city") or {}).get("name"),
            state=(venue.get("state") or {}).get("stateCode"),
            dma_id=dma_id,
            starts_at=_parse_dt(dates.get("start", {}).get("dateTime")),
            onsale_starts_at=_parse_dt(sales.get("startDateTime")),
            onsale_ends_at=_parse_dt(sales.get("endDateTime")),
            url=raw.get("url"),
            image_url=image_url,
            min_price=min_price,
            max_price=max_price,
            currency=currency,
            status_code=dates.get("status", {}).get("code"),
            restrictions_text=restrictions_text,
            extra={
                "attraction_ids": [a.get("id") for a in attractions if a.get("id")],
                "promoter": (raw.get("promoter") or {}).get("name"),
            },
        )

    @staticmethod
    def _annotate_tour_context(events: list[RawEvent]) -> list[RawEvent]:
        """Count how many dates each artist plays, overall and per market.

        Scarcity matters: one Seattle date for a hot artist is a much better
        trade than a four-night residency, because the four-night run soaks up
        the same demand across 4x the supply.
        """
        totals: dict[str, int] = {}
        per_market: dict[tuple[str, str | None], int] = {}
        for ev in events:
            key = (ev.artist_name or ev.name).lower()
            totals[key] = totals.get(key, 0) + 1
            mk = (key, ev.dma_id)
            per_market[mk] = per_market.get(mk, 0) + 1

        for ev in events:
            key = (ev.artist_name or ev.name).lower()
            blocked, reason = detect_transfer_block(ev.restrictions_text)
            ev.extra["tour_dates_total"] = totals[key]
            ev.extra["tour_dates_in_market"] = per_market[(key, ev.dma_id)]
            ev.extra["transfer_blocked"] = blocked
            ev.extra["transfer_block_reason"] = reason
        return events
