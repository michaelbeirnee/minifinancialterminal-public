# Future changes: normalizing strategy test results

Status: planned, not implemented (noted 2026-08-13).

When a signal family is graded for forward returns, the results need to be
normalized on three axes before they are comparable — across events, across
families, and across time. Phase 2 of the insider-cluster calibration showed
what happens otherwise: pooled means driven by a single stock (SEZL),
overlapping event windows inflating t-stats, and a lottery-like distribution
where the median event loses while rare winners carry the average.

## Where normalization stands today

- `backend/thesis/memory.py::grade_pending` stamps each event as **excess
  return vs its benchmark over the identical window** (axis 1: market move
  removed).
- `report()` leads with **medians and hit rates**, which are robust to the
  outlier tail.

## Axis 2 — risk: divide by volatility (biggest gap, do first)

A +40% excess move in a 90%-vol microcap is not five times better than +8%
in a mega cap, but the raw excess columns treat it that way.

```
vol_adj = excess_return / (trailing_daily_vol × sqrt(horizon_sessions))
```

- Trailing vol: 63-session std of daily returns ending at the entry index.
  `grade_pending` already holds the full price panel, so this is nearly free.
- Store `vol_adj_1m/3m/6m/12m` **alongside** the raw excess columns — raw
  answers "what would I have made", vol-adjusted answers "is this signal
  real".
- Unitless, so families with very different typical names (microcap insider
  clusters vs large-cap estimate drift) become directly comparable.

## Axis 3 — time: aggregation across events

Per-event numbers overstate significance when events cluster in calendar
time (thirty clusters in one month are mostly one bet on that month).

1. **Cheap:** de-overlapped t-stat in `report()` — at most one event per
   symbol per horizon window (this was done by hand in Phase 2; it belongs
   in the code).
2. **Proper, once a second family exists:** the **calendar-time portfolio**.
   Each day, hold every event whose horizon window is open, equal-weighted;
   the family collapses to one daily return series whose mean/Sharpe is
   automatically normalized for clustering. This is the right yardstick for
   the convergence test (≥2 families in a window, as a portfolio vs each
   family alone).

## Smaller items

- **Per-event benchmark assignment at record time.** The `benchmark` column
  already exists per event; assign IWM (or a sector ETF) to small caps so a
  new family isn't accidentally a size bet in disguise. (Phase 2 showed
  size-matching didn't rescue insider clusters, but it should be the
  default going forward.)
- **Rank vs universe (hold in reserve).** Record the percentile of the
  event's forward return within all stocks over the same window. Fully
  outlier-proof (null ⇒ mean percentile 50), but needs a universe price
  panel per grading run. Only reach for this if vol-adjusted numbers still
  look outlier-dominated.

## Concrete next step

Add trailing-vol capture and `vol_adj_*` columns to `grade_pending`, plus a
de-overlapped t-stat to `report()`. Small change; makes the existing graded
events immediately re-readable in normalized units. Save the calendar-time
portfolio for when the second signal family comes online.
