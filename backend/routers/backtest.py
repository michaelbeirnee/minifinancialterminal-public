"""Backtest endpoints: run + persist + list history, plus research tools
(parameter sweep, walk-forward, cost sensitivity)."""
from __future__ import annotations

import json
from typing import List, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..backtest import analysis
from ..backtest.engine import run_backtest
from ..backtest.execution_research import build_execution_panels, current_execution_book
from ..backtest.factor_risk import (
    build_factor_risk_model,
    factor_risk_model_summary,
    portfolio_risk_diagnostics,
)
from ..backtest.strategies import REGISTRY
from ..backtest.stat_arb import stat_arb_snapshot
from ..backtest.walkforward_research import walk_forward_multisource_portfolio
from ..backtest.signal_research import (
    adaptive_stat_arb_snapshot,
    research_signal_suite,
    signal_catalog,
)
from ..backtest.multisource_research import (
    MULTISOURCE_SPEC_BY_NAME,
    archive_current_snapshots,
    build_feature_panels,
    build_multisource_signal_library,
    current_symbol_classifications,
    multisource_signal_catalog,
)
from ..data.provider import get_history, get_price_panel
from ..database import get_db
from ..models import BacktestRun, User
from ..portfolio import overlays
from ..schemas import (
    BacktestRequest,
    BacktestSweepRequest,
    CostSensitivityRequest,
    FactorRiskSnapshotRequest,
    ResearchArchiveRequest,
    SignalResearchRequest,
    SignalPortfolioWalkForwardRequest,
    StatArbSnapshotRequest,
    WalkForwardRequest,
)

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


@router.get("/strategies")
def list_strategies(_: User = Depends(get_current_user)) -> dict:
    return {"strategies": sorted(REGISTRY.keys())}


@router.post("/run")
def run(
    req: BacktestRequest,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        prices = get_price_panel(req.symbols, req.start, req.end)
        if prices.empty:
            raise ValueError("No price data for requested symbols/period")

        # Open prices give the event-driven engine realistic next-bar fills.
        opens = None
        if req.engine == "event_driven":
            opens = _open_panel(req.symbols, req.start, req.end, prices)

        result = run_backtest(
            prices=prices,
            strategy=req.strategy,
            params=req.params,
            engine=req.engine,
            commission_bps=req.commission_bps,
            slippage_bps=req.slippage_bps,
            initial_capital=req.initial_capital,
            opens=opens,
            vol_target=req.vol_target,
            vol_lookback=req.vol_lookback,
            max_leverage=req.max_leverage,
            stop_loss=req.stop_loss,
            trailing_stop=req.trailing_stop,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    payload = result.to_dict()

    # Benchmark equity (buy & hold the benchmark) for the chart, plus
    # benchmark-relative attribution (alpha/beta, IR, capture ratios).
    if req.benchmark:
        try:
            bench = get_history(req.benchmark, req.start, req.end)["close"]
            bench = bench.reindex(result.equity.index).ffill()
            norm = req.initial_capital * bench / bench.iloc[0]
            payload["benchmark"] = {
                "symbol": req.benchmark.upper(),
                "values": [round(float(v), 4) for v in norm.values],
                "total_return": round(float(bench.iloc[-1] / bench.iloc[0] - 1), 6),
                "attribution": analysis.benchmark_attribution(
                    result.returns, bench.pct_change()
                ),
            }
        except Exception:  # noqa: BLE001
            payload["benchmark"] = None

    if req.monte_carlo:
        payload["monte_carlo"] = analysis.monte_carlo(
            result.returns, initial_capital=req.initial_capital
        )

    run_row = BacktestRun(
        owner_id=current.id,
        strategy=req.strategy,
        symbols=",".join(s.upper() for s in req.symbols),
        start=req.start,
        end=req.end or "",
        params_json=json.dumps(req.params),
        metrics_json=json.dumps(result.metrics),
        sharpe=result.metrics.get("sharpe"),
        total_return=result.metrics.get("total_return"),
    )
    db.add(run_row)
    db.commit()
    db.refresh(run_row)

    payload["run_id"] = run_row.id
    return payload


@router.post("/stat_arb/snapshot")
def stat_arb_signal(
    req: StatArbSnapshotRequest, _: User = Depends(get_current_user)
) -> dict:
    """Latest multi-signal stat-arb target with component attribution."""
    try:
        prices = get_price_panel(req.symbols, req.start, req.end)
        if prices.empty:
            raise ValueError("No price data for requested symbols/period")
        return stat_arb_snapshot(prices, req.params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/signals/catalog")
def signals_catalog(_: User = Depends(get_current_user)) -> dict:
    """Registered signal formulas and their research families/data sources."""
    return {"signals": signal_catalog()}


@router.get("/signals/multisource_catalog")
def multisource_signals_catalog(_: User = Depends(get_current_user)) -> dict:
    """All price, volume, fundamental, event, archived and relationship signals."""
    return {"signals": multisource_signal_catalog()}


@router.post("/signals/risk_snapshot")
def factor_risk_snapshot(
    req: FactorRiskSnapshotRequest, _: User = Depends(get_current_user)
) -> dict:
    """Inspect the current point-in-time factor/covariance model for a basket."""
    try:
        prices = get_price_panel(req.symbols, req.start, req.end)
        if prices.empty:
            raise ValueError("No price data for requested symbols/period")
        model = build_factor_risk_model(
            prices,
            lookback=req.lookback_days,
            min_obs=req.min_observations,
            residual_shrinkage=req.residual_covariance_shrinkage,
        )
        payload = factor_risk_model_summary(model)
        if req.weights:
            weights = pd.Series({str(k).upper(): float(v) for k, v in req.weights.items()}, dtype=float)
            payload["portfolio_risk"] = portfolio_risk_diagnostics(weights, model)
        else:
            payload["portfolio_risk"] = None
        return payload
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/signals/archive")
def archive_signal_inputs(
    req: ResearchArchiveRequest,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Capture today's estimates/options so future research remains point in time."""
    try:
        return archive_current_snapshots(
            req.symbols,
            db,
            include_estimates=req.include_estimates,
            include_options=req.include_options,
            include_crowding=req.include_crowding,
            include_raw=req.include_raw,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/signals/multisource_research")
def multisource_signal_research(
    req: SignalResearchRequest,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Run the same OOS gates across every point-in-time data family available."""
    try:
        prices = get_price_panel(req.symbols, req.start, req.end)
        if prices.empty:
            raise ValueError("No price data for requested symbols/period")
        groups = None
        classification_status = {
            "requested_level": req.neutralize_by,
            "mode": "disabled",
            "coverage": 0.0,
            "point_in_time": None,
        }
        if req.neutralize_by != "none":
            groups, classification_status = current_symbol_classifications(
                req.symbols, req.neutralize_by
            )
        features = build_feature_panels(prices, params=req.params, db=db)
        execution_panels = None
        if req.execution_aware:
            execution_panels = build_execution_panels(
                prices,
                features.panels,
                adv_window=req.execution_adv_window,
                vol_window=req.execution_vol_window,
                spread_window=req.execution_spread_window,
            )
        built = build_multisource_signal_library(
            prices,
            params=req.params,
            features=features,
            db=db,
            signals=req.signals,
        )
        if not built.library.components:
            raise ValueError("No requested signals had point-in-time data in this period")
        report = research_signal_suite(
            prices,
            params=req.params,
            signals=None,
            horizons=req.horizons,
            primary_horizon=req.primary_horizon,
            train_days=req.train_days,
            test_days=req.test_days,
            purge_days=req.purge_days,
            min_names=req.min_names,
            min_oos_ic=req.min_oos_ic,
            min_oos_t_stat=req.min_oos_t_stat,
            min_positive_folds=req.min_positive_folds,
            min_coverage=req.min_coverage,
            min_oos_observations=req.min_oos_observations,
            library=built.library,
            signal_specs=built.specs,
            groups=groups,
            group_label=None if req.neutralize_by == "none" else req.neutralize_by,
            min_group_names=req.min_group_names,
            fdr_alpha=req.fdr_alpha,
            redundancy_threshold=req.redundancy_threshold,
            redundancy_min_overlap=req.redundancy_min_overlap,
            execution_panels=execution_panels,
            research_capital_dollars=req.research_capital_dollars,
            max_adv_participation=req.max_adv_participation,
            execution_commission_bps=req.execution_commission_bps,
            execution_slippage_bps=req.execution_slippage_bps,
            impact_coefficient=req.impact_coefficient,
            execution_quantile=req.execution_quantile,
            min_capacity_fill=req.min_capacity_fill,
            min_net_alpha_bps=req.min_net_alpha_bps,
        )
        requested = (
            list(dict.fromkeys(req.signals))
            if req.signals is not None
            else list(MULTISOURCE_SPEC_BY_NAME)
        )
        available = set(built.library.components)
        report["source_status"] = built.source_status
        report["classification_status"] = classification_status
        if req.execution_aware and execution_panels is not None:
            report["current_execution_book"] = current_execution_book(
                prices,
                built.library.components,
                built.library.beta,
                report,
                execution_panels,
                capital_dollars=req.research_capital_dollars,
                max_adv_participation=req.max_adv_participation,
                commission_bps=req.execution_commission_bps,
                slippage_bps=req.execution_slippage_bps,
                impact_coefficient=req.impact_coefficient,
            )
        else:
            report["current_execution_book"] = None
        report["available_signal_count"] = len(available)
        report["catalog_signal_count"] = len(MULTISOURCE_SPEC_BY_NAME)
        report["unavailable_signals"] = [name for name in requested if name not in available]
        return report
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/signals/walk_forward_portfolio")
def walk_forward_signal_portfolio(
    req: SignalPortfolioWalkForwardRequest,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Simulate historical research vintages feeding a cost-aware neutral book."""
    try:
        prices = get_price_panel(req.symbols, req.start, req.end)
        if prices.empty:
            raise ValueError("No price data for requested symbols/period")

        groups = None
        classification_status = {
            "requested_level": req.neutralize_by,
            "mode": "disabled",
            "coverage": 0.0,
            "point_in_time": None,
        }
        if req.neutralize_by != "none":
            groups, classification_status = current_symbol_classifications(
                req.symbols, req.neutralize_by
            )

        # Fetch every dated family once. The walk-forward engine slices this
        # library at each research vintage, so future rows never enter an old
        # selection decision.
        features = build_feature_panels(prices, params=req.params, db=db)
        built = build_multisource_signal_library(
            prices,
            params=req.params,
            features=features,
            db=db,
            signals=req.signals,
        )
        execution_panels = None
        if req.execution_aware:
            execution_panels = build_execution_panels(
                prices,
                features.panels,
                adv_window=req.execution_adv_window,
                vol_window=req.execution_vol_window,
                spread_window=req.execution_spread_window,
            )

        output = walk_forward_multisource_portfolio(
            prices,
            params=req.params,
            features=features,
            db=db,
            signals=req.signals,
            built=built,
            groups=groups,
            group_label=None if req.neutralize_by == "none" else req.neutralize_by,
            min_group_names=req.min_group_names,
            primary_horizon=req.primary_horizon,
            train_days=req.train_days,
            test_days=req.test_days,
            purge_days=req.purge_days,
            min_names=req.min_names,
            min_oos_ic=req.min_oos_ic,
            min_oos_t_stat=req.min_oos_t_stat,
            min_positive_folds=req.min_positive_folds,
            min_coverage=req.min_coverage,
            min_oos_observations=req.min_oos_observations,
            fdr_alpha=req.fdr_alpha,
            redundancy_threshold=req.redundancy_threshold,
            redundancy_min_overlap=req.redundancy_min_overlap,
            execution_panels=execution_panels,
            execution_aware=req.execution_aware,
            research_capital_dollars=req.research_capital_dollars,
            max_adv_participation=req.max_adv_participation,
            execution_commission_bps=req.execution_commission_bps,
            execution_slippage_bps=req.execution_slippage_bps,
            impact_coefficient=req.impact_coefficient,
            execution_quantile=req.execution_quantile,
            min_capacity_fill=req.min_capacity_fill,
            min_net_alpha_bps=req.min_net_alpha_bps,
            execution_adv_window=req.execution_adv_window,
            execution_vol_window=req.execution_vol_window,
            execution_spread_window=req.execution_spread_window,
            portfolio_rebalance_days=req.portfolio_rebalance_days,
            research_refresh_days=req.research_refresh_days,
            gross_target=req.gross_target,
            initial_capital=req.initial_capital,
            alpha_risk_aware=req.alpha_risk_aware,
            sleeve_lookback_days=req.sleeve_lookback_days,
            sleeve_correlation_threshold=req.sleeve_correlation_threshold,
            max_sleeve_budget=req.max_sleeve_budget,
            max_cluster_budget=req.max_cluster_budget,
            event_budget_cap=req.event_budget_cap,
            max_name_weight=req.max_name_weight,
            max_crowded_short_gross=req.max_crowded_short_gross,
            crowded_short_threshold=req.crowded_short_threshold,
            apply_group_constraints=req.apply_group_constraints,
            group_net_cap=req.group_net_cap,
            borrow_aware=req.borrow_aware,
            base_borrow_bps=req.base_borrow_bps,
            crowding_surcharge_bps=req.crowding_surcharge_bps,
            hard_to_borrow_short_float=req.hard_to_borrow_short_float,
            hard_to_borrow_days_to_cover=req.hard_to_borrow_days_to_cover,
            stock_risk_aware=req.stock_risk_aware,
            factor_risk_lookback_days=req.factor_risk_lookback_days,
            factor_risk_refresh_days=req.factor_risk_refresh_days,
            factor_risk_min_observations=req.factor_risk_min_observations,
            residual_covariance_shrinkage=req.residual_covariance_shrinkage,
            target_annual_volatility=req.target_annual_volatility,
            max_market_factor_exposure=req.max_market_factor_exposure,
            max_style_factor_exposure=req.max_style_factor_exposure,
            covariance_risk_aversion=req.covariance_risk_aversion,
        )
        payload = output.to_dict()
        payload["classification_status"] = classification_status
        payload["available_signal_count"] = len(built.library.components)
        payload["catalog_signal_count"] = len(MULTISOURCE_SPEC_BY_NAME)
        requested = (
            list(dict.fromkeys(req.signals))
            if req.signals is not None
            else list(MULTISOURCE_SPEC_BY_NAME)
        )
        payload["unavailable_signals"] = [
            name for name in requested if name not in built.library.components
        ]
        return payload
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/signals/research")
def signal_research(
    req: SignalResearchRequest, _: User = Depends(get_current_user)
) -> dict:
    """Score each signal independently across rolling out-of-sample test blocks."""
    try:
        prices = get_price_panel(req.symbols, req.start, req.end)
        if prices.empty:
            raise ValueError("No price data for requested symbols/period")
        groups = None
        classification_status = {
            "requested_level": req.neutralize_by,
            "mode": "disabled",
            "coverage": 0.0,
            "point_in_time": None,
        }
        if req.neutralize_by != "none":
            groups, classification_status = current_symbol_classifications(
                req.symbols, req.neutralize_by
            )
        execution_panels = None
        if req.execution_aware:
            light_features = build_feature_panels(
                prices,
                params={
                    "include_volume": True,
                    "include_fundamentals": False,
                    "include_events": False,
                    "include_archived_snapshots": False,
                },
                db=None,
            )
            execution_panels = build_execution_panels(
                prices,
                light_features.panels,
                adv_window=req.execution_adv_window,
                vol_window=req.execution_vol_window,
                spread_window=req.execution_spread_window,
            )
        report = research_signal_suite(
            prices,
            params=req.params,
            signals=req.signals,
            horizons=req.horizons,
            primary_horizon=req.primary_horizon,
            train_days=req.train_days,
            test_days=req.test_days,
            purge_days=req.purge_days,
            min_names=req.min_names,
            min_oos_ic=req.min_oos_ic,
            min_oos_t_stat=req.min_oos_t_stat,
            min_positive_folds=req.min_positive_folds,
            min_coverage=req.min_coverage,
            min_oos_observations=req.min_oos_observations,
            groups=groups,
            group_label=None if req.neutralize_by == "none" else req.neutralize_by,
            min_group_names=req.min_group_names,
            fdr_alpha=req.fdr_alpha,
            redundancy_threshold=req.redundancy_threshold,
            redundancy_min_overlap=req.redundancy_min_overlap,
            execution_panels=execution_panels,
            research_capital_dollars=req.research_capital_dollars,
            max_adv_participation=req.max_adv_participation,
            execution_commission_bps=req.execution_commission_bps,
            execution_slippage_bps=req.execution_slippage_bps,
            impact_coefficient=req.impact_coefficient,
            execution_quantile=req.execution_quantile,
            min_capacity_fill=req.min_capacity_fill,
            min_net_alpha_bps=req.min_net_alpha_bps,
        )
        report["classification_status"] = classification_status
        return report
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/signals/adaptive_snapshot")
def adaptive_signal_snapshot(
    req: StatArbSnapshotRequest, _: User = Depends(get_current_user)
) -> dict:
    """Latest research-gated stat-arb target and trailing signal-quality weights."""
    try:
        prices = get_price_panel(req.symbols, req.start, req.end)
        if prices.empty:
            raise ValueError("No price data for requested symbols/period")
        return adaptive_stat_arb_snapshot(prices, req.params)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/sweep")
def sweep(req: BacktestSweepRequest, _: User = Depends(get_current_user)) -> dict:
    """Grid-search strategy parameters on the vectorized engine."""
    try:
        prices = get_price_panel(req.symbols, req.start, req.end)
        if prices.empty:
            raise ValueError("No price data for requested symbols/period")
        return analysis.sweep(
            prices,
            req.strategy,
            req.param_grid,
            metric=req.metric,
            commission_bps=req.commission_bps,
            slippage_bps=req.slippage_bps,
            initial_capital=req.initial_capital,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/walk_forward")
def walk_forward(req: WalkForwardRequest, _: User = Depends(get_current_user)) -> dict:
    """Rolling out-of-sample evaluation with a purge gap between fit and test."""
    try:
        prices = get_price_panel(req.symbols, req.start, req.end)
        if prices.empty:
            raise ValueError("No price data for requested symbols/period")
        return analysis.walk_forward(
            prices,
            req.strategy,
            params=req.params,
            param_grid=req.param_grid,
            train_days=req.train_days,
            test_days=req.test_days,
            purge_days=req.purge_days,
            metric=req.metric,
            commission_bps=req.commission_bps,
            slippage_bps=req.slippage_bps,
            initial_capital=req.initial_capital,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/cost_sensitivity")
def cost_sensitivity(req: CostSensitivityRequest, _: User = Depends(get_current_user)) -> dict:
    """Re-run one configuration across a ladder of cost assumptions."""
    try:
        prices = get_price_panel(req.symbols, req.start, req.end)
        if prices.empty:
            raise ValueError("No price data for requested symbols/period")
        return analysis.cost_sensitivity(
            prices,
            req.strategy,
            params=req.params,
            multipliers=tuple(req.multipliers),
            commission_bps=req.commission_bps,
            slippage_bps=req.slippage_bps,
            initial_capital=req.initial_capital,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/history")
def history(
    current: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    runs = (
        db.query(BacktestRun)
        .filter(BacktestRun.owner_id == current.id)
        .order_by(BacktestRun.created_at.desc())
        .limit(50)
        .all()
    )
    return {
        "runs": [
            {
                "id": r.id,
                "strategy": r.strategy,
                "symbols": r.symbols,
                "start": r.start,
                "end": r.end,
                "sharpe": r.sharpe,
                "total_return": r.total_return,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in runs
        ]
    }


def _open_panel(symbols, start, end, close_panel):
    """Build a panel of open prices aligned to the close panel index."""
    import pandas as pd

    cols = {}
    for sym in symbols:
        try:
            cols[sym.upper()] = get_history(sym, start, end)["open"]
        except Exception:  # noqa: BLE001
            continue
    if not cols:
        return None
    return pd.DataFrame(cols).reindex(close_panel.index).ffill()


# --------------------------------------------------------------------------- #
# CBOE reference overlays (docs/hedge-construction.md step 7)
# --------------------------------------------------------------------------- #
def fetch_overlay_panel(symbols: List[str], start: str, end: Optional[str]) -> pd.DataFrame:
    """Close prices for the strategy indexes. Split out so tests can stub it."""
    series = {}
    for symbol in symbols:
        try:
            series[symbol] = get_history(symbol, start, end)["close"]
        except Exception:  # noqa: BLE001 - a missing index is reported, not fatal
            continue
    if not series:
        return pd.DataFrame()
    panel = pd.DataFrame(series).sort_index()
    panel.index = pd.to_datetime(panel.index)
    if panel.index.tz is not None:
        panel.index = panel.index.tz_convert(None)
    return panel


@router.get("/overlays")
def hedge_overlays(
    start: str = "2010-01-01",
    end: Optional[str] = None,
    reference: str = overlays.DEFAULT_REFERENCE,
    risk_free_rate: float = 0.0,
    _: User = Depends(get_current_user),
) -> dict:
    """What running a standard hedge continuously has actually cost.

    CBOE's published strategy indexes are the honest proxy here: we hold no
    historical option chains, so this is evidence about hedging policy rather
    than a backtest of our own. Buy-write comes back under ``comparators``
    because it sets no floor and must not be read as protection.
    """
    symbols = [reference] + [o.symbol for o in overlays.OVERLAYS]
    panel = fetch_overlay_panel(symbols, start, end)
    if panel.empty or reference not in panel.columns:
        raise HTTPException(
            status_code=502,
            detail="Could not load {} — the overlay comparison needs the reference index".format(
                reference
            ),
        )
    try:
        return overlays.compare(panel, reference, risk_free_rate=risk_free_rate)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
