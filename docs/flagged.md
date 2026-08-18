# Flagged — change detection

Status: implemented 2026-08-17. `backend/flagged/` (catalogue, fact plumbing,
document readers, detectors, market screens), `backend/extensions/flagged.py`
(the `/flagged/*` commands, the signal-log recorder, and the `flagged_market`
idea source), a **Flagged** view in the web UI, tested in
`tests/test_flagged.py`.

Every screen elsewhere here measures a *level*: a P/E, a margin, a short
interest, a twelve-month return. Levels are the commodity part of market data —
the number is on the tape, every vendor sells it, and by the time a screen can
rank on it the ranking is common knowledge. What is not commodity is the
*delta* between two filings by the same filer, because computing one means
holding both documents open and knowing which parts of them are comparable.

That is what this section does. Twelve flag types, each a diff — ten of a
company's newest filing against its own previous one, and two that read across
filers:

| Flag | What moved | Read from |
|---|---|---|
| `risk_factor_added` / `risk_factor_removed` | Item 1A paragraphs with no counterpart in the other year's report | two 10-K / 20-F documents |
| `concentration_appeared` / `concentration_vanished` | a customer, supplier or receivable concentration stated in one annual report and not the other | two annual reports |
| `auditor_change` | a change of certifying accountant — an 8-K Item 4.01, or the PCAOB firm id on the cover page changing | filing index + inline XBRL |
| `buyback_share_gap` | cash out for repurchases while the diluted share count still rose | XBRL facts |
| `deferred_revenue_divergence` | deferred revenue and recognised revenue growing apart | XBRL facts |
| `receivables_outrunning_sales` | receivables growing faster than the sales behind them — DSO rising | XBRL facts |
| `new_accounting_concept` | a concept the filer had never tagged before | XBRL facts |
| `rating_shift` | a one-sided cluster of sell-side rating actions, or the consensus mix drifting | vendor (Yahoo) |
| `institutional_flow` | a quarter-over-quarter change in reported 13F holdings at a small cap large enough, in days of the name's own volume, to be a liquidity event | SEC 13F data sets + the tape |
| `read_through` | several members of a disclosed-end-market cluster reporting the same inflection while one exposed member's consensus has not moved | peers' filing XBRL + consensus |

Two properties make the set worth having together.

**Every flag is dated.** A filing has a filing date, and that is the first day
anyone outside the company could have known. So a flag drops straight into the
graded signal log (`backend/thesis/memory.py`) with an honest `known_on`, is
measured against its benchmark once each horizon elapses, and earns or fails to
earn a base rate exactly like every other idea source. Nothing here asserts that
a flag predicts anything; the log is what will eventually say. Anchoring on the
period end instead — a date months before the filing existed — would credit the
flag with a move nobody could have traded, and the detectors refuse to do it.

**The numeric half is computable for the whole market without a vendor.** SEC's
XBRL frames endpoint answers "every filer's value for this concept in this
period" in one request, so receivables-against-sales for a couple of thousand
filers is four to six requests rather than a couple of thousand
(`/flagged/market`). Each row carries its percentile in the distribution the
screen just computed — a rank no single-company read can know.

## Five commands

```
GET /api/v1/flagged/scan?symbol=NVDA,CRM&kinds=all&period=annual
GET /api/v1/flagged/market?screen=receivables&year=2025&limit=50
GET /api/v1/flagged/flows?direction=distribution&max_market_cap_bn=2
GET /api/v1/flagged/read_through?symbol=AMAT
GET /api/v1/flagged/catalogue?read_from=document
```

`scan` runs every flag type for one or more symbols. `kinds` narrows to a
comma-separated subset; the document flags cost two filing downloads per symbol
on a cold cache, everything else is one cached SEC object and one vendor call.
An empty result is the ordinary answer for a company where nothing moved, and
`extra.skipped` says which readers could not run — so no flags never silently
means no problems.

`market` computes one accrual flag across every SEC filer. `screen` is
`receivables`, `deferred_revenue` or `buybacks`; `year` defaults to the newest
calendar year whose annual frames are complete, which lags the calendar by a
quarter or so.

`flows` and `read_through` are the two cross-filer flags and have their own
sections below.

`catalogue` is the flag types themselves — what each compares, where it is read
from, and **how it characteristically produces a false positive**. That last
column is the point of the catalogue: every flag here has a routine explanation,
and a reader should start from the objection rather than the headline. The
Flagged view prints it under every row.

The market screen is also registered as an idea source (`flagged_market`), so
`POST /api/theses/triage?source=flagged_market&screen=buybacks` builds anomaly
cards from it and sends them through the same one-call model triage as every
other funnel — with the catalogue's artifact note as the rule the model has to
argue past.

## What the diff has to get right

Most of the code is not the diffs; it is the things that make a diff honest.

**Which two filings.** The two most recent annual reports, amendments
excluded — a 10-K/A restates a filing rather than succeeding it, and diffing
one against its own original reports the amendment, which is a different
question. Foreign private issuers (20-F, 40-F) are read with their own section
markers.

**Finding Item 1A.** "Item 1A. Risk Factors" appears at least twice in every
10-K — once in the table of contents — and "see Item 1A" appears dozens of times
in the MD&A. Openers and closers must be *heading-shaped* (short lines), an
opener with no closer after it is the last cross-reference in the document
rather than a section start, and the longest span between a heading pair wins.
The first cut of this let a late cross-reference claim everything to the end of
the filing and reported Salesforce's cash-flow discussion as new risk factors.

**Deciding two paragraphs are the same paragraph.** Filers edit a risk factor a
word at a time ("hosting facilities" becomes "providers", "find" becomes
"identify"). Five-word shingles punish that so hard a fifteen-word bullet with
two edits reads as new. Matching is on **bigram Jaccard ≥ 0.30 or content-word
Jaccard ≥ 0.40** — either is sufficient — with both thresholds set against a
filer that rewrote its whole Item 1A (Salesforce FY2026 vs FY2025): unrelated
boilerplate tops out near 0.25 on bigrams, every pair a reader would call an
edit sits above 0.30, and content words catch the bullet reworded end to end
that still names the same things. Every reported paragraph carries
`best_match`, the score against its nearest counterpart, so the reader can
judge the threshold. When additions and removals arrive in similar numbers the
row says `rewrite_suspected` and scores itself down: two risk factors merged
read as two removals and one addition.

**Concentration is a sentence, not a tag.** "One customer accounted for 21% of
net sales" — the percentage in prose and the counterparty usually unnamed. Each
statement is reduced to a key of *role | basis | counterparty-or-quantifier* so
a filer rewording the sentence is not a disclosure vanishing and appearing at
once. Four things this parser had to learn from real filings: the threshold
("10% or more") is nearly always the first percentage in the sentence and is not
the exposure; the current year is stated before the comparative ("34% and 38%
as of 2025 and 2024"), so the first remaining percentage wins; a sentence that
names only earlier years *is* the comparative and belongs to last year's
filing; and "cellular network carriers", "retailers", "payors" are customers
described by their trade. A "no customer exceeded 10%" statement is kept as a
negated key — its disappearance is the most useful thing in the diff.

**The auditor.** `dei:AuditorName` and `dei:AuditorFirmId` are inline-XBRL facts
in the document since fiscal 2021 and are *not* echoed by the companyfacts API,
so they are read straight out of the HTML tags. The PCAOB id is the key: a firm
that renames itself keeps its number, and comparing names turns every restyle
into an apparent auditor change. Before 2021 the only evidence is the signature
under the audit report, read last and marked as the weaker source it is. An 8-K
Item 4.01 on the filing index is the stronger trace and the earlier date, and
wins the anchor when it exists.

**Same-period, same-form, same-tag.** A receivable and a revenue are only
comparable when their periods end together; a June year-end's December balance
against its June revenue reads as DSO tripling when, like for like, it fell —
which is exactly what SEC's calendar frames do to off-calendar filers, so the
market join insists on aligned period ends and reports how many filers it
dropped rather than mis-compared. Deferred revenue must come from the *same
concept* both years, or a filer mid-migration from `DeferredRevenueCurrent` to
`ContractWithCustomerLiabilityCurrent` shows a collapse that never happened.
"Silenced" concepts are measured against the previous filing *of the same
form*, because a 10-Q tags a fraction of what a 10-K does and every quarter
would otherwise read as a tag migration.

**Ranking on the capped measure.** Ranked raw, every market list is led by
whatever filer has the most pathological denominator — a crypto treasury that
issued a hundred times its float, a shell whose revenue went from nothing to
something. Past the cap the screen cannot tell two filers apart on the measure,
so it stops pretending to and orders them by how much business is behind the
number. The raw figure still reaches the row in full.

## What the tops of the lists actually are

Running the market screens on CY2025 vs CY2024 puts recognisable names at the
top and the catalogue's own artifact notes explain most of them:

- *Receivables outrunning sales* — Arrow, Omnicom, Bunge, Insight, Marvell,
  CoreWeave. Omnicom absorbed IPG and Bunge absorbed Viterra: "an acquisition
  consolidated mid-year" is the first routine cause listed for this flag.
- *Buybacks against a rising count* — ConocoPhillips, Capital One, TKO,
  Coinbase. Marathon Oil and Discover were paid for in stock: "an acquisition
  paid in stock swamps a year of buybacks by design."
- *Deferred revenue diverging* — Arista, Synopsys, EPAM, Rivian, Arm. Arista's
  deferred balance running far ahead of revenue is bookings ahead of
  recognition, the bullish reading; Synopsys is Ansys purchase accounting.

That is the intended shape. The screen surfaces the change, the artifact note
supplies the objection, and the reader — or the triage model, which receives
the same note as a rule — decides whether this filer is the exception.

## What it cannot see

- Risk-factor and concentration flags exist only for filers with two annual
  reports on EDGAR; a company's first 10-K has nothing to diff against.
- The concept diff is only as good as the taxonomy: most first appearances are
  the FASB moving rather than the company, which is why a curated `watched` set
  is named individually and the rest is counted.
- The market screens see the calendar frames SEC publishes; an off-calendar
  filer whose balance and flow frames come from different fiscal periods is
  dropped, and the per-symbol scan reads it instead.
- The rating flag is the one row in this section with no filing behind it.
  Yahoo's action feed is mostly "maintains", so the consensus-mix drift is read
  alongside it; a mix-only flag anchors to the first of the month, so a daily
  scan does not file the same drift under thirty dates. It carries no
  conventional reading on purpose — a wave of downgrades is a short setup and a
  capitulation bottom in equal measure.

## Cost and caching

A fact table (`flagged.facts.fact_table`) is one cached SEC object per filer,
flattened once with `filed`, `accn` and `form` kept — the three things the
statement builders discard and the one thing that makes a change dated. A
document read (`flagged.documents.read`) is one download and three extractions
— risk factors, concentration statements, the auditor — cached as a few
kilobytes of parsed result keyed by URL and `PARSER_VERSION`, never as the
multi-megabyte filing; bumping the version invalidates every stored parse when
the rules move. The market screens are four to six frame requests plus one
cheap submissions-index lookup per *hit* to recover the true filing date. Two
flags of the same type on the same filing date are merged before they reach the
log: one filing is one event, however many rows it produced.

## Institutional-flow inflections at small caps

Status: implemented 2026-08-18. `backend/providers/thirteenf.py` (SEC Form 13F
data sets, the CUSIP→ticker map, the per-CUSIP flow table),
`backend/flagged/flows.py` (the gate and the screen), `/flagged/flows`, the
**Institutional flow** tab, and the `institutional_flows` idea source. Tested
in `tests/test_flows.py`.

A quarter-over-quarter change in reported 13F holdings is a *sentiment
reading* at a large cap — a few million shares of a name that trades fifty
million a day. At a small cap it is *the tape*: a fund that cut from twelve
million shares to a quarter-million in a name trading three hundred thousand a
day spent the quarter being most of the volume, and if it still holds three
million more it will spend part of the next one that way too. The number that
separates the two cases is the change divided by average daily volume, and
that is what `/flagged/flows` computes for every US-listed small cap.

**Source.** SEC publishes every 13F information table filed in a three-month
window as one structured data set (~100 MB, ~3.8M rows). Reading it with the
right dtypes takes seconds; the archive is fetched uncached and only a
four-column per-(filer, CUSIP) position table is kept per period. Positions
are built *per report period* (a window is mostly one quarter plus a tail of
late filings): a filer's newest original or `RESTATEMENT` amendment wins and
`NEW HOLDINGS` amendments add; principal-amount rows, puts, calls, notes,
warrants, preferreds and 13F-NT notices are dropped. Tickers come from SEC's
fails-to-deliver files — the one free source that lists CUSIP and symbol side
by side across the market, and it covers thinly traded names best, which is
exactly where this screen looks.

**The change is not the flow.** `net_change` is computed only across filers
present in *both* periods; a manager crossing the reporting threshold, or
filing late, is a change of paperwork and its shares are reported separately
(`entering_filer_shares` / `departing_filer_shares`). Where nearly every holder
of a CUSIP left and nearly nothing remains, the likeliest cause is a change of
identity — a merger exchange, a redomicile, a reverse split issuing a new
CUSIP (Amcor's old Jersey CUSIP showed 605 "exits" the quarter its new one
appeared) — and such rows are labelled and set aside rather than reported as
selling.

**The gate.** Days of volume = |net change| / the quarter's average daily
volume (Yahoo, one batched download for the ~400 candidates that clear a cheap
pre-gate on change-as-share-of-outstanding). Small cap = closing price ×
cover-page shares outstanding (the `dei` frame, market-wide in one request)
between $50M and $2B by default; at least five days of volume; at least $250k
a day of dollar volume; at least fifty sessions traded (a February listing has
no average to divide by). Then the labels the first live runs demanded, each
scored down rather than dropped and excluded from the screen by default:

- `spac` — hedge funds accumulate blank-check shares for the trust yield in
  names that trade nothing; every one is "sixty days of volume".
- `issuance_suspected` — shares outstanding grew by at least half the net
  accumulation *and* by 2% of the company: the buyers bought from the company
  (a PIPE, a registered direct), and the shares never crossed the tape.
- `single_filer_suspect` — one filer is most of the flow and claims over a
  quarter of the company: one adviser's table scaled by a thousand put it at
  58% of a bank it had never heard of. Anyone really there files a 13D.
- `denominator_suspect` — reported institutional shares exceed 110% of shares
  outstanding: the cover count is stale or a shared position is double-counted.
- `foreign_domicile` — the `dei` frame's `loc` says CA/IL/GB; the US line may
  be a fraction of the volume, so days of volume is overstated. Score halved.
- `passive_share` — the share of gross flow from index managers (Vanguard,
  BlackRock, State Street, Geode…), because the June Russell reconstitution is
  exactly this screen's shape and none of it is a decision. Score damped
  proportionally.

**What is on the row.** `top_buyers` / `top_sellers` with what each still
holds; `overhang_days` — the net sellers' remaining position in days of the
same tape, the forecastable part; `institutional_pct`; the quarter and the
`known_on`, which is the **45-day filing deadline** — the day the aggregate
became knowable from EDGAR — never the period end and never the data-set
publication date. Ranking is by the damped score, then the overhang, then raw
days: an exit two-thirds done at forty days beats one finished at sixty.

**What it found** (2026-03-31 vs 2025-12-31, distribution): Cardlytics (BofA
and Jane Street out, 44 days), Flexsteel, EVI Industries (29 days sold, 30
days still held by the sellers), PSQ Holdings (Alyeska, 14 days of overhang),
Docebo (Warburg Pincus, labelled CA-ON), Fidelis Insurance (CVC, 17 days of
overhang). Accumulation rows are more often issuance and are labelled so.

**Lag.** A 13F is due 45 days after quarter end and SEC publishes the data set
about a fortnight after the window closes, so the freshest flow row is always
for the quarter before last. That is the disclosure regime, not the code, and
`extra.period_end` says which quarter it is.

## Shared-end-market read-through

Status: implemented 2026-08-18. `backend/flagged/readthrough.py`,
`/flagged/read_through`, the **Read-through** tab, the `read_through` idea
source. Tested in `tests/test_readthrough.py`.

A company that sells into China, or into data centres, shares that end market
with every other company that discloses the same line — and US filers disclose
them, by geography and by product, in the revenue note of every 10-Q, which
`backend/providers/segments.py` already reads out of the XBRL instance. When
several members of such a cluster report the same inflection in the shared
line in the same fiscal quarter, the read-through to a member with the same
exposure is not a guess. It is their disclosure, and the only question is
whether the fourth's consensus has moved yet.

1. **Cluster.** The hub's peer group (`/equity/compare/peers`, plus anything
   passed in `peers`), kept to members that disaggregate revenue. Lines are
   normalised so "Greater China", "China" and "PRC" are one key, "United
   States", "North America" and "Americas" another, "Data Center" and
   "Datacenter" another; residuals ("Other", "Rest of World") are not lines.
2. **Inflection.** For each member's shared line, year-over-year growth in the
   most recent quarter minus the same in the quarter before — matched on
   dates, not positions, so a fiscal wobble or a hole in the series does not
   pair the wrong quarters. Ten growth-rate points is the bar.
3. **Cohort and confirmers.** Members whose latest quarter ended within 75
   days of the newest in the cluster are the same fiscal cohort (year-ends
   differ by up to two months across a peer group). At least three of them,
   and at least half of those that reported, inflecting the same way is a
   common inflection.
4. **Laggard.** A member with at least 10% of revenue on the line whose
   next-year EPS consensus drifted less than 2% over ninety days, or the wrong
   way, or less than a third of the confirmers' median drift in the
   inflection's direction. If the laggard has itself reported and its own line
   agrees, its disclosure joins the evidence; if it has not reported, the
   peers' are the evidence and its print is the catalyst; if it reported and
   diverged, it is an exception and not a row.

The row anchors on the filing date of the last confirmer needed to make the
pattern — the first day the read-through could have been made from public
documents — and carries every confirmer's growth, inflection, quarter, filing
date and consensus drift, plus the whole cluster, so the chain of evidence can
be checked link by link. `extra.lines` lists every shared line the cluster has,
fired or not, so a line that did not produce a row can still be read.

On the semicap cluster around Applied Materials (2026-08-18): North America
accelerating at six of ten reporters (KLA +93pp, AMAT +45pp, AXTI +53pp) and
Benchmark Electronics — 45% of revenue there, own line agrees, consensus +7%
against the confirmers' median +22% — the laggard. China accelerating at four
of seven with Lam the dissenter; no exposed member unmoved.

`read_through` is not part of `kinds=all` in `/flagged/scan` — it reads a
whole cluster — and has to be asked for by name.
