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
from ..data.provider import get_history, get_price_panel
from ..database import get_db
from ..models import BacktestRun, User
from ..portfolio import overlays
from ..schemas import (
    BacktestRequest,
    BacktestSweepRequest,
    CostSensitivityRequest,
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
