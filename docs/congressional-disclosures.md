# Congressional trading disclosures

Status: implemented 2026-08-16. `backend/providers/congress.py`,
`backend/extensions/congress.py`, registered as a triage source in
`backend/thesis/sources.py`, tested in `tests/test_congress.py`.

A corporate insider files a Form 4 because they are an officer of the issuer.
A senator files a Periodic Transaction Report because the STOCK Act says a
legislator who trades over $1,000 must say so within 45 days. Both are people
with information trading in public view, and the second set has no free
structured feed that is also a primary source — the vendors who sell one are
reading the same filings.

So this reads the filings. It sits *beside* the Form 4 funnel rather than
inside it: same shape, different population, separately measured.

## What the disclosure actually says

> Transaction Date `07/07/2026` · Owner `Self` · Ticker `AMCR` · Asset
> `Amcor plc` · Type `Purchase` · Amount `$50,001 - $100,000`

Four columns carry the signal and each has a limit worth stating once:

- **Ticker.** Present for listed securities, absent for municipal bonds, real
  estate and funds held by name. An absent ticker is a fact about the asset,
  not a company that can be plotted, so the column stays null rather than
  carrying a placeholder.
- **Amount** is a *bracket*, and the top one ("Over $50,000,000") is
  open-ended. Both ends are reported and neither is the trade. `amount_floor`
  on a cluster row sums the lower bounds and is labelled as a lower bound;
  restating it as "the money involved" would be inventing data.
- **Owner** separates the member's own account and a joint one from a spouse's
  or a dependent child's. The household is covered by the statute; the member
  did not necessarily place the trade. `self_directed` counts the ones they
  were a party to, and `self_directed_only=true` drops the rest before the gate
  sees them.
- **Transaction date** can precede the filing by up to 45 days. Everything the
  platform measures anchors on the **filing** date, because that is the first
  day anyone outside the household could have acted. Anchoring on the trade
  date would score a signal nobody could have traded.

## The gate

One member trading is a fact about that member. `/thesis/congress_clusters`
fires only where several *different* members disclosed the same direction
inside one window, keyed on filing date — the cluster is what the public could
watch forming, and disclosures of trades months apart routinely land the same
week.

`family` splits the signal log four ways: direction (`buy` / `sell`) crossed
with whose account (`self` / `household`). Those are different bets, and a
pooled base rate would hide exactly the distinction the gate went to the
trouble of drawing. `disclosure_lag_days` is the median per-disclosure lag
across the window, which is how you tell people who acted at the same time
from people who merely filed at the same time.

Emissions land in `signal_events` under `congress_cluster:*` like every other
scanner's, so the calibration loop grades this population on the same ruler as
Form 4 clusters and eventually says which is worth more.

## What it cannot see

- **The House.** 435 of 535 members file their PTRs as PDFs at
  `disclosures-clerk.house.gov`. Parsing them means a PDF dependency this
  project does not have, plus OCR for the paper filings. The House index
  (`{year}FD.ZIP`) names who filed and when but never what they bought, which
  cannot support a per-symbol signal. **Coverage is the Senate: 100 of 535.**
  Every result says so, because thin coverage that goes unmentioned reads as
  no coverage.
- **Size.** Brackets only, as above.
- **Intent.** The filing says what and when, never why. A member rebalancing a
  target-date fund and a member acting on a hearing produce the same row.
- **Anything under $1,000**, and anything in a qualified blind trust, which is
  reported without transaction detail.

The honest summary: this is the primary source, complete for the chamber it
covers, one click from the filing on every row, and blind to four fifths of
Congress.

## Cost and caching

A cold sweep is one search request plus one document fetch per filing — about
200 requests for a 120-day window, spaced by the shared throttle, so a few
minutes. Filed reports never change, so documents are cached for a week and
the search index for a day; later sweeps are effectively free.
`_PARSER_VERSION` is baked into the cache keys, so changing any rule above
retires every parse made under the old ones.

The search endpoint requires agreeing to a prohibition notice, which sets a
session cookie; the provider does that once per process and re-does it if the
session expires mid-sweep.
