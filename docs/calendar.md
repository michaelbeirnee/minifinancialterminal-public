# The calendar and its event types

Status: implemented 2026-08-16. `backend/extensions/calendar.py`,
`CalendarEvent` in `backend/models.py`, the notes CRUD in
`backend/routers/user.py`, the Calendar view in `frontend/`, tested in
`tests/test_calendar.py`.

Earnings, dividends, splits, IPOs, macro releases and the Fed's own calendar
arrive in seven shapes from three sources. Read separately they are seven tables
that happen to have dates in them. `/calendar/events` normalises all of them
onto one row so a single view can ask *what lands between these two dates, of
these types, for these symbols* and get one sorted answer.

```
date  time  type  type_label  symbol  name  title  detail  importance  source
```

Every feed's extra columns survive inside `detail` as text rather than widening
the schema for one source's benefit. A row is a thing that happens on a day.

## What the event-type list can and cannot carry

The filter rail is the honest part of this feature. A terminal's event-type list
usually runs to a dozen entries, and this platform — free and public sources
only — can fill nine of them:

| Type | Source | Notes |
|---|---|---|
| Earnings Release | Nasdaq, Yahoo | Session timing and consensus EPS where published |
| Ex-Dividend | Nasdaq | The date that has to be owned through |
| Dividend Payable | Nasdaq | When the cash actually lands |
| Stock Split | Nasdaq, Yahoo | On the execution date |
| IPO | Nasdaq, Yahoo | Priced, expected, filed and withdrawn |
| Economic Release | Yahoo, FRED | Consensus and prior where published |
| FOMC Decision | Federal Reserve | The committee's own schedule; what a past meeting did to the target range, and whether an upcoming one carries the dot plot. The minutes release rides on the same type |
| Fed Speeches | Federal Reserve | Speeches, congressional testimony and Board press releases, from the Fed's own feeds — published records rather than a schedule |
| Custom / Notes | You | Stored per account, never sent anywhere |

The other seven — earnings calls, sales results, conference appearances,
shareholder meetings, corporate access, analyst marketing, deal roadshows — come
from investor-relations feeds and broker calendars with no free public
equivalent. `/calendar/event_types` returns them anyway, with `available: false`
and a `why`, and the UI renders them greyed out.

That is deliberate. A filter that silently omits corporate access looks like a
calendar with nothing scheduled; one that lists it disabled tells you where the
gap is. The same reasoning runs through the rest of this feature: the failure
mode a calendar has to avoid is *looking empty when it is actually incomplete*.

**Earnings calls** deserve their own note, because the omission looks like an
oversight. No free feed publishes call times as data. What the public sources do
give is the session the release lands in — pre-market or after-hours — and that
rides on the earnings row as `time` rather than being inflated into a separate
event with a made-up clock time.

## The two providers are not interchangeable

`provider=` is a real choice, not a fallback preference:

- **`nasdaq`** serves one **day** per request, with US corporate detail — EPS
  forecasts, dividend rates, split ratios — and is the only source here with a
  dividend calendar at all. A month costs a month of requests.
- **`yahoo`** serves a **date range** in one request, covers the whole world and
  carries the macro calendar, but publishes no dividends.

Neither covers everything, and the provider argument is a preference about *how*
to fetch rather than permission to drop a type that was explicitly asked for. So
each falls through to the other for what it lacks: ask Nasdaq for `economic` and
the macro rows come from Yahoo; ask Yahoo for `dividend_ex` and the dividend
rows come from Nasdaq, with a warning that this part of the window is a day
walk.

`fomc` and `fedspeak` belong to neither and are collected whichever provider is
serving the rest of the window. Yahoo's macro calendar lists the *release of a statistic*;
the meeting that sets the rate every statistic is read against is published by
the committee itself, so that is where it comes from. See
[fed-policy.md](fed-policy.md).

### Why the day walk is slow, and why it is not parallelised

`nasdaq.com` is throttled to one request every 0.4s in `backend/core/http.py`,
deliberately — it is a polite rate limit on a public endpoint that returns no
429. A cold month of Nasdaq events is therefore around three minutes; the same
month warm is about a second, because every day is cached.

Firing the walk through a thread pool would trade that politeness for latency
against a host that never asked to be hammered, so the walk stays sequential and
the cost is disclosed instead: the UI says which sources serve a day at a time
before the wait starts, and the frontend defaults to Yahoo with dividends
unticked so a first paint is quick.

`max_days` caps the walk. When it bites, the number of unread days comes back in
the warnings rather than quietly shortening the window.

## Ranking, and the two ways it could have lied

`importance` is 1-3. For company events it is market cap — 3 above $50bn, 2
above $2bn — and for macro releases it is a headline series *and* a region whose
data moves other markets. The same indicator out of a small economy is a 2 at
most: it is real data, it just is not what a US book reprices on.

Two filtering traps are worth spelling out because both were live bugs before
they were fixed:

**The size floor is applied by Yahoo, not afterwards.** Filtering locally still
pages a month of every micro-cap on earth and then throws it away — and whatever
the row cap cut off is missing from the *end* of the window, so the calendar goes
blank halfway through the month. `market_calendar(..., market_cap=)` pushes the
floor into the query, which is why a month view now spans the month.

**Naming symbols overrides the size floor.** A symbol filter is a more specific
intent than a threshold. Without the override, filtering to a company you
actually hold returns nothing whenever it happens to be smaller than the floor —
the filter silently answering a question nobody asked. The response says so in
its warnings, and the UI disables the size control while symbols are set.

## The row cap

`limit` truncates from the end of the window, which on a grid is
indistinguishable from a quiet fortnight. So `/calendar/events` returns
`extra.truncated` and a warning naming the date it actually stopped at. Anything
that renders this feed should surface that warning; the bundled UI does.

## Custom notes

The one event type that is the user's own writing. `calendar_events` is scoped
to the account, and `event_date` is a `YYYY-MM-DD` **string** rather than a Date
or DateTime column: the feeds normalise to plain dates and the grid keys on
them, so storing a timestamp would invite a timezone shifting a note onto the
wrong day — which for a calendar is the whole ballgame.

Notes are never filtered by the size floor. It is a market-cap threshold and a
note has no market cap.

```
GET    /api/user/calendar?start_date=&end_date=&symbol=
POST   /api/user/calendar          {event_date, title, symbol?, detail?, time?, importance?}
PATCH  /api/user/calendar/{id}
DELETE /api/user/calendar/{id}
```

## Related commands

`/calendar/economic` normalises, ranks and filters macro releases so they can
merge into the unified feed. `/economy/calendar` hands back each provider's
frame unchanged — reach for that one to see raw columns, and this one to put
releases on a calendar beside earnings. The per-type `/equity/calendar/*`
commands remain the way to pull a single feed in its native shape.

## Fixed along the way

- Nasdaq's IPO calendar is served at `ipo/calendar`, not `calendar/ipo` like the
  other three. The symmetric-looking URL had been 404ing.
- `yahoo.market_calendar` never passed a limit through, so it returned 12 rows
  whatever was asked for — which made `limit=200` a no-op on every
  `/equity/calendar/*` command and `/economy/calendar`. It now pages, 100 rows
  at a time, which is Yahoo's per-request cap.
