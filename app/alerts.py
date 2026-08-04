"""Candidate selection and email delivery."""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .models import Alert, Event, Score
from .notify import deliver_email

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"

# Below this, the model didn't have enough data to be worth your attention.
MIN_CONFIDENCE = 0.35


@dataclass(slots=True)
class Candidate:
    event: Event
    score: Score

    @property
    def reasons(self) -> list[str]:
        return self.score.factors.get("_reasons", []) or []

    @property
    def kind(self) -> str:
        return self.score.factors.get("_kind", "momentum")

    @property
    def buy_url(self) -> str:
        return self.event.url or self._search_url()

    def _search_url(self) -> str:
        q = urllib.parse.quote_plus(
            f"{self.event.artist_name or self.event.name} {self.event.city or ''} tickets"
        )
        return f"https://www.ticketmaster.com/search?q={q}"

    @property
    def comps_url(self) -> str:
        """Resale comparison so you can check the spread before buying."""
        q = urllib.parse.quote_plus(self.event.artist_name or self.event.name)
        return f"https://www.stubhub.com/secure/search?q={q}"

    @property
    def calendar_url(self) -> str:
        """Add-to-calendar for onsale reminders."""
        when = self.event.onsale_starts_at or self.event.starts_at
        if when is None:
            return ""
        stamp = when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        end = (when + timedelta(hours=1)).astimezone(timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )
        params = urllib.parse.urlencode(
            {
                "action": "TEMPLATE",
                "text": f"Onsale: {self.event.name}",
                "dates": f"{stamp}/{end}",
                "details": self.buy_url,
            }
        )
        return f"https://calendar.google.com/calendar/render?{params}"

    @property
    def top_factors(self) -> list[tuple[str, float]]:
        pairs = [
            (k.replace("_", " "), v)
            for k, v in self.score.factors.items()
            if not k.startswith("_") and isinstance(v, (int, float))
        ]
        return sorted(pairs, key=lambda p: -p[1])[:4]


def select_candidates(
    session: Session, settings: Settings, now: datetime | None = None
) -> list[Candidate]:
    """Latest score per event, above threshold, not recently alerted."""
    now = now or datetime.now(timezone.utc)

    # Newest score row per event.
    latest = (
        select(Score.event_id, func.max(Score.computed_at).label("computed_at"))
        .group_by(Score.event_id)
        .subquery()
    )
    rows = session.execute(
        select(Score, Event)
        .join(
            latest,
            (Score.event_id == latest.c.event_id)
            & (Score.computed_at == latest.c.computed_at),
        )
        .join(Event, Event.id == Score.event_id)
        .where(Score.score >= settings.alert_score_threshold)
        .where(Score.confidence >= MIN_CONFIDENCE)
    ).all()

    cooldown_start = now - timedelta(hours=settings.alert_cooldown_hours)
    recently_alerted = set(
        session.scalars(
            select(Alert.event_id).where(Alert.sent_at >= cooldown_start)
        ).all()
    )

    candidates = [
        Candidate(event=event, score=score)
        for score, event in rows
        if not score.vetoes and event.id not in recently_alerted
    ]
    # Rank by conviction, not raw score: a confident 0.70 beats a shaky 0.85.
    candidates.sort(key=lambda c: -(c.score.score * c.score.confidence))
    return candidates[: settings.max_alerts_per_email]


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )


def render_email(candidates: list[Candidate], now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now(timezone.utc)
    env = _jinja_env()
    html = env.get_template("email.html").render(candidates=candidates, now=now)

    lines = [f"{len(candidates)} ticket opportunities — {now:%Y-%m-%d %H:%M UTC}", ""]
    for i, c in enumerate(candidates, 1):
        ev = c.event
        when = f"{ev.starts_at:%a %b %d, %Y}" if ev.starts_at else "date TBA"
        price = f"from ${ev.min_price:.0f}" if ev.min_price else "price TBA"
        lines += [
            f"{i}. [{c.score.score:.2f} / conf {c.score.confidence:.2f}] {ev.name}",
            f"   {ev.venue_name or '?'}, {ev.city or '?'} — {when} — {price}",
            *[f"   * {r}" for r in c.reasons],
            f"   Buy:   {c.buy_url}",
            f"   Comps: {c.comps_url}",
            "",
        ]
    lines += [
        "---",
        "Scores are estimates from public listing data, not guarantees.",
        "Verify transferability on the event page before buying to resell.",
    ]
    return "\n".join(lines), html


def send_email(
    candidates: list[Candidate], settings: Settings | None = None
) -> Path | None:
    """Send (or, in dry-run mode, write to ./outbox/) the opportunity digest.

    Returns the outbox path when dry-running, else None.
    """
    settings = settings or get_settings()
    if not candidates:
        log.info("No candidates above threshold; no email sent")
        return None

    subject = f"[tickets] {len(candidates)} opportunities — top: {candidates[0].event.name}"
    text, html = render_email(candidates)
    return deliver_email(subject, text, html, settings, slug="digest")


def record_alerts(session: Session, candidates: list[Candidate]) -> None:
    for c in candidates:
        session.add(Alert(event_id=c.event.id, score=c.score.score))
