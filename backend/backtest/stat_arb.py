"""Cross-sectional statistical-arbitrage signal construction.

The strategy is deliberately price-only so it can run inside the existing
backtest stack without introducing a second data-loading path.  It combines
several weak, differently-paced signals, then removes the two exposures that
would otherwise dominate a small long/short book: net dollar exposure and the
book's rolling beta to the supplied universe.

Nothing in this module executes trades.  It produces target weights; the normal
backtest engines apply the platform-wide one-bar execution lag and cost model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class StatArbOutput:
    """Research artifact returned by :func:`build_stat_arb`.

    ``weights`` are the target portfolio weights consumed by the backtester.
    ``score`` is the blended cross-sectional alpha score.  ``components`` keeps
    the normalized input signals separate so a caller can inspect attribution.
    ``beta`` is the rolling single-name beta to the equal-weight universe used
    by the neutralization step.  ``schedule`` is True on the bars where a new
    target was set; ``weights`` holds the last scheduled target in between.
    """

    weights: pd.DataFrame
    score: pd.DataFrame
    components: dict[str, pd.DataFrame]
    beta: pd.DataFrame
    decision_weights: pd.DataFrame
    schedule: pd.Series
    rebalance_days: int


def _positive_int(params: dict[str, Any], key: str, default: int, minimum: int = 1) -> int:
    value = int(params.get(key, default))
    if value < minimum:
        raise ValueError(f"{key} must be >= {minimum}")
    return value


def _nonnegative_float(params: dict[str, Any], key: str, default: float) -> float:
    value = float(params.get(key, default))
    if value < 0:
        raise ValueError(f"{key} must be >= 0")
    return value


def _rank_score(frame: pd.DataFrame) -> pd.DataFrame:
    """Robust cross-sectional score in roughly [-1, 1].

    Percentile ranks are far less sensitive to one extreme print than raw
    z-scores.  Subtracting each row's mean keeps the cross-section centered even
    when some symbols are missing during warm-up.
    """

    ranked = frame.rank(axis=1, pct=True, method="average")
    centered = 2.0 * (ranked - 0.5)
    return centered.sub(centered.mean(axis=1), axis=0)


def _rolling_beta(returns: pd.DataFrame, window: int) -> tuple[pd.Series, pd.DataFrame]:
    """Equal-weight universe return and rolling beta for every symbol."""

    universe = returns.mean(axis=1)
    variance = universe.rolling(window, min_periods=window).var().replace(0.0, np.nan)
    covariance = returns.rolling(window, min_periods=window).cov(universe)
    beta = covariance.div(variance, axis=0)
    return universe, beta.replace([np.inf, -np.inf], np.nan)


def _neutralize_row(score: pd.Series, beta: pd.Series, gross_target: float) -> pd.Series:
    """Project a cross-section off the constant and rolling-beta vectors.

    Regressing the alpha score on ``[1, beta]`` and using the residual gives a
    weight vector whose sum and beta dot-product are both zero (up to floating
    point error).  It is then scaled to the requested gross exposure.
    """

    valid = score.notna() & beta.notna()
    out = pd.Series(0.0, index=score.index, dtype=float)
    if valid.sum() < 3 or gross_target <= 0:
        return out

    y = score.loc[valid].to_numpy(dtype=float)
    b = beta.loc[valid].to_numpy(dtype=float)
    x = np.column_stack([np.ones(len(b)), b])

    # If every beta is effectively identical, beta neutrality is the same
    # constraint as dollar neutrality.  Demeaning is numerically safer than an
    # ill-conditioned two-column least-squares solve.
    if float(np.nanstd(b)) < 1e-10:
        resid = y - float(np.mean(y))
    else:
        fitted = x @ np.linalg.lstsq(x, y, rcond=None)[0]
        resid = y - fitted

    gross = float(np.abs(resid).sum())
    if not np.isfinite(gross) or gross < 1e-12:
        return out
    out.loc[valid] = resid * (gross_target / gross)
    return out


def _neutral_weights(score: pd.DataFrame, beta: pd.DataFrame, gross_target: float) -> pd.DataFrame:
    rows = [
        _neutralize_row(score.loc[dt], beta.loc[dt], gross_target)
        for dt in score.index
    ]
    out = pd.DataFrame(rows, index=score.index)
    return out.reindex(columns=score.columns).fillna(0.0)


# Any fixed business day works as the schedule origin; a Monday reads cleanly.
_SCHEDULE_EPOCH = np.datetime64("2000-01-03")


def _rebalance_schedule(index: pd.Index, every: int) -> pd.Series:
    """True on bars where a new target is set.

    The schedule is anchored to the calendar — business days since a fixed
    epoch, grouped into ``every``-day blocks, rebalancing on the first bar of
    each block — rather than to the panel's first row.  A positional schedule
    would give the same calendar date a different target depending on where
    the requested window happens to start (or on how much history a symbol
    has), which makes snapshots irreproducible and walk-forward folds
    inconsistent with full-sample runs.
    """

    if every <= 1:
        return pd.Series(True, index=index)
    if isinstance(index, pd.DatetimeIndex):
        days = index.tz_localize(None) if index.tz is not None else index
        blocks = np.busday_count(_SCHEDULE_EPOCH, days.values.astype("datetime64[D]")) // every
        mask = np.r_[True, blocks[1:] != blocks[:-1]]
    else:  # non-date index: positional fallback
        mask = (np.arange(len(index)) % every) == 0
    return pd.Series(mask, index=index)


def _rebalance(weights: pd.DataFrame, schedule: pd.Series) -> pd.DataFrame:
    """Hold the most recent scheduled target between rebalance bars."""

    if schedule.all():
        return weights
    out = weights.copy()
    out.loc[~schedule, :] = np.nan
    return out.ffill().fillna(0.0)


def build_stat_arb(prices: pd.DataFrame, params: dict[str, Any] | None = None) -> StatArbOutput:
    """Build a small multi-signal cross-sectional stat-arb portfolio.

    Signals
    -------
    residual_reversal
        Fade the last few days of idiosyncratic return after removing rolling
        exposure to the equal-weight universe.
    residual_momentum
        Favor persistent idiosyncratic strength over a longer horizon while
        skipping the newest bars so it is not just the inverse of reversal.
    low_idio_vol
        Favor lower residual volatility and short higher residual volatility.

    Parameters are intentionally compact and all calculations are trailing-only.
    The backtest engine still shifts the resulting targets by one bar before
    applying returns.
    """

    params = params or {}
    if prices is None or prices.empty:
        raise ValueError("stat_arb requires a non-empty price panel")
    if prices.shape[1] < 3:
        raise ValueError("stat_arb needs at least 3 symbols for cross-sectional neutralization")

    prices = prices.sort_index().astype(float)
    if not prices.index.is_unique:
        prices = prices[~prices.index.duplicated(keep="last")]

    beta_window = _positive_int(params, "beta_window", 63, minimum=10)
    reversal_lookback = _positive_int(params, "reversal_lookback", 5)
    momentum_lookback = _positive_int(params, "momentum_lookback", 126, minimum=5)
    momentum_skip = _positive_int(params, "momentum_skip", 21, minimum=0)
    vol_window = _positive_int(params, "vol_window", 63, minimum=10)
    smooth_span = _positive_int(params, "smooth_span", 3)
    rebalance_days = _positive_int(params, "rebalance_days", 5)

    reversal_weight = _nonnegative_float(params, "reversal_weight", 1.0)
    momentum_weight = _nonnegative_float(params, "momentum_weight", 0.65)
    low_vol_weight = _nonnegative_float(params, "low_vol_weight", 0.35)
    gross_target = _nonnegative_float(params, "gross_target", 1.0)
    if gross_target > 1.0:
        raise ValueError("gross_target must be <= 1.0; use the existing vol-target overlay for leverage")
    if reversal_weight + momentum_weight + low_vol_weight <= 0:
        raise ValueError("at least one stat_arb signal weight must be positive")

    returns = prices.pct_change(fill_method=None)
    universe, beta = _rolling_beta(returns, beta_window)
    residual = returns - beta.mul(universe, axis=0)

    reversal_raw = -residual.rolling(
        reversal_lookback, min_periods=reversal_lookback
    ).sum()
    momentum_raw = residual.shift(momentum_skip).rolling(
        momentum_lookback, min_periods=momentum_lookback
    ).sum()
    low_vol_raw = -residual.rolling(vol_window, min_periods=vol_window).std()

    components = {
        "residual_reversal": _rank_score(reversal_raw),
        "residual_momentum": _rank_score(momentum_raw),
        "low_idio_vol": _rank_score(low_vol_raw),
    }

    score = (
        reversal_weight * components["residual_reversal"]
        + momentum_weight * components["residual_momentum"]
        + low_vol_weight * components["low_idio_vol"]
    ) / (reversal_weight + momentum_weight + low_vol_weight)

    # A short trailing smoother lowers churn without pulling in future data.
    if smooth_span > 1:
        score = score.ewm(span=smooth_span, adjust=False, min_periods=1).mean()

    schedule = _rebalance_schedule(prices.index, rebalance_days)
    raw_weights = _neutral_weights(score, beta, gross_target)
    weights = _rebalance(raw_weights, schedule)
    weights = weights.reindex(index=prices.index, columns=prices.columns).fillna(0.0)

    return StatArbOutput(
        weights=weights,
        score=score,
        components=components,
        beta=beta,
        decision_weights=raw_weights,
        schedule=schedule,
        rebalance_days=rebalance_days,
    )


def stat_arb_snapshot(prices: pd.DataFrame, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the latest target plus per-signal attribution for API/UI use."""

    output = build_stat_arb(prices, params)
    current_dt = output.weights.index[-1]
    w = output.weights.loc[current_dt]
    if float(w.abs().sum()) <= 1e-12:
        raise ValueError("Not enough history to form a stat_arb portfolio with these settings")

    # The held target is whatever the most recent scheduled rebalance decided
    # (it is non-zero here, or ``w`` would be flat).  Explain it using the
    # score/beta snapshot from that decision bar, while also reporting how much
    # beta drift the held target has accumulated by the latest bar.
    signal_dt = output.schedule.index[output.schedule.to_numpy().nonzero()[0][-1]]
    beta = output.beta.loc[signal_dt]
    current_beta = output.beta.loc[current_dt]
    score = output.score.loc[signal_dt]
    component_rows = {name: frame.loc[signal_dt] for name, frame in output.components.items()}

    rows = []
    for symbol in output.weights.columns:
        weight = float(w.get(symbol, 0.0))
        rows.append(
            {
                "symbol": symbol,
                "side": "long" if weight > 1e-10 else "short" if weight < -1e-10 else "flat",
                "weight": round(weight, 8),
                "score": None if pd.isna(score.get(symbol)) else round(float(score[symbol]), 8),
                "beta": None if pd.isna(beta.get(symbol)) else round(float(beta[symbol]), 8),
                **{
                    name: None if pd.isna(values.get(symbol)) else round(float(values[symbol]), 8)
                    for name, values in component_rows.items()
                },
            }
        )

    beta_exposure_at_signal = float((w * beta.fillna(0.0)).sum())
    beta_exposure_current = float((w * current_beta.fillna(0.0)).sum())
    return {
        "as_of": current_dt.date().isoformat() if hasattr(current_dt, "date") else str(current_dt),
        "signal_as_of": signal_dt.date().isoformat() if hasattr(signal_dt, "date") else str(signal_dt),
        "gross_exposure": round(float(w.abs().sum()), 8),
        "net_exposure": round(float(w.sum()), 8),
        "beta_exposure": round(beta_exposure_current, 8),
        "beta_exposure_at_signal": round(beta_exposure_at_signal, 8),
        "positions": sorted(rows, key=lambda r: abs(r["weight"]), reverse=True),
        "params": dict(params or {}),
    }
