# Signal research layer

The signal-research layer separates **feature ideas** from **portfolio construction**. A signal has to show independent predictive evidence before it is allowed into the adaptive stat-arb book.

## Signal registry

`backend/backtest/signal_research.py` currently registers nine trailing-only, price-derived cross-sectional signals:

| Signal | Family | Idea |
| --- | --- | --- |
| `residual_reversal` | reversal | Fade short residual moves after broad beta is removed. |
| `one_day_reversal` | reversal | Faster one-day residual reversal. |
| `residual_momentum` | momentum | Long-horizon residual trend, skipping the newest bars. |
| `medium_residual_momentum` | momentum | Faster residual trend. |
| `trend_consistency` | momentum | Residual trend scaled by its own noise. |
| `high_proximity` | momentum | Proximity to the trailing high. |
| `low_idio_vol` | risk | Lower residual volatility. |
| `volatility_compression` | risk | Short-run residual volatility versus its long-run baseline. |
| `downside_resilience` | risk | Lower downside residual volatility. |

The registry records `family`, `description`, and `source`. Future OHLCV, fundamental, filing, news, options, flow, and cross-asset builders can use the same research interface.

## Independent signal scoring

`POST /api/backtest/signals/research` evaluates each signal separately. It does not judge the combined strategy first and then infer which feature worked.

For each requested forward horizon it computes:

- daily cross-sectional Spearman information coefficient (IC),
- IC t-statistic,
- share of positive IC days,
- top-minus-bottom forward-return spread,
- rolling test-fold consistency,
- cross-sectional rank turnover,
- usable data coverage.

The default decay curve is 1, 5, 10, and 21 trading days.

## Rolling out-of-sample blocks

A test block follows a training block and a purge gap. Signal formulas and parameters are fixed before the test block. The final `horizon` bars of each test block are excluded so their labels do not run into the next block.

Default validation gates at the 5-day horizon are:

- OOS mean IC >= 0.01,
- OOS IC t-stat >= 0.5,
- at least half of test folds have positive mean IC,
- coverage >= 50%,
- at least 30 OOS observations.

Signals are tagged `validated`, `watch`, or `reject`. A `recommended_blend` is produced only from validated signals and is research output, not a license to use full-sample weights inside a historical backtest.

## Adaptive strategy with no label leakage

`stat_arb_research` is a separate strategy in the normal backtest registry. It does not use the full-sample research recommendation.

Instead, it:

1. builds all selected trailing signal scores,
2. computes each signal's historical cross-sectional IC,
3. delays an `h`-day IC observation by `h` bars, because the forward return is not known until then,
4. estimates signal quality only from those already-realized labels,
5. drops signals below the configured IC/t-stat gates,
6. keeps only the strongest configured number of active signals,
7. normalizes their quality weights,
8. blends their current scores,
9. removes net-dollar and rolling-beta exposure,
10. hands target weights to the existing one-bar-lagged execution engine.

Useful parameters:

```json
{
  "quality_horizon": 5,
  "quality_window": 126,
  "quality_min_periods": 40,
  "min_signal_ic": 0.0,
  "min_signal_t_stat": 0.0,
  "max_active_signals": 4,
  "rebalance_days": 5,
  "gross_target": 1.0
}
```

Use `research_signals` to restrict the candidate list, for example:

```json
{
  "research_signals": [
    "residual_reversal",
    "residual_momentum",
    "low_idio_vol",
    "volatility_compression"
  ]
}
```

## API

- `GET /api/backtest/signals/catalog` — signal registry and metadata.
- `POST /api/backtest/signals/research` — independent OOS signal report and decay curves.
- `POST /api/backtest/signals/adaptive_snapshot` — current adaptive blend, trailing IC evidence, and long/short target.
- `POST /api/backtest/run` with `strategy="stat_arb_research"` — historical simulation through the existing cost and execution stack.

## What comes next

The evaluator is intentionally source-agnostic. The next data expansion should pass richer research inputs (OHLCV first, then fundamentals/events/options/flows) into the same registry rather than embedding provider calls inside individual strategy functions. That keeps research reproducible and avoids a backtest changing because an external endpoint changed later.
