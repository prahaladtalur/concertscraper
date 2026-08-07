# concertscraper

A ticket-resale research tool. It watches Ticketmaster's official API, tracks how
the cheapest available ticket moves over time, scores events for appreciation
potential, and emails you ranked opportunities with one-click links. It also
tracks the tickets you actually bought and sends a daily mark-to-market report.

It does **not** buy anything, bypass queues, solve CAPTCHAs, or automate
checkout. See [Legal boundaries](#legal-boundaries) for why that's a deliberate
design decision and not a missing feature.

---

## Is this business viable? Read this first

Short answer: **viable as a personal research edge, not as a "scrape and flip"
machine.** Four things kill the naive version, and you should size your
expectations to them before writing a line of code or spending a dollar.

**1. Non-transferable tickets are the whole ballgame.**
Ticketmaster SafeTix and AXS Mobile ID increasingly bind tickets to the buyer's
account, with transfer disabled or resale restricted to face value on the
official exchange. A ticket you cannot transfer has no resale value at any
price, no matter how hot the artist. This is why `transfer_blocked` is a hard
veto in the scoring model rather than a penalty — it zeroes the score outright.
Expect a meaningful share of the hottest tours to be un-flippable for exactly
this reason.

**2. Dynamic pricing already eats your margin.**
Ticketmaster's "Official Platinum" algorithmically raises face price toward what
the market will bear. That is precisely the surplus resellers used to capture.
Ticketmaster also owns the largest resale marketplace, so on a typical trade
they take a cut on the way in and on the way out, and they set the entry price.
You are trading against the house, in the house.

**3. Scraping the sites directly does not work for long.**
Ticketmaster and AXS both prohibit automated collection in their terms, and both
run aggressive bot detection. An HTML scraper will break on every layout change
and can get your IP and account banned. This project uses Ticketmaster's
official, free Discovery API instead — documented, stable, and permitted.

**4. The BOTS Act is real law.**
The Better Online Ticket Sales Act of 2016 (15 U.S.C. § 45c) makes it illegal
to circumvent security measures, access controls, or purchase limits on a ticket
seller in order to acquire tickets, and to resell tickets knowingly obtained
that way. The FTC enforces it, with penalties in the millions. Collecting public
price data for research is a different act from circumventing an access control
to buy. This project stays firmly on the first side of that line.

**So where is the actual edge?** Nobody systematically tracks the *price floor
over time* across a whole metro. Ticketmaster publishes the current price range
but no history. If you accumulate that history yourself, you can see inventory
being consumed — the cheap seats sell first, so the minimum price ratchets up —
weeks before it's obvious. That time series is the moat, it's legal to build,
and it compounds: the tool gets more useful the longer it runs.

**Honest expectations.** Treat this as a tool that surfaces maybe a handful of
genuinely good opportunities a month in one metro, on which you make the buy
decision yourself. Start with small position sizes, track your realized P/L in
the portfolio module, and let real numbers — not the model's optimism — tell you
whether it's worth scaling. Also check your state's resale laws: a few states
and many venues restrict resale, and some require a license to resell at volume.
None of this is financial or legal advice.

---

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then add your API key

# Try the whole pipeline on synthetic data, no credentials needed:
python -m app.cli seed
python -m app.cli top
```

To run against live data, get a free Ticketmaster Discovery API key at
<https://developer.ticketmaster.com/> and set `TICKETMASTER_API_KEY` in `.env`.
Optionally add Spotify client credentials for the artist-demand signal.

```bash
python -m app.cli poll        # fetch, store, score
python -m app.cli digest      # build the email digest
```

Then start the web app, which also runs the scheduler:

```bash
uvicorn app.main:app --reload
# dashboard at http://localhost:8000
```

`EMAIL_DRY_RUN=true` (the default) writes rendered emails to `./outbox/`
instead of sending them. Open the HTML file in a browser to see exactly what
you'd receive. Set it to `false` and fill in the SMTP settings to go live.

---

## Running it for real

The tool is only useful if it runs continuously — momentum needs two price
snapshots at least 12 hours apart, and the history compounds. Two supported
setups:

**Serverless (no host to manage).** GitHub Actions runs the cron, Neon provides
a free Postgres for state. Costs nothing. See **[docs/DEPLOY.md](docs/DEPLOY.md)**
for the full walkthrough, including the 60-day scheduled-workflow auto-disable
that this setup has to work around.

| Workflow | Schedule (UTC) | Does |
| --- | --- | --- |
| `poll` | every 3h at :17 | fetch, snapshot, score, email opportunities |
| `report` | daily 15:10 | re-price holdings, email the P/L report |
| `tests` | every push | run the suite |
| `keepalive` | monthly | heartbeat commit so the schedules stay enabled |

**Always-on host.** `uvicorn app.main:app` runs both schedules in-process via
APScheduler and serves the dashboard. More reliable and gives you the dashboard
on your phone; costs a few dollars a month. No code changes needed — the same
`DATABASE_URL` works with SQLite on a persistent volume or with Postgres.

Both paths use the same code. Postgres support is verified against a real
Postgres 16 server, not just SQLite: every timestamp column lands as
`timestamptz`, and connection pooling uses `pool_pre_ping` so a database that
has scaled to zero while idle reconnects instead of erroring.

---

## How the scoring works

Six factors, each normalised to 0–1 and weighted. Every alert explains itself in
plain language, because an alert you can't justify is one you shouldn't act on.

| Factor | Weight | What it measures |
| --- | --- | --- |
| `price_momentum` | 0.26 | Rise in the cheapest ticket between first and last sighting |
| `floor_pressure` | 0.15 | How far the current floor sits above its all-time observed low |
| `artist_demand` | 0.20 | Spotify popularity (0–100) plus log-scaled follower count |
| `scarcity` | 0.14 | Fewer dates in the market and smaller rooms mean fixed supply |
| `timing` | 0.13 | Position on the appreciation curve; peaks around 3–8 weeks out |
| `headroom` | 0.12 | Strong demand against a low face price is where margin lives |

`price_momentum` carries the most weight because it's the only signal derived
from our own observation rather than a static attribute — and therefore the only
one a competitor without a price history can't replicate.

**Confidence is reported separately from score.** Factors return nothing when
the data isn't there, the weighted mean is renormalised over what's available,
and `confidence` tells you how much of the model actually fired. A 0.85 at 0.30
confidence is a guess; a 0.70 at 0.90 confidence is a signal. Ranking uses
`score × confidence`, so a confident good opportunity outranks a shaky great
one. Anything below `MIN_CONFIDENCE` (0.35) never emails you at all.

**Vetoes are absolute**, not penalties. Any of these zeroes the score:

- transfer blocked (non-transferable, resale prohibited, paperless, credit-card
  entry, face-value-only exchange, ID-must-match-purchaser)
- event cancelled, postponed, or rescheduled
- off sale with no upcoming onsale
- event already happened, or under 48 hours out — no runway to resell

New events get a `cold_start` label and are scored on artist and scarcity alone
until two price snapshots exist at least 12 hours apart. Events that haven't
gone on sale yet are labelled `upcoming_onsale` and surfaced as reminders rather
than buy links, with an add-to-calendar link.

---

## Portfolio tracking

Record what you bought, and get a daily report on what it's worth.

```bash
# Record a purchase. The event URL is preferred — the id inside it is what
# makes daily auto-pricing possible.
python -m app.cli buy --url "https://www.ticketmaster.com/event/0E006012ABCD1234" \
    --qty 2 --price 58 --fees 24 --section Orch --row F

# Anything bought off-platform still gets tracked, just not auto-priced.
python -m app.cli buy --name "Festival pass" --qty 1 --price 320

python -m app.cli holdings     # positions and P/L
python -m app.cli value        # re-price now
python -m app.cli report       # build and send the daily report
python -m app.cli sell 2 --price 180 --fees 50   # realise a position
```

Example output:

```
  ID  QTY   BASIS/EA    NOW/EA         P/L      DAY  EVENT
   2    4  $      55       $92       +$148   +50.8%  Turnstile
   1    2  $      70       $74         +$8   -11.9%  Phoebe Bridgers
   3    1  $     320         —           —        —  Off-platform festival pass

Cost basis:  $680.00
Est. value:  $516.00
Unrealized:  $156.00  (+43.3%)
Realized:    $0.00

1 of 3 position(s) could not be priced; totals cover the rest only.
```

Fees are amortised across the order, so `BASIS/EA` is your true all-in cost per
ticket and P/L is honest. Selling fees are netted out of realised P/L too.

The daily job runs at `DAILY_REPORT_HOUR_UTC` (default 15:00). It marks every
open position, emails the summary, and — if Twilio is configured — texts you
about any position that moved more than `POSITION_MOVE_ALERT_PCT` (default 15%).
Set `SMS_DAILY_SUMMARY=true` to get a text every day regardless.

Positions whose event date has passed are retired as `expired` rather than
re-priced forever.

### What the valuation number actually means

Read this before you trust a P/L figure. The only price feed available without a
partner agreement is Ticketmaster's **primary** listing range, which includes
their dynamic pricing. That is a *proxy* for the resale market, not the resale
market. Real resale comps live behind partner-only APIs on StubHub and
Ticketmaster's own exchange.

So: the **direction of travel is reliable**, the **absolute level is an
estimate**. Every report row carries a resale-comps link — open it before you
list or sell. Positions that couldn't be priced are reported as "unpriced" and
excluded from the totals rather than silently marked at zero, because a real
cost basis against a $0 value would read as a total loss.

To conserve API quota, a holding linked to an event the poller already refreshed
in the last 12 hours is valued from that stored price with no extra request. A
portfolio inside your watched markets therefore costs essentially nothing
against the 5,000-request daily budget.

---

## Commands

| Command | Purpose |
| --- | --- |
| `init` | Create database tables |
| `preflight` | Check that credentials actually work |
| `poll` | One full cycle: fetch, store, enrich, score |
| `score` | Rescore stored events without fetching |
| `top [--limit N]` | Print the current ranking |
| `digest` | Build and deliver the opportunity email |
| `seed` | Load synthetic data; no credentials needed |
| `buy` | Record a purchase |
| `sell <id> --price P` | Mark a holding sold |
| `holdings [--all]` | Show positions and P/L |
| `value` | Re-price open holdings now |
| `report [--no-refresh]` | Send the daily portfolio report |

HTTP endpoints: `GET /` (dashboard), `GET /healthz`, `GET /api/events`,
`GET /api/portfolio`, and `POST` triggers at `/api/poll`, `/api/rescore`,
`/api/digest`, `/api/portfolio/report`.

---

## Legal boundaries

Deliberately built in, and worth understanding before you change anything:

- **Official API only.** Ticketmaster's Discovery API is documented and free
  (5,000 requests/day, ~5 req/sec). The client rate-limits itself under that and
  slices its date window to avoid the deep-paging cap rather than hammering.
- **No HTML scraping of Ticketmaster.** Despite this project's name, it doesn't
  scrape Ticketmaster at all.
- **AXS is disabled by default.** AXS has no public API and its terms prohibit
  automated collection. The adapter refuses to run unless you explicitly opt in,
  fetches and honours `robots.txt` first, treats an unreachable `robots.txt` as
  "disallow everything", and ships with no parser. See `docs/SOURCES.md`.
- **No purchase automation, ever.** No queue bypass, no CAPTCHA solving, no
  automated checkout, no buying past posted limits. That's the conduct the BOTS
  Act targets. Alerts hand you a link; you decide and click.

The tool is a research aid. The judgement, the purchase, and the legal
responsibility for how you resell all stay with you.

---

## Development

```bash
pip install -r requirements.txt
python -m pytest              # 106 tests, no network access required
```

Tests use `respx` to mock HTTP and an in-memory SQLite database. The suite
covers scoring factor behaviour and bounds, every veto, transfer-block phrase
detection, Ticketmaster response parsing, AXS robots.txt guardrails, the
ingest/snapshot/dedupe path, alert thresholds and cooldowns, portfolio cost-basis
and P/L maths, the local-vs-API valuation fallback, database-URL normalisation for
hosted Postgres, and HTML escaping in both email templates.

### Layout

```
app/
  config.py       env-driven settings
  models.py       SQLAlchemy models (+ UTC-normalising DateTime)
  db.py           engine and session_scope
  scoring.py      the appreciation model — the interesting part
  ingest.py       fetch -> upsert -> snapshot -> enrich -> score
  alerts.py       candidate selection and the opportunity digest
  portfolio.py    positions, mark-to-market, daily report
  notify.py       SMTP email and optional Twilio SMS
  main.py         FastAPI app, dashboard, APScheduler jobs
  cli.py          command line entry points
  sources/
    base.py       RawEvent contract, rate limiter, transfer-block detection
    ticketmaster.py   Discovery API v2 adapter
    spotify.py    artist demand metrics
    axs.py        robots-respecting stub, off by default
```

### If you want to extend it

The highest-value additions, roughly in order:

1. **Real resale comps.** Apply for StubHub or Ticketmaster partner API access.
   This converts every estimate in the portfolio into an actual number and is by
   far the biggest single upgrade available.
2. **Backtesting.** Once you have a few months of snapshots, replay history to
   measure whether the score actually predicted appreciation, then refit
   `WEIGHTS` on evidence instead of intuition. Right now those weights are
   reasoned priors, not fitted parameters — that's the model's main weakness.
3. **Sell-side signals.** The system tells you when to buy but not when to sell.
   A falling floor inside 14 days of the event is your exit signal.
4. **More markets.** `MARKET_DMA_IDS` takes a comma-separated list; the request
   budget is the only real constraint.
