"""Signal research and adaptive cross-sectional portfolio construction.

This module sits between raw feature ideas and the stat-arb portfolio.  Signal
formulas stay independent from portfolio construction so each one can be
measured on its own before it is allowed to influence capital.

The first registry is deliberately price-only because the existing backtest API
passes a close-price panel.  The registry metadata includes ``source`` so later
OHLCV, fundamental, filing, options, news, flow, and cross-asset builders can be
added without changing the evaluator.

Research discipline
-------------------
* Every signal is trailing-only.
* Predictive quality is measured with cross-sectional rank IC and top-minus-
  bottom spread at several forward horizons.
* Rolling test blocks are kept separate from a preceding training window and
  purge gap.  Signal formulas are fixed; the test blocks therefore measure
  genuinely unseen periods rather than a parameter fit on the same labels.
* The adaptive portfolio uses only *realized* historical IC.  A horizon-h IC
  observation is delayed h bars before it can affect a signal's live weight.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from .research_controls import (
    benjamini_hochberg,
    choose_cluster_representatives,
    correlation_clusters,
    group_coverage,
    group_neutral_ic,
    group_neutral_spread,
    one_sided_positive_p_value,
    signal_correlation_matrix,
)
from .stat_arb import (
    _neutral_weights,
    _positive_int,
    _rank_score,
    _rebalance,
    _rebalance_schedule,
    _rolling_beta,
)


@dataclass(frozen=True)
class SignalSpec:
    name: str
    family: str
    description: str
    source: str = "price"


@dataclass(frozen=True)
class SignalLibraryOutput:
    components: dict[str, pd.DataFrame]
    beta: pd.DataFrame
    residual_returns: pd.DataFrame


@dataclass(frozen=True)
class AdaptiveStatArbOutput:
    weights: pd.DataFrame
    score: pd.DataFrame
    components: dict[str, pd.DataFrame]
    beta: pd.DataFrame
    signal_weights: pd.DataFrame
    signal_quality: pd.DataFrame
    decision_weights: pd.DataFrame
    schedule: pd.Series
    rebalance_days: int
    quality_horizon: int


SIGNAL_SPECS: tuple[SignalSpec, ...] = (
    SignalSpec(
        "residual_reversal",
        "reversal",
        "Fade the last few days of idiosyncratic return after broad beta is removed.",
    ),
    SignalSpec(
        "one_day_reversal",
        "reversal",
        "Fade the most recent idiosyncratic daily move; a faster, higher-turnover reversal test.",
    ),
    SignalSpec(
        "residual_momentum",
        "momentum",
        "Favor persistent long-horizon idiosyncratic strength while skipping the newest bars.",
    ),
    SignalSpec(
        "medium_residual_momentum",
        "momentum",
        "A medium-horizon residual trend signal that reacts faster than the long trend feature.",
    ),
    SignalSpec(
        "trend_consistency",
        "momentum",
        "Reward residual trends whose average return is large relative to their own noise.",
    ),
    SignalSpec(
        "high_proximity",
        "momentum",
        "Rank stocks by proximity to their trailing high; persistent leaders score higher.",
    ),
    SignalSpec(
        "low_idio_vol",
        "risk",
        "Favor lower idiosyncratic volatility and short higher residual volatility.",
    ),
    SignalSpec(
        "volatility_compression",
        "risk",
        "Favor names whose short-run residual volatility is low relative to their longer baseline.",
    ),
    SignalSpec(
        "downside_resilience",
        "risk",
        "Favor names with lower trailing downside residual volatility.",
    ),
)

_SPEC_BY_NAME = {spec.name: spec for spec in SIGNAL_SPECS}


def signal_catalog() -> list[dict[str, str]]:
    return [
        {
            "name": spec.name,
            "family": spec.family,
            "description": spec.description,
            "source": spec.source,
        }
        for spec in SIGNAL_SPECS
    ]


def _selected_signal_names(signals: Iterable[str] | None) -> list[str]:
    if signals is None:
        return [spec.name for spec in SIGNAL_SPECS]
    names = list(dict.fromkeys(str(name) for name in signals))
    unknown = [name for name in names if name not in _SPEC_BY_NAME]
    if unknown:
        raise ValueError(f"Unknown research signals: {unknown}. Available: {sorted(_SPEC_BY_NAME)}")
    if not names:
        raise ValueError("At least one research signal is required")
    return names


def _validate_prices(prices: pd.DataFrame) -> pd.DataFrame:
    if prices is None or prices.empty:
        raise ValueError("signal research requires a non-empty price panel")
    if prices.shape[1] < 3:
        raise ValueError("signal research needs at least 3 symbols")
    out = prices.sort_index().astype(float)
    if not out.index.is_unique:
        out = out[~out.index.duplicated(keep="last")]
    return out


def build_signal_library(
    prices: pd.DataFrame,
    params: dict[str, Any] | None = None,
    signals: Iterable[str] | None = None,
) -> SignalLibraryOutput:
    """Build the registered trailing-only cross-sectional signal panels."""

    params = params or {}
    prices = _validate_prices(prices)
    selected = _selected_signal_names(signals)

    beta_window = _positive_int(params, "beta_window", 63, minimum=10)
    reversal_lookback = _positive_int(params, "reversal_lookback", 5)
    momentum_lookback = _positive_int(params, "momentum_lookback", 126, minimum=5)
    momentum_skip = _positive_int(params, "momentum_skip", 21, minimum=0)
    vol_window = _positive_int(params, "vol_window", 63, minimum=10)

    medium_lookback = _positive_int(params, "medium_momentum_lookback", 63, minimum=10)
    medium_skip = _positive_int(params, "medium_momentum_skip", 5, minimum=0)
    consistency_window = _positive_int(params, "consistency_window", 63, minimum=10)
    high_window = _positive_int(params, "high_window", 252, minimum=20)
    vol_short = _positive_int(params, "vol_short_window", 21, minimum=5)
    vol_long = _positive_int(params, "vol_long_window", 126, minimum=20)
    downside_window = _positive_int(params, "downside_window", 63, minimum=10)
    if vol_short >= vol_long:
        raise ValueError("vol_short_window must be smaller than vol_long_window")

    returns = prices.pct_change(fill_method=None)
    universe, beta = _rolling_beta(returns, beta_window)
    residual = returns - beta.mul(universe, axis=0)

    raw: dict[str, pd.DataFrame] = {}
    raw["residual_reversal"] = -residual.rolling(
        reversal_lookback, min_periods=reversal_lookback
    ).sum()
    raw["one_day_reversal"] = -residual
    raw["residual_momentum"] = residual.shift(momentum_skip).rolling(
        momentum_lookback, min_periods=momentum_lookback
    ).sum()
    raw["medium_residual_momentum"] = residual.shift(medium_skip).rolling(
        medium_lookback, min_periods=medium_lookback
    ).sum()

    trend_mean = residual.rolling(consistency_window, min_periods=consistency_window).mean()
    trend_sd = residual.rolling(consistency_window, min_periods=consistency_window).std()
    raw["trend_consistency"] = trend_mean.div(trend_sd.replace(0.0, np.nan)) * np.sqrt(
        consistency_window
    )

    trailing_high = prices.rolling(high_window, min_periods=high_window).max()
    raw["high_proximity"] = prices.div(trailing_high.replace(0.0, np.nan)) - 1.0

    raw["low_idio_vol"] = -residual.rolling(vol_window, min_periods=vol_window).std()
    short_vol = residual.rolling(vol_short, min_periods=vol_short).std()
    long_vol = residual.rolling(vol_long, min_periods=vol_long).std()
    raw["volatility_compression"] = -short_vol.div(long_vol.replace(0.0, np.nan))

    downside = residual.clip(upper=0.0).pow(2)
    raw["downside_resilience"] = -np.sqrt(
        downside.rolling(downside_window, min_periods=downside_window).mean()
    )

    components = {name: _rank_score(raw[name]) for name in selected}
    return SignalLibraryOutput(components=components, beta=beta, residual_returns=residual)


def forward_returns(prices: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Forward close-to-close return known only after ``horizon`` future bars."""
    horizon = int(horizon)
    if horizon < 1:
        raise ValueError("forward horizon must be >= 1")
    return prices.shift(-horizon).div(prices) - 1.0


def cross_sectional_ic(
    signal: pd.DataFrame,
    future_returns: pd.DataFrame,
    min_names: int = 3,
) -> pd.Series:
    """Daily Spearman rank information coefficient across the universe.

    Pandas ranks the full cross-section in C-backed vectorized operations; the
    row-wise correlation then avoids Python loops over dates, which matters for
    broad research universes and several forward horizons.
    """

    min_names = max(3, int(min_names))
    common_index = signal.index.intersection(future_returns.index)
    common_columns = signal.columns.intersection(future_returns.columns)
    s = signal.loc[common_index, common_columns]
    r = future_returns.loc[common_index, common_columns]
    valid = s.notna() & r.notna() & np.isfinite(s) & np.isfinite(r)
    s_rank = s.where(valid).rank(axis=1, method="average", pct=True)
    r_rank = r.where(valid).rank(axis=1, method="average", pct=True)
    count = valid.sum(axis=1)
    ic = s_rank.corrwith(r_rank, axis=1)
    return ic.where(count >= min_names)


def cross_sectional_spread(
    signal: pd.DataFrame,
    future_returns: pd.DataFrame,
    min_names: int = 5,
    quantile: float = 0.2,
) -> pd.Series:
    """Daily top-minus-bottom signal-bucket forward return spread."""

    if not 0 < quantile < 0.5:
        raise ValueError("quantile must be between 0 and 0.5")
    min_names = max(3, int(min_names))
    common_index = signal.index.intersection(future_returns.index)
    common_columns = signal.columns.intersection(future_returns.columns)
    s = signal.loc[common_index, common_columns]
    r = future_returns.loc[common_index, common_columns]
    valid = s.notna() & r.notna() & np.isfinite(s) & np.isfinite(r)
    count = valid.sum(axis=1)
    order = s.where(valid).rank(axis=1, method="first", ascending=True)
    bucket = np.ceil(count * quantile).clip(lower=1)
    bottom_mask = order.le(bucket, axis=0)
    top_cutoff = count - bucket
    top_mask = order.gt(top_cutoff, axis=0)
    spread = r.where(top_mask).mean(axis=1) - r.where(bottom_mask).mean(axis=1)
    return spread.where(count >= min_names)


def _t_stat(values: pd.Series) -> float | None:
    clean = values.dropna()
    if len(clean) < 2:
        return None
    sd = float(clean.std(ddof=1))
    if not np.isfinite(sd) or sd < 1e-12:
        return None
    return float(clean.mean() / (sd / np.sqrt(len(clean))))


def _score_turnover(signal: pd.DataFrame) -> float:
    ranks = signal.rank(axis=1, pct=True, method="average")
    daily = ranks.diff().abs().mean(axis=1).dropna()
    return float(daily.mean()) if not daily.empty else 0.0


def _coverage(signal: pd.DataFrame, future: pd.DataFrame) -> float:
    eligible = future.notna()
    denom = int(eligible.sum().sum())
    if denom == 0:
        return 0.0
    valid = signal.notna() & eligible
    return float(valid.sum().sum() / denom)


def _fold_windows(
    index: pd.Index,
    train_days: int,
    test_days: int,
    purge_days: int,
    horizon: int,
) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    start = 0
    span = train_days + purge_days + test_days
    while start + span <= len(index):
        train_start = start
        train_end = start + train_days
        test_start = train_end + purge_days
        test_end = test_start + test_days
        # Labels must finish inside the test block so folds do not borrow the
        # next block's outcome data.
        eval_end = max(test_start, test_end - horizon)
        if eval_end > test_start:
            windows.append(
                {
                    "train_start": train_start,
                    "train_end": train_end,
                    "test_start": test_start,
                    "test_end": test_end,
                    "eval_index": index[test_start:eval_end],
                }
            )
        start += test_days
    return windows


def _metric_block(ic: pd.Series, spread: pd.Series, horizon: int = 1) -> dict[str, Any]:
    clean_ic = ic.dropna()
    clean_spread = spread.reindex(clean_ic.index).dropna()
    t_stat = _t_stat(clean_ic)
    # An h-day forward return overlaps its neighbours for h-1 bars, so daily
    # ICs at horizon h are strongly autocorrelated and the full-series t-stat
    # overstates significance.  The p-value that feeds false-discovery control
    # is therefore computed on every h-th IC observation only — a plain t-test
    # is honest on that non-overlapping subsample.  The descriptive t-stat
    # above keeps the full series.
    step = max(1, int(horizon))
    non_overlapping = clean_ic.iloc[::step]
    p_value = one_sided_positive_p_value(_t_stat(non_overlapping), len(non_overlapping))
    return {
        "observations": int(len(clean_ic)),
        "mean_ic": None if clean_ic.empty else round(float(clean_ic.mean()), 6),
        "ic_t_stat": None if t_stat is None else round(t_stat, 4),
        "ic_p_value": None if p_value is None else round(float(p_value), 8),
        "ic_p_value_observations": int(len(non_overlapping)),
        "ic_hit_rate": None
        if clean_ic.empty
        else round(float((clean_ic > 0).mean()), 6),
        "mean_spread": None
        if clean_spread.empty
        else round(float(clean_spread.mean()), 8),
    }


def research_signal_suite(
    prices: pd.DataFrame,
    params: dict[str, Any] | None = None,
    signals: Iterable[str] | None = None,
    horizons: Iterable[int] = (1, 5, 10, 21),
    primary_horizon: int = 5,
    train_days: int = 252,
    test_days: int = 63,
    purge_days: int = 5,
    min_names: int = 3,
    min_oos_ic: float = 0.01,
    min_oos_t_stat: float = 0.5,
    min_positive_folds: float = 0.5,
    min_coverage: float = 0.5,
    min_oos_observations: int = 30,
    library: SignalLibraryOutput | None = None,
    signal_specs: dict[str, SignalSpec] | None = None,
    groups: Mapping[str, Any] | pd.Series | None = None,
    group_label: str | None = None,
    min_group_names: int = 2,
    fdr_alpha: float = 0.10,
    redundancy_threshold: float = 0.80,
    redundancy_min_overlap: int = 100,
) -> dict[str, Any]:
    """Evaluate every requested signal independently, with rolling OOS blocks.

    Once a library contains many related ideas, raw IC significance is not
    enough.  The optional research controls are deliberately one-way hurdles:

    * group-neutral evidence must pass *in addition to* the raw evidence,
    * Benjamini-Hochberg controls the false-discovery rate across all signals,
    * highly correlated survivors compete for one representative per cluster.

    None of these controls can promote a signal that failed the original OOS
    gates.
    """

    prices = _validate_prices(prices)
    horizons = tuple(dict.fromkeys(int(h) for h in horizons))
    if not horizons or any(h < 1 or h > 63 for h in horizons):
        raise ValueError("horizons must contain integers from 1 to 63")
    primary_horizon = int(primary_horizon)
    if primary_horizon not in horizons:
        horizons = tuple(sorted(set(horizons + (primary_horizon,))))
    train_days = int(train_days)
    test_days = int(test_days)
    purge_days = int(purge_days)
    if train_days < 60 or test_days < 10 or purge_days < 0:
        raise ValueError("train_days >= 60, test_days >= 10, and purge_days >= 0 are required")
    if len(prices) < train_days + purge_days + test_days:
        raise ValueError(
            "Not enough history for one signal-research fold: need at least "
            f"{train_days + purge_days + test_days} bars, got {len(prices)}"
        )
    min_group_names = max(2, int(min_group_names))
    fdr_alpha = float(fdr_alpha)
    redundancy_threshold = float(redundancy_threshold)
    redundancy_min_overlap = max(2, int(redundancy_min_overlap))
    if not 0.0 < fdr_alpha <= 1.0:
        raise ValueError("fdr_alpha must be > 0 and <= 1")
    if not 0.0 <= redundancy_threshold <= 1.0:
        raise ValueError("redundancy_threshold must be between 0 and 1")

    if library is None:
        library = build_signal_library(prices, params=params, signals=signals)
        spec_map = _SPEC_BY_NAME
    else:
        if signals is not None:
            requested = list(dict.fromkeys(str(name) for name in signals))
            missing = [name for name in requested if name not in library.components]
            if missing:
                raise ValueError(f"Signals unavailable in supplied research library: {missing}")
            library = SignalLibraryOutput(
                components={name: library.components[name] for name in requested},
                beta=library.beta,
                residual_returns=library.residual_returns,
            )
        spec_map = dict(_SPEC_BY_NAME)
        if signal_specs:
            spec_map.update(signal_specs)
        missing_specs = [name for name in library.components if name not in spec_map]
        if missing_specs:
            raise ValueError(f"Missing signal metadata for: {missing_specs}")
    future_by_h = {h: forward_returns(prices, h) for h in horizons}
    windows_by_h = {
        h: _fold_windows(prices.index, train_days, test_days, purge_days, h) for h in horizons
    }
    classification_coverage = group_coverage(prices.columns, groups)
    # A supplied-but-empty mapping means neutralization was requested but the
    # classifier failed.  Keep the hurdle enabled so research fails closed
    # instead of silently reverting to raw IC.
    group_enabled = groups is not None
    group_name = str(group_label or "group") if group_enabled else None

    reports: list[dict[str, Any]] = []
    for name, component in library.components.items():
        by_horizon: dict[str, Any] = {}
        neutral_by_horizon: dict[str, Any] = {}
        primary_fold_rows: list[dict[str, Any]] = []
        primary_neutral_fold_rows: list[dict[str, Any]] = []
        for horizon in horizons:
            future = future_by_h[horizon]
            ic = cross_sectional_ic(component, future, min_names=min_names)
            spread = cross_sectional_spread(
                component, future, min_names=max(3, min_names), quantile=0.2
            )
            neutral_ic = None
            neutral_spread = None
            if group_enabled:
                neutral_ic = group_neutral_ic(
                    component,
                    future,
                    groups,
                    min_names=min_names,
                    min_group_names=min_group_names,
                )
                neutral_spread = group_neutral_spread(
                    component,
                    future,
                    groups,
                    min_names=max(3, min_names),
                    min_group_names=min_group_names,
                    quantile=0.2,
                )
            windows = windows_by_h[horizon]
            oos_mask = pd.Series(False, index=prices.index)
            fold_rows = []
            neutral_fold_rows = []
            for fold_no, window in enumerate(windows, start=1):
                eval_index = window["eval_index"]
                oos_mask.loc[eval_index] = True
                fold_ic = ic.reindex(eval_index)
                fold_spread = spread.reindex(eval_index)
                block = _metric_block(fold_ic, fold_spread, horizon=horizon)
                block.update(
                    {
                        "fold": fold_no,
                        "test_start": str(prices.index[window["test_start"]].date()),
                        "test_end": str(prices.index[window["test_end"] - 1].date()),
                    }
                )
                fold_rows.append(block)
                if group_enabled and neutral_ic is not None and neutral_spread is not None:
                    neutral_block = _metric_block(
                        neutral_ic.reindex(eval_index),
                        neutral_spread.reindex(eval_index),
                        horizon=horizon,
                    )
                    neutral_block.update(
                        {
                            "fold": fold_no,
                            "test_start": str(prices.index[window["test_start"]].date()),
                            "test_end": str(prices.index[window["test_end"] - 1].date()),
                        }
                    )
                    neutral_fold_rows.append(neutral_block)

            oos_ic = ic.reindex(prices.index).where(oos_mask)
            oos_spread = spread.reindex(prices.index).where(oos_mask)
            metrics = _metric_block(oos_ic, oos_spread, horizon=horizon)
            positive_folds = [
                row["mean_ic"] > 0
                for row in fold_rows
                if row["mean_ic"] is not None and row["observations"] > 0
            ]
            metrics["positive_fold_rate"] = (
                None if not positive_folds else round(float(np.mean(positive_folds)), 6)
            )
            metrics["folds"] = len(fold_rows)
            by_horizon[str(horizon)] = metrics
            if group_enabled and neutral_ic is not None and neutral_spread is not None:
                neutral_oos_ic = neutral_ic.reindex(prices.index).where(oos_mask)
                neutral_oos_spread = neutral_spread.reindex(prices.index).where(oos_mask)
                neutral_metrics = _metric_block(
                    neutral_oos_ic, neutral_oos_spread, horizon=horizon
                )
                neutral_positive_folds = [
                    row["mean_ic"] > 0
                    for row in neutral_fold_rows
                    if row["mean_ic"] is not None and row["observations"] > 0
                ]
                neutral_metrics["positive_fold_rate"] = (
                    None
                    if not neutral_positive_folds
                    else round(float(np.mean(neutral_positive_folds)), 6)
                )
                neutral_metrics["folds"] = len(neutral_fold_rows)
                neutral_by_horizon[str(horizon)] = neutral_metrics
            if horizon == primary_horizon:
                primary_fold_rows = fold_rows
                primary_neutral_fold_rows = neutral_fold_rows

        primary = by_horizon[str(primary_horizon)]
        coverage = _coverage(component, future_by_h[primary_horizon])
        turnover = _score_turnover(component)
        mean_ic = primary["mean_ic"]
        t_stat = primary["ic_t_stat"]
        positive_fold_rate = primary["positive_fold_rate"]
        enough_evidence = primary["observations"] >= int(min_oos_observations)
        raw_validated = bool(
            enough_evidence
            and mean_ic is not None
            and mean_ic >= float(min_oos_ic)
            and t_stat is not None
            and t_stat >= float(min_oos_t_stat)
            and positive_fold_rate is not None
            and positive_fold_rate >= float(min_positive_folds)
            and coverage >= float(min_coverage)
        )

        neutral_primary = neutral_by_horizon.get(str(primary_horizon)) if group_enabled else None
        neutral_validated = True
        if group_enabled:
            n_mean = None if neutral_primary is None else neutral_primary["mean_ic"]
            n_t = None if neutral_primary is None else neutral_primary["ic_t_stat"]
            n_fold = None if neutral_primary is None else neutral_primary["positive_fold_rate"]
            n_obs = 0 if neutral_primary is None else neutral_primary["observations"]
            neutral_validated = bool(
                classification_coverage >= float(min_coverage)
                and n_obs >= int(min_oos_observations)
                and n_mean is not None
                and n_mean >= float(min_oos_ic)
                and n_t is not None
                and n_t >= float(min_oos_t_stat)
                and n_fold is not None
                and n_fold >= float(min_positive_folds)
            )
        base_validated = bool(raw_validated and neutral_validated)

        effective_mean = float(mean_ic or 0.0)
        effective_t = float(t_stat or 0.0)
        effective_stability = float(positive_fold_rate or 0.0)
        if group_enabled:
            effective_mean = min(effective_mean, float((neutral_primary or {}).get("mean_ic") or 0.0))
            effective_t = min(effective_t, float((neutral_primary or {}).get("ic_t_stat") or 0.0))
            effective_stability = min(
                effective_stability,
                float((neutral_primary or {}).get("positive_fold_rate") or 0.0),
            )
        ic_strength = max(effective_mean, 0.0)
        evidence = max(min(effective_t, 3.0), 0.0) / 3.0
        stability = max(effective_stability, 0.0)
        turnover_penalty = 1.0 / (1.0 + 2.0 * max(turnover, 0.0))
        research_score = ic_strength * (0.5 + 0.5 * evidence) * (
            0.5 + 0.5 * stability
        ) * turnover_penalty

        spec = spec_map[name]
        reports.append(
            {
                "name": name,
                "family": spec.family,
                "source": spec.source,
                "description": spec.description,
                "status": "pending_controls",
                "validated": False,
                "base_validated": base_validated,
                "raw_validated": raw_validated,
                "group_neutral_validated": neutral_validated if group_enabled else None,
                "research_score": round(float(research_score), 8),
                "coverage": round(float(coverage), 6),
                "score_turnover": round(float(turnover), 6),
                "primary_horizon": primary_horizon,
                "primary": primary,
                "decay": by_horizon,
                "folds": primary_fold_rows,
                "group_neutral_primary": neutral_primary,
                "group_neutral_decay": neutral_by_horizon if group_enabled else None,
                "group_neutral_folds": primary_neutral_fold_rows if group_enabled else None,
                "_effective_p_value": max(
                    float(primary.get("ic_p_value") if primary.get("ic_p_value") is not None else 1.0),
                    float(
                        (neutral_primary or {}).get("ic_p_value")
                        if group_enabled and (neutral_primary or {}).get("ic_p_value") is not None
                        else (1.0 if group_enabled else 0.0)
                    ),
                ),
                "_enough_evidence": enough_evidence,
            }
        )

    # Multiple-testing control is applied across every tested hypothesis, not
    # only the attractive ones.  Group-neutral p-values are combined
    # conservatively by taking the worse p-value, so group metadata can never
    # improve a signal's significance.
    p_values = {row["name"]: row["_effective_p_value"] for row in reports}
    q_values = benjamini_hochberg(p_values)

    correlation = signal_correlation_matrix(
        library.components, min_overlap=redundancy_min_overlap
    )
    clusters = correlation_clusters(correlation, threshold=redundancy_threshold)
    fdr_eligible = {
        row["name"]: bool(
            row["base_validated"]
            and q_values.get(row["name"]) is not None
            and float(q_values[row["name"]]) <= fdr_alpha
        )
        for row in reports
    }
    representatives = choose_cluster_representatives(
        clusters,
        score_by_signal={row["name"]: row["research_score"] for row in reports},
        q_by_signal=q_values,
        eligible_by_signal=fdr_eligible,
    )
    cluster_id: dict[str, int] = {}
    for cid, members in enumerate(clusters, start=1):
        for name in members:
            cluster_id[name] = cid

    for row in reports:
        name = row["name"]
        q_value = q_values.get(name)
        fdr_pass = bool(q_value is not None and float(q_value) <= fdr_alpha)
        representative = representatives.get(name, name)
        redundancy_pass = bool(name == representative or not fdr_eligible.get(name, False))
        final_validated = bool(row["base_validated"] and fdr_pass and redundancy_pass)
        reasons: list[str] = []
        if not row["raw_validated"]:
            reasons.append("raw_oos_gate")
        if group_enabled and not row["group_neutral_validated"]:
            reasons.append(f"{group_name}_neutral_gate")
        if row["base_validated"] and not fdr_pass:
            reasons.append("false_discovery_rate")
        if row["base_validated"] and fdr_pass and name != representative:
            reasons.append(f"redundant_with:{representative}")

        row["validated"] = final_validated
        row["status"] = (
            "validated"
            if final_validated
            else "watch"
            if row["_enough_evidence"] and (row["primary"].get("mean_ic") or 0.0) > 0
            else "reject"
        )
        row["exclusion_reasons"] = reasons
        row["fdr"] = {
            "method": "benjamini_hochberg",
            "alpha": round(fdr_alpha, 6),
            "p_value": round(float(row["_effective_p_value"]), 8),
            "q_value": None if q_value is None else round(float(q_value), 8),
            "passed": fdr_pass,
        }
        corr_to_rep = None
        if representative in correlation.index and name in correlation.index:
            value = correlation.loc[name, representative]
            corr_to_rep = None if pd.isna(value) else float(abs(value))
        row["redundancy"] = {
            "cluster_id": cluster_id.get(name),
            "representative": representative,
            "absolute_correlation_to_representative": None
            if corr_to_rep is None
            else round(corr_to_rep, 6),
            "passed": redundancy_pass,
        }
        row.pop("_effective_p_value", None)
        row.pop("_enough_evidence", None)

    reports.sort(key=lambda row: row["research_score"], reverse=True)
    valid_rows = [row for row in reports if row["validated"] and row["research_score"] > 0]
    total_score = sum(row["research_score"] for row in valid_rows)
    blend = [
        {
            "signal": row["name"],
            "weight": round(row["research_score"] / total_score, 6),
        }
        for row in valid_rows
    ] if total_score > 0 else []

    cluster_rows = []
    for cid, members in enumerate(clusters, start=1):
        rep = representatives.get(members[0], members[0]) if members else None
        cluster_rows.append(
            {
                "cluster_id": cid,
                "representative": rep,
                "members": members,
                "size": len(members),
            }
        )

    return {
        "as_of": str(prices.index[-1].date()),
        "symbols": list(prices.columns),
        "bars": len(prices),
        "primary_horizon": primary_horizon,
        "horizons": list(horizons),
        "fold_config": {
            "train_days": train_days,
            "test_days": test_days,
            "purge_days": purge_days,
            "folds": len(windows_by_h[primary_horizon]),
        },
        "validation": {
            "min_oos_ic": min_oos_ic,
            "min_oos_t_stat": min_oos_t_stat,
            "min_positive_folds": min_positive_folds,
            "min_coverage": min_coverage,
            "min_oos_observations": min_oos_observations,
        },
        "research_controls": {
            "group_neutralization": {
                "enabled": group_enabled,
                "label": group_name,
                "classification_coverage": round(classification_coverage, 6),
                "min_group_names": min_group_names,
                "rule": "raw_and_group_neutral_must_both_pass" if group_enabled else "raw_only",
            },
            "false_discovery": {
                "method": "benjamini_hochberg",
                "alpha": round(fdr_alpha, 6),
                "hypotheses": len([v for v in q_values.values() if v is not None]),
                "passed": sum(
                    1 for value in q_values.values() if value is not None and float(value) <= fdr_alpha
                ),
            },
            "redundancy": {
                "absolute_correlation_threshold": round(redundancy_threshold, 6),
                "min_overlap": redundancy_min_overlap,
                "clusters": cluster_rows,
                "surviving_representatives": [row["name"] for row in valid_rows],
            },
        },
        "signals": reports,
        "recommended_blend": blend,
    }


def _historical_signal_quality(
    components: dict[str, pd.DataFrame],
    prices: pd.DataFrame,
    horizon: int,
    window: int,
    min_periods: int,
    min_names: int,
    min_ic: float,
    min_t_stat: float,
    max_signals: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Trailing IC quality and normalized signal weights with label delay.

    ``IC[t]`` uses the return from t through t+h, so it is not observable until
    t+h.  Rolling evidence is therefore shifted forward by ``horizon`` bars
    before it can influence the live blend.
    """

    future = forward_returns(prices, horizon)
    raw_quality: dict[str, pd.Series] = {}
    reported_quality: dict[str, pd.Series] = {}
    for name, component in components.items():
        ic = cross_sectional_ic(component, future, min_names=min_names)
        rolling_mean = ic.rolling(window, min_periods=min_periods).mean()
        rolling_sd = ic.rolling(window, min_periods=min_periods).std(ddof=1)
        rolling_n = ic.rolling(window, min_periods=min_periods).count()
        t_stat = rolling_mean.div(rolling_sd.replace(0.0, np.nan) / np.sqrt(rolling_n))

        known_mean = rolling_mean.shift(horizon)
        known_t = t_stat.shift(horizon)
        eligible = (known_mean >= min_ic) & (known_t.fillna(0.0) >= min_t_stat)
        # IC is the primary economic evidence.  T-stat boosts persistent edges
        # but is capped so one quiet window cannot monopolize the blend.
        raw = known_mean.clip(lower=0.0) * (1.0 + known_t.clip(lower=0.0, upper=3.0))
        raw_quality[name] = raw.where(eligible, 0.0).fillna(0.0)
        reported_quality[name] = known_mean

    raw_frame = pd.DataFrame(raw_quality, index=prices.index).fillna(0.0)
    quality = pd.DataFrame(reported_quality, index=prices.index)
    if max_signals > 0 and max_signals < len(raw_frame.columns):
        rank = raw_frame.rank(axis=1, method="first", ascending=False)
        raw_frame = raw_frame.where(rank <= max_signals, 0.0)
    denom = raw_frame.sum(axis=1).replace(0.0, np.nan)
    signal_weights = raw_frame.div(denom, axis=0).fillna(0.0)
    return quality, signal_weights


def build_adaptive_stat_arb(
    prices: pd.DataFrame,
    params: dict[str, Any] | None = None,
) -> AdaptiveStatArbOutput:
    """Build a stat-arb book whose signal mix is validated on trailing IC only."""

    params = params or {}
    prices = _validate_prices(prices)
    selected = params.get("research_signals")
    library = build_signal_library(prices, params=params, signals=selected)

    horizon = _positive_int(params, "quality_horizon", 5)
    quality_window = _positive_int(params, "quality_window", 126, minimum=20)
    quality_min_periods = _positive_int(params, "quality_min_periods", 40, minimum=10)
    if quality_min_periods > quality_window:
        raise ValueError("quality_min_periods must be <= quality_window")
    min_names = _positive_int(params, "quality_min_names", 3, minimum=3)
    max_signals = _positive_int(params, "max_active_signals", 4, minimum=1)
    min_ic = float(params.get("min_signal_ic", 0.0))
    min_t_stat = float(params.get("min_signal_t_stat", 0.0))
    gross_target = float(params.get("gross_target", 1.0))
    if not 0 <= gross_target <= 1.0:
        raise ValueError("gross_target must be between 0 and 1.0")
    smooth_span = _positive_int(params, "smooth_span", 3)
    rebalance_days = _positive_int(params, "rebalance_days", 5)

    quality, signal_weights = _historical_signal_quality(
        library.components,
        prices,
        horizon=horizon,
        window=quality_window,
        min_periods=quality_min_periods,
        min_names=min_names,
        min_ic=min_ic,
        min_t_stat=min_t_stat,
        max_signals=max_signals,
    )

    smoothed = {
        name: (
            component.ewm(span=smooth_span, adjust=False, min_periods=1).mean()
            if smooth_span > 1
            else component
        )
        for name, component in library.components.items()
    }
    score = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    for name, component in smoothed.items():
        score = score.add(component.fillna(0.0).mul(signal_weights[name], axis=0), fill_value=0.0)

    raw_weights = _neutral_weights(score, library.beta, gross_target)
    # No validated signal means no position, rather than a stale neutralized
    # score leaking through a period where the quality gate is closed.
    active = signal_weights.sum(axis=1) > 1e-12
    raw_weights = raw_weights.where(active, 0.0)
    schedule = _rebalance_schedule(prices.index, rebalance_days)
    weights = _rebalance(raw_weights, schedule)
    weights = weights.reindex(index=prices.index, columns=prices.columns).fillna(0.0)

    return AdaptiveStatArbOutput(
        weights=weights,
        score=score,
        components=library.components,
        beta=library.beta,
        signal_weights=signal_weights,
        signal_quality=quality,
        decision_weights=raw_weights,
        schedule=schedule,
        rebalance_days=rebalance_days,
        quality_horizon=horizon,
    )


def adaptive_stat_arb_snapshot(
    prices: pd.DataFrame,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Latest adaptive target plus the trailing quality assigned to each signal."""

    output = build_adaptive_stat_arb(prices, params)
    current_dt = output.weights.index[-1]
    w = output.weights.loc[current_dt]
    if float(w.abs().sum()) <= 1e-12:
        raise ValueError(
            "No research signal had enough positive trailing evidence at the latest "
            "rebalance; use more history or looser gates"
        )
    # The held target is whatever the most recent scheduled rebalance decided
    # (it is non-zero here, or ``w`` would be flat).
    signal_dt = output.schedule.index[output.schedule.to_numpy().nonzero()[0][-1]]
    beta_then = output.beta.loc[signal_dt]
    beta_now = output.beta.loc[current_dt]
    score = output.score.loc[signal_dt]
    positions = []
    for symbol in output.weights.columns:
        weight = float(w.get(symbol, 0.0))
        positions.append(
            {
                "symbol": symbol,
                "side": "long" if weight > 1e-10 else "short" if weight < -1e-10 else "flat",
                "weight": round(weight, 8),
                "score": None if pd.isna(score.get(symbol)) else round(float(score[symbol]), 8),
                "beta": None
                if pd.isna(beta_then.get(symbol))
                else round(float(beta_then[symbol]), 8),
            }
        )

    signal_rows = []
    for name in output.signal_weights.columns:
        q = output.signal_quality.loc[signal_dt, name]
        signal_rows.append(
            {
                "signal": name,
                "blend_weight": round(float(output.signal_weights.loc[signal_dt, name]), 8),
                "trailing_ic": None if pd.isna(q) else round(float(q), 8),
            }
        )
    signal_rows.sort(key=lambda row: row["blend_weight"], reverse=True)

    return {
        "as_of": str(current_dt.date()),
        "signal_as_of": str(signal_dt.date()),
        "quality_horizon": output.quality_horizon,
        "gross_exposure": round(float(w.abs().sum()), 8),
        "net_exposure": round(float(w.sum()), 8),
        "beta_exposure": round(float((w * beta_now.fillna(0.0)).sum()), 8),
        "beta_exposure_at_signal": round(float((w * beta_then.fillna(0.0)).sum()), 8),
        "signals": signal_rows,
        "positions": sorted(positions, key=lambda row: abs(row["weight"]), reverse=True),
        "params": dict(params or {}),
    }
