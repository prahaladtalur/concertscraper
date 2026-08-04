"""Spotify Web API — artist demand signal.

Ticketmaster tells us what is on sale; it does not tell us who is hot.
Spotify's `popularity` (0-100, relative to every artist on the platform) plus
follower count is the cheapest reliable proxy for ticket demand, and the
client-credentials flow needs no user login.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta, timezone

import httpx

from .base import RateLimiter

log = logging.getLogger(__name__)

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"


class SpotifyClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        requests_per_second: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self._limiter = RateLimiter(requests_per_second)
        self._client = client
        self._owns_client = client is None
        self._token: str | None = None
        self._token_expires: datetime = datetime.min.replace(tzinfo=timezone.utc)

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def __aenter__(self) -> "SpotifyClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ensure_token(self) -> str | None:
        if not self.configured:
            return None
        if self._token and datetime.now(timezone.utc) < self._token_expires:
            return self._token

        assert self._client is not None
        creds = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        resp = await self._client.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {creds}"},
        )
        if resp.status_code != 200:
            log.error("Spotify token request failed: %s", resp.text[:200])
            return None
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires = datetime.now(timezone.utc) + timedelta(
            seconds=payload.get("expires_in", 3600) - 60
        )
        return self._token

    async def artist_metrics(self, artist_name: str) -> dict | None:
        """Look up an artist by name and return popularity/followers/genres."""
        token = await self._ensure_token()
        if not token:
            return None

        assert self._client is not None
        await self._limiter.acquire()
        try:
            resp = await self._client.get(
                f"{API_URL}/search",
                params={"q": artist_name, "type": "artist", "limit": 5},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("Spotify search failed for %r: %s", artist_name, exc)
            return None

        items = resp.json().get("artists", {}).get("items", [])
        if not items:
            return None

        # Spotify's relevance ranking is decent but an exact name match is
        # safer than trusting position 0 (tribute bands rank surprisingly well).
        target = artist_name.strip().lower()
        exact = next((a for a in items if a.get("name", "").lower() == target), None)
        artist = exact or items[0]

        return {
            "spotify_id": artist.get("id"),
            "popularity": artist.get("popularity"),
            "followers": (artist.get("followers") or {}).get("total"),
            "genres": artist.get("genres") or [],
            "matched_name": artist.get("name"),
            "exact_match": exact is not None,
        }
