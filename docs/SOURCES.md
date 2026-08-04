# Data sources, terms, and what's actually allowed

Read this before adding a source or enabling AXS. The constraints below drove
most of the architecture, so changing them changes the design.

## Ticketmaster Discovery API v2 — the backbone

**Status: official, free, permitted. This is the primary source.**

- Register: <https://developer.ticketmaster.com/>
- Docs: <https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/>
- Quota: 5,000 requests/day, roughly 5 requests/second
- Auth: an `apikey` query parameter

Using the documented API means we never touch Ticketmaster's bot defences, never
violate their terms of use, and don't break when they change their HTML. It also
means we inherit their limits, which the adapter is built around.

### Quirks the adapter handles

**Deep paging is capped.** The API rejects requests where `page * size >= 1000`,
so a single wide query silently truncates once a market has more than ~1,000
events in range. `TicketmasterSource.fetch_events` slices the lookahead into
30-day windows and pages within each, which keeps every request well under the
wall and makes coverage complete rather than merely plausible.

**`priceRanges` is often absent.** Before the public onsale there's usually no
price at all, and after it there may be several entries (standard, VIP,
platinum). `_price_span` takes the widest span across tiers, and falls back from
a missing bound to the other one rather than returning a half-open range.

**Dates come in two shapes.** `dates.start.dateTime` is UTC and reliable;
TBA events carry only `dates.start.localDate`. The adapter treats a missing
`dateTime` as an unknown date rather than guessing, and the `timing` scoring
factor simply doesn't fire for those.

**Capacity is a string.** `_embedded.venues[0].capacity` arrives as text when
it's present at all, hence the `isdigit()` guard.

**Restrictions live in prose.** There is no structured "is this transferable"
field. The signal is buried in `pleaseNote` and `info` free text, which is why
`detect_transfer_block` pattern-matches phrases like "non-transferable", "no
resale", "paperless", and "credit card entry". This is the single most important
filter in the system, so if you find a phrasing it misses, add it to
`_TRANSFER_BLOCK_PATTERNS` in `app/sources/base.py` and add a test case.

### What it does *not* give you

Resale prices. Discovery returns primary inventory only. Everything the portfolio
module reports is therefore a primary-listing proxy — see the caveat in the
README. Ticketmaster's resale exchange has an API, but it's partner-only.

## Spotify Web API — demand signal

**Status: official, free, permitted. Optional.**

- Register: <https://developer.spotify.com/dashboard>
- Flow: client credentials — no user login, no redirect URI needed

`popularity` (0–100, relative to every artist on the platform) plus follower
count is the cheapest reliable proxy for who's hot. The client caches its bearer
token until a minute before expiry and refreshes lazily.

One caveat worth knowing: Spotify's search relevance ranking puts tribute bands
and soundalikes surprisingly high, so `artist_metrics` prefers an exact
case-insensitive name match over position 0 and only falls back to the top result
when there's no exact hit. Artists that resolve to nothing are marked
`lookup_failed` so the enricher doesn't retry them every cycle.

The system runs fine without Spotify — the `artist_demand` and `headroom` factors
just don't fire, and `confidence` drops accordingly, which is the honest outcome.

## AXS — disabled by default, and probably should stay that way

**Status: no public API. Terms prohibit automated collection.**

AXS publishes no developer API. Their Terms of Use prohibit automated data
collection, and the site runs bot detection. `ENABLE_AXS` therefore defaults to
`false`, and the adapter is built to fail closed:

1. It returns immediately unless `ENABLE_AXS=true` is set explicitly.
2. It fetches `https://www.axs.com/robots.txt` and checks the target path against
   it before any other request.
3. An unreachable or unparseable `robots.txt` is treated as `Disallow: /`.
4. It identifies itself honestly in its user agent and crawls at one request
   every four seconds.
5. **It ships with no parser.** Even if robots.txt permitted the path, the fetch
   method returns an empty list.

That last point is deliberate. Writing a speculative parser for a site whose
terms forbid the activity would be building the thing the guardrails exist to
prevent. If AXS ever publishes an API, write a proper adapter against it.

The rest of the system is designed so AXS is pure optional upside: Ticketmaster
covers the large majority of North American concert inventory, and every scoring
factor works on Ticketmaster data alone.

## Sources considered and rejected

**SeatGeek** — had a genuinely nice public API with a demand `score` field, but
closed new public API registrations. Partner access only now.

**StubHub** — has an API, partner-only. Worth applying for: it's the single
biggest upgrade available to this project, because it turns every portfolio
estimate into a real resale comp.

**Vivid Seats, Gametime, TickPick** — no public APIs.

**Bandsintown / Songkick** — useful for tour-date discovery and would improve
the `scarcity` factor's tour-wide date counts, but neither adds pricing.
Songkick's API is partner-gated; Bandsintown's requires an app id and is
oriented around artist pages rather than market sweeps. A reasonable future
addition, low priority.

**Google Trends (`pytrends`)** — tempting as a demand signal, but it's an
unofficial scraper of an undocumented endpoint, rate-limits aggressively, and
returns relative rather than absolute numbers that are hard to compare across
artists. Spotify popularity is more stable and officially sanctioned.

## The legal line, concretely

The distinction that matters is **collecting public information** versus
**circumventing a control**.

Reading published prices through a documented API is the former. Solving a
CAPTCHA, bypassing a queue, defeating a rate limit or bot check, using multiple
accounts to exceed a posted ticket limit, or automating checkout is the latter —
and the latter is what the BOTS Act of 2016 (15 U.S.C. § 45c) prohibits, with
FTC enforcement and penalties per violation that have run into the millions. It
also bans knowingly reselling tickets acquired that way.

This project stays on the collection side of that line, and the guardrails above
exist to keep it there even when someone is moving fast. If you add a source, the
test to apply is: *would the operator's terms permit this, and am I accessing
anything a normal visitor couldn't?* If either answer is no, don't.

Separately, resale itself is regulated at the state level. Some states cap
markups or require a reseller license above a volume threshold; many venues void
tickets resold outside their official channel. Check your own jurisdiction — none
of this is legal advice.
