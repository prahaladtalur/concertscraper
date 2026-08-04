"""FastAPI app: dashboard, manual triggers, and the background scheduler."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select

from .alerts import Candidate, record_alerts, select_candidates, send_email
from .config import get_settings
from .db import init_db, session_scope
from .ingest import rescore_all, run_cycle
from .models import Alert, Artist, Event, PriceSnapshot, Score
from .portfolio import build_report, run_daily_report

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("concertscraper")

settings = get_settings()
TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html"])
)

scheduler = AsyncIOScheduler(timezone="UTC")


async def scheduled_cycle() -> None:
    """Poll, score, and email anything new that clears the bar."""
    try:
        await run_cycle(settings)
    except Exception:
        log.exception("Poll cycle failed")
        return

    try:
        with session_scope() as session:
            candidates = select_candidates(session, settings)
            if not candidates:
                return
            send_email(candidates, settings)
            record_alerts(session, candidates)
    except Exception:
        log.exception("Digest delivery failed")


async def scheduled_portfolio_report() -> None:
    """Daily mark-to-market on everything you've bought."""
    try:
        await run_daily_report(settings)
    except Exception:
        log.exception("Daily portfolio report failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if not settings.ticketmaster_api_key:
        log.warning(
            "TICKETMASTER_API_KEY is not set — polling will return nothing. "
            "Get a free key at https://developer.ticketmaster.com/"
        )
    scheduler.add_job(
        scheduled_cycle,
        IntervalTrigger(minutes=settings.poll_interval_minutes),
        id="poll",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        scheduled_portfolio_report,
        CronTrigger(hour=settings.daily_report_hour_utc, minute=0, timezone="UTC"),
        id="daily_portfolio",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info(
        "Scheduler started: poll every %d min, portfolio report daily at %02d:00 UTC",
        settings.poll_interval_minutes,
        settings.daily_report_hour_utc,
    )
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="concertscraper", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        # Show more than the email does, and don't apply the cooldown here.
        widened = settings.model_copy(update={"max_alerts_per_email": 60})
        candidates = select_candidates(session, widened, now=now)

        latest = (
            select(Score.event_id, func.max(Score.computed_at).label("computed_at"))
            .group_by(Score.event_id)
            .subquery()
        )
        vetoed = session.scalar(
            select(func.count())
            .select_from(Score)
            .join(
                latest,
                (Score.event_id == latest.c.event_id)
                & (Score.computed_at == latest.c.computed_at),
            )
            .where(Score.score == 0.0)
        )
        stats = {
            "events": session.scalar(select(func.count()).select_from(Event)) or 0,
            "snapshots": session.scalar(select(func.count()).select_from(PriceSnapshot)) or 0,
            "artists": session.scalar(select(func.count()).select_from(Artist)) or 0,
            "alerts": session.scalar(select(func.count()).select_from(Alert)) or 0,
            "vetoed": vetoed or 0,
        }
        positions, totals = build_report(session, include_sold=False)
        html = _env.get_template("dashboard.html").render(
            candidates=candidates,
            stats=stats,
            positions=positions,
            totals=totals,
            now=now,
        )
    return HTMLResponse(html)


@app.post("/api/poll")
async def api_poll() -> JSONResponse:
    return JSONResponse(await run_cycle(settings))


@app.post("/api/rescore")
def api_rescore() -> JSONResponse:
    with session_scope() as session:
        results = rescore_all(session)
        return JSONResponse({"scored": len(results)})


@app.post("/api/digest")
def api_digest() -> JSONResponse:
    with session_scope() as session:
        candidates = select_candidates(session, settings)
        if not candidates:
            return JSONResponse({"sent": 0, "note": "nothing above threshold"})
        path = send_email(candidates, settings)
        record_alerts(session, candidates)
        return JSONResponse(
            {
                "sent": len(candidates),
                "dry_run_file": str(path) if path else None,
                "top": candidates[0].event.name,
            }
        )


@app.post("/api/portfolio/report")
async def api_portfolio_report() -> JSONResponse:
    return JSONResponse(await run_daily_report(settings))


@app.get("/api/portfolio")
def api_portfolio() -> JSONResponse:
    with session_scope() as session:
        reports, totals = build_report(session, include_sold=True)
        return JSONResponse(
            {
                "totals": {
                    k: (round(v, 2) if isinstance(v, float) else v)
                    for k, v in totals.items()
                },
                "positions": [
                    {
                        "id": r.holding.id,
                        "event_name": r.holding.event_name,
                        "status": r.holding.status,
                        "quantity": r.holding.quantity,
                        "cost_per_ticket": round(r.holding.cost_per_ticket, 2),
                        "total_cost": round(r.holding.total_cost, 2),
                        "unit_value": r.unit_value,
                        "unrealized_pnl": (
                            round(r.unrealized_pnl, 2)
                            if r.unrealized_pnl is not None
                            else None
                        ),
                        "pnl_pct": (
                            round(r.pnl_pct, 4) if r.pnl_pct is not None else None
                        ),
                        "day_change_pct": (
                            round(r.day_change_pct, 4)
                            if r.day_change_pct is not None
                            else None
                        ),
                        "realized_pnl": (
                            round(r.holding.realized_pnl, 2)
                            if r.holding.realized_pnl is not None
                            else None
                        ),
                        "days_to_event": r.days_to_event,
                        "event_url": r.holding.event_url,
                        "comps_url": r.comps_url,
                    }
                    for r in reports
                ],
            }
        )


@app.get("/api/events")
def api_events(limit: int = 100) -> JSONResponse:
    """Raw ranked feed, for piping into a spreadsheet or another tool."""
    with session_scope() as session:
        widened = settings.model_copy(
            update={"max_alerts_per_email": limit, "alert_score_threshold": 0.0}
        )
        candidates: list[Candidate] = select_candidates(session, widened)
        return JSONResponse(
            [
                {
                    "name": c.event.name,
                    "artist": c.event.artist_name,
                    "venue": c.event.venue_name,
                    "city": c.event.city,
                    "starts_at": c.event.starts_at.isoformat() if c.event.starts_at else None,
                    "min_price": c.event.min_price,
                    "max_price": c.event.max_price,
                    "score": round(c.score.score, 4),
                    "confidence": round(c.score.confidence, 4),
                    "kind": c.kind,
                    "reasons": c.reasons,
                    "url": c.buy_url,
                }
                for c in candidates
            ]
        )


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "scheduler_running": scheduler.running}
