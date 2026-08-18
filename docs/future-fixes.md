# Future fixes

A running list of things that are known, deliberate or blocked rather than
forgotten — and what would close each one. Everything here was found while
building something else; nothing on this list is breaking today.

Opened 2026-08-17 with what came out of the Fed work
([fed-policy.md](fed-policy.md)).

---

## Blocked by the source

### BLS returns 403 to automated readers

`https://www.bls.gov/schedule/news_release/*.htm` and even the agency's own
`feed/*.rss` refuse this client — with and without a browser User-Agent — so
**CPI and the jobs report have no key-free release schedule**.

*Where it bites:* `/economy/fed/data_reaction` can date FOMC events, Fed
communications and BEA releases (PCE, GDP), and marks every other day `none`.
The two biggest data days of the month are in that gap.

*What closes it:* setting a free `MFT_FRED_API_KEY` — `fred.release_dates()` is
the official US release calendar and the command already folds it in when the
key is present. Failing that, a mirror that republishes the BLS schedule, or the
BLS API's `latest` flag walked backwards, which dates a *print* rather than a
*release*. Not worth scraping through a proxy.

### Press-conference transcripts are PDFs

The chair's press conference is the most-watched Fed communication of the cycle
and it is published as a PDF. This project has no PDF dependency, deliberately.

*Where it bites:* `/economy/fed/statement` applies its language flags and
sentence diff to the statement — the document the committee actually voted on —
and the press conference is only flagged and linked on the meeting row.

*What closes it:* adding a PDF reader. Worth noting that the same dependency
would unlock the **House** STOCK Act filings, which are PDFs too and are the
reason `providers/congress.py` covers 100 of 535 members. One dependency, two
features — which is the only argument for taking it on.

### Yahoo's macro calendar is thinner than it looks

For July 2026 it returned 75 rows for the whole month, 8 of them US, and no CPI.
It is a "top events" feed rather than a release calendar.

*Where it bites:* `/calendar/economic` and `/economy/calendar` on the Yahoo
provider. The FRED provider is the authoritative alternative and needs the free
key.

*What closes it:* nothing on Yahoo's side. The honest fix is to lean on FRED
where a key exists and keep saying so in the warnings, which both commands do.

### The FOMC calendar page carries about six years

`fomccalendars.htm` holds roughly the current year, the five before it and the
one ahead. Older meetings live on per-year archive pages
(`fomchistorical<year>.htm`), one fetch each.

*Where it bites:* `/economy/fed/meetings`, and therefore the meeting-level joins
for statements, minutes and projections. The *decisions* are unaffected —
`rate_changes` runs to 1982 off FRED.

*What closes it:* a loop over the archive pages, cached at
`TTL_REFERENCE`. Cheap to add if anyone wants pre-2021 statement diffs; not
worth the requests until they do.

---

## Things that will rot

### No chair is hardcoded, and that is on purpose

The data currently shows **Warsh** delivering the Semiannual Monetary Policy
Report to Congress, with a Board release naming **Powell** chair pro tempore. Any
copy, doc or filter that names a specific chair goes stale the moment the seat
changes.

`/economy/fed/communications` therefore filters by surname and *reports* the
speakers it saw rather than knowing who is who. If a "chair" concept is ever
wanted (a `role` column, a chair-only filter), it needs a roster with dates —
the Board publishes one, and it would need maintaining.

### The hawkish / dovish phrase lists are a judgment call

`HAWKISH`, `DOVISH` and `GUIDANCE` in `backend/extensions/fed_signals.py` are
explicit lists, and every hit is returned so a reader can disagree with any one
of them. But committee language drifts — "additional firming" was the phrase of
2023 and is not the phrase of today.

*What it needs:* a periodic read of recent statements to see which listed
phrases have stopped appearing and which new terms of art are carrying the
direction. This is curation, not a bug, and the design keeps it honest in the
meantime: the flags are evidence, not a score.

### The Fed's HTML tables are the contract

`parse_calendar` raises a `ProviderError` when the meeting page stops looking
like itself, which is right. The projections parser is softer: if the Fed
restructures Table 1, `_sep_table` returns *fewer rows* rather than failing.

*What closes it:* a shape assertion in `fomc.projections` — a known variable
(the federal funds rate) and a `Longer run` horizon must both be present, or
raise. Worth doing next time that file is open.

### Documented command counts are hand-maintained

They drift silently. As of this commit README and the registry agree (306
commands, economy 47), but **`docs/index.html` still shows `etf` as 11 where the
registry says 16** — pre-existing, from the `basket/*` work.

*What closes it:* a test that reads the counts out of README and
`docs/index.html` and asserts them against `registry.coverage()`. Small test,
ends the class of problem.

---

## Development gotchas

### A cached DataFrame outlives a parser fix

`@cached("fomc.meetings", ttl=TTL_FUNDAMENTAL)` and friends persist pickled
frames in `data_cache/cache.db` for a day to a week. Change a parser and the old
shape keeps being served until the TTL lapses — which during this work produced
a `re.Match` inside a cached frame, a missing `vintage` column, and a
`press_conference` flag that was right in the code and wrong in the answer.

*What closes it:* a version component in the cache key — `cached(prefix,
version=2)`, or hashing the function's source into the key — so a changed
parser is a cache miss by construction. Until then: delete the offending rows
from `cache_entries` after changing a parser (the key is
`sha256("prefix|func|args|kwargs")[:32]`).

---

## Open calls someone should confirm

- **Calendar defaults.** `fomc` is ticked by default in the UI (eight events a
  year, low noise); `fedspeak` is not (a few a week). Both are one click.
- **Speech importance.** Congressional testimony, Jackson Hole and the policy
  documents rank 3; ordinary speeches rank 2, so they disappear under the
  calendar's default "Major" size filter. That is consistent with how the macro
  type behaves, but it does mean ticking Fed Speeches on a Major view can look
  empty.
- **Balance-sheet regime band.** ±$20bn a month is the neutral zone, chosen so
  that ordinary reserve management is not labelled a policy decision. It is a
  threshold, not a fact.
