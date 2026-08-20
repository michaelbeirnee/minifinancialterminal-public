"""Execution-aware diagnostics for cross-sectional signal research.

The research layer first asks whether a signal predicts returns.  This module
asks the second question: could the signal plausibly survive implementation at
a stated capital base using only information known at the decision date?

The inputs are intentionally simple and point in time:
* adjusted close prices,
* dated volume for rolling dollar ADV,
* dated high/low bars for a Corwin-Schultz spread proxy,
* trailing close-to-close volatility for a square-root impact estimate.

These diagnostics are not a substitute for venue-level quotes or an execution
simulator.  They are a conservative filter that prevents a high-turnover idea
from looking attractive solely because the research report ignored trading
friction and capacity.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .stat_arb import _neutral_weights


@dataclass(frozen=True)
class ExecutionPanels:
    adv_dollars: pd.DataFrame
    spread_bps: pd.DataFrame
    volatility_bps: pd.DataFrame
    source_status: dict[str, Any]


def _aligned_panel(
    value: pd.DataFrame | None,
    index: pd.Index,
    columns: pd.Index,
) -> pd.DataFrame:
    if value is None or value.empty:
        return pd.DataFrame(np.nan, index=index, columns=columns, dtype=float)
    out = value.copy()
    out.index = pd.DatetimeIndex(pd.to_datetime(out.index)).tz_localize(None).normalize()
    out = out[~out.index.duplicated(keep="last")]
    out.columns = [str(c).upper() for c in out.columns]
    return out.reindex(index=index, columns=columns).apply(pd.to_numeric, errors="coerce")


def corwin_schultz_spread_bps(
    high: pd.DataFrame,
    low: pd.DataFrame,
    smooth_window: int = 5,
) -> pd.DataFrame:
    """Estimate an effective bid/ask spread from daily high/low observations.

    Corwin-Schultz is useful here because the free historical feed lacks dated
    bid/ask quotes.  Negative estimates are floored at zero and the daily proxy
    is smoothed to reduce single-bar noise.  The output is a *full* spread in
    basis points; execution charges half of it for a one-way trade.
    """

    high = high.astype(float)
    low = low.astype(float).where(lambda x: x > 0)
    ratio = high.div(low).where(lambda x: x >= 1.0)
    log_hl = np.log(ratio)
    beta = log_hl.pow(2) + log_hl.shift(1).pow(2)
    two_day_high = high.combine(high.shift(1), np.maximum)
    two_day_low = low.combine(low.shift(1), np.minimum)
    gamma = np.log(two_day_high.div(two_day_low).where(lambda x: x >= 1.0)).pow(2)

    denom = 3.0 - 2.0 * np.sqrt(2.0)
    alpha = (
        (np.sqrt(2.0 * beta) - np.sqrt(beta)) / denom
        - np.sqrt(gamma / denom)
    ).clip(lower=0.0)
    exp_alpha = np.exp(alpha.clip(upper=5.0))
    spread = 2.0 * (exp_alpha - 1.0) / (1.0 + exp_alpha)
    spread_bps = (spread * 10_000.0).clip(lower=0.0, upper=500.0)
    window = max(1, int(smooth_window))
    if window > 1:
        spread_bps = spread_bps.rolling(window, min_periods=1).median()
    return spread_bps


def build_execution_panels(
    prices: pd.DataFrame,
    feature_panels: Mapping[str, pd.DataFrame] | None = None,
    *,
    adv_window: int = 20,
    vol_window: int = 20,
    spread_window: int = 5,
) -> ExecutionPanels:
    """Create point-in-time liquidity and cost panels aligned to ``prices``."""

    prices = prices.sort_index().astype(float)
    columns = pd.Index([str(c).upper() for c in prices.columns])
    prices = prices.copy()
    prices.columns = columns
    feature_panels = feature_panels or {}

    volume = _aligned_panel(feature_panels.get("volume"), prices.index, columns)
    high = _aligned_panel(feature_panels.get("high"), prices.index, columns)
    low = _aligned_panel(feature_panels.get("low"), prices.index, columns)

    adv_window = max(5, int(adv_window))
    vol_window = max(5, int(vol_window))
    dollar_volume = volume * prices
    adv = dollar_volume.rolling(adv_window, min_periods=max(5, adv_window // 2)).median()

    if high.notna().any().any() and low.notna().any().any():
        spread_bps = corwin_schultz_spread_bps(high, low, smooth_window=spread_window)
        spread_mode = "corwin_schultz_daily_high_low_proxy"
    else:
        spread_bps = pd.DataFrame(np.nan, index=prices.index, columns=columns, dtype=float)
        spread_mode = "unavailable"

    returns = prices.pct_change(fill_method=None)
    vol_bps = returns.rolling(vol_window, min_periods=max(5, vol_window // 2)).std() * 10_000.0

    volume_coverage = float(volume.notna().sum().sum() / max(1, volume.size))
    spread_coverage = float(spread_bps.notna().sum().sum() / max(1, spread_bps.size))
    return ExecutionPanels(
        adv_dollars=adv,
        spread_bps=spread_bps,
        volatility_bps=vol_bps,
        source_status={
            "available": volume_coverage > 0.0,
            "volume_coverage": round(volume_coverage, 6),
            "spread_coverage": round(spread_coverage, 6),
            "spread_estimator": spread_mode,
            "adv_window": adv_window,
            "vol_window": vol_window,
            "spread_window": max(1, int(spread_window)),
            "point_in_time": True,
        },
    )


def signal_target_weights(
    signal: pd.DataFrame,
    quantile: float = 0.20,
    min_names: int = 5,
) -> pd.DataFrame:
    """Unit-gross, dollar-neutral top/bottom targets derived from a signal."""

    if not 0.0 < float(quantile) < 0.5:
        raise ValueError("execution quantile must be between 0 and 0.5")
    min_names = max(3, int(min_names))
    signal = signal.astype(float)
    valid = signal.notna() & np.isfinite(signal)
    count = valid.sum(axis=1)
    order = signal.where(valid).rank(axis=1, method="first", ascending=True)
    bucket = np.ceil(count * float(quantile)).clip(lower=1)
    bottom = order.le(bucket, axis=0)
    top = order.gt(count - bucket, axis=0)

    long_count = top.sum(axis=1).replace(0, np.nan)
    short_count = bottom.sum(axis=1).replace(0, np.nan)
    weights = pd.DataFrame(0.0, index=signal.index, columns=signal.columns)
    weights = weights.mask(top, 0.5).div(long_count, axis=0).where(top, weights)
    short_weight = pd.DataFrame(-0.5, index=signal.index, columns=signal.columns).div(
        short_count, axis=0
    )
    weights = weights.where(~bottom, short_weight)
    return weights.where(count >= min_names, 0.0).fillna(0.0)


def _series_t_stat(values: pd.Series) -> float | None:
    clean = values.replace([np.inf, -np.inf], np.nan).dropna()
    if len(clean) < 2:
        return None
    sd = float(clean.std(ddof=1))
    if not np.isfinite(sd) or sd < 1e-12:
        return None
    return float(clean.mean() / (sd / np.sqrt(len(clean))))


def signal_execution_diagnostics(
    signal: pd.DataFrame,
    future_returns: pd.DataFrame,
    oos_mask: pd.Series,
    panels: ExecutionPanels,
    *,
    capital_dollars: float = 10_000_000.0,
    max_adv_participation: float = 0.05,
    commission_bps: float = 1.0,
    slippage_bps: float = 0.5,
    impact_coefficient: float = 0.10,
    quantile: float = 0.20,
    min_names: int = 5,
) -> dict[str, Any]:
    """Estimate OOS implementation cost and capacity for one signal.

    Returns are for a unit-gross portfolio (0.5 long / 0.5 short).  Costs are
    charged on the daily change in target weights.  Capacity fill is the share
    of desired trade dollars that fits under ``max_adv_participation`` at the
    requested capital base.  Cost is still reported on desired turnover; the
    fill statistic separately prevents an oversized portfolio from passing.
    """

    capital_dollars = float(capital_dollars)
    max_adv_participation = float(max_adv_participation)
    commission_bps = max(0.0, float(commission_bps))
    slippage_bps = max(0.0, float(slippage_bps))
    impact_coefficient = max(0.0, float(impact_coefficient))
    if capital_dollars <= 0:
        raise ValueError("research capital must be positive")
    if not 0.0 < max_adv_participation <= 1.0:
        raise ValueError("max ADV participation must be > 0 and <= 1")

    index = signal.index.intersection(future_returns.index)
    columns = signal.columns.intersection(future_returns.columns)
    signal = signal.loc[index, columns]
    future = future_returns.loc[index, columns]
    weights = signal_target_weights(signal, quantile=quantile, min_names=min_names)
    selected = oos_mask.reindex(index).fillna(False).astype(bool)
    trades = weights.diff().abs()
    if len(trades):
        trades.iloc[0] = weights.iloc[0].abs()
    # Each disconnected OOS block is evaluated from cash.  Otherwise a stable
    # ranking could appear costless simply because the target was formed in the
    # preceding training block.
    oos_entry = selected & ~selected.shift(1, fill_value=False)
    if oos_entry.any():
        trades.loc[oos_entry] = weights.loc[oos_entry].abs()

    adv = panels.adv_dollars.reindex(index=index, columns=columns)
    spread = panels.spread_bps.reindex(index=index, columns=columns)
    vol = panels.volatility_bps.reindex(index=index, columns=columns)

    trade_dollars = trades * capital_dollars
    capacity_dollars = adv * max_adv_participation
    executable_dollars = trade_dollars.combine(capacity_dollars, np.minimum)
    executable_dollars = executable_dollars.where(trade_dollars > 0.0, 0.0)
    desired_by_day = trade_dollars.sum(axis=1)
    executable_by_day = executable_dollars.sum(axis=1, min_count=1)
    fill = executable_by_day.div(desired_by_day.replace(0.0, np.nan)).clip(0.0, 1.0)

    participation = trade_dollars.div(adv.replace(0.0, np.nan)).clip(lower=0.0)
    impact_bps = impact_coefficient * vol.clip(lower=0.0) * np.sqrt(participation.clip(upper=1.0))
    half_spread = (spread / 2.0).fillna(0.0)
    per_dollar_cost_bps = commission_bps + slippage_bps + half_spread + impact_bps.fillna(0.0)
    cost_fraction = (trades * per_dollar_cost_bps / 10_000.0).sum(axis=1, min_count=1)

    gross_return = (weights * future).sum(axis=1, min_count=1)
    net_return = gross_return - cost_fraction
    turnover = trades.sum(axis=1)

    # Portfolio capacity implied by each desired weight change.  A lower-tail
    # statistic is more useful than the absolute minimum, which can be driven
    # by one stale or tiny print.
    capacity_by_trade = capacity_dollars.div(trades.replace(0.0, np.nan))
    capacity_stack = (
        capacity_by_trade.where(selected, axis=0)
        .stack()
        .replace([np.inf, -np.inf], np.nan)
        .dropna()
    )

    active_trade = desired_by_day > 0.0
    selected_active = selected & active_trade
    gross_oos = gross_return.where(selected).dropna()
    net_oos = net_return.where(selected).dropna()
    cost_oos = cost_fraction.where(selected_active).dropna()
    turnover_oos = turnover.where(selected_active).dropna()
    fill_oos = fill.where(selected_active).dropna()
    part_oos = (
        participation.where(trades > 0.0).where(selected, axis=0)
        .stack()
        .replace([np.inf, -np.inf], np.nan)
        .dropna()
    )

    gross_mean = None if gross_oos.empty else float(gross_oos.mean())
    net_mean = None if net_oos.empty else float(net_oos.mean())
    cost_mean = None if cost_oos.empty else float(cost_oos.mean())
    net_t = _series_t_stat(net_oos)

    return {
        "observations": int(len(net_oos)),
        "capital_dollars": round(capital_dollars, 2),
        "max_adv_participation": round(max_adv_participation, 6),
        "gross_return": None if gross_mean is None else round(gross_mean, 8),
        "gross_alpha_bps": None if gross_mean is None else round(gross_mean * 10_000.0, 4),
        "cost_return": None if cost_mean is None else round(cost_mean, 8),
        "cost_bps": None if cost_mean is None else round(cost_mean * 10_000.0, 4),
        "net_return": None if net_mean is None else round(net_mean, 8),
        "net_alpha_bps": None if net_mean is None else round(net_mean * 10_000.0, 4),
        "net_t_stat": None if net_t is None else round(net_t, 4),
        "positive_net_rate": None if net_oos.empty else round(float((net_oos > 0.0).mean()), 6),
        "one_way_turnover": None if turnover_oos.empty else round(float(turnover_oos.mean()), 6),
        "capacity_fill": None if fill_oos.empty else round(float(fill_oos.mean()), 6),
        "avg_participation": None if part_oos.empty else round(float(part_oos.mean()), 6),
        "p95_participation": None if part_oos.empty else round(float(part_oos.quantile(0.95)), 6),
        "capacity_dollars_p10": None
        if capacity_stack.empty
        else round(float(capacity_stack.quantile(0.10)), 2),
        "capacity_dollars_median": None
        if capacity_stack.empty
        else round(float(capacity_stack.quantile(0.50)), 2),
        "cost_assumptions": {
            "commission_bps": round(commission_bps, 6),
            "slippage_bps": round(slippage_bps, 6),
            "impact_coefficient": round(impact_coefficient, 6),
            "spread_charge": "half_corwin_schultz_proxy_per_one_way_trade",
            "impact": "coefficient_x_daily_vol_x_sqrt_participation",
        },
    }


def current_execution_book(
    prices: pd.DataFrame,
    components: Mapping[str, pd.DataFrame],
    beta: pd.DataFrame,
    research_report: Mapping[str, Any],
    panels: ExecutionPanels,
    *,
    capital_dollars: float = 10_000_000.0,
    max_adv_participation: float = 0.05,
    commission_bps: float = 1.0,
    slippage_bps: float = 0.5,
    impact_coefficient: float = 0.10,
    gross_target: float = 1.0,
) -> dict[str, Any]:
    """Turn the cost-filtered research blend into today's executable target.

    Research selection can use the whole historical sample because this helper
    only constructs the *current* target.  It is intentionally not applied
    retrospectively to old bars.  Capacity is enforced by scaling the entire
    neutral book from a flat-start assumption, preserving both dollar and beta
    neutrality instead of clipping names independently.
    """

    blend = list(research_report.get("recommended_blend") or [])
    if not blend:
        return {
            "status": "no_validated_signals",
            "as_of": str(prices.index[-1].date()),
            "positions": [],
            "signals": [],
        }

    selected = [(row["signal"], float(row["weight"])) for row in blend if row.get("signal") in components]
    if not selected:
        return {
            "status": "no_available_blend_components",
            "as_of": str(prices.index[-1].date()),
            "positions": [],
            "signals": [],
        }

    score = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    for name, weight in selected:
        score = score.add(components[name].fillna(0.0) * weight, fill_value=0.0)
    raw_weights = _neutral_weights(score, beta, float(gross_target))
    dt = prices.index[-1]
    raw = raw_weights.loc[dt].fillna(0.0)
    beta_now = beta.reindex(index=prices.index, columns=prices.columns).ffill().loc[dt].fillna(0.0)
    adv = panels.adv_dollars.reindex(index=prices.index, columns=prices.columns).ffill().loc[dt]
    spread = panels.spread_bps.reindex(index=prices.index, columns=prices.columns).ffill().loc[dt]
    vol = panels.volatility_bps.reindex(index=prices.index, columns=prices.columns).ffill().loc[dt]

    active = raw.abs() > 1e-12
    capacity_scale = 1.0
    missing_liquidity = []
    if active.any():
        ratios = []
        for symbol in raw.index[active]:
            adv_value = adv.get(symbol)
            if pd.isna(adv_value) or float(adv_value) <= 0.0:
                missing_liquidity.append(symbol)
                continue
            required = float(abs(raw[symbol]) * capital_dollars)
            if required > 0.0:
                ratios.append(float(max_adv_participation * float(adv_value) / required))
        if missing_liquidity:
            capacity_scale = 0.0
        elif ratios:
            capacity_scale = min(1.0, max(0.0, min(ratios)))

    executable = raw * capacity_scale
    positions: list[dict[str, Any]] = []
    portfolio_entry_cost = 0.0
    for symbol in raw.index:
        target_weight = float(raw[symbol])
        executable_weight = float(executable[symbol])
        adv_value = None if pd.isna(adv.get(symbol)) else float(adv[symbol])
        spread_value = 0.0 if pd.isna(spread.get(symbol)) else max(0.0, float(spread[symbol]))
        vol_value = 0.0 if pd.isna(vol.get(symbol)) else max(0.0, float(vol[symbol]))
        participation = None
        cost_bps = None
        if adv_value is not None and adv_value > 0.0 and abs(executable_weight) > 0.0:
            participation = abs(executable_weight) * capital_dollars / adv_value
            cost_bps = (
                max(0.0, float(commission_bps))
                + max(0.0, float(slippage_bps))
                + spread_value / 2.0
                + max(0.0, float(impact_coefficient)) * vol_value * np.sqrt(min(participation, 1.0))
            )
            portfolio_entry_cost += abs(executable_weight) * cost_bps
        positions.append(
            {
                "symbol": symbol,
                "side": "long" if executable_weight > 1e-10 else "short" if executable_weight < -1e-10 else "flat",
                "score": None if pd.isna(score.loc[dt, symbol]) else round(float(score.loc[dt, symbol]), 8),
                "target_weight": round(target_weight, 8),
                "executable_weight": round(executable_weight, 8),
                "adv_dollars": None if adv_value is None else round(adv_value, 2),
                "entry_participation": None if participation is None else round(float(participation), 6),
                "estimated_one_way_cost_bps": None if cost_bps is None else round(float(cost_bps), 4),
            }
        )

    report_rows = {row.get("name"): row for row in research_report.get("signals", [])}
    signal_rows = []
    weighted_net = 0.0
    weighted_gross = 0.0
    has_net = False
    for name, weight in selected:
        row = report_rows.get(name, {})
        ex = row.get("execution") or {}
        net = ex.get("net_alpha_bps")
        gross = ex.get("gross_alpha_bps")
        if net is not None:
            weighted_net += weight * float(net)
            has_net = True
        if gross is not None:
            weighted_gross += weight * float(gross)
        signal_rows.append(
            {
                "signal": name,
                "blend_weight": round(weight, 6),
                "gross_alpha_bps": gross,
                "net_alpha_bps": net,
                "cost_bps": ex.get("cost_bps"),
                "capacity_fill": ex.get("capacity_fill"),
            }
        )

    gross = float(executable.abs().sum())
    net_exposure = float(executable.sum())
    beta_exposure = float((executable * beta_now).sum())
    return {
        "status": "ready" if capacity_scale > 0.0 else "liquidity_unavailable",
        "as_of": str(dt.date()),
        "assumption": "flat_start_full_rebalance",
        "capital_dollars": round(float(capital_dollars), 2),
        "max_adv_participation": round(float(max_adv_participation), 6),
        "capacity_scale": round(float(capacity_scale), 6),
        "target_gross_exposure": round(float(raw.abs().sum()), 8),
        "executable_gross_exposure": round(gross, 8),
        "net_exposure": round(net_exposure, 8),
        "beta_exposure": round(beta_exposure, 8),
        "estimated_entry_cost_bps_of_capital": round(float(portfolio_entry_cost), 4),
        "blend_gross_alpha_bps": round(weighted_gross, 4),
        "blend_net_alpha_bps": round(weighted_net, 4) if has_net else None,
        "missing_liquidity_symbols": missing_liquidity,
        "signals": signal_rows,
        "positions": sorted(positions, key=lambda row: abs(row["executable_weight"]), reverse=True),
    }
