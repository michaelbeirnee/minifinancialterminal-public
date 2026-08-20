# Cross-sectional statistical arbitrage

Status: implemented 2026-08-19 in `backend/backtest/stat_arb.py`, registered as
`stat_arb` in the strategy registry, exposed through
`POST /api/backtest/stat_arb/snapshot`, and covered by `tests/test_stat_arb.py`.

This is the first portfolio-level research rule in the terminal that assumes no
single signal is strong enough to trade by itself. It blends several weak
price-derived signals, strips broad directional exposure, and lets the existing
backtest stack apply the execution lag, costs, walk-forward tests, and sizing
overlays.

## Signal stack

For every symbol, daily return is split into a rolling broad-universe component
and a residual:

```
return_i,t = beta_i,t * universe_return_t + residual_i,t
```

The alpha score combines three cross-sectional percentile ranks:

1. `residual_reversal`: negative of the last 5 residual-return days. A name that
   overshot its peers gets a positive fade score.
2. `residual_momentum`: residual return over 126 days, skipping the newest 21
   days. This captures slower persistence without simply negating reversal.
3. `low_idio_vol`: negative 63-day residual volatility. Lower-noise names rank
   above high-noise names.

Default blend:

```
1.00 * residual_reversal
0.65 * residual_momentum
0.35 * low_idio_vol
```

A 3-day trailing smoother reduces churn. Targets refresh every 5 business days
by default; both are configurable. The rebalance schedule is anchored to the
calendar (fixed 5-business-day blocks counted from a fixed epoch), not to the
first row of the requested window, so the same date always gets the same
target regardless of the query's `start` — snapshots are reproducible and
walk-forward folds rebalance on the same days as a full-sample run.

The blended score needs roughly `beta_window + momentum_skip +
momentum_lookback` bars of history (about 210 at the defaults) before all
three components are defined; the book is flat until then. Walk-forward runs
should use `train_days` comfortably above that warm-up, or the in-sample fit
sees only a handful of active bars.

## Portfolio construction

The score is not used as a directional portfolio. For each date, it is projected
away from two cross-sectional vectors:

- the constant vector, forcing net dollar exposure to zero;
- each symbol's rolling beta to the supplied universe, forcing first-order beta
  exposure to zero.

The residual vector from that projection is scaled to gross exposure 1.0. The
platform's existing volatility-targeting overlay can add or remove leverage
later; the strategy itself never exceeds 1.0 gross.

The normal backtest engine then delays those targets one bar before applying
returns, so a close observed on day T cannot earn day-T return.

## API

Backtest it through the normal endpoint:

```json
POST /api/backtest/run
{
  "strategy": "stat_arb",
  "symbols": ["AAPL", "MSFT", "NVDA", "AMD", "AVGO", "QCOM", "AMZN", "META"],
  "start": "2022-01-01",
  "params": {
    "reversal_lookback": 5,
    "momentum_lookback": 126,
    "momentum_skip": 21,
    "beta_window": 63,
    "vol_window": 63,
    "smooth_span": 3,
    "rebalance_days": 5,
    "reversal_weight": 1.0,
    "momentum_weight": 0.65,
    "low_vol_weight": 0.35,
    "gross_target": 1.0
  }
}
```

Inspect the latest target and score attribution without running a simulation:

```json
POST /api/backtest/stat_arb/snapshot
{
  "symbols": ["AAPL", "MSFT", "NVDA", "AMD", "AVGO", "QCOM", "AMZN", "META"],
  "start": "2023-01-01",
  "params": {"rebalance_days": 5}
}
```

The response includes each symbol's target weight, side, blended score, rolling
beta, and all three component scores, plus portfolio gross, net, and beta
exposure.

## What this still is not

This is daily-bar research infrastructure, not a production quantitative desk.
A stronger second stage should add point-in-time universe membership, corporate
action handling, borrow/short constraints, sector and industry neutrality,
liquidity-based capacity limits, volume-aware transaction cost curves, richer
fundamental/event/flow signals, signal decay diagnostics, and intraday data plus
an order/execution simulator. Those additions should sit around this signal ->
neutralize -> size -> execute boundary rather than replacing it.

Two smaller caveats worth knowing:

- Beta is measured against the equal-weight mean of the *supplied* basket,
  which includes the symbol itself. With very small baskets each name is a
  large share of its own "market", so residuals shrink toward zero. Use 8+
  single stocks, and leave broad index ETFs like SPY out of the basket — the
  index is the exposure being stripped, not a peer.
- Like every strategy on the vectorized engine, weights held between
  rebalances are constant in *weight* space, which implicitly rebalances back
  to the stale target daily at zero cost — turnover is slightly understated
  for a long/short book. The event-driven engine executes those daily
  re-hedges explicitly and charges commission and slippage on them, so running
  both engines brackets the true cost.
