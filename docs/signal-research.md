# Signal research layer

The signal-research layer separates **feature ideas** from **portfolio construction**. A signal has to show independent predictive evidence before it is allowed into a live blend.

The research stack now has two libraries:

1. `backend/backtest/signal_research.py` — cheap, price-only signals that can be rebuilt from a close-price panel.
2. `backend/backtest/multisource_research.py` — point-in-time signals built from volume, filing-lagged fundamentals, dated events, archived estimates/options, and trailing peer relationships.

Both feed the same out-of-sample evaluator. A richer data source does not get easier validation gates.

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

## Existing adaptive price strategy

`stat_arb_research` remains the live-safe adaptive price strategy. It uses only already-realized trailing IC and delays an `h`-day label by `h` bars before that label can affect signal weights.

The multi-source layer deliberately keeps data acquisition/research separate from that strategy path for now. Historical volume/fundamental/event/peer signals can already be evaluated honestly; estimates/options need enough archived history before an adaptive multi-source portfolio is worth enabling.

## API

- `GET /api/backtest/signals/catalog` — original price-only registry.
- `GET /api/backtest/signals/multisource_catalog` — all signal families and archive requirements.
- `POST /api/backtest/signals/research` — price-only OOS research.
- `POST /api/backtest/signals/multisource_research` — point-in-time multi-source OOS research.
- `POST /api/backtest/signals/archive` — capture today's estimates/options for future research.
- `POST /api/backtest/signals/adaptive_snapshot` — current price-only adaptive target.
- `POST /api/backtest/run` with `strategy="stat_arb_research"` — current adaptive price-only simulation.

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

## Next research upgrades

The next additions should improve research quality rather than simply multiply feature count:

1. sector/industry neutralization before IC measurement,
2. correlation-aware signal clustering so near-duplicate predictors do not all pass independently,
3. multiple-testing controls and false-discovery-rate reporting,
4. capacity/liquidity filters tied to ADV and expected turnover,
5. archived daily estimates/options jobs so those families collect data automatically,
6. a walk-forward multi-source portfolio whose blend is fit only on each prior training window.
