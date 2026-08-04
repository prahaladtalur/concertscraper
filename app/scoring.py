"""Appreciation scoring.

The thesis: a ticket appreciates when demand outruns a supply that cannot
expand. Everything here is a proxy for one side of that inequality, and every
factor is explainable — an alert you can't justify is an alert you shouldn't
act on.

Design notes:
  * Factors return None when the input data isn't there. The weighted mean is
    renormalised over whatever is available, and `confidence` reports how much
    of the model actually fired. A 0.8 at 0.3 confidence is a guess; a 0.7 at
    0.9 confidence is a signal.
  * Vetoes are absolute, not penalties. A non-transferable ticket scores 0 no
    matter how hot the artist is, because you cannot sell it.
  * Nothing here places orders. It ranks things for a human to look at.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import Event, PriceSnapshot

# Relative importance of each factor. Momentum dominates because it is the one
# signal derived from our own observation rather than a static attribute.
WEIGHTS: dict[str, float] = {
    "price_momentum": 0.26,
    "floor_pressure": 0.15,
    "artist_demand": 0.20,
    "scarcity": 0.14,
    "timing": 0.13,
    "headroom": 0.12,
}

# Minimum hours between the first and last snapshot before momentum is
# considered meaningful rather than noise.
MIN_MOMENTUM_SPAN_HOURS = 12.0


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _interp(x: float, points: list[tuple[float, float]]) -> float:
    """Piecewise-linear interpolation over (x, y) knots sorted by x."""
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


@dataclass(slots=True)
class ScoreResult:
    score: float
    confidence: float
    factors: dict[str, float | None]
    vetoes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    # "momentum" | "upcoming_onsale" | "cold_start"
    kind: str = "momentum"

    @property
    def actionable(self) -> bool:
        return not self.vetoes


# --------------------------------------------------------------------------
# Individual factors
# --------------------------------------------------------------------------
def price_momentum(snapshots: list[PriceSnapshot]) -> tuple[float | None, str | None]:
    """Rise in the cheapest available ticket between first and last sighting.

    A rising floor is the cleanest evidence that inventory is being consumed:
    the cheap seats sell first, so the minimum price ratchets up.
    """
    priced = [s for s in snapshots if s.min_price]
    if len(priced) < 2:
        return None, None

    priced.sort(key=lambda s: s.captured_at)
    first, last = priced[0], priced[-1]
    span_hours = (last.captured_at - first.captured_at).total_seconds() / 3600.0
    if span_hours < MIN_MOMENTUM_SPAN_HOURS:
        return None, None

    pct = (last.min_price - first.min_price) / first.min_price
    # +20% over the observation window saturates the factor; -20% floors it.
    score = clamp(0.5 + pct * 2.5)
    if pct >= 0.05:
        reason = (
            f"floor price up {pct * 100:.0f}% "
            f"(${first.min_price:.0f} to ${last.min_price:.0f}) over {span_hours / 24:.1f}d"
        )
    elif pct <= -0.05:
        reason = f"floor price down {abs(pct) * 100:.0f}% — demand softening"
    else:
        reason = "floor price flat"
    return score, reason


def floor_pressure(snapshots: list[PriceSnapshot]) -> tuple[float | None, str | None]:
    """How far above its all-time observed low the current floor sits.

    Distinct from momentum: momentum is first-to-last, this is current-to-best.
    An event whose floor dipped and recovered has pressure but no momentum.
    """
    priced = [s for s in snapshots if s.min_price]
    if len(priced) < 2:
        return None, None

    priced.sort(key=lambda s: s.captured_at)
    historical_low = min(s.min_price for s in priced)
    current = priced[-1].min_price
    if historical_low <= 0:
        return None, None

    lift = current / historical_low
    score = clamp((lift - 1.0) / 0.35)
    reason = (
        f"floor sits {(lift - 1) * 100:.0f}% above its observed low of ${historical_low:.0f}"
        if lift > 1.02
        else None
    )
    return score, reason


def artist_demand(event: Event) -> tuple[float | None, str | None]:
    """Spotify popularity plus follower base as a demand proxy."""
    artist = event.artist
    if artist is None or artist.popularity is None:
        return None, None

    # Popularity is 0-100 platform-wide; below ~35 rarely sells out a room.
    pop = clamp((artist.popularity - 35) / 45.0)

    if artist.followers and artist.followers > 0:
        # 10k followers -> 0, 10M -> 1.
        fol = clamp((math.log10(artist.followers) - 4.0) / 3.0)
        score = 0.6 * pop + 0.4 * fol
        detail = f"Spotify popularity {artist.popularity}/100, {artist.followers:,} followers"
    else:
        score = pop
        detail = f"Spotify popularity {artist.popularity}/100"

    return score, detail if score >= 0.5 else None


def scarcity(event: Event) -> tuple[float | None, str | None]:
    """Fewer dates and smaller rooms mean fixed supply against the same demand."""
    parts: list[tuple[float, float]] = []  # (score, weight)
    notes: list[str] = []

    dates = event.tour_dates_in_market or 1
    date_score = _interp(
        float(dates), [(1, 1.0), (2, 0.7), (3, 0.5), (4, 0.32), (6, 0.15)]
    )
    parts.append((date_score, 0.55))
    if dates == 1:
        notes.append("only date in this market")
    elif dates >= 3:
        notes.append(f"{dates} dates in market — supply spread thin")

    if event.venue_capacity:
        # 1,000-cap club -> 1.0; 50,000-seat stadium -> 0.
        cap_score = clamp(1.0 - (math.log10(event.venue_capacity) - 3.0) / 1.7)
        parts.append((cap_score, 0.45))
        if event.venue_capacity <= 3000:
            notes.append(f"small room ({event.venue_capacity:,} cap)")

    total_weight = sum(w for _, w in parts)
    score = sum(s * w for s, w in parts) / total_weight
    return score, "; ".join(notes) or None


def timing(event: Event, now: datetime) -> tuple[float | None, str | None]:
    """Where the event sits on the resale appreciation curve.

    Prices tend to climb from onsale, peak roughly 3-8 weeks out as casual
    buyers commit, then get volatile in the final week when sellers panic.
    """
    if event.starts_at is None:
        return None, None
    days = (event.starts_at - now).total_seconds() / 86400.0
    score = _interp(
        days,
        [
            (0, 0.0),
            (3, 0.35),
            (10, 0.62),
            (21, 0.9),
            (35, 1.0),
            (70, 0.95),
            (120, 0.66),
            (200, 0.42),
            (300, 0.22),
        ],
    )
    if 21 <= days <= 70:
        return score, f"{days:.0f} days out — peak appreciation window"
    if days < 10:
        return score, f"only {days:.0f} days out — short runway to resell"
    return score, None


def headroom(event: Event, demand: float | None) -> tuple[float | None, str | None]:
    """Cheap face value against strong demand is where the margin lives."""
    if demand is None or not event.min_price:
        return None, None

    # $25 face -> 0 (lots of room), $200 -> 1 (already priced up).
    price_level = clamp((event.min_price - 25.0) / 175.0)
    score = clamp(0.5 + (demand - price_level))

    reason = None
    if demand >= 0.6 and price_level <= 0.35:
        reason = f"strong demand against a ${event.min_price:.0f} floor"
    elif price_level >= 0.8:
        reason = f"${event.min_price:.0f} floor already rich — thin margin"
    return score, reason


# --------------------------------------------------------------------------
# Vetoes
# --------------------------------------------------------------------------
DEAD_STATUSES = {"cancelled", "canceled", "postponed", "rescheduled"}


def collect_vetoes(event: Event, now: datetime) -> list[str]:
    vetoes: list[str] = []

    if event.transfer_blocked:
        reason = event.transfer_block_reason or "transfer restricted"
        vetoes.append(f"cannot resell: {reason}")

    status = (event.status_code or "").lower()
    if status in DEAD_STATUSES:
        vetoes.append(f"event {status}")
    if status == "offsale" and not event.onsale_starts_at:
        vetoes.append("off sale — nothing to buy")

    if event.starts_at is not None:
        days = (event.starts_at - now).total_seconds() / 86400.0
        if days < 0:
            vetoes.append("event already happened")
        elif days < 2:
            vetoes.append("under 48h to showtime — no time to resell")

    return vetoes


# --------------------------------------------------------------------------
# Top level
# --------------------------------------------------------------------------
def score_event(
    event: Event, snapshots: list[PriceSnapshot] | None = None, now: datetime | None = None
) -> ScoreResult:
    now = now or datetime.now(timezone.utc)
    snapshots = snapshots if snapshots is not None else list(event.snapshots)

    vetoes = collect_vetoes(event, now)
    if vetoes:
        return ScoreResult(
            score=0.0,
            confidence=1.0,
            factors={k: None for k in WEIGHTS},
            vetoes=vetoes,
            reasons=[],
            kind="vetoed",
        )

    reasons: list[str] = []
    factors: dict[str, float | None] = {}

    def record(key: str, result: tuple[float | None, str | None]) -> float | None:
        value, reason = result
        factors[key] = value
        if reason:
            reasons.append(reason)
        return value

    record("price_momentum", price_momentum(snapshots))
    record("floor_pressure", floor_pressure(snapshots))
    demand = record("artist_demand", artist_demand(event))
    record("scarcity", scarcity(event))
    record("timing", timing(event, now))
    record("headroom", headroom(event, demand))

    available = {k: v for k, v in factors.items() if v is not None}
    if not available:
        return ScoreResult(
            score=0.0,
            confidence=0.0,
            factors=factors,
            reasons=["no usable signal yet"],
            kind="cold_start",
        )

    weight_sum = sum(WEIGHTS[k] for k in available)
    score = sum(v * WEIGHTS[k] for k, v in available.items()) / weight_sum

    # Confidence is how much of the model's weight actually had data, nudged
    # up by a longer price history.
    coverage = weight_sum / sum(WEIGHTS.values())
    history_bonus = clamp(len([s for s in snapshots if s.min_price]) / 8.0) * 0.15
    confidence = clamp(coverage * 0.85 + history_bonus)

    kind = "momentum"
    if event.onsale_starts_at and event.onsale_starts_at > now:
        kind = "upcoming_onsale"
        days = (event.onsale_starts_at - now).total_seconds() / 86400.0
        reasons.insert(0, f"goes on sale in {days:.1f} days — not yet purchasable")
    elif factors.get("price_momentum") is None:
        kind = "cold_start"

    return ScoreResult(
        score=score,
        confidence=confidence,
        factors=factors,
        vetoes=[],
        reasons=reasons,
        kind=kind,
    )
