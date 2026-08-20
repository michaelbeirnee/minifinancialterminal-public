# Signal research layer

The signal-research layer separates **feature ideas** from **portfolio construction**. A signal has to show independent predictive evidence before it is allowed into a live blend.

The research stack now has two libraries:

1. `backend/backtest/signal_research.py` — cheap, price-only signals that can be rebuilt from a close-price panel.
2. `backend/backtest/multisource_research.py` — point-in-time signals built from volume, filing-lagged fundamentals, dated events, archived estimates/options, and trailing peer relationships.

Both feed the same out-of-sample evaluator. A richer data source does not get easier validation gates.

The evaluator also has a second-stage research-control layer. Once many ideas
are tested, a standalone IC/t-stat is no longer sufficient evidence. A signal
now has to survive group-neutral diagnostics, multiple-testing control, a
redundancy cluster, and execution-aware net-alpha/capacity gates before it
appears in `recommended_blend`.

## Point-in-time rule

The central rule is **do not backfill information the platform did not know yet**.

| Source | Historical treatment |
| --- | --- |
| Price / volume | Dated OHLCV bars; immediately backtestable. |
| Fundamentals | Existing filing-lagged trailing multiples; a quarter becomes usable only after `fundamental_lag_days` (45 by default). |
| Earnings surprise | Reported event is stamped onto the next trading day, then decays. |
| Analyst action | Upgrade/downgrade is stamped onto the next trading day, then decays. |
| Peer relationships | Peer sets are recomputed from trailing correlations only and refreshed on a calendar-anchored schedule (fixed business-day blocks, like the strategy rebalances), so the same date selects the same peers regardless of the query's start. |
| Analyst estimates | **Archive first.** Yahoo exposes mostly current estimate tables, so the platform stores them on the actual capture date. |
| Option chains | **Archive first.** Current IV/skew/open-interest features are stored on the actual capture date. |

Archived estimates/options are forward-filled only after capture and expire after a configurable number of business days. They are never filled into dates before the capture. A snapshot captured on a weekend or market holiday surfaces on the next trading session rather than being dropped.

## Signal registry

### Price-derived

The original nine signals remain unchanged:

- `residual_reversal`
- `one_day_reversal`
- `residual_momentum`
- `medium_residual_momentum`
- `trend_consistency`
- `high_proximity`
- `low_idio_vol`
- `volatility_compression`
- `downside_resilience`

### Volume

- `volume_confirmed_momentum` — residual strength reinforced by unusually high volume.
- `volume_shock_reversal` — fade large one-day residual shocks when volume is unusually high.
- `liquidity_improvement` — short-run Amihud illiquidity versus its longer baseline.

### Fundamentals

These use the existing filing-lagged `multiples_history` path:

- `fcf_yield_value`
- `earnings_yield_value`
- `sales_yield_value`
- `ebitda_yield_value`

### Events

- `post_earnings_surprise` — reported EPS surprise with configurable half-life and expiry.
- `analyst_action_momentum` — recent upgrade/downgrade balance with configurable decay.

### Estimate archive

- `eps_revision_breadth`
- `eps_estimate_acceleration`
- `target_price_upside`
- `low_estimate_dispersion`

### Options archive

- `put_call_oi_contrarian`
- `iv_richness`
- `downside_skew_contrarian`
- `iv_term_structure`

### Cross-sectional relationships

- `peer_spread_reversal` — fade a stock-specific spread versus dynamically selected correlated peers.
- `peer_catchup` — favor a name that lagged a recent move in its selected peers.

The multi-source catalog therefore exposes the original price signals plus the new families, and marks estimate/options signals as `archive_required`.

## Capturing non-backfillable features

`POST /api/backtest/signals/archive` captures today's estimate and option-chain features for the selected symbols.

The new `research_feature_snapshots` table stores:

- capture date,
- symbol,
- family (`estimates` or `options`),
- provider,
- JSON feature payload,
- capture timestamp.

The unique key is `(as_of_date, symbol, family)`, so repeated capture on the same day updates that day's snapshot instead of creating duplicates.

Typical archived estimate features:

- 30-day EPS revision breadth,
- 30-day EPS estimate change,
- estimate dispersion,
- consensus target-price upside.

Typical archived option features:

- put/call open-interest imbalance,
- downside IV skew,
- ATM IV minus trailing realized volatility,
- near/far ATM IV term slope.

A fresh install correctly reports these signals as unavailable for historical testing. They become testable only after real snapshots accumulate.

## Independent signal scoring

`POST /api/backtest/signals/multisource_research` builds every requested point-in-time signal that is actually available in the selected date range, then passes the combined library into the same evaluator used by price-only research.

For each forward horizon it computes:

- daily cross-sectional Spearman information coefficient (IC),
- IC t-statistic,
- share of positive IC days,
- top-minus-bottom forward-return spread,
- rolling test-fold consistency,
- cross-sectional rank turnover,
- usable data coverage.

The default decay curve is 1, 5, 10, and 21 trading days.

The response also includes:

- `source_status` — coverage/warnings for each data family,
- `available_signal_count`,
- `catalog_signal_count`,
- `unavailable_signals` — especially useful for archive-first families.

## Rolling out-of-sample blocks

A test block follows a training block and a purge gap. Signal formulas and parameters are fixed before the test block. The final `horizon` bars of each test block are excluded so their labels do not run into the next block.

Default validation gates at the 5-day horizon are:

- OOS mean IC >= 0.01,
- OOS IC t-stat >= 0.5,
- at least half of test folds have positive mean IC,
- coverage >= 50%,
- at least 30 OOS observations.

Signals are tagged `validated`, `watch`, or `reject`. `recommended_blend` is research output only. It must not be replayed backwards through the same history as if its full-sample weights had been known at the time.

## Research controls after the raw OOS gates

### 1. Sector / industry neutral evidence

When `neutralize_by` is `sector` or `industry`, the engine ranks the signal and
forward returns *inside each group* before computing the daily IC. Long-short
spreads are also formed inside each group and then averaged across groups.

The rule is deliberately conservative:

`raw OOS gates pass AND group-neutral OOS gates pass`

The group test never replaces a failed raw test. This matters because the
current Yahoo company profile contains today's sector/industry classification,
not a complete historical GICS classification timeline. The API reports this
as `current_snapshot_conservative_filter`. Present-day classification metadata
can therefore reject an apparent sector bet, but it cannot promote an idea
that failed the point-in-time raw test.

Per signal the response includes:

- `raw_validated`,
- `group_neutral_validated`,
- `group_neutral_primary`,
- `group_neutral_decay`,
- `group_neutral_folds`.

Top-level `classification_status` describes coverage and explicitly says
whether the classification itself is point in time.

### 2. False-discovery-rate control

Testing 20-100 signals guarantees that some apparently good t-stats happen by
chance. The engine computes a one-sided p-value for positive OOS IC, then runs
the Benjamini-Hochberg procedure across every tested signal.

At a forward horizon of `h` days, daily ICs overlap their neighbours for `h-1`
bars and are therefore strongly autocorrelated — a t-test over the full daily
series counts the same information many times and overstates significance
severely (on pure noise, roughly 3x too many signals clear p < 0.05 at the
5-day horizon). The p-value feeding the FDR step is therefore computed on
every `h`-th IC observation only, where a plain t-test is honest; the reported
`ic_t_stat` still describes the full series, and `ic_p_value_observations`
shows how many non-overlapping observations backed the p-value.

The default is:

- `fdr_alpha = 0.10`.

If group-neutral testing is enabled, the worse of the raw and group-neutral
p-values is used. A signal must have `q_value <= fdr_alpha` to survive.

Each signal includes:

```json
{
  "fdr": {
    "method": "benjamini_hochberg",
    "alpha": 0.1,
    "p_value": 0.012,
    "q_value": 0.071,
    "passed": true
  }
}
```

This controls the expected share of false discoveries among the signals that
survive the multiple-testing step; it does not prove that any individual
signal is economically real.

### 3. Correlation-aware redundancy clusters

The engine flattens each cross-sectional signal score over date x symbol,
centers each date, and measures pairwise signal correlation. Signals connected
at or above the absolute-correlation threshold are put into the same cluster.

Default:

- `redundancy_threshold = 0.80`.

Only the strongest evidence-qualified member of a cluster can enter the
recommended blend. A duplicate remains visible in the report with an exclusion
reason such as `redundant_with:residual_momentum`.

This prevents five variations of the same momentum feature from being counted
as five independent sources of alpha.

### 4. Execution-aware net alpha and capacity

When `execution_aware=true`, the evaluator builds a simple unit-gross
long/short target for every signal and judges implementation using only dated
inputs available at that decision date:

- 20-day rolling median dollar ADV from historical price x volume,
- a Corwin-Schultz effective-spread proxy from daily high/low bars,
- trailing daily volatility,
- requested commission/slippage assumptions,
- square-root impact: `impact_coefficient * daily_vol * sqrt(participation)`,
- a hard ADV participation ceiling at the requested research capital.

Each disconnected OOS fold starts from cash for cost purposes, so a stable
ranking cannot get a free entry merely because its target existed in the prior
training block. The response adds, per signal:

- gross alpha in basis points for a unit-gross top/bottom portfolio,
- estimated implementation cost in basis points,
- net alpha and net t-stat,
- one-way target turnover,
- average and 95th-percentile ADV participation,
- achievable capacity fill at the requested capital base,
- lower-tail and median implied capacity dollars.

The execution gate is also one-way: statistical evidence must already pass.
A signal is rejected when expected net alpha falls below `min_net_alpha_bps` or
when achievable fill is below `min_capacity_fill`. Among correlated statistical
survivors, the cluster representative is chosen using the execution-adjusted
selection score, so a cheaper near-duplicate can correctly beat a costly one.

Daily high/low data is not a historical quote feed. The spread field is
therefore explicitly labeled as a Corwin-Schultz proxy, not an observed NBBO.

### Current cost-aware target

Multi-source research also returns `current_execution_book`. It uses only
signals that survived every research gate, blends their current scores, and
projects the result to a dollar/beta-neutral target. Under a flat-start
assumption, if entering that target would breach the ADV ceiling, the engine
scales the *entire* book by one common factor. This preserves both neutrality
constraints; independently clipping illiquid names would not.

The current book reports target versus executable gross exposure, capacity
scale, estimated entry cost, per-name ADV participation/cost, and a
blend-weighted historical net-alpha diagnostic. This is a present-day target,
not a historical strategy: the full-sample research selection is never replayed
backward through its own evaluation window.

### Final survival rule

A signal is `validated` only when all applicable conditions hold:

1. raw OOS IC / t-stat / fold consistency / coverage gates pass,
2. the sector/industry-neutral version also passes when enabled,
3. Benjamini-Hochberg q-value passes the requested FDR cutoff,
4. net alpha and ADV capacity clear the execution gates when enabled,
5. the signal is the selected representative of its correlation cluster.

The API exposes the exact failures in `exclusion_reasons`, so a `watch` signal
can be distinguished from a statistically weak signal, a sector-contaminated
signal, a cost/capacity failure, and a duplicate of a stronger predictor.

## Walk-forward multi-source fund simulation

`POST /api/backtest/signals/walk_forward_portfolio` closes the loop between research and portfolio construction. It does **not** take today's full-sample `recommended_blend` and replay it backward. Instead it builds a sequence of historical research vintages.

The sequence is:

1. build the complete point-in-time signal library once,
2. wait until one full train / purge / OOS test block exists,
3. on each research refresh date, slice prices, signals and execution inputs to that historical prefix only,
4. re-run raw OOS gates, group-neutral evidence, FDR, execution/capacity tests and redundancy selection on that prefix,
5. freeze the resulting blend as a research vintage,
6. on each portfolio rebalance, use only the latest vintage completed at or before that signal date,
7. blend that day's current point-in-time signal scores, project the score to a dollar/beta-neutral target,
8. trade from the **existing** book toward the new target, uniformly scaling the delta when the ADV ceiling binds,
9. apply the resulting target one bar later and charge spread/slippage/impact costs on that execution bar.

Research refresh and portfolio rebalance are intentionally separate clocks. The default research refresh is one OOS test block (`test_days`), while the portfolio can rebalance more frequently (5 trading days by default). This mimics a research process that periodically approves or removes predictors while the trading book continues to update current scores between research committee vintages.

The response includes:

- net and pre-execution-cost equity curves,
- standard performance statistics for both,
- every research vintage and its frozen blend,
- every portfolio decision with the research vintage it used,
- capacity scale, turnover, estimated cost, net exposure and beta exposure at each decision,
- signal selection frequency and average weight through time,
- counts of capacity-constrained decisions and active trading days.

The anti-leakage invariant is explicit: changing prices or external feature rows **after** a historical cutoff must not alter any research vintage or portfolio decision before that cutoff. Tests enforce this alongside the one-bar execution lag.

### Capacity during the walk-forward run

The current-book helper uses a flat-start entry assumption. The historical simulation is stricter and more realistic: capacity is measured on `new_target - existing_book`. If any desired trade would exceed the ADV limit, one common scalar is applied to the complete trade vector. This preserves dollar neutrality because both the old and desired books are dollar neutral. Beta can drift between rebalances as rolling betas change; the API reports that realized beta exposure on every decision rather than hiding it.

## Existing adaptive price strategy

`stat_arb_research` remains the live-safe adaptive price strategy. It uses only already-realized trailing IC and delays an `h`-day label by `h` bars before that label can affect signal weights.

The walk-forward multi-source simulation is the historical research harness. It remains distinct from a live production strategy because archive-first estimate/options features only become useful after enough real snapshots have accumulated.

## API

- `GET /api/backtest/signals/catalog` — original price-only registry.
- `GET /api/backtest/signals/multisource_catalog` — all signal families and archive requirements.
- `POST /api/backtest/signals/research` — price-only OOS research.
- `POST /api/backtest/signals/multisource_research` — point-in-time multi-source OOS research.
- `POST /api/backtest/signals/walk_forward_portfolio` — historical research vintages feeding a cost-aware neutral portfolio.
- `POST /api/backtest/signals/archive` — capture today's estimates/options/crowding fields for future research and short-risk controls.
- `POST /api/backtest/signals/adaptive_snapshot` — current price-only adaptive target.
- `POST /api/backtest/run` with `strategy="stat_arb_research"` — current adaptive price-only simulation.

`SignalResearchRequest` also accepts:

```json
{
  "neutralize_by": "sector",
  "min_group_names": 2,
  "fdr_alpha": 0.10,
  "redundancy_threshold": 0.80,
  "redundancy_min_overlap": 100,
  "execution_aware": true,
  "research_capital_dollars": 10000000,
  "max_adv_participation": 0.05,
  "execution_commission_bps": 1.0,
  "execution_slippage_bps": 0.5,
  "impact_coefficient": 0.10,
  "min_capacity_fill": 0.90,
  "min_net_alpha_bps": 0.0
}
```

The walk-forward portfolio request extends those fields with:

```json
{
  "research_refresh_days": 63,
  "portfolio_rebalance_days": 5,
  "gross_target": 1.0,
  "initial_capital": 10000000,
  "alpha_risk_aware": true,
  "sleeve_lookback_days": 126,
  "sleeve_correlation_threshold": 0.60,
  "max_sleeve_budget": 0.45,
  "max_cluster_budget": 0.65,
  "event_budget_cap": 0.25,
  "max_name_weight": 0.20,
  "max_crowded_short_gross": 0.15,
  "borrow_aware": true,
  "base_borrow_bps": 30.0
}
```

Set `neutralize_by` to `none` to disable only the group-neutral hurdle. FDR and
redundancy controls still run.

## Useful parameters

```json
{
  "fundamental_lag_days": 45,
  "estimate_archive_ffill_days": 30,
  "options_archive_ffill_days": 5,
  "earnings_surprise_half_life": 5,
  "earnings_surprise_max_days": 21,
  "analyst_action_half_life": 10,
  "analyst_action_max_days": 42,
  "peer_window": 63,
  "peer_count": 3,
  "peer_refresh_days": 5,
  "peer_spread_lookback": 5,
  "peer_z_window": 63
}
```

Each source family can also be disabled with `include_volume`, `include_fundamentals`, `include_events`, or `include_archived_snapshots`.

## Alpha sleeves, concentration and borrow risk

The walk-forward fund simulation now adds a second allocation layer after signal
selection. Validated signals are grouped into interpretable family sleeves
(reversal, momentum, risk, volume, fundamentals, events, estimates, options and
relationships). Each sleeve gets its own one-bar-lagged historical PnL stream.
Trailing sleeve volatility and the vintage's validated blend share determine a
raw risk budget.

Risk allocation then applies one-way concentration controls:

- a maximum budget for any one sleeve,
- a shared cap for sleeves whose trailing PnL correlation exceeds the configured threshold,
- a separate cap for the event sleeve,
- a maximum absolute stock weight,
- a maximum gross short allocation to names flagged as crowded,
- optional group-net caps (off by default because current Yahoo sector/industry labels are not a historical classification timeline).

Security-level constraints are solved as a projection of the desired neutral
book. Dollar and rolling-beta neutrality remain equality constraints; name,
gross, crowded-short and optional group limits are inequalities. If the
projection cannot satisfy the constraints, the engine fails closed to a flat
book rather than silently violating them.

Short costs are charged every held day, not only on rebalance dates. Without an
observed stock-loan feed the default is a transparent general-collateral base
fee. Archived short-interest / days-to-cover snapshots can add a crowding
surcharge and can conservatively mark extreme names as unavailable to short
from that capture date onward. The same feature store already accepts future
point-in-time `borrow_fee_annual_bps` and `short_available` fields from a prime
broker or licensed borrow source without changing the simulator.

The response now includes sleeve activation/risk-budget history, per-decision
constraint diagnostics, total execution cost, total borrow cost and the borrow
data mode used.

## Next research upgrades

The highest-value remaining additions are:

1. an observed point-in-time borrow/locate feed instead of the GC + crowding proxy,
2. a dated sector/industry classification history so group caps can run without current-label contamination,
3. a multi-factor covariance model for stock-level residual risk and factor crowding,
4. HAC (Newey-West style) significance estimates, which would recover the
   efficiency the current non-overlapping subsampling gives up while staying
   honest about overlapping forward-return horizons,
5. automated daily archive jobs for estimates, options and crowding snapshots.
