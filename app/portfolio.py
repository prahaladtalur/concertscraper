"""Position tracking and daily mark-to-market.

An important caveat, stated plainly because it changes how you read the
numbers: the only price feed we have legal access to is Ticketmaster's
*primary* listing range (which includes their dynamic "Official Platinum"
pricing). That is a proxy for the resale market, not the resale market itself.
Real resale comps live behind partner-only APIs on StubHub and Ticketmaster's
own exchange.

So treat `unit_value` as "what comparable seats are currently listed at on the
primary market" and use the comps link in each report row to eyeball the actual
number before you act. The direction of travel is reliable; the absolute level
is an estimate.
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import session_scope
from .models import (
    Event,
    Holding,
    HoldingStatus,
    HoldingValuation,
    utcnow,
)
from .notify import deliver_email, deliver_sms
from .sources.ticketmaster import TicketmasterSource, event_id_from_url

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"


# --------------------------------------------------------------------------
# Recording positions
# --------------------------------------------------------------------------
def add_holding(
    session: Session,
    *,
    event_url: str | None = None,
    source_event_id: str | None = None,
    event_name: str | None = None,
    quantity: int = 1,
    unit_cost: float,
    fees: float = 0.0,
    section: str | None = None,
    row: str | None = None,
    notes: str | None = None,
    purchased_at: datetime | None = None,
) -> Holding:
    """Record a purchase.

    Supply either a Ticketmaster event URL (preferred — the id is extracted so
    daily refreshes can re-price it) or a bare `event_name` for anything bought
    off-platform. Descriptive fields are backfilled from a matching local event
    when one exists.
    """
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if unit_cost < 0:
        raise ValueError("unit_cost cannot be negative")

    tm_id = source_event_id or event_id_from_url(event_url)
    if not tm_id and not event_name:
        raise ValueError(
            "need a Ticketmaster event URL, an event id, or an --name to record"
        )

    event = None
    if tm_id:
        # Match on the id alone rather than (source, id). Source ids are opaque
        # and effectively unique, and filtering by source would miss an
        # already-tracked event that arrived through a different adapter.
        event = session.scalar(select(Event).where(Event.source_id == tm_id))

    holding = Holding(
        event_id=event.id if event else None,
        source="ticketmaster" if tm_id else "manual",
        source_event_id=tm_id,
        event_name=event_name or (event.name if event else "(unnamed purchase)"),
        artist_name=event.artist_name if event else None,
        venue_name=event.venue_name if event else None,
        city=event.city if event else None,
        event_starts_at=event.starts_at if event else None,
        event_url=event_url or (event.url if event else None),
        quantity=quantity,
        unit_cost=unit_cost,
        fees=fees,
        section=section,
        row=row,
        notes=notes,
        purchased_at=purchased_at or utcnow(),
        status=HoldingStatus.HELD,
    )
    session.add(holding)
    session.flush()
    return holding


def mark_sold(
    session: Session,
    holding_id: int,
    sale_unit_price: float,
    sale_fees: float = 0.0,
    sold_at: datetime | None = None,
) -> Holding:
    holding = session.get(Holding, holding_id)
    if holding is None:
        raise ValueError(f"no holding with id {holding_id}")
    holding.status = HoldingStatus.SOLD
    holding.sale_unit_price = sale_unit_price
    holding.sale_fees = sale_fees
    holding.sold_at = sold_at or utcnow()
    return holding


# --------------------------------------------------------------------------
# Daily valuation
# --------------------------------------------------------------------------
# A locally-tracked event refreshed inside this window is trusted as-is, so a
# holding in a watched market costs zero extra API quota to value.
LOCAL_PRICE_MAX_AGE = timedelta(hours=12)


@dataclass(slots=True)
class _Target:
    """Everything needed to value one holding, read before any HTTP happens."""

    holding_id: int
    source_event_id: str | None
    starts_at: datetime | None
    basis: float
    quantity: int
    local_min: float | None = None
    local_max: float | None = None
    local_status: str | None = None


async def refresh_valuations(
    settings: Settings | None = None, now: datetime | None = None
) -> int:
    """Re-price every open position against the current primary listing range.

    Prefers the price the main poller already stored for a linked event, and
    only falls back to a per-holding API lookup when there's no fresh local
    figure. That keeps a portfolio inside watched markets essentially free
    against the 5,000/day request budget.
    """
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    local_cutoff = now - LOCAL_PRICE_MAX_AGE

    with session_scope() as session:
        open_holdings = session.scalars(
            select(Holding).where(
                Holding.status.in_([HoldingStatus.HELD, HoldingStatus.LISTED])
            )
        ).all()
        # Read everything up front; the session closes before any HTTP call.
        targets: list[_Target] = []
        for h in open_holdings:
            target = _Target(
                holding_id=h.id,
                source_event_id=h.source_event_id,
                starts_at=h.event_starts_at,
                basis=h.cost_per_ticket,
                quantity=h.quantity,
            )
            event = session.get(Event, h.event_id) if h.event_id else None
            if (
                event is not None
                and event.min_price is not None
                and event.last_seen_at >= local_cutoff
            ):
                target.local_min = event.min_price
                target.local_max = event.max_price
                target.local_status = event.status_code
                # A linked event carries the authoritative date.
                target.starts_at = event.starts_at or target.starts_at
            targets.append(target)

    if not targets:
        log.info("No open holdings to value")
        return 0

    written = from_local = 0
    async with TicketmasterSource(
        api_key=settings.ticketmaster_api_key,
        dma_ids=settings.dma_id_list,
        requests_per_second=settings.requests_per_second,
    ) as tm:
        for t in targets:
            # Retire positions whose event has passed rather than re-pricing a
            # ticket that can no longer be sold.
            if t.starts_at is not None and t.starts_at < now:
                with session_scope() as session:
                    holding = session.get(Holding, t.holding_id)
                    if holding is not None and holding.is_open:
                        holding.status = HoldingStatus.EXPIRED
                continue

            valuation = HoldingValuation(holding_id=t.holding_id, captured_at=now)
            raw = None

            if t.local_min is not None:
                valuation.market_min_price = t.local_min
                valuation.market_max_price = t.local_max
                valuation.status_code = t.local_status
                valuation.unit_value = t.local_min
                valuation.unrealized_pnl = (t.local_min - t.basis) * t.quantity
                from_local += 1
            else:
                raw = (
                    await tm.fetch_event_by_id(t.source_event_id)
                    if t.source_event_id
                    else None
                )
                if raw is None:
                    valuation.error = (
                        "no source event id — re-add with --url to enable pricing"
                        if not t.source_event_id
                        else "source lookup failed"
                    )
                else:
                    valuation.market_min_price = raw.min_price
                    valuation.market_max_price = raw.max_price
                    valuation.status_code = raw.status_code
                    # Mark conservatively at the current listing floor.
                    valuation.unit_value = raw.min_price
                    if raw.min_price is not None:
                        valuation.unrealized_pnl = (
                            raw.min_price - t.basis
                        ) * t.quantity
                    if raw.min_price is None:
                        valuation.error = "event has no published price range"

            with session_scope() as session:
                session.add(valuation)
                holding = session.get(Holding, t.holding_id)
                # Backfill descriptive fields the first time we resolve the event.
                if holding is not None and raw is not None:
                    holding.event_name = holding.event_name or raw.name
                    holding.artist_name = holding.artist_name or raw.artist_name
                    holding.venue_name = holding.venue_name or raw.venue_name
                    holding.city = holding.city or raw.city
                    holding.event_starts_at = holding.event_starts_at or raw.starts_at
                    holding.event_url = holding.event_url or raw.url
            written += 1

    log.info(
        "Valued %d open holdings (%d from local price data, %d via API)",
        written,
        from_local,
        written - from_local,
    )
    return written


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
@dataclass(slots=True)
class PositionReport:
    holding: Holding
    latest: HoldingValuation | None
    previous: HoldingValuation | None

    @property
    def unit_value(self) -> float | None:
        return self.latest.unit_value if self.latest else None

    @property
    def market_value(self) -> float | None:
        if self.unit_value is None:
            return None
        return self.unit_value * self.holding.quantity

    @property
    def unrealized_pnl(self) -> float | None:
        if self.market_value is None:
            return None
        return self.market_value - self.holding.total_cost

    @property
    def pnl_pct(self) -> float | None:
        cost = self.holding.total_cost
        pnl = self.unrealized_pnl
        if pnl is None or cost <= 0:
            return None
        return pnl / cost

    @property
    def day_change(self) -> float | None:
        """Per-ticket change since the previous valuation."""
        if (
            self.latest is None
            or self.previous is None
            or self.latest.unit_value is None
            or self.previous.unit_value is None
        ):
            return None
        return self.latest.unit_value - self.previous.unit_value

    @property
    def day_change_pct(self) -> float | None:
        change = self.day_change
        if change is None or not self.previous or not self.previous.unit_value:
            return None
        return change / self.previous.unit_value

    @property
    def days_to_event(self) -> int | None:
        if self.holding.event_starts_at is None:
            return None
        delta = self.holding.event_starts_at - datetime.now(timezone.utc)
        return int(delta.total_seconds() // 86400)

    @property
    def comps_url(self) -> str:
        q = urllib.parse.quote_plus(
            self.holding.artist_name or self.holding.event_name
        )
        return f"https://www.stubhub.com/secure/search?q={q}"

    @property
    def stale(self) -> bool:
        return self.latest is None or self.latest.unit_value is None


def build_report(
    session: Session, include_sold: bool = False
) -> tuple[list[PositionReport], dict]:
    """Assemble per-position reports plus portfolio totals."""
    query = select(Holding).order_by(Holding.purchased_at.desc())
    if not include_sold:
        query = query.where(
            Holding.status.in_([HoldingStatus.HELD, HoldingStatus.LISTED])
        )
    holdings = session.scalars(query).all()

    reports: list[PositionReport] = []
    for holding in holdings:
        valuations = session.scalars(
            select(HoldingValuation)
            .where(HoldingValuation.holding_id == holding.id)
            .order_by(HoldingValuation.captured_at.desc())
            .limit(2)
        ).all()
        latest = valuations[0] if valuations else None
        previous = valuations[1] if len(valuations) > 1 else None
        reports.append(PositionReport(holding, latest, previous))

    # Sort biggest movers first — that's what you actually want to read.
    reports.sort(key=lambda r: -(abs(r.day_change_pct or 0.0)))

    open_reports = [r for r in reports if r.holding.is_open]
    priced = [r for r in open_reports if r.market_value is not None]

    total_cost = sum(r.holding.total_cost for r in open_reports)
    total_value = sum(r.market_value for r in priced)
    # Only compare against the cost of positions we could actually price.
    priced_cost = sum(r.holding.total_cost for r in priced)

    realized = sum(
        h.realized_pnl
        for h in session.scalars(
            select(Holding).where(Holding.status == HoldingStatus.SOLD)
        ).all()
        if h.realized_pnl is not None
    )

    totals = {
        "positions": len(open_reports),
        "tickets": sum(r.holding.quantity for r in open_reports),
        "total_cost": total_cost,
        "priced_cost": priced_cost,
        "priced_positions": len(priced),
        # None rather than 0.0 when nothing could be priced: a real basis shown
        # against a $0 value reads as a total loss, which would be a lie.
        "total_value": total_value if priced else None,
        "unrealized_pnl": total_value - priced_cost if priced else None,
        "unrealized_pct": (
            (total_value - priced_cost) / priced_cost if priced_cost > 0 else None
        ),
        "unpriced": len(open_reports) - len(priced),
        "realized_pnl": realized,
    }
    return reports, totals


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html"])
    )


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.0f}"


def render_report(
    reports: list[PositionReport], totals: dict, now: datetime | None = None
) -> tuple[str, str]:
    now = now or datetime.now(timezone.utc)
    html = _jinja_env().get_template("portfolio_email.html").render(
        reports=reports, totals=totals, now=now
    )

    pct = totals["unrealized_pct"]
    pct_str = f" ({pct * 100:+.1f}%)" if pct is not None else ""
    lines = [
        f"Portfolio — {now:%Y-%m-%d %H:%M UTC}",
        "",
        f"Positions:   {totals['positions']} ({totals['tickets']} tickets)",
        f"Cost basis:  {_money(totals['total_cost'])}",
        f"Est. value:  {_money(totals['total_value'])}",
        f"Unrealized:  {_money(totals['unrealized_pnl'])}{pct_str}",
        f"Realized:    {_money(totals['realized_pnl'])}",
        "",
    ]
    if totals["unpriced"]:
        lines += [
            f"{totals['unpriced']} of {totals['positions']} positions could not be "
            "priced; totals cover the rest only.",
            "",
        ]
    for r in reports:
        if not r.holding.is_open:
            continue
        day = (
            f"{r.day_change_pct * 100:+.1f}% today"
            if r.day_change_pct is not None
            else "no prior mark"
        )
        pnl_pct = f" ({r.pnl_pct * 100:+.1f}%)" if r.pnl_pct is not None else ""
        when = (
            f"{r.holding.event_starts_at:%b %d}"
            if r.holding.event_starts_at
            else "date TBA"
        )
        lines += [
            f"* {r.holding.event_name} — {when} — x{r.holding.quantity}",
            f"    basis {_money(r.holding.cost_per_ticket)}/ea"
            f" -> now {_money(r.unit_value)}/ea  [{day}]",
            f"    P/L {_money(r.unrealized_pnl)}{pnl_pct}",
            f"    comps: {r.comps_url}",
        ]
        if r.stale:
            lines.append(f"    (not priced: {r.latest.error if r.latest else 'no data'})")
    lines += [
        "",
        "---",
        "Values reflect current PRIMARY listing floors, a proxy for resale, not",
        "actual resale comps. Check the comps link before acting.",
    ]
    return "\n".join(lines), html


def sms_summary(totals: dict, movers: list[PositionReport]) -> str:
    pct = totals["unrealized_pct"]
    pct_str = f" {pct * 100:+.1f}%" if pct is not None else ""
    if totals["priced_positions"] == 0:
        parts = [
            f"Tickets: {totals['positions']} positions, none could be priced "
            f"this run (basis {_money(totals['total_cost'])})."
        ]
    else:
        parts = [
            f"Tickets: {_money(totals['total_value'])} value, "
            f"{_money(totals['unrealized_pnl'])}{pct_str} unrealized "
            f"across {totals['positions']} positions."
        ]
    for r in movers[:3]:
        if r.day_change_pct is None:
            continue
        parts.append(
            f"{r.holding.event_name[:28]} {r.day_change_pct * 100:+.0f}% "
            f"({_money(r.unit_value)}/ea)"
        )
    return " | ".join(parts)


async def run_daily_report(
    settings: Settings | None = None, refresh: bool = True
) -> dict:
    """Re-price holdings, then email (and optionally text) the summary."""
    settings = settings or get_settings()

    if refresh:
        await refresh_valuations(settings)

    with session_scope() as session:
        reports, totals = build_report(session, include_sold=False)

    if not reports:
        log.info("No holdings to report on")
        return {"positions": 0, "sent": False}

    text, html = render_report(reports, totals)
    if totals["priced_positions"]:
        subject = (
            f"[tickets] Portfolio {_money(totals['unrealized_pnl'])} unrealized "
            f"across {totals['positions']} positions"
        )
    else:
        subject = f"[tickets] Portfolio — {totals['positions']} positions, none priced"
    path = deliver_email(subject, text, html, settings, slug="portfolio")

    # Anything that moved more than the threshold gets called out by text.
    movers = [
        r
        for r in reports
        if r.day_change_pct is not None
        and abs(r.day_change_pct) >= settings.position_move_alert_pct
    ]
    sms_sent = False
    if settings.sms_configured and (movers or settings.sms_daily_summary):
        sms_sent = deliver_sms(sms_summary(totals, movers), settings)

    return {
        "positions": totals["positions"],
        "unrealized_pnl": (
            round(totals["unrealized_pnl"], 2)
            if totals["unrealized_pnl"] is not None
            else None
        ),
        "movers": len(movers),
        "sms_sent": sms_sent,
        "dry_run_file": str(path) if path else None,
        "sent": True,
    }
