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
from ..backtest.strategies import REGISTRY
from ..backtest.stat_arb import stat_arb_snapshot
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
    ResearchArchiveRequest,
    SignalResearchRequest,
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
        )
        requested = (
            list(dict.fromkeys(req.signals))
            if req.signals is not None
            else list(MULTISOURCE_SPEC_BY_NAME)
        )
        available = set(built.library.components)
        report["source_status"] = built.source_status
        report["classification_status"] = classification_status
        report["available_signal_count"] = len(available)
        report["catalog_signal_count"] = len(MULTISOURCE_SPEC_BY_NAME)
        report["unavailable_signals"] = [name for name in requested if name not in available]
        return report
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
