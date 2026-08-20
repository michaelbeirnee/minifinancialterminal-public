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
from .factor_risk import build_factor_risk_model, portfolio_risk_diagnostics
from .alpha_risk import (
    BorrowPanels,
    build_alpha_sleeve_plan,
    build_borrow_panels,
    build_sleeve_target,
    daily_borrow_costs,
    project_portfolio_constraints,
)
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
    daily_borrow_costs: pd.Series
    gross_equity: pd.Series
    equity: pd.Series
    research_vintages: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    selection_summary: list[dict[str, Any]]
    sleeve_summary: list[dict[str, Any]]
    source_status: dict[str, Any]
    borrow_status: dict[str, Any]
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
        total_execution_cost_dollars = float((self.daily_costs * prior_equity).sum())
        total_borrow_cost_dollars = float((self.daily_borrow_costs * prior_equity).sum())
        total_cost_dollars = total_execution_cost_dollars + total_borrow_cost_dollars
        risk_rows = [
            row.get("executed_risk") for row in self.decisions
            if isinstance(row.get("executed_risk"), dict) and row.get("executed_risk", {}).get("status") == "ready"
        ]
        avg_predicted_vol = (
            float(np.mean([float(row.get("predicted_annual_volatility", 0.0)) for row in risk_rows]))
            if risk_rows else 0.0
        )
        avg_effective_risk_names = (
            float(np.mean([float(row.get("effective_risk_names", 0.0)) for row in risk_rows]))
            if risk_rows else 0.0
        )
        max_name_risk_share = (
            max(float(row.get("max_positive_name_risk_share", 0.0)) for row in risk_rows)
            if risk_rows else 0.0
        )
        worst_stress = (
            min(float(row.get("worst_stress_return", 0.0)) for row in risk_rows)
            if risk_rows else 0.0
        )
        risk_model_refreshes = len({str(row.get("as_of")) for row in risk_rows if row.get("as_of")})

        return {
            "engine": "walk_forward_multisource",
            "as_of": str(self.equity.index[-1].date()),
            "metrics": compute_metrics(self.equity),
            "gross_metrics": compute_metrics(self.gross_equity),
            "total_costs": round(total_cost_dollars, 2),
            "execution_costs": round(total_execution_cost_dollars, 2),
            "borrow_costs": round(total_borrow_cost_dollars, 2),
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
            "sleeve_summary": self.sleeve_summary,
            "source_status": self.source_status,
            "borrow_status": self.borrow_status,
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
                "average_daily_borrow_bps": round(float(self.daily_borrow_costs.mean() * 10_000.0), 6),
                "average_active_sleeves": round(float(np.mean([len(v.get("sleeves", [])) for v in self.research_vintages])) if self.research_vintages else 0.0, 4),
                "average_sleeve_budget_utilization": round(float(np.mean([float(v.get("sleeve_budget_sum", 0.0)) for v in self.research_vintages])) if self.research_vintages else 0.0, 6),
                "risk_model_decisions": len(risk_rows),
                "risk_model_refreshes": risk_model_refreshes,
                "average_predicted_annual_volatility": round(avg_predicted_vol, 6),
                "average_effective_risk_names": round(avg_effective_risk_names, 4),
                "max_name_risk_share": round(max_name_risk_share, 6),
                "worst_factor_stress_return": round(worst_stress, 6),
            },
            "config": self.config,
            "methodology": {
                "signal_selection": "latest_completed_research_vintage_only",
                "research_history": "prefix_only_no_future_rows",
                "execution_lag_bars": 1,
                "portfolio_capacity": "uniform_scale_of_delta_to_target",
                "archive_rule": "estimates_options_and_crowding_exist_only_after_actual_capture",
                "alpha_allocation": "family_sleeves_with_trailing_risk_budgets_and_correlation_caps",
                "stock_risk_model": "trailing_factor_covariance_plus_shrunk_residual_covariance_prefix_only",
                "risk_projection": "minimum_distance_plus_covariance_penalty_with_volatility_and_factor_caps",
                "borrow_cost": "daily_short_notional_times_point_in_time_or_proxy_annual_borrow_rate",
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


def _sleeve_summary(vintages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stats: dict[str, dict[str, float]] = {}
    for vintage in vintages:
        for row in vintage.get("sleeves", []):
            name = str(row.get("name", "other"))
            item = stats.setdefault(name, {"count": 0.0, "budget": 0.0, "vol": 0.0, "vol_count": 0.0})
            item["count"] += 1.0
            item["budget"] += float(row.get("risk_budget", 0.0) or 0.0)
            vol = row.get("annualized_volatility")
            if vol is not None:
                item["vol"] += float(vol)
                item["vol_count"] += 1.0
    total = max(1, len(vintages))
    rows = []
    for name, item in stats.items():
        count = int(item["count"])
        rows.append({
            "sleeve": name,
            "active_vintages": count,
            "activation_rate": round(count / total, 6),
            "average_risk_budget": round(item["budget"] / max(1, count), 6),
            "average_annualized_volatility": (
                None if item["vol_count"] <= 0 else round(item["vol"] / item["vol_count"], 6)
            ),
        })
    return sorted(rows, key=lambda row: (row["active_vintages"], row["average_risk_budget"]), reverse=True)


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
    alpha_risk_aware: bool = True,
    sleeve_lookback_days: int = 126,
    sleeve_correlation_threshold: float = 0.60,
    max_sleeve_budget: float = 0.45,
    max_cluster_budget: float = 0.65,
    event_budget_cap: float = 0.25,
    max_name_weight: float = 0.20,
    max_crowded_short_gross: float = 0.15,
    crowded_short_threshold: float = 0.65,
    apply_group_constraints: bool = False,
    group_net_cap: float = 0.10,
    borrow_aware: bool = True,
    base_borrow_bps: float = 30.0,
    crowding_surcharge_bps: float = 900.0,
    hard_to_borrow_short_float: float = 0.35,
    hard_to_borrow_days_to_cover: float = 15.0,
    stock_risk_aware: bool = True,
    factor_risk_lookback_days: int = 252,
    factor_risk_refresh_days: int = 21,
    factor_risk_min_observations: int = 80,
    residual_covariance_shrinkage: float = 0.50,
    target_annual_volatility: float | None = 0.12,
    max_market_factor_exposure: float = 0.05,
    max_style_factor_exposure: float = 0.15,
    covariance_risk_aversion: float = 0.25,
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
    factor_risk_refresh_days = max(1, int(factor_risk_refresh_days))
    gross_target = float(gross_target)
    research_capital_dollars = float(research_capital_dollars)
    initial_capital = float(initial_capital or research_capital_dollars)
    if gross_target <= 0.0 or gross_target > 3.0:
        raise ValueError("gross_target must be > 0 and <= 3")
    if research_capital_dollars <= 0.0 or initial_capital <= 0.0:
        raise ValueError("capital must be positive")
    if not 0.0 < float(max_adv_participation) <= 1.0:
        raise ValueError("max_adv_participation must be > 0 and <= 1")
    if not 0.0 < float(max_name_weight) <= 1.0:
        raise ValueError("max_name_weight must be > 0 and <= 1")
    if not 0.0 <= float(event_budget_cap) <= 1.0:
        raise ValueError("event_budget_cap must be between 0 and 1")
    if not 0.0 <= float(max_crowded_short_gross) <= 1.5:
        raise ValueError("max_crowded_short_gross must be between 0 and 1.5")
    if target_annual_volatility is not None and not 0.0 < float(target_annual_volatility) <= 2.0:
        raise ValueError("target_annual_volatility must be > 0 and <= 2 when enabled")
    if not 0.0 <= float(residual_covariance_shrinkage) <= 1.0:
        raise ValueError("residual_covariance_shrinkage must be between 0 and 1")
    if float(max_market_factor_exposure) < 0.0 or float(max_style_factor_exposure) < 0.0:
        raise ValueError("factor exposure caps must be non-negative")
    if float(covariance_risk_aversion) < 0.0:
        raise ValueError("covariance_risk_aversion must be non-negative")

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

    borrow_panels: BorrowPanels | None = None
    if borrow_aware:
        feature_map = features.panels if features is not None else {}
        borrow_panels = build_borrow_panels(
            prices,
            feature_map,
            base_borrow_bps=base_borrow_bps,
            crowding_surcharge_bps=crowding_surcharge_bps,
            hard_to_borrow_short_float=hard_to_borrow_short_float,
            hard_to_borrow_days_to_cover=hard_to_borrow_days_to_cover,
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
        sleeve_plan = (
            build_alpha_sleeve_plan(
                hist_prices,
                hist_library,
                report,
                built.specs,
                lookback=sleeve_lookback_days,
                correlation_threshold=sleeve_correlation_threshold,
                max_sleeve_budget=max_sleeve_budget,
                max_cluster_budget=max_cluster_budget,
                event_budget_cap=event_budget_cap,
            )
            if alpha_risk_aware
            else {"sleeves": [], "budget_sum": 0.0, "correlation_clusters": [], "correlations": []}
        )
        vintage = {
            "as_of": str(prices.index[pos].date()),
            "bars_seen": pos + 1,
            "folds": int(report.get("fold_config", {}).get("folds", 0)),
            "available_signals": len(report.get("signals", [])),
            "validated_signals": len(validated),
            "blend": blend,
            "survivors": [row.get("name") for row in validated],
            "sleeves": sleeve_plan.get("sleeves", []),
            "sleeve_budget_sum": sleeve_plan.get("budget_sum", 0.0),
            "sleeve_correlation_clusters": sleeve_plan.get("correlation_clusters", []),
        }
        research_vintages.append(vintage)
        vintage_by_pos[pos] = {"report": report, "vintage": vintage, "sleeve_plan": sleeve_plan}

    columns = built.library.beta.columns
    target_weights = pd.DataFrame(np.nan, index=prices.index, columns=columns, dtype=float)
    daily_costs = pd.Series(0.0, index=prices.index, dtype=float)
    decisions: list[dict[str, Any]] = []
    current = pd.Series(0.0, index=columns, dtype=float)

    latest_vintage_pos: int | None = None
    research_pointer = 0
    latest_factor_risk_model = None
    latest_factor_risk_pos: int | None = None
    latest_factor_risk_error: str | None = None
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
        beta_row = built.library.beta.reindex(index=[dt], columns=columns).iloc[0]
        if alpha_risk_aware:
            target, effective_sleeves = build_sleeve_target(
                dt,
                built.library.components,
                built.library.beta,
                vintage_payload.get("sleeve_plan", {}),
                min_names=min_names,
                gross_target=gross_target,
            )
            effective_blend = [
                member
                for sleeve in effective_sleeves
                for member in sleeve.get("members", [])
            ]
        else:
            target, effective_blend = _blend_target(
                dt,
                built.library.components,
                built.library.beta,
                blend,
                min_names=min_names,
                gross_target=gross_target,
            )
            effective_sleeves = []

        factor_risk_model = None
        factor_risk_error = None
        if stock_risk_aware:
            should_refresh_risk = (
                latest_factor_risk_model is None
                or latest_factor_risk_pos is None
                or pos - latest_factor_risk_pos >= factor_risk_refresh_days
            )
            if should_refresh_risk:
                try:
                    latest_factor_risk_model = build_factor_risk_model(
                        prices.reindex(columns=columns).loc[:dt],
                        as_of=dt,
                        lookback=factor_risk_lookback_days,
                        min_obs=factor_risk_min_observations,
                        residual_shrinkage=residual_covariance_shrinkage,
                    )
                    latest_factor_risk_pos = pos
                    latest_factor_risk_error = None
                except ValueError as exc:
                    latest_factor_risk_error = str(exc)
            factor_risk_model = latest_factor_risk_model
            factor_risk_error = latest_factor_risk_error

        constraint_info: dict[str, Any] = {"status": "disabled"}
        if alpha_risk_aware:
            shortable_row = None
            crowding_row = None
            if borrow_panels is not None:
                shortable_row = borrow_panels.shortable.reindex(index=[dt], columns=columns).iloc[0]
                crowding_row = borrow_panels.crowding_score.reindex(index=[dt], columns=columns).iloc[0]
            projection_beta = beta_row.copy()
            if stock_risk_aware and factor_risk_model is None:
                target = pd.Series(0.0, index=columns, dtype=float)
                constraint_info = {
                    "status": "flat",
                    "reason": "factor_risk_model_unavailable",
                    "risk_model_error": factor_risk_error,
                }
            else:
                if factor_risk_model is not None:
                    modeled = set(factor_risk_model.covariance.index)
                    projection_beta.loc[[name for name in columns if name not in modeled]] = np.nan
                factor_caps = None
                if factor_risk_model is not None:
                    factor_caps = {
                        name: (
                            float(max_market_factor_exposure)
                            if name == "MKT"
                            else float(max_style_factor_exposure)
                        )
                        for name in factor_risk_model.exposures.columns
                    }
                target, constraint_info = project_portfolio_constraints(
                    target,
                    projection_beta,
                    gross_limit=gross_target,
                    max_name_weight=max_name_weight,
                    shortable=shortable_row,
                    crowding_score=crowding_row,
                    crowded_short_threshold=crowded_short_threshold,
                    max_crowded_short_gross=max_crowded_short_gross,
                    groups=groups if apply_group_constraints else None,
                    group_net_cap=group_net_cap if apply_group_constraints else None,
                    covariance=(factor_risk_model.covariance if factor_risk_model is not None else None),
                    factor_exposures=(factor_risk_model.exposures if factor_risk_model is not None else None),
                    factor_exposure_caps=factor_caps,
                    target_annual_vol=(float(target_annual_volatility) if stock_risk_aware and target_annual_volatility is not None else None),
                    risk_aversion=(float(covariance_risk_aversion) if stock_risk_aware else 0.0),
                )

        target_risk = (
            portfolio_risk_diagnostics(target, factor_risk_model)
            if factor_risk_model is not None and constraint_info.get("status") == "ready"
            else {
                "status": "unavailable" if factor_risk_model is None else "flat",
                "reason": factor_risk_error if factor_risk_model is None else constraint_info.get("reason"),
            }
        )

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
        executed_risk = (
            portfolio_risk_diagnostics(current, factor_risk_model)
            if factor_risk_model is not None
            else {"status": "unavailable", "reason": factor_risk_error}
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
                "sleeves": effective_sleeves,
                "risk_constraints": constraint_info,
                "target_risk": target_risk,
                "executed_risk": executed_risk,
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
    borrow_cost_series = daily_borrow_costs(
        held_weights, borrow_panels if borrow_aware else None, default_annual_borrow_bps=base_borrow_bps
    ) if borrow_aware else pd.Series(0.0, index=prices.index, dtype=float)
    net_returns = gross_returns - daily_costs - borrow_cost_series
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
        "alpha_risk_aware": bool(alpha_risk_aware),
        "sleeve_lookback_days": int(sleeve_lookback_days),
        "sleeve_correlation_threshold": round(float(sleeve_correlation_threshold), 6),
        "max_sleeve_budget": round(float(max_sleeve_budget), 6),
        "max_cluster_budget": round(float(max_cluster_budget), 6),
        "event_budget_cap": round(float(event_budget_cap), 6),
        "max_name_weight": round(float(max_name_weight), 6),
        "max_crowded_short_gross": round(float(max_crowded_short_gross), 6),
        "crowded_short_threshold": round(float(crowded_short_threshold), 6),
        "apply_group_constraints": bool(apply_group_constraints),
        "group_net_cap": round(float(group_net_cap), 6),
        "borrow_aware": bool(borrow_aware),
        "base_borrow_bps": round(float(base_borrow_bps), 6),
        "crowding_surcharge_bps": round(float(crowding_surcharge_bps), 6),
        "stock_risk_aware": bool(stock_risk_aware),
        "factor_risk_lookback_days": int(factor_risk_lookback_days),
        "factor_risk_refresh_days": int(factor_risk_refresh_days),
        "factor_risk_min_observations": int(factor_risk_min_observations),
        "residual_covariance_shrinkage": round(float(residual_covariance_shrinkage), 6),
        "target_annual_volatility": None if target_annual_volatility is None else round(float(target_annual_volatility), 6),
        "max_market_factor_exposure": round(float(max_market_factor_exposure), 6),
        "max_style_factor_exposure": round(float(max_style_factor_exposure), 6),
        "covariance_risk_aversion": round(float(covariance_risk_aversion), 6),
    }

    return WalkForwardResearchOutput(
        target_weights=target_weights,
        held_weights=held_weights,
        gross_returns=gross_returns,
        net_returns=net_returns,
        daily_costs=daily_costs,
        daily_borrow_costs=borrow_cost_series,
        gross_equity=gross_equity,
        equity=equity,
        research_vintages=research_vintages,
        decisions=decisions,
        selection_summary=_selection_summary(research_vintages),
        sleeve_summary=_sleeve_summary(research_vintages),
        source_status=dict(built.source_status),
        borrow_status=(borrow_panels.source_status if borrow_panels is not None else {"mode": "disabled"}),
        config=config,
    )
