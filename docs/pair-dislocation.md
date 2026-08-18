# Pair dislocations along disclosed links

Status: implemented 2026-08-18. `backend/thesis/pairs.py`,
`backend/extensions/thesis_pairs.py`, registered as the `pair_dislocation` idea
source in `backend/thesis/sources.py`, tested in `tests/test_thesis_pairs.py`.

Builds directly on [supply-chain-relationships.md](supply-chain-relationships.md)
(the supplier and customer edges) and the peer group in
`backend/providers/peers.py` (the shared-segment edges), and sits next to
[link-propagation.md](link-propagation.md), the other funnel that selects on an
edge rather than a node.

---

Run an Engle-Granger test across every pair in the S&P 500 and you will find
thousands of "cointegrated" pairs. At a 5% threshold, five in a hundred unrelated
pairs pass by construction; with 125,000 pairs that is more than six thousand
spurious relationships, and every one of them will look like a mean-reversion
trade until it isn't. The statistics are not the problem. The search is.

So this funnel refuses to test a pair unless something in a filing joins the two
companies first. Restricting the search to economically justified pairs is what
keeps this from being data mining: a pair with no mechanism is never tested, so a
pair that passes has, at minimum, a reason to have passed. Three kinds of link
are admitted, all of them read out of the filings rather than fitted to returns:

* **Supplier / customer.** One of the two discloses a quantified concentration in
  the other — "sales to Hub accounted for 27% of our net sales" — read by the
  supply-chain miner from either side's annual report.
* **Shared segment.** The two operate in the same line of business, evidenced
  either by one naming the other as competition in its 10-K, or by two
  independent classifications (the filer's own SIC registration and the market
  industry list) concurring. A comparable a single filing-cabinet label produced
  is not, by default, enough.

Only then is the price relationship measured — and it is measured out of sample.
A candidate reads:

```
  pair: HUB vs SUP · supplier (27.0% of SUP net sales) · 10-K 2026-02-19
  spread: +11.1σ now · +5.1σ mean over 63d · 46 of 63d outside · broken · hedge 0.81 · half-life 4d
  fit: Engle-Granger p 0.000 on history · 0.293 over full window · return corr 0.81 · 750 obs · rich HUB / cheap SUP · SUP moved -35.0% · here -14.7% · cap $900.0B
  linked by: "Sales to Hub accounted for 27% of our net sales."
```

(Synthetic figures from the test fixture.) The last line is the sentence that
admitted the pair to the test at all. Without
it the model is being asked to trust that a cointegration result means
something, which is exactly the claim a restricted search space exists to make
checkable.

## The scan

**1. Choose anchors.** Either the caller names them (`symbols=NVDA,AAPL`), or the
funnel takes the `anchor_universe` largest US listings above
`min_anchor_market_cap_bn` and uses the first `max_anchors`. Big companies are
where the edges are: a concentration disclosure names the counterparty that is a
quarter of somebody's revenue, and that counterparty is rarely small.

**2. Draw the pairs.** For each anchor, three legs are read concurrently and none
is allowed to fail the others: the filings that name the anchor in a
concentration disclosure, the anchor's own annual report, and its peer group.
Every counterparty that clears the gates — `relationships`, `min_exposure_pct`,
`peer_evidence` — becomes one pair `(anchor, counterparty)`. A company reached by
more than one leg keeps its best-evidenced edge: a disclosed percentage beats a
shared segment, and a bigger percentage beats a smaller one. Pairs are never
drawn between two counterparties, because only the anchor's links were read.
`extra.anchors_drawn` and `extra.anchors_skipped` say what each anchor
contributed, and each `dropped` count says which gate removed what — "no pairs"
from a company nobody discloses and "no pairs" from a company whose every link
was below the floor are different answers.

**3. Test the pairs.** Daily closes over `lookback_years` are fetched for the
whole pair universe at once. For each pair, the most recent `recent_days` are
held out. On the *history* alone: `log anchor` is regressed on `log other`
(hedge ratio, intercept), the residual spread's mean and sigma are taken, its
half-life is fitted from an AR(1), and Engle-Granger is run. The recent window
is then projected onto that fitted relationship, so `z_now` — the last day's
spread in historical sigmas — is an out-of-sample reading, not a residual the
fit already minimised. Pairs with fewer than `min_obs` overlapping days are not
tested; pairs whose history never tested as cointegrated (`p_value_history >
max_p_value`) are counted and dropped, because there was no relationship in
prices to break whatever the filing says about the business.

## The two states

A tested pair whose `|z_now| ≥ z_threshold` is flagged in one of two families.
The split is the Engle-Granger p-value read over the *whole* window, recent days
included:

* **`dislocated_pair`** — the whole window still tests as cointegrated at
  `max_p_value`. The relationship looks intact and the spread is at an extreme.
* **`broken_pair`** — the deviation is large enough that the whole window no
  longer tests as cointegrated. Something in the relationship itself may have
  changed, which is both the more interesting and the more dangerous reading: a
  broken pair has no statistical reason to close.

Neither is a trade. A supplier trading three sigmas cheap to the customer that is
27% of its revenue is either a lag or the market having read a filing that says
the concentration ended — and nothing in the spread tells the two apart. The
card carries the sentence that joined the pair, the filing it came from, and the
numbers; the join between them is the reader's.

`include_intact=true` also emits the tested pairs that are within threshold, so
the whole tested set is visible rather than only the tail — the same reason the
propagation funnel emits its already-reflected rows.

## The candidate

A pair is two companies and a triage card is one. The row's `symbol` is the leg
that moved **less** over the recent window: the one that has not repriced
against the other, and so the one where an un-done join might live. `pair_with`
is the other leg, `mover` is which one moved and by how much, and `rich_leg` /
`cheap_leg` say which side of the fitted line each sits on — a positive z means
the anchor is above the line the other predicts for it. This is a convention and
the artifact rule says so: the leg that moved may be the one that is right.

## The score

```
score = z_term × fit × link × reversion × sign
```

* `z_term` — `min(|z_now| / 4, 1)`; four historical sigmas is the top of the scale.
* `fit` — `1 − p_value_history`; a pair that barely cleared the ceiling contributes
  little whatever its z-score, because a z-score against a line that never fit
  is a number and not a reading.
* `link` — `0.5 + 0.5 × strength`, where strength is the disclosed exposure
  term for supplier/customer links (the same one link propagation uses; a
  receivables disclosure is credit exposure and weighs less than a revenue one)
  and, for shared segments, 1.0 when named as competition in a filing, 0.7 when
  two classifications agree, 0.4 for a single classification.
* `reversion` — 1.0 when the fitted half-life is within the recent window, 0.75
  when it is longer (the spread reverts too slowly for the recent window to be a
  dislocation of it), 0.5 when the spread does not revert at all.
* `sign` — 1.0 for a positive hedge ratio, 0.5 for one at or below zero. A pair
  the fit found moving *against* each other may be cointegrated, but it is not
  the "moves with" relationship a supplier, customer or competitor implies.

The score is sign-neutral, and it is the same formula for both families: the
graded signal log is what will eventually say whether broken pairs and
dislocated pairs deserve different treatment, and a scanner should not
pre-empt its own measurement.

## What it cannot see

* **The link is as old as its filing.** The pairs come from annual reports; a
  supplier disclosed at 27% two years ago may have been designed out since,
  which would *explain* a broken spread rather than contradict it. `filing_date`
  is on every row and the filing is linked.
* **Engle-Granger is a weak, asymmetric test.** It is run with the anchor as the
  dependent variable and reported with its p-value. Restricting the search cuts
  the false-positive count; it does not remove it. `extra.pairs_tested` and
  `extra.expected_false_cointegrations` (`pairs_tested × max_p_value`) are
  returned so the reader can see how many chances the search had to fool them.
* **Both states describe the spread, not the business.** A spread that no longer
  cointegrates is what a relationship that has genuinely ended looks like, and
  also what a large lag looks like. Nothing here tells them apart; the filings
  might.
* **The history contains whatever regime it contains.** Three years of daily
  closes fit one hedge ratio; a pair whose relationship changed slope halfway
  through will fit badly and may fail the history test for that reason alone.
* **Coverage stops where SEC filing does.** A private counterparty, or one that
  never crossed a disclosure threshold, has no edge and no pair. The absence of a
  pair is not evidence of independence.

## Future fixes, with judgment

### 1. Johansen instead of Engle-Granger — **decline for now**

Johansen is symmetric and handles more than two series, but every pair here is
two series with a designated anchor, and the asymmetry is reported rather than
hidden. The improvement is real and small; the added dependency surface and the
harder-to-explain statistic are not worth it until the graded log says the
false-positive rate is the binding problem.

### 2. Rolling hedge ratio — **accept the limit**

A Kalman-filtered beta would track a slowly changing relationship. It would also
absorb the very dislocation the funnel is looking for, and it makes "how many
sigmas from history" a moving target. A fixed history fit with the recent window
held out is the honest measurement; regime changes fail the history test, which
is the right outcome.

### 3. Shared segment by segment *label* — **build if the segments reader grows a
cross-filer index**

The XBRL segment reader recovers each filer's reportable segments. Two companies
both reporting a "Data Center" segment is a stronger shared-segment claim than a
shared SIC code, and one nobody had to be named for. It needs a normalised
cross-filer label index that does not exist yet; when it does, it becomes a
fourth evidence label above `agree`.

### 4. Counterparty–counterparty pairs — **decline**

Two suppliers of the same hub are joined by the hub, not by each other; a
disclosed link between them is exactly what this funnel requires and exactly
what that pair lacks. Draw pairs around each supplier as its own anchor instead.

### 5. Event anchoring — **fix, shared with propagation**

Rows are recorded on the scan date, not on the day the spread crossed the
threshold, so the family's base rate depends on how promptly the scanner runs.
`days_outside` and `z_recent_extreme` are on the row so a later grader can
back-date; the fix belongs in the signal log and is the same one link
propagation needs.

## Parameters

| name | default | what it does |
| --- | --- | --- |
| `symbols` | — | comma-separated anchors; empty means the largest US listings |
| `anchor_universe` | 40 | largest US listings considered as anchors when none are named |
| `max_anchors` | 4 | anchors actually drawn around; each is an EDGAR full-text search plus filings |
| `relationships` | `supplier,customer,shared_segment` | which link kinds admit a pair |
| `min_exposure_pct` | 0.0 | concentration a supplier/customer link must carry; 0 admits any quantified link |
| `peer_evidence` | `agree` | `filings`, `agree` or `any` — how a shared segment must be evidenced |
| `peers` | 8 | comparables read per anchor; 0 turns the leg off |
| `min_anchor_market_cap_bn` | 10.0 | size floor for a discovered anchor |
| `lookback_years` | 3 | daily prices fitted and tested on |
| `recent_days` | 63 | trading days held out and read against the fit |
| `min_obs` | 250 | overlapping days a pair needs to be tested at all |
| `z_threshold` | 2.0 | historical sigmas from the fit required to flag |
| `max_p_value` | 0.10 | Engle-Granger ceiling on the history; the same ceiling over the whole window splits dislocated from broken |
| `include_intact` | false | also emit tested pairs within the threshold |
| `years` | 4 | how far back to look for a filing disclosing the link |
| `limit` | 20 | candidate cards sent to triage |

Named anchors beyond `max_anchors` are reported in `warnings` rather than
dropped silently; a leg that failed for an anchor is a warning too, and pairs
that could not be priced are counted in `extra.pairs_untestable`.

## Cost

The first draw around an anchor is the expensive part: ~19 EDGAR full-text
searches for the concentration phrases, the filings that answered, the anchor's
own annual report, and the peer group's three sources. All of it is cached hard
afterwards. Prices are one daily-history request per company in the pair
universe, fetched concurrently and cached for the day; the statistics
themselves are milliseconds.
