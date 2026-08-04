"""Ingest pipeline: fetch -> upsert -> snapshot -> enrich -> score."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import session_scope
from .models import Artist, Event, PriceSnapshot, Score, utcnow
from .scoring import score_event
from .sources import AxsSource, RawEvent, SpotifyClient, TicketmasterSource

log = logging.getLogger(__name__)

# Re-check an artist's Spotify metrics no more than once a week.
ARTIST_REFRESH = timedelta(days=7)


async def collect_raw_events(settings: Settings) -> list[RawEvent]:
    raw: list[RawEvent] = []

    async with TicketmasterSource(
        api_key=settings.ticketmaster_api_key,
        dma_ids=settings.dma_id_list,
        classification_name=settings.classification_name,
        lookahead_days=settings.lookahead_days,
        requests_per_second=settings.requests_per_second,
    ) as tm:
        raw.extend(await tm.fetch_events())

    async with AxsSource(enabled=settings.enable_axs) as axs:
        raw.extend(await axs.fetch_events())

    return raw


def upsert_events(session: Session, raw_events: list[RawEvent]) -> tuple[int, int]:
    """Insert or update events and append a price snapshot for each.

    Snapshots are appended only when something changed, so a flat event does
    not accumulate identical rows — but we always keep at least one per day so
    the momentum window has anchors.
    """
    created = updated = 0
    now = utcnow()

    for raw in raw_events:
        event = session.scalar(
            select(Event).where(
                Event.source == raw.source, Event.source_id == raw.source_id
            )
        )
        if event is None:
            event = Event(source=raw.source, source_id=raw.source_id)
            session.add(event)
            created += 1
        else:
            updated += 1

        event.name = raw.name
        event.artist_name = raw.artist_name
        event.venue_name = raw.venue_name
        event.venue_capacity = raw.venue_capacity
        event.city = raw.city
        event.state = raw.state
        event.dma_id = raw.dma_id
        event.starts_at = raw.starts_at
        event.onsale_starts_at = raw.onsale_starts_at
        event.onsale_ends_at = raw.onsale_ends_at
        event.url = raw.url
        event.image_url = raw.image_url
        event.currency = raw.currency
        event.restrictions_text = raw.restrictions_text
        event.transfer_blocked = bool(raw.extra.get("transfer_blocked"))
        event.transfer_block_reason = raw.extra.get("transfer_block_reason")
        event.tour_dates_total = raw.extra.get("tour_dates_total", 1)
        event.tour_dates_in_market = raw.extra.get("tour_dates_in_market", 1)
        event.last_seen_at = now

        price_changed = (
            event.min_price != raw.min_price
            or event.max_price != raw.max_price
            or event.status_code != raw.status_code
        )

        event.min_price = raw.min_price
        event.max_price = raw.max_price
        event.status_code = raw.status_code

        session.flush()  # ensure event.id exists for the snapshot FK

        latest = session.scalar(
            select(PriceSnapshot)
            .where(PriceSnapshot.event_id == event.id)
            .order_by(PriceSnapshot.captured_at.desc())
            .limit(1)
        )
        stale = latest is None or (now - latest.captured_at) > timedelta(hours=24)
        if price_changed or stale:
            session.add(
                PriceSnapshot(
                    event_id=event.id,
                    captured_at=now,
                    min_price=raw.min_price,
                    max_price=raw.max_price,
                    status_code=raw.status_code,
                )
            )

        _link_artist(session, event, raw.artist_name)

    return created, updated


def _link_artist(session: Session, event: Event, artist_name: str | None) -> None:
    if not artist_name:
        return
    artist = session.scalar(select(Artist).where(Artist.name == artist_name))
    if artist is None:
        artist = Artist(name=artist_name)
        session.add(artist)
        session.flush()
    event.artist_id = artist.id


async def enrich_artists(settings: Settings, limit: int = 120) -> int:
    """Fill in Spotify metrics for artists we haven't looked up recently."""
    client = SpotifyClient(settings.spotify_client_id, settings.spotify_client_secret)
    if not client.configured:
        log.info("Spotify credentials absent; skipping demand enrichment")
        return 0

    cutoff = utcnow() - ARTIST_REFRESH
    with session_scope() as session:
        stale = session.scalars(
            select(Artist)
            .where(
                Artist.lookup_failed.is_(False),
                (Artist.updated_at.is_(None)) | (Artist.updated_at < cutoff),
            )
            .limit(limit)
        ).all()
        names = [(a.id, a.name) for a in stale]

    if not names:
        return 0

    enriched = 0
    async with client:
        for artist_id, name in names:
            metrics = await client.artist_metrics(name)
            with session_scope() as session:
                artist = session.get(Artist, artist_id)
                if artist is None:
                    continue
                artist.updated_at = utcnow()
                if metrics is None:
                    artist.lookup_failed = True
                    continue
                artist.spotify_id = metrics["spotify_id"]
                artist.popularity = metrics["popularity"]
                artist.followers = metrics["followers"]
                artist.genres = metrics["genres"]
                enriched += 1

    log.info("Enriched %d/%d artists via Spotify", enriched, len(names))
    return enriched


def rescore_all(session: Session, now: datetime | None = None) -> list[tuple[Event, Score]]:
    """Recompute scores for every future event and persist the results."""
    now = now or datetime.now(timezone.utc)
    events = session.scalars(
        select(Event).where((Event.starts_at.is_(None)) | (Event.starts_at > now))
    ).all()

    out: list[tuple[Event, Score]] = []
    for event in events:
        snapshots = session.scalars(
            select(PriceSnapshot).where(PriceSnapshot.event_id == event.id)
        ).all()
        result = score_event(event, list(snapshots), now=now)
        row = Score(
            event_id=event.id,
            computed_at=now,
            score=result.score,
            factors={
                **{k: v for k, v in result.factors.items()},
                "_reasons": result.reasons,
                "_kind": result.kind,
            },
            vetoes=result.vetoes,
            confidence=result.confidence,
        )
        session.add(row)
        out.append((event, row))

    log.info("Scored %d events", len(out))
    return out


async def run_cycle(settings: Settings | None = None) -> dict:
    """One full poll: fetch, store, enrich, rescore. Returns a summary."""
    settings = settings or get_settings()

    raw = await collect_raw_events(settings)
    with session_scope() as session:
        created, updated = upsert_events(session, raw)

    enriched = await enrich_artists(settings)

    with session_scope() as session:
        scored = rescore_all(session)
        count = len(scored)

    summary = {
        "fetched": len(raw),
        "created": created,
        "updated": updated,
        "artists_enriched": enriched,
        "scored": count,
        "at": utcnow().isoformat(),
    }
    log.info("Cycle complete: %s", summary)
    return summary
