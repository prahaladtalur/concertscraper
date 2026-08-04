# Deploying on GitHub Actions + Neon

This runs the whole thing with no server: GitHub Actions provides the cron and
the secret storage, Neon provides a free Postgres for state. Total cost $0 on a
private repo within the included Actions minutes.

**Read [the failure mode](#the-one-thing-that-will-break-this) before you rely
on it.** It's the real tradeoff of this setup, and it's mitigated but not
eliminated.

## Why an external database

Actions runners are ephemeral — the filesystem is destroyed when a run ends. A
SQLite file would vanish between runs, and the price history *is* the asset
here, so it has to live somewhere durable. Neon's free tier is plenty: this
workload is a few thousand small rows a month.

## 1. Create the database

1. Sign up at <https://neon.tech> and create a project.
2. Copy the connection string from the dashboard. It looks like:

   ```
   postgresql://user:password@ep-cool-name-123456.us-west-2.aws.neon.tech/neondb?sslmode=require
   ```

Paste it verbatim — `app/db.py` rewrites the scheme to the driver SQLAlchemy
needs (`postgresql+psycopg://`), so you don't have to hand-edit it. Keep the
`?sslmode=require`.

## 2. Add repository secrets

**Settings → Secrets and variables → Actions → Secrets → New repository secret.**

Required:

| Secret | Value |
| --- | --- |
| `DATABASE_URL` | The Neon connection string from step 1 |
| `TICKETMASTER_API_KEY` | From <https://developer.ticketmaster.com/> |

For email delivery (without these, digests are written to the run's artifact
instead of sent):

| Secret | Value |
| --- | --- |
| `SMTP_HOST` | e.g. `smtp.gmail.com` |
| `SMTP_USER` | Your SMTP username |
| `SMTP_PASSWORD` | Your SMTP password — for Gmail, an [App Password](https://support.google.com/accounts/answer/185833), not your account password |
| `EMAIL_FROM` | Sender address |
| `EMAIL_TO` | Where alerts go; comma-separated for several |

Optional — artist demand signal, from <https://developer.spotify.com/dashboard>:
`SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`. Without them the `artist_demand`
and `headroom` factors don't fire and confidence drops accordingly, which is the
honest outcome rather than a silent guess.

Optional — SMS on large position moves, from <https://console.twilio.com/>:
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `SMS_TO`.

## 3. Tune behaviour with repository variables

**Same page → Variables tab.** All optional; the defaults are in parentheses.

| Variable | Default | Notes |
| --- | --- | --- |
| `MARKET_DMA_IDS` | `324` | Ticketmaster DMA ids, comma-separated. 324 = Seattle-Tacoma |
| `CLASSIFICATION_NAME` | `Music` | Keeps it to concerts |
| `LOOKAHEAD_DAYS` | `240` | How far ahead to scan |
| `ALERT_SCORE_THRESHOLD` | `0.62` | Raise it if you get too many alerts |
| `ALERT_COOLDOWN_HOURS` | `48` | Minimum gap before re-alerting the same event |
| `MAX_ALERTS_PER_EMAIL` | `12` | |
| `POSITION_MOVE_ALERT_PCT` | `0.15` | Text you when a position moves this much |
| `SMS_DAILY_SUMMARY` | `false` | `true` texts you daily regardless of moves |
| `EMAIL_DRY_RUN` | `false` | `true` renders to the run artifact instead of sending |

Note `DAILY_REPORT_HOUR_UTC` has **no effect here** — it drives the in-process
scheduler used when you run `uvicorn`. On Actions the cron in
`.github/workflows/report.yml` is what decides the time.

## 4. First run

Set the `EMAIL_DRY_RUN` variable to `true`, then **Actions → poll → Run
workflow**. When it finishes, download the `outbox-*` artifact and open the HTML
to see exactly what you'd have been emailed. Once it looks right, set
`EMAIL_DRY_RUN` back to `false`.

## What runs when

| Workflow | Schedule (UTC) | Does |
| --- | --- | --- |
| `poll` | every 3h at :17 | fetch, snapshot prices, score, email opportunities |
| `report` | daily 15:10 | re-price holdings, email the P/L report |
| `tests` | every push | run the suite |
| `keepalive` | monthly | commit a heartbeat (see below) |

The `:17` and `:10` offsets are deliberate — scheduled jobs across all of GitHub
pile up at the top of the hour and get queued or dropped.

GitHub cron is **UTC only and does not follow daylight saving**, so 15:10 UTC is
8:10am Pacific in summer and 7:10am in winter. Edit the cron if you want a fixed
local time.

## Recording purchases

The CLI is how you record a buy, and it needs to write to the same database. Run
it locally with `DATABASE_URL` pointed at Neon:

```bash
export DATABASE_URL='postgresql://...your neon string...'
python -m app.cli buy --url "https://www.ticketmaster.com/event/0E006012ABCD1234" \
    --qty 2 --price 58 --fees 24
python -m app.cli holdings
```

Put that `export` in a local `.env` file instead and it'll be picked up
automatically. `.env` is gitignored — keep it that way.

## The one thing that will break this

**GitHub disables scheduled workflows on a repository with no activity for 60
days, and scheduled runs do not themselves count as activity.** For a tool whose
entire value is an uninterrupted price history, quietly stopping after two
months is the worst available failure mode: you wouldn't get an error, just no
more emails, and by the time you noticed you'd have a two-month hole in the one
dataset that can't be backfilled.

`keepalive.yml` mitigates it by committing a timestamp to `.github/last-heartbeat`
on the 1st of each month, which is real repo activity and resets the clock. Cost
is 12 trivial commits a year. If you push to this repo regularly anyway, delete
that workflow.

Two residual risks the keepalive doesn't cover:

- **Cron is best-effort.** Under load GitHub delays or drops scheduled runs.
  Missing an occasional 3-hour poll is harmless — momentum only needs two
  snapshots 12h apart, and `upsert_events` forces a snapshot every 24h
  regardless of whether the price changed. Missing days in a row starts to
  matter.
- **Nothing tells you it stopped.** Consider a calendar reminder to glance at the
  Actions tab monthly. If the emails stop arriving, that's your signal.

If the history ever becomes valuable enough that a gap would genuinely hurt,
move to an always-on host — the `uvicorn app.main:app` path already runs both
schedules in-process via APScheduler, so it's a redeploy, not a rewrite.

## Verifying locally against real Postgres

Worth doing before trusting Neon, and it's how the Postgres support here was
checked:

```bash
export PGDATA=/var/lib/postgresql/csdata
sudo -u postgres /usr/lib/postgresql/16/bin/initdb -D $PGDATA -A trust
sudo -u postgres /usr/lib/postgresql/16/bin/pg_ctl -D $PGDATA \
    -o '-p 55432 -k /tmp' -l /tmp/pg.log start
psql -h /tmp -p 55432 -U postgres -c 'CREATE DATABASE cs;'

export DATABASE_URL='postgresql://postgres@/cs?host=/tmp&port=55432'
python -m app.cli init && python -m app.cli seed && python -m app.cli top
```

All timestamp columns should come back as `timestamp with time zone`:

```bash
psql -h /tmp -p 55432 -U postgres -d cs -c "
SELECT table_name, column_name, data_type FROM information_schema.columns
WHERE table_schema='public' AND data_type LIKE '%timestamp%';"
```

## Costs

- **Actions**: ~250 runs/month at 1–2 min each. Private repos include 2,000
  min/month on the free plan, so this fits with room to spare.
- **Neon**: free tier covers this comfortably.
- **Ticketmaster**: free, 5,000 requests/day. A 3-hour poll of one market uses a
  small fraction. Holdings inside a watched market cost zero extra requests —
  they're valued from prices the poll already stored.
