"""Source adapter contract plus shared helpers.

Every adapter normalises whatever the upstream returns into `RawEvent` so the
ingest and scoring layers never learn a vendor's field names.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(slots=True)
class RawEvent:
    source: str
    source_id: str
    name: str

    artist_name: str | None = None
    venue_name: str | None = None
    venue_capacity: int | None = None
    city: str | None = None
    state: str | None = None
    dma_id: str | None = None

    starts_at: datetime | None = None
    onsale_starts_at: datetime | None = None
    onsale_ends_at: datetime | None = None

    url: str | None = None
    image_url: str | None = None

    min_price: float | None = None
    max_price: float | None = None
    currency: str | None = None
    status_code: str | None = None

    restrictions_text: str | None = None
    # Populated by ingest, not by adapters.
    extra: dict = field(default_factory=dict)


class EventSource(Protocol):
    name: str

    async def fetch_events(self) -> list[RawEvent]: ...


class RateLimiter:
    """Simple async token-bucket-ish spacer.

    Ticketmaster allows ~5 requests/second; we default a little under that.
    """

    def __init__(self, per_second: float) -> None:
        self._min_gap = 1.0 / per_second if per_second > 0 else 0.0
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self._min_gap <= 0:
            return
        async with self._lock:
            gap = time.monotonic() - self._last
            if gap < self._min_gap:
                await asyncio.sleep(self._min_gap - gap)
            self._last = time.monotonic()


# Phrases that mean "you will not be able to resell this at a markup".
# Ordered roughly by how conclusive they are.
_TRANSFER_BLOCK_PATTERNS: list[tuple[str, str]] = [
    # "non-transferable", "non transferable", "nontransferable"
    (r"non[\s\-_]?transferab", "tickets marked non-transferable"),
    (r"\bno resale\b|resale (is )?(not permitted|prohibited)", "resale prohibited"),
    (r"transfer (is )?(disabled|not (available|enabled|permitted))", "transfer disabled"),
    (r"paperless", "paperless ticketing"),
    (r"credit card entry", "credit-card entry required"),
    (r"paper ?less|will ?call only", "will-call-only entry"),
    (r"face ?value (exchange|resale) only", "face-value-only resale"),
    (r"lead (booker|attendee) must", "lead attendee must attend"),
    (r"photo ?id (and|&)? ?(matching )?(the )?(purchaser|name)", "ID must match purchaser"),
]


def detect_transfer_block(*texts: str | None) -> tuple[bool, str | None]:
    """Scan restriction prose for signals that resale is blocked.

    Returns (blocked, human readable reason). This is the single most
    important filter in the whole system: a ticket you cannot transfer has
    no resale value regardless of how much demand there is.
    """
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return False, None
    for pattern, reason in _TRANSFER_BLOCK_PATTERNS:
        if re.search(pattern, blob):
            return True, reason
    return False, None
