# Propagation along disclosed links

Status: implemented 2026-08-17. `backend/thesis/propagation.py`,
`backend/extensions/thesis_propagation.py`, registered as the `link_propagation`
idea source in `backend/thesis/sources.py`, tested in
`tests/test_thesis_propagation.py`.

Builds directly on [supply-chain-relationships.md](supply-chain-relationships.md)
(the edges) and [revenue-segments.md](revenue-segments.md) (one of the three ways
a hub is read).

---

Every other funnel on the thesis menu selects on a property of one company: what
it is worth, what it earns, how far its price has moved. This one selects on a
**relationship between two companies** — and the relationship was not fitted to
returns. One of the two wrote it down:

> "During fiscal year 2024, NVIDIA Corporation and Cisco Systems, Inc.
> contributed 35.1% and 13.4%, respectively, of our revenues."

That sentence is a mechanism with a number on it, filed under an accounting rule.
So it can be walked: when something material moves at NVIDIA, everyone who wrote
a sentence like that becomes a candidate, and the percentage is how much of them
it moves.

A candidate reads:

```
  link: 35.1% of revenue comes from NVDA — demand exposure · 10-K 2025-08-19
  hub NVDA: moved up via consensus, segment · next-FY consensus +12.0% over 90d · price 3m -0.0%
  hub segment: Compute & Networking +88.3% y/y after +71.1% (91.3% of hub revenue, accelerating)
  here: consensus +5.0% over 90d · net 2 revisions across 4 analysts · price 3m -17.1% · cap $21.4B
  disclosed: "During fiscal year 2024, NVIDIA Corporation and Cisco Systems, Inc.
              contributed 35.1% and 13.4%, respectively, of our revenues."
```

Every clause is checkable and the third line is the falsifier: *this company's
estimates have not moved* is a claim that dies the day they do.

## The scan

**1. Find a hub that was hit.** Either the caller names one (`hubs=NVDA,AAPL`),
or the funnel reads the next-fiscal-year EPS consensus across the largest US
listings and keeps the ones whose number has moved by `min_hub_drift_pct` over
ninety days. That is the cheap market-wide detector and the timely one — a
guidance change lands in the consensus within days — and it is also the weakest
evidence in the system, because it measures the sell side rather than the
business.

**2. Confirm it in the accounts.** For the handful of hubs selected, read the
filer's own quarterly segment revenue and look for a segment worth at least 10%
of revenue whose year-over-year growth has moved one way for two quarters
running:

| trend | test |
| --- | --- |
| `contracting` | shrank year-over-year twice in a row |
| `decelerating` | growth fell by ≥5 percentage points |
| `accelerating` | growth rose by ≥5 percentage points, and is positive |

One axis is used, not three: reportable segments if the filer tags them, else
product lines, else geography. Mixing them would describe the same revenue twice.
The largest qualifying segment wins, with contraction outranking deceleration at
any size. This is the channel worth having — it is demand, disaggregated by the
company, with no forecast in it — and it is the slow one, which is why it runs on
a shortlist rather than a universe. `read_segments=false` switches it off.

**3. Walk the edges.** Ask the supply-chain miner for every filer whose own
annual report puts a percentage on its dependence on that hub, keep the ones
above `min_exposure_pct`, and read each survivor's own consensus.

Hubs are walked sequentially. Each walk already fans out inside the miner and the
segment reader, both against EDGAR, which is rate-limited and asks to be treated
politely.

## Which way the money flows

`relationship` comes off the miner written from the hub's point of view, and the
funnel restates it as the mechanism it implies:

| miner | `link` | what a hub shock does to the counterparty |
| --- | --- | --- |
| `supplier` (sells **to** the hub) | `demand` | its revenue is what the hub's demand moves |
| `customer` (buys **from** the hub) | `supply` | its costs and its supply are what move |

The basis the percentage was disclosed on decides how much of a demand channel it
is at all: revenue and net sales count in full, purchases at 0.7, and accounts
receivable at 0.5 — a receivables concentration is credit exposure to the same
name, and only becomes a thesis if the hub is in trouble as a payer rather than
as a buyer.

## The three families

Everything hangs on whether anyone has already done the join, so that is what the
row is filed under — and the log splits base rates on it:

| family | meaning |
| --- | --- |
| `unreflected_exposure` | covered, and the consensus has not moved (<2% over 90 days, fewer than 3 net revisions) |
| `reflected_exposure` | covered, and it has |
| `uncovered_exposure` | nobody covers it, so there is no estimate that failed to move |

`uncovered` is not a stronger `unreflected`, and collapsing the two would be the
most misleading thing this could do: an uncovered row is an exposure with **no
falsifier attached**, which is a different object from one whose falsifier is
live and currently passing.

`reflected` rows are emitted rather than filtered out. A scanner that shows only
the unpriced half of its own output can never be measured against the other half,
and the graded log is the whole point.

## The score

```
exposure  = min(disclosed_pct / 50%, 1) × basis weight
shock     = the strongest hub channel, 0-1
latency   = 1.0 unreflected · 0.8 uncovered · 0.3 reflected
score     = exposure × (0.25 + 0.75 × shock) × latency
```

The shock term never falls to zero, so opening the gate all the way
(`min_hub_drift_pct=0`, which walks any named hub) still orders rows by exposure
instead of collapsing them into a tie. That configuration is an exposure map, and
the score should say it is a weak one rather than say nothing.

## What it cannot see

Five limits, all structural, all restated in the source's `artifact_rule` so the
triage model has to argue past them.

1. **The edge is exactly as old as its filing.** A concentration disclosed a year
   ago may already have ended — which would be the actual news, and is invisible
   from here. `filing_date` is on every row.
2. **The percentage is of a whole company; the shock is of one segment.** Nothing
   in either filing says the counterparty serves the segment that moved. That
   join is an inference and it belongs to the reader.
3. **Magnitude only travels one way.** A hub naming a counterparty says what the
   counterparty is worth to the *hub* — "12% of our purchases" tells you nothing
   about how much of the supplier's revenue the hub is. So only
   counterparty-disclosed edges are walked, and the hub's own naming is ignored
   even though the miner returns it.
4. **"Estimates have not moved" has three causes.** Nobody is looking, everybody
   looked and judged it immaterial, or the counterparty has already re-sourced
   the revenue. Only the first is an opportunity, and the scanner cannot tell
   them apart.
5. **Coverage stops where SEC filing does.** A private contract manufacturer, or
   a supplier that never crossed its disclosure threshold, has no edge here at
   all. The absence of a link is never evidence of independence — and the largest
   exposed company is frequently the one that files nowhere.

## Future fixes, with judgment

Ranked by what they are worth, not by what they cost. Two of them are declines,
which is the useful half of a list like this.

### 1. Read the concentration *series* out of the sentence already mined — **build**

The cleanest signal in this corpus is not a shock at the hub. It is a
counterparty's own disclosed percentage falling between filings: 27% one year,
12% the next, is a customer loss stated by the filer, in its own words, with no
inference across the link at all. Nothing else here reads a change in the
**relationship** rather than a change at one end of it.

The expensive way to get it is to parse the previous filing too — the miner keeps
only the newest per company. The cheap way is that the filers usually put the
history in the sentence that was already mined. Across a nine-edge sample of AAPL
and NVDA:

| filer | mined sentence | is it a series? |
| --- | --- | --- |
| CRUS | "approximately 91 percent, 89 percent, and 87 percent" | yes — three years |
| SWKS | "67%, 69% and 66%" | yes |
| SITM | "approximately 17%, 22%, and 21% … for the years ended December 31, 2025, 2024, and 2023" | yes — and falling hard |
| QRVO | "50% and 47% of total revenue in fiscal years 2026 and 2025" | yes |
| CGTL | "90.4% and 98.9% of our revenues" | yes |
| SNX | "12%, 12%, and 11% … and sales from HP, Inc. comprised approximately 10%" | yes, **plus a second customer in the same sentence** |
| FN | "NVIDIA Corporation and Cisco Systems, Inc. contributed 35.1% and 13.4%, respectively" | **no — two companies, one year** |

Six of nine carry the history for free. The seventh is the reason to be careful:
read FN's sentence as a series and you have invented a customer *win* from 13.4%
to 35.1% that never happened, silently, on a card that looks exactly like the
real ones. "Respectively" does not discriminate — FN uses it too.

What does: a list of **periods** in the sentence whose cardinality matches the
list of percentages, and exactly one registrant name in it. That is a rule of the
same kind as the ones already in `disclosures_in`, it fails closed, and SITM
above (22% → 17% while Apple was fine) is the sort of row it would surface —
which no shock-driven walk can reach, because the shock is at the counterparty.

### 2. Anchor the event on when the hub moved, not when the scan ran — **fix, before the log fills**

Every row is recorded with `known_on` = today. That is right for *detectability*
and wrong for *measurement*: a consensus that moved six weeks ago is graded as
though it were fresh, so the forward returns in the signal log start partway
through the move they are supposed to measure, and this family's base rate ends
up depending on how promptly somebody ran the scanner.

The honest anchor is the earliest date the drift was visible, which is not
recoverable from a 90-day trailing consensus. The workable one is the hub's most
recent earnings date when it falls inside the drift window, and the scan date
otherwise, with the row saying which it used. Worth doing before this family
accumulates graded events, because re-anchoring afterwards invalidates the
record it is meant to improve.

### 3. Let a shrinking segment *select* a hub, not merely confirm one — **build, but on the scheduler**

The segment channel is the only one reading demand rather than expectations, and
it currently cannot select anything: hubs are chosen on consensus drift, so a hub
whose segment is contracting while its consensus holds is unreachable unless the
price channel happens to fire — and that configuration, where the accounts and
the estimates disagree, is the single most interesting one this scanner could
find.

Inline it is impossible: megabytes of XBRL per filer against a universe. On
`backend/thesis/scheduler.py`, which already runs the grading sweep, it is a
nightly precompute of segment trends for the top ~200 names into the same cache
the reader uses, after which selection is free. Piggyback rather than start a
second process.

### 4. Automatically map the hub's segment to the counterparty's products — **decline**

This is the biggest inferential gap in the funnel (limit 2 above), which makes it
the most tempting thing to automate: correlate the hub's segment series against
the counterparty's own product lines and promote the best match as the channel.
It should not be built. Both series are short, quarterly and noisy, several
candidate pairs will fit by chance, and the output would be a fitted correlation
wearing the clothes of a disclosed mechanism — the exact thing this funnel exists
to avoid. Put both segment tables on the screen side by side and let the reader
make the join, which is cheap, honest, and already most of the value.

### 5. Walk two hops — **decline**

C depends on B depends on A is arithmetically available and analytically empty.
The magnitudes multiply: a 27% link onto a 30% link is 8% of C, below the noise
floor of everything else on its card, while the staleness doubles — two filings,
each up to a year old — and the segment-to-product assumption is made twice.
Defensible only where the first hop is above 50%, which is rare enough to handle
by reading rather than by scanning.

### 6. `uncovered_exposure` may never earn a base rate — **accept, and say so**

Base rates are withheld below ten graded events (`memory.MIN_GRADED`), and
uncovered counterparties are the scarcest of the three families. That family may
sit permanently without a measured prior. That is the correct behaviour — a rate
computed from four events is noise dressed as evidence — but it means the family
whose rows have **no falsifier attached** is also the one the log will be
quietest about, and a reader should know that those two facts are not
independent.

## Parameters

| name | default | what it does |
| --- | --- | --- |
| `hubs` | — | comma-separated symbols to walk; empty means discover |
| `hub_universe` | 60 | largest US listings read when discovering (2 estimate requests each) |
| `max_hubs` | 3 | how many hubs are actually walked; each is a full-text search plus filings |
| `min_hub_drift_pct` | 3.0 | consensus move required of a hub over 90 days; 0 walks any named hub |
| `min_exposure_pct` | 10.0 | disclosed dependence required of a counterparty |
| `min_market_cap_bn` | 0.3 | counterparty size floor; an unreadable profile keeps its slot |
| `read_segments` | true | read each hub's quarterly segment revenue |
| `years` | 4 | how far back to look for a filing disclosing the link |
| `limit` | 20 | candidate cards sent to triage |

Named hubs beyond `max_hubs` are reported in `warnings` rather than dropped
silently, and `extra.hubs_skipped` says why each unwalked hub was unwalked —
nothing moved, nobody discloses it, or the miner failed.

## Cost

The first walk of a hub is the expensive one: ~19 EDGAR full-text searches, then
the filings that answered, then a few megabytes of XBRL per hub for the segment
read. Everything is cached hard afterwards — a filing never changes — so the
second run is fast. `max_hubs` is the knob that decides how much of it happens.
