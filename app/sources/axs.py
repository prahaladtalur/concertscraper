"""AXS adapter — deliberately conservative, disabled by default.

AXS publishes no public API, and its Terms of Use prohibit automated
collection. This adapter therefore:

  1. Refuses to run unless ENABLE_AXS=true is set explicitly.
  2. Fetches and honours /robots.txt before every run, and aborts if the
     target path is disallowed for our user agent.
  3. Crawls at one request every few seconds with an identifying user agent.
  4. Only reads the public JSON that the event-listing pages themselves call.

Read docs/SOURCES.md before enabling this. The Ticketmaster Discovery API
gives you a legal, stable signal; AXS is strictly optional upside and the
main reason the rest of the system is built to work without it.
"""

from __future__ import annotations

import logging
from urllib.robotparser import RobotFileParser

import httpx

from .base import RateLimiter, RawEvent

log = logging.getLogger(__name__)

ROBOTS_URL = "https://www.axs.com/robots.txt"
USER_AGENT = "concertscraper/0.1 (personal price research; contact via repo)"


class AxsDisallowed(RuntimeError):
    """Raised when robots.txt disallows the path we were about to fetch."""


class AxsSource:
    name = "axs"

    def __init__(
        self,
        enabled: bool = False,
        requests_per_second: float = 0.25,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.enabled = enabled
        self._limiter = RateLimiter(requests_per_second)
        self._client = client
        self._owns_client = client is None
        self._robots: RobotFileParser | None = None

    async def __aenter__(self) -> "AxsSource":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0, headers={"User-Agent": USER_AGENT}
            )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _load_robots(self) -> RobotFileParser:
        if self._robots is not None:
            return self._robots
        assert self._client is not None
        parser = RobotFileParser()
        try:
            resp = await self._client.get(ROBOTS_URL)
            resp.raise_for_status()
            parser.parse(resp.text.splitlines())
        except httpx.HTTPError as exc:
            # Unreachable robots.txt is treated as "disallow everything".
            log.warning("Could not read AXS robots.txt (%s); refusing to crawl", exc)
            parser.parse(["User-agent: *", "Disallow: /"])
        self._robots = parser
        return parser

    async def can_fetch(self, url: str) -> bool:
        robots = await self._load_robots()
        return robots.can_fetch(USER_AGENT, url)

    async def fetch_events(self) -> list[RawEvent]:
        if not self.enabled:
            log.info("AXS source disabled (set ENABLE_AXS=true to opt in)")
            return []

        target = "https://www.axs.com/events"
        if not await self.can_fetch(target):
            log.warning(
                "AXS robots.txt disallows %s — skipping. This is the expected "
                "outcome; rely on the Ticketmaster Discovery API instead.",
                target,
            )
            return []

        await self._limiter.acquire()
        log.info("AXS crawl permitted by robots.txt but no parser is implemented")
        # Intentionally left unimplemented: if robots.txt ever permits this,
        # write the parser then, against whatever the page actually serves.
        return []
