"""True walk-forward portfolio simulation for the multi-source signal stack.

The signal research endpoint answers "which predictors survive today?".  This
module answers the harder historical question: what would have happened if the
research process itself had been rerun through time and capital had only used
research that was complete at each decision date?

Design
------
* Multi-source signal panels are built once from point-in-time inputs.  Every
  research vintage receives only a prefix of those panels and prices.
* Research is refreshed on a fixed cadence after at least one complete OOS
  block exists.  A portfolio rebalance may only use the latest completed
  research vintage at or before that signal date.
* Current signal values are blended using that frozen vintage's approved
  weights, then projected to dollar/beta-neutral portfolio weights.
* Capacity is applied to the *trade from the existing book* to the new target.
  The whole trade vector is scaled uniformly when an ADV limit binds, avoiding
  name-by-name clipping that would manufacture net exposure.
* The target created from a signal date is held starting one bar later, matching
  the daily backtest engine's no-look-ahead convention.

The historical execution model intentionally remains a daily-bar research
proxy.  It uses dated ADV, Corwin-Schultz spread estimates, trailing volatility,
commissions/slippage and square-root impact; it is not a venue-level fill model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from ..reports.generator import compute_metrics
from .execution_research import ExecutionPanels, build_execution_panels
from .multisource_research import (
    FeaturePanels,
    MultisourceLibraryOutput,
    build_feature_panels,
    build_multisource_signal_library,
)
from .signal_research import SignalLibraryOutput, research_signal_suite
from .stat_arb import _neutral_weights


@dataclass(frozen=True)
class WalkForwardResearchOutput:
    target_weights: pd.DataFrame
    held_weights: pd.DataFrame
    gross_returns: pd.Series
    net_returns: pd.Series
    daily_costs: pd.Series
    gross_equity: pd.Series
    equity: pd.Series
    research_vintages: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    selection_summary: list[dict[str, Any]]
    source_status: dict[str, Any]
    config: dict[str, Any]

    def to_dict(self, max_points: int = 1500) -> dict[str, Any]:
        equity = self.equity
        gross_equity = self.gross_equity
        if len(equity) > max_points:
            step = len(equity) // max_points + 1
            equity = equity.iloc[::step]
            gross_equity = gross_equity.reindex(equity.index)

        decision_frame = pd.DataFrame(self.decisions)
        traded = decision_frame[decision_frame.get("turnover", pd.Series(dtype=float)) > 1e-12] if not decision_frame.empty else decision_frame
        capacity_constrained = (
            int((decision_frame["capacity_scale"] < 0.999999).sum())
            if not decision_frame.empty and "capacity_scale" in decision_frame
            else 0
        )
        avg_capacity = (
            float(decision_frame["capacity_scale"].mean())
            if not decision_frame.empty and "capacity_scale" in decision_frame
            else 1.0
        )
        avg_turnover = (
            float(traded["turnover"].mean())
            if not traded.empty and "turnover" in traded
            else 0.0
        )
        avg_cost_bps = (
            float(traded["cost_bps_of_capital"].mean())
            if not traded.empty and "cost_bps_of_capital" in traded
            else 0.0
        )
        prior_equity = self.equity.shift(1).fillna(float(self.config["initial_capital"]))
        total_cost_dollars = float((self.daily_costs * prior_equity).sum())

        return {
            "engine": "walk_forward_multisource",
            "as_of": str(self.equity.index[-1].date()),
            "metrics": compute_metrics(self.equity),
            "gross_metrics": compute_metrics(self.gross_equity),
            "total_costs": round(total_cost_dollars, 2),
            "total_turnover": round(float(sum(float(row.get("turnover", 0.0)) for row in self.decisions)), 6),
            "equity_curve": {
                "dates": [str(dt.date()) for dt in equity.index],
                "values": [round(float(v), 4) for v in equity.values],
            },
            "gross_equity_curve": {
                "dates": [str(dt.date()) for dt in gross_equity.index],
                "values": [round(float(v), 4) for v in gross_equity.values],
            },
            "research_vintages": self.research_vintages,
            "decisions": self.decisions,
            "selection_summary": self.selection_summary,
            "source_status": self.source_status,
            "simulation": {
                "research_vintages": len(self.research_vintages),
                "portfolio_decisions": len(self.decisions),
                "traded_decisions": int(len(traded)),
                "capacity_constrained_decisions": capacity_constrained,
                "average_capacity_scale": round(avg_capacity, 6),
                "average_one_way_turnover": round(avg_turnover, 6),
                "average_cost_bps_of_capital": round(avg_cost_bps, 4),
                "active_days": int((self.held_weights.abs().sum(axis=1) > 1e-12).sum()),
                "average_gross_exposure": round(float(self.held_weights.abs().sum(axis=1).mean()), 6),
                "max_abs_net_exposure": round(float(self.held_weights.sum(axis=1).abs().max()), 10),
            },
            "config": self.config,
            "methodology": {
                "signal_selection": "latest_completed_research_vintage_only",
                "research_history": "prefix_only_no_future_rows",
                "execution_lag_bars": 1,
                "portfolio_capacity": "uniform_scale_of_delta_to_target",
                "archive_rule": "estimates_and_options_exist_only_after_actual_capture",
            },
        }


def _slice_library(library: SignalLibraryOutput, index: pd.Index) -> SignalLibraryOutput:
    return SignalLibraryOutput(
        components={
            name: frame.reindex(index=index)
            for name, frame in library.components.items()
        },
        beta=library.beta.reindex(index=index),
        residual_returns=library.residual_returns.reindex(index=index),
    )


def _slice_execution(panels: ExecutionPanels | None, index: pd.Index) -> ExecutionPanels | None:
    if panels is None:
        return None
    return ExecutionPanels(
        adv_dollars=panels.adv_dollars.reindex(index=index),
        spread_bps=panels.spread_bps.reindex(index=index),
        volatility_bps=panels.volatility_bps.reindex(index=index),
        source_status=dict(panels.source_status),
    )


def _blend_target(
    dt: pd.Timestamp,
    components: Mapping[str, pd.DataFrame],
    beta: pd.DataFrame,
    blend: list[dict[str, Any]],
    *,
    min_names: int,
    gross_target: float,
) -> tuple[pd.Series, list[dict[str, Any]]]:
    columns = beta.columns
    active: list[tuple[str, float]] = []
    for row in blend:
        name = str(row.get("signal", ""))
        if name not in components:
            continue
        weight = float(row.get("weight", 0.0) or 0.0)
        current = components[name].reindex(index=[dt], columns=columns).iloc[0]
        finite = current.notna() & np.isfinite(current)
        if weight > 0.0 and int(finite.sum()) >= max(3, int(min_names)):
            active.append((name, weight))

    total = sum(weight for _, weight in active)
    if total <= 0.0:
        return pd.Series(0.0, index=columns, dtype=float), []

    score = pd.Series(0.0, index=columns, dtype=float)
    used: list[dict[str, Any]] = []
    for name, raw_weight in active:
        weight = raw_weight / total
        row = components[name].loc[dt].reindex(columns).replace([np.inf, -np.inf], np.nan)
        score = score.add(row.fillna(0.0) * weight, fill_value=0.0)
        used.append({"signal": name, "weight": round(float(weight), 6)})

    score_frame = pd.DataFrame([score], index=[dt], columns=columns)
    beta_frame = beta.reindex(index=[dt], columns=columns)
    target = _neutral_weights(score_frame, beta_frame, float(gross_target)).iloc[0]
    return target.fillna(0.0), used


def _execute_delta(
    previous: pd.Series,
    target: pd.Series,
    dt: pd.Timestamp,
    beta_row: pd.Series,
    panels: ExecutionPanels | None,
    *,
    capital_dollars: float,
    max_adv_participation: float,
    commission_bps: float,
    slippage_bps: float,
    impact_coefficient: float,
) -> tuple[pd.Series, dict[str, Any]]:
    previous = previous.astype(float).fillna(0.0)
    target = target.reindex(previous.index).astype(float).fillna(0.0)
    desired_delta = target - previous
    active = desired_delta.abs() > 1e-12

    capacity_scale = 1.0
    missing_liquidity: list[str] = []
    adv = pd.Series(np.nan, index=previous.index, dtype=float)
    spread = pd.Series(0.0, index=previous.index, dtype=float)
    vol = pd.Series(0.0, index=previous.index, dtype=float)

    if panels is not None and active.any():
        adv = panels.adv_dollars.reindex(index=[dt], columns=previous.index).iloc[0]
        spread = panels.spread_bps.reindex(index=[dt], columns=previous.index).iloc[0].fillna(0.0)
        vol = panels.volatility_bps.reindex(index=[dt], columns=previous.index).iloc[0].fillna(0.0)
        ratios: list[float] = []
        for symbol in previous.index[active]:
            adv_value = adv.get(symbol)
            if pd.isna(adv_value) or float(adv_value) <= 0.0:
                missing_liquidity.append(str(symbol))
                continue
            required = abs(float(desired_delta[symbol])) * capital_dollars
            if required > 0.0:
                ratios.append(max_adv_participation * float(adv_value) / required)
        if missing_liquidity:
            capacity_scale = 0.0
        elif ratios:
            capacity_scale = min(1.0, max(0.0, min(ratios)))

    executed_delta = desired_delta * capacity_scale
    executable = previous + executed_delta
    turnover = float(executed_delta.abs().sum())
    cost_fraction = 0.0
    max_participation = 0.0

    for symbol in previous.index[executed_delta.abs() > 1e-12]:
        delta = abs(float(executed_delta[symbol]))
        participation = 0.0
        spread_value = 0.0
        vol_value = 0.0
        if panels is not None:
            adv_value = adv.get(symbol)
            if pd.notna(adv_value) and float(adv_value) > 0.0:
                participation = delta * capital_dollars / float(adv_value)
                max_participation = max(max_participation, participation)
            spread_value = max(0.0, float(spread.get(symbol) or 0.0))
            vol_value = max(0.0, float(vol.get(symbol) or 0.0))
        one_way_bps = (
            max(0.0, commission_bps)
            + max(0.0, slippage_bps)
            + spread_value / 2.0
            + max(0.0, impact_coefficient) * vol_value * np.sqrt(min(max(participation, 0.0), 1.0))
        )
        cost_fraction += delta * one_way_bps / 10_000.0

    beta_exposure = float((executable * beta_row.reindex(previous.index).fillna(0.0)).sum())
    return executable, {
        "capacity_scale": round(float(capacity_scale), 6),
        "turnover": round(turnover, 6),
        "cost_fraction": float(cost_fraction),
        "cost_bps_of_capital": round(float(cost_fraction * 10_000.0), 4),
        "max_trade_adv_participation": round(float(max_participation), 6),
        "missing_liquidity": missing_liquidity,
        "gross_exposure": round(float(executable.abs().sum()), 8),
        "net_exposure": round(float(executable.sum()), 10),
        "beta_exposure": round(beta_exposure, 10),
    }


def _selection_summary(vintages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stats: dict[str, dict[str, float]] = {}
    for vintage in vintages:
        for row in vintage.get("blend", []):
            name = str(row["signal"])
            item = stats.setdefault(name, {"count": 0.0, "weight": 0.0})
            item["count"] += 1.0
            item["weight"] += float(row.get("weight", 0.0))
    total = max(1, len(vintages))
    rows = []
    for name, item in stats.items():
        count = int(item["count"])
        rows.append(
            {
                "signal": name,
                "selected_vintages": count,
                "selection_rate": round(count / total, 6),
                "average_weight_when_selected": round(item["weight"] / max(1, count), 6),
            }
        )
    return sorted(rows, key=lambda row: (row["selected_vintages"], row["average_weight_when_selected"]), reverse=True)


def walk_forward_multisource_portfolio(
    prices: pd.DataFrame,
    *,
    params: Mapping[str, Any] | None = None,
    features: FeaturePanels | None = None,
    db: Session | None = None,
    signals: Iterable[str] | None = None,
    built: MultisourceLibraryOutput | None = None,
    groups: Mapping[str, Any] | pd.Series | None = None,
    group_label: str | None = None,
    min_group_names: int = 2,
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
    fdr_alpha: float = 0.10,
    redundancy_threshold: float = 0.80,
    redundancy_min_overlap: int = 100,
    execution_panels: ExecutionPanels | None = None,
    execution_aware: bool = True,
    research_capital_dollars: float = 10_000_000.0,
    max_adv_participation: float = 0.05,
    execution_commission_bps: float = 1.0,
    execution_slippage_bps: float = 0.5,
    impact_coefficient: float = 0.10,
    execution_quantile: float = 0.20,
    min_capacity_fill: float = 0.90,
    min_net_alpha_bps: float = 0.0,
    execution_adv_window: int = 20,
    execution_vol_window: int = 20,
    execution_spread_window: int = 5,
    portfolio_rebalance_days: int = 5,
    research_refresh_days: int | None = None,
    gross_target: float = 1.0,
    initial_capital: float | None = None,
) -> WalkForwardResearchOutput:
    """Run the research-selection-execution loop through historical time."""

    params = dict(params or {})
    if prices is None or prices.empty:
        raise ValueError("walk-forward research requires a non-empty price panel")
    prices = prices.sort_index().astype(float)
    if not prices.index.is_unique:
        prices = prices[~prices.index.duplicated(keep="last")]
    if prices.shape[1] < 3:
        raise ValueError("walk-forward research needs at least 3 symbols")

    primary_horizon = int(primary_horizon)
    train_days = int(train_days)
    test_days = int(test_days)
    purge_days = int(purge_days)
    portfolio_rebalance_days = max(1, int(portfolio_rebalance_days))
    research_refresh_days = max(1, int(research_refresh_days or test_days))
    gross_target = float(gross_target)
    research_capital_dollars = float(research_capital_dollars)
    initial_capital = float(initial_capital or research_capital_dollars)
    if gross_target <= 0.0 or gross_target > 3.0:
        raise ValueError("gross_target must be > 0 and <= 3")
    if research_capital_dollars <= 0.0 or initial_capital <= 0.0:
        raise ValueError("capital must be positive")
    if not 0.0 < float(max_adv_participation) <= 1.0:
        raise ValueError("max_adv_participation must be > 0 and <= 1")

    warmup = train_days + purge_days + test_days
    if len(prices) < warmup + 1:
        raise ValueError(
            "Not enough history for a walk-forward research portfolio: need at least "
            f"{warmup + 1} bars, got {len(prices)}"
        )

    if built is None:
        features = features or build_feature_panels(prices, params=params, db=db)
        built = build_multisource_signal_library(
            prices,
            params=params,
            features=features,
            db=db,
            signals=signals,
        )
    if not built.library.components:
        raise ValueError("No requested signals had point-in-time data in this period")

    if execution_aware and execution_panels is None:
        if features is None:
            features = build_feature_panels(
                prices,
                params={
                    **params,
                    "include_volume": True,
                    "include_fundamentals": False,
                    "include_events": False,
                    "include_archived_snapshots": False,
                },
                db=None,
            )
        execution_panels = build_execution_panels(
            prices,
            features.panels,
            adv_window=execution_adv_window,
            vol_window=execution_vol_window,
            spread_window=execution_spread_window,
        )

    # Research refreshes are anchored to the first date on which one complete
    # train/purge/test block exists.  Portfolio decisions can be more frequent;
    # each one references the latest already-completed vintage.
    first_research_pos = warmup - 1
    research_positions = list(range(first_research_pos, len(prices), research_refresh_days))
    vintage_by_pos: dict[int, dict[str, Any]] = {}
    research_vintages: list[dict[str, Any]] = []

    for pos in research_positions:
        hist_index = prices.index[: pos + 1]
        hist_prices = prices.loc[hist_index]
        hist_library = _slice_library(built.library, hist_index)
        hist_execution = _slice_execution(execution_panels, hist_index) if execution_aware else None
        report = research_signal_suite(
            hist_prices,
            params=params,
            signals=None,
            horizons=(primary_horizon,),
            primary_horizon=primary_horizon,
            train_days=train_days,
            test_days=test_days,
            purge_days=purge_days,
            min_names=min_names,
            min_oos_ic=min_oos_ic,
            min_oos_t_stat=min_oos_t_stat,
            min_positive_folds=min_positive_folds,
            min_coverage=min_coverage,
            min_oos_observations=min_oos_observations,
            library=hist_library,
            signal_specs=built.specs,
            groups=groups,
            group_label=group_label,
            min_group_names=min_group_names,
            fdr_alpha=fdr_alpha,
            redundancy_threshold=redundancy_threshold,
            redundancy_min_overlap=redundancy_min_overlap,
            execution_panels=hist_execution,
            research_capital_dollars=research_capital_dollars,
            max_adv_participation=max_adv_participation,
            execution_commission_bps=execution_commission_bps,
            execution_slippage_bps=execution_slippage_bps,
            impact_coefficient=impact_coefficient,
            execution_quantile=execution_quantile,
            min_capacity_fill=min_capacity_fill,
            min_net_alpha_bps=min_net_alpha_bps,
        )
        blend = list(report.get("recommended_blend") or [])
        validated = [row for row in report.get("signals", []) if row.get("validated")]
        vintage = {
            "as_of": str(prices.index[pos].date()),
            "bars_seen": pos + 1,
            "folds": int(report.get("fold_config", {}).get("folds", 0)),
            "available_signals": len(report.get("signals", [])),
            "validated_signals": len(validated),
            "blend": blend,
            "survivors": [row.get("name") for row in validated],
        }
        research_vintages.append(vintage)
        vintage_by_pos[pos] = {"report": report, "vintage": vintage}

    columns = built.library.beta.columns
    target_weights = pd.DataFrame(np.nan, index=prices.index, columns=columns, dtype=float)
    daily_costs = pd.Series(0.0, index=prices.index, dtype=float)
    decisions: list[dict[str, Any]] = []
    current = pd.Series(0.0, index=columns, dtype=float)

    latest_vintage_pos: int | None = None
    research_pointer = 0
    decision_positions = list(range(first_research_pos, max(first_research_pos, len(prices) - 1), portfolio_rebalance_days))
    for pos in decision_positions:
        while research_pointer < len(research_positions) and research_positions[research_pointer] <= pos:
            latest_vintage_pos = research_positions[research_pointer]
            research_pointer += 1
        if latest_vintage_pos is None:
            continue
        dt = prices.index[pos]
        execution_dt = prices.index[pos + 1] if pos + 1 < len(prices) else None
        vintage_payload = vintage_by_pos[latest_vintage_pos]
        report = vintage_payload["report"]
        blend = list(report.get("recommended_blend") or [])
        target, effective_blend = _blend_target(
            dt,
            built.library.components,
            built.library.beta,
            blend,
            min_names=min_names,
            gross_target=gross_target,
        )
        beta_row = built.library.beta.reindex(index=[dt], columns=columns).iloc[0]
        current, execution = _execute_delta(
            current,
            target,
            dt,
            beta_row,
            execution_panels if execution_aware else None,
            capital_dollars=research_capital_dollars,
            max_adv_participation=max_adv_participation,
            commission_bps=execution_commission_bps,
            slippage_bps=execution_slippage_bps,
            impact_coefficient=impact_coefficient,
        )
        target_weights.loc[dt] = current
        if execution_dt is not None:
            daily_costs.loc[execution_dt] += float(execution["cost_fraction"])

        decisions.append(
            {
                "signal_date": str(dt.date()),
                "execution_date": None if execution_dt is None else str(execution_dt.date()),
                "research_as_of": vintage_payload["vintage"]["as_of"],
                "validated_signals": vintage_payload["vintage"]["validated_signals"],
                "blend": effective_blend,
                "target_gross_exposure": round(float(target.abs().sum()), 8),
                "capacity_scale": execution["capacity_scale"],
                "executable_gross_exposure": execution["gross_exposure"],
                "net_exposure": execution["net_exposure"],
                "beta_exposure": execution["beta_exposure"],
                "turnover": execution["turnover"],
                "cost_bps_of_capital": execution["cost_bps_of_capital"],
                "max_trade_adv_participation": execution["max_trade_adv_participation"],
                "missing_liquidity": execution["missing_liquidity"],
            }
        )

    target_weights = target_weights.ffill().fillna(0.0)
    held_weights = target_weights.shift(1).fillna(0.0)
    returns = prices.reindex(columns=columns).pct_change(fill_method=None).fillna(0.0)
    gross_returns = (held_weights * returns).sum(axis=1)
    net_returns = gross_returns - daily_costs
    gross_equity = initial_capital * (1.0 + gross_returns).cumprod()
    equity = initial_capital * (1.0 + net_returns).cumprod()

    config = {
        "primary_horizon": primary_horizon,
        "train_days": train_days,
        "test_days": test_days,
        "purge_days": purge_days,
        "research_refresh_days": research_refresh_days,
        "portfolio_rebalance_days": portfolio_rebalance_days,
        "gross_target": round(gross_target, 6),
        "initial_capital": round(initial_capital, 2),
        "research_capital_dollars": round(research_capital_dollars, 2),
        "execution_aware": bool(execution_aware),
        "max_adv_participation": round(float(max_adv_participation), 6),
        "execution_commission_bps": round(float(execution_commission_bps), 6),
        "execution_slippage_bps": round(float(execution_slippage_bps), 6),
        "impact_coefficient": round(float(impact_coefficient), 6),
        "fdr_alpha": round(float(fdr_alpha), 6),
        "redundancy_threshold": round(float(redundancy_threshold), 6),
        "group_neutralization": group_label if groups is not None else None,
    }

    return WalkForwardResearchOutput(
        target_weights=target_weights,
        held_weights=held_weights,
        gross_returns=gross_returns,
        net_returns=net_returns,
        daily_costs=daily_costs,
        gross_equity=gross_equity,
        equity=equity,
        research_vintages=research_vintages,
        decisions=decisions,
        selection_summary=_selection_summary(research_vintages),
        source_status=dict(built.source_status),
        config=config,
    )
