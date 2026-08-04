"""Command line entry points.

Discovery:
    python -m app.cli init      # create tables
    python -m app.cli poll      # one fetch + score cycle
    python -m app.cli score     # rescore stored events only
    python -m app.cli top       # print the current ranking
    python -m app.cli digest    # build and send/write the opportunity digest
    python -m app.cli seed      # load synthetic data (no API key needed)

Portfolio:
    python -m app.cli buy --url URL --qty 2 --price 58 --fees 24
    python -m app.cli holdings  # positions and P/L
    python -m app.cli value     # re-price open holdings now
    python -m app.cli report    # send the daily mark-to-market report
    python -m app.cli sell ID --price 180 --fees 50
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .alerts import record_alerts, select_candidates, send_email
from .config import get_settings
from .db import init_db, session_scope
from .ingest import rescore_all, run_cycle
from .models import Artist, Event, PriceSnapshot
from .portfolio import (
    add_holding,
    build_report,
    mark_sold,
    refresh_valuations,
    run_daily_report,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
log = logging.getLogger("cli")


def cmd_init(_: argparse.Namespace) -> int:
    init_db()
    print("Tables created.")
    return 0


def cmd_poll(_: argparse.Namespace) -> int:
    init_db()
    settings = get_settings()
    if not settings.ticketmaster_api_key:
        print(
            "No TICKETMASTER_API_KEY set. Get a free key at\n"
            "  https://developer.ticketmaster.com/\n"
            "or run `python -m app.cli seed` to try the pipeline on synthetic data.",
            file=sys.stderr,
        )
        return 1
    summary = asyncio.run(run_cycle(settings))
    for key, value in summary.items():
        print(f"{key:18} {value}")
    return 0


def cmd_score(_: argparse.Namespace) -> int:
    init_db()
    with session_scope() as session:
        print(f"Scored {len(rescore_all(session))} events.")
    return 0


def cmd_digest(_: argparse.Namespace) -> int:
    init_db()
    settings = get_settings()
    with session_scope() as session:
        candidates = select_candidates(session, settings)
        if not candidates:
            print("Nothing above the alert threshold.")
            return 0
        path = send_email(candidates, settings)
        record_alerts(session, candidates)
        print(f"{len(candidates)} candidates.")
        if path:
            print(f"Dry run — digest written to {path}")
    return 0


def cmd_top(args: argparse.Namespace) -> int:
    init_db()
    settings = get_settings().model_copy(
        update={"alert_score_threshold": 0.0, "max_alerts_per_email": args.limit}
    )
    with session_scope() as session:
        candidates = select_candidates(session, settings)
        if not candidates:
            print("No scored events yet. Run `poll` or `seed` first.")
            return 0
        for i, c in enumerate(candidates, 1):
            ev = c.event
            when = f"{ev.starts_at:%Y-%m-%d}" if ev.starts_at else "TBA"
            price = f"${ev.min_price:.0f}" if ev.min_price else "—"
            print(
                f"{i:2}. {c.score.score:.2f} (conf {c.score.confidence:.2f})  "
                f"{when}  {price:>6}  {ev.name[:58]}"
            )
            for reason in c.reasons[:2]:
                print(f"      · {reason}")
    return 0


def cmd_seed(_: argparse.Namespace) -> int:
    """Insert a handful of synthetic events with price history.

    Lets you exercise scoring, ranking, and the email template end to end
    before wiring up any API credentials.
    """
    init_db()
    now = datetime.now(timezone.utc)

    fixtures = [
        # (name, artist, venue, cap, days_out, popularity, followers,
        #  price_path, dates_in_market, restrictions)
        (
            "Phoebe Bridgers",
            "Phoebe Bridgers",
            "Paramount Theatre",
            2800,
            34,
            82,
            4_200_000,
            [58, 62, 71, 84],
            1,
            None,
        ),
        (
            "Turnstile",
            "Turnstile",
            "Showbox SoDo",
            1800,
            26,
            74,
            1_100_000,
            [45, 47, 52, 61],
            1,
            None,
        ),
        (
            "Mega Stadium Act",
            "Mega Stadium Act",
            "Lumen Field",
            68000,
            120,
            91,
            32_000_000,
            [110, 108, 106, 105],
            3,
            None,
        ),
        (
            "Locked Down Tour",
            "Locked Down Tour",
            "Climate Pledge Arena",
            17000,
            40,
            88,
            9_000_000,
            [95, 104, 118, 131],
            1,
            "All tickets are mobile-only and non-transferable. Resale is prohibited.",
        ),
        (
            "Local Opener Night",
            "Local Opener Night",
            "Sunset Tavern",
            200,
            12,
            29,
            18_000,
            [15, 15, 15, 15],
            1,
            None,
        ),
    ]

    with session_scope() as session:
        for (
            name,
            artist_name,
            venue,
            cap,
            days_out,
            popularity,
            followers,
            prices,
            dates_in_market,
            restrictions,
        ) in fixtures:
            artist = session.scalar(select(Artist).where(Artist.name == artist_name))
            if artist is None:
                artist = Artist(name=artist_name)
                session.add(artist)
            artist.popularity = popularity
            artist.followers = followers
            artist.updated_at = now
            session.flush()

            # Mimic real Ticketmaster ids (alphanumeric, no separators) so the
            # URL parser used by `buy` behaves the same on seed data.
            slug = "".join(ch for ch in artist_name.upper() if ch.isalnum())[:12]
            source_id = f"SEED{slug}"
            event = session.scalar(
                select(Event).where(
                    Event.source == "seed", Event.source_id == source_id
                )
            )
            if event is None:
                event = Event(source="seed", source_id=source_id)
                session.add(event)

            blocked = bool(restrictions and "non-transferable" in restrictions.lower())
            event.name = name
            event.artist_name = artist_name
            event.artist_id = artist.id
            event.venue_name = venue
            event.venue_capacity = cap
            event.city = "Seattle"
            event.state = "WA"
            event.dma_id = "324"
            event.starts_at = now + timedelta(days=days_out)
            event.onsale_starts_at = now - timedelta(days=20)
            event.url = f"https://www.ticketmaster.com/event/{source_id}"
            event.min_price = float(prices[-1])
            event.max_price = float(prices[-1]) * 3.4
            event.currency = "USD"
            event.status_code = "onsale"
            event.restrictions_text = restrictions
            event.transfer_blocked = blocked
            event.transfer_block_reason = (
                "tickets marked non-transferable" if blocked else None
            )
            event.tour_dates_total = dates_in_market
            event.tour_dates_in_market = dates_in_market
            session.flush()

            session.query(PriceSnapshot).filter(
                PriceSnapshot.event_id == event.id
            ).delete()
            # Four snapshots, two days apart, so momentum has a real window.
            for i, price in enumerate(prices):
                session.add(
                    PriceSnapshot(
                        event_id=event.id,
                        captured_at=now - timedelta(days=2 * (len(prices) - 1 - i)),
                        min_price=float(price),
                        max_price=float(price) * 3.4,
                        status_code="onsale",
                    )
                )

        rescore_all(session, now=now)

    print(f"Seeded {len(fixtures)} synthetic events. Try `python -m app.cli top`.")
    return 0


# --------------------------------------------------------------------------
# Portfolio commands
# --------------------------------------------------------------------------
def cmd_buy(args: argparse.Namespace) -> int:
    init_db()
    with session_scope() as session:
        try:
            holding = add_holding(
                session,
                event_url=args.url,
                source_event_id=args.event_id,
                event_name=args.name,
                quantity=args.qty,
                unit_cost=args.price,
                fees=args.fees,
                section=args.section,
                row=args.row,
                notes=args.notes,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(
            f"Recorded #{holding.id}: {holding.quantity}x {holding.event_name} "
            f"at ${holding.unit_cost:.2f}/ea (all-in "
            f"${holding.cost_per_ticket:.2f}/ea, total ${holding.total_cost:.2f})"
        )
        if not holding.source_event_id:
            print(
                "note: no Ticketmaster event id — this position cannot be "
                "auto-priced. Re-add with --url to enable daily valuation.",
                file=sys.stderr,
            )
    return 0


def cmd_sell(args: argparse.Namespace) -> int:
    init_db()
    with session_scope() as session:
        try:
            holding = mark_sold(session, args.id, args.price, args.fees)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        pnl = holding.realized_pnl or 0.0
        print(
            f"Sold #{holding.id} {holding.event_name}: "
            f"{holding.quantity}x at ${args.price:.2f}/ea "
            f"-> realized {'+' if pnl >= 0 else '-'}${abs(pnl):.2f}"
        )
    return 0


def cmd_holdings(args: argparse.Namespace) -> int:
    init_db()
    with session_scope() as session:
        reports, totals = build_report(session, include_sold=args.all)
        if not reports:
            print("No holdings recorded. Use `buy` to add one.")
            return 0

        print(
            f"{'ID':>4}  {'QTY':>3}  {'BASIS/EA':>9}  {'NOW/EA':>8}  "
            f"{'P/L':>10}  {'DAY':>7}  EVENT"
        )
        for r in reports:
            h = r.holding
            now_ea = f"${r.unit_value:,.0f}" if r.unit_value is not None else "—"
            pnl = (
                f"{'+' if r.unrealized_pnl >= 0 else '-'}${abs(r.unrealized_pnl):,.0f}"
                if r.unrealized_pnl is not None
                else "—"
            )
            day = (
                f"{r.day_change_pct * 100:+.1f}%"
                if r.day_change_pct is not None
                else "—"
            )
            flag = "" if h.is_open else f"  [{h.status}]"
            print(
                f"{h.id:>4}  {h.quantity:>3}  ${h.cost_per_ticket:>8,.0f}  "
                f"{now_ea:>8}  {pnl:>10}  {day:>7}  {h.event_name[:40]}{flag}"
            )

        def dollars(v):
            return "—" if v is None else f"${v:,.2f}"

        pct = totals["unrealized_pct"]
        pct_str = f"  ({pct * 100:+.1f}%)" if pct is not None else ""
        print()
        print(f"Cost basis:  {dollars(totals['total_cost'])}")
        print(f"Est. value:  {dollars(totals['total_value'])}")
        print(f"Unrealized:  {dollars(totals['unrealized_pnl'])}{pct_str}")
        print(f"Realized:    {dollars(totals['realized_pnl'])}")
        if totals["unpriced"]:
            print(
                f"\n{totals['unpriced']} of {totals['positions']} position(s) could "
                "not be priced; totals cover the rest only."
            )
    return 0


def cmd_value(_: argparse.Namespace) -> int:
    init_db()
    settings = get_settings()
    if not settings.ticketmaster_api_key:
        # Holdings linked to a locally-tracked event still price fine without a
        # key; only unlinked ones need the API lookup.
        print(
            "note: no TICKETMASTER_API_KEY — pricing only positions with recent "
            "local price data.",
            file=sys.stderr,
        )
    count = asyncio.run(refresh_valuations(settings))
    print(f"Valued {count} open holdings.")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    init_db()
    summary = asyncio.run(run_daily_report(get_settings(), refresh=not args.no_refresh))
    for key, value in summary.items():
        print(f"{key:18} {value}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="concertscraper")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create database tables").set_defaults(func=cmd_init)
    sub.add_parser("poll", help="fetch, store, and score").set_defaults(func=cmd_poll)
    sub.add_parser("score", help="rescore stored events").set_defaults(func=cmd_score)
    sub.add_parser("digest", help="build and deliver the digest").set_defaults(
        func=cmd_digest
    )
    sub.add_parser("seed", help="load synthetic demo data").set_defaults(func=cmd_seed)

    top = sub.add_parser("top", help="print current ranking")
    top.add_argument("--limit", type=int, default=20)
    top.set_defaults(func=cmd_top)

    buy = sub.add_parser("buy", help="record tickets you purchased")
    buy.add_argument("--url", help="Ticketmaster event URL (enables auto-pricing)")
    buy.add_argument("--event-id", help="Ticketmaster event id, if you have it")
    buy.add_argument("--name", help="event name, for off-platform purchases")
    buy.add_argument("--qty", type=int, default=1, help="number of tickets")
    buy.add_argument("--price", type=float, required=True, help="price per ticket paid")
    buy.add_argument("--fees", type=float, default=0.0, help="total order fees")
    buy.add_argument("--section")
    buy.add_argument("--row")
    buy.add_argument("--notes")
    buy.set_defaults(func=cmd_buy)

    sell = sub.add_parser("sell", help="mark a holding sold")
    sell.add_argument("id", type=int, help="holding id (see `holdings`)")
    sell.add_argument("--price", type=float, required=True, help="sale price per ticket")
    sell.add_argument("--fees", type=float, default=0.0, help="total selling fees")
    sell.set_defaults(func=cmd_sell)

    holdings = sub.add_parser("holdings", help="show positions and P/L")
    holdings.add_argument("--all", action="store_true", help="include sold positions")
    holdings.set_defaults(func=cmd_holdings)

    sub.add_parser("value", help="re-price open holdings now").set_defaults(
        func=cmd_value
    )

    report = sub.add_parser("report", help="send the daily portfolio report")
    report.add_argument(
        "--no-refresh", action="store_true", help="report on stored values only"
    )
    report.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
