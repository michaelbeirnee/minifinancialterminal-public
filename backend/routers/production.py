"""Production trading endpoints: registry, daily cycle, ledger, reconciliation.

Everything here is deliberately boring: promote research into a frozen
vintage, run the deterministic daily cycle, read back what it did, and
reconcile the ledger against the broker. Order submission requires both the
per-request ``orders_enabled`` flag and the MFT_TRADING_ENABLED kill switch.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from sqlalchemy import func

from ..auth import get_current_user
from ..backtest.multisource_research import archive_current_snapshots
from ..config import settings
from ..database import get_db
from ..models import (
    ProductionOrder,
    ProductionRun,
    ProductionSignalVintage,
    RawObservation,
    User,
)
from ..schemas import CaptureRequest, ProductionRunRequest, ReconcileRequest, VintagePromoteRequest
from ..trading.production import (
    latest_approved_vintage,
    reconcile,
    research_and_promote,
    resolve_capture_universe,
    run_daily_cycle,
)

router = APIRouter(prefix="/api/production", tags=["production"])


def _vintage_row(v: ProductionSignalVintage, full: bool = False) -> dict:
    row = {
        "id": v.id,
        "status": v.status,
        "as_of": v.as_of,
        "symbols": v.symbols.split(","),
        "signal_count": len(v.blend or []),
        "sleeve_count": len(v.sleeves or []),
        "notes": v.notes,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "retired_at": v.retired_at.isoformat() if v.retired_at else None,
    }
    if full:
        row.update({
            "params": v.params, "blend": v.blend, "sleeves": v.sleeves,
            "evidence": v.evidence, "config": v.config,
        })
    return row


def _run_row(r: ProductionRun, full: bool = False) -> dict:
    row = {
        "id": r.id,
        "as_of": r.as_of,
        "status": r.status,
        "vintage_id": r.vintage_id,
        "broker": r.broker,
        "orders_enabled": r.orders_enabled,
        "nav": r.nav,
        "risk_model_as_of": r.risk_model_as_of,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
    if full:
        row.update({
            "target": r.target, "risk": r.risk, "gateway": r.gateway,
            "stages": r.stages, "config": r.config,
        })
    return row


@router.get("/status")
def production_status(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    vintage = latest_approved_vintage(db)
    last_run = db.query(ProductionRun).order_by(ProductionRun.id.desc()).first()
    open_orders = db.query(ProductionOrder).filter(ProductionOrder.status == "submitted").count()
    return {
        "trading_enabled": bool(settings.trading_enabled),
        "approved_vintage": None if vintage is None else _vintage_row(vintage),
        "last_run": None if last_run is None else _run_row(last_run),
        "open_orders": open_orders,
    }


@router.post("/vintages/promote")
def promote(
    req: VintagePromoteRequest,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Run multisource research end to end and freeze the surviving blend."""
    try:
        vintage, report = research_and_promote(
            db,
            symbols=req.symbols,
            start=req.start,
            end=req.end,
            params=req.params,
            research_kwargs=req.research,
            sleeve_kwargs=req.sleeves,
            notes=req.notes,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "vintage": _vintage_row(vintage, full=True),
        "validated_signals": sum(1 for row in report.get("signals", []) if row.get("validated")),
        "tested_signals": len(report.get("signals", [])),
    }


@router.get("/vintages")
def list_vintages(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    rows = db.query(ProductionSignalVintage).order_by(ProductionSignalVintage.id.desc()).limit(50).all()
    return {"vintages": [_vintage_row(v) for v in rows]}


@router.get("/vintages/{vintage_id}")
def vintage_detail(
    vintage_id: int, _: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    v = db.get(ProductionSignalVintage, vintage_id)
    if v is None:
        raise HTTPException(status_code=404, detail="No such vintage")
    return _vintage_row(v, full=True)


@router.post("/vintages/{vintage_id}/retire")
def retire_vintage(
    vintage_id: int, _: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    from datetime import datetime, timezone

    v = db.get(ProductionSignalVintage, vintage_id)
    if v is None:
        raise HTTPException(status_code=404, detail="No such vintage")
    v.status = "retired"
    v.retired_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    return _vintage_row(v)


@router.post("/run")
def run_cycle(
    req: ProductionRunRequest,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Execute one deterministic daily cycle (records everything, pass or fail)."""
    try:
        run = run_daily_cycle(
            db,
            orders_enabled=req.orders_enabled,
            broker_kind=req.broker,
            capture_snapshots=req.capture_snapshots,
            as_of=req.as_of,
            config=req.config,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    orders = db.query(ProductionOrder).filter(ProductionOrder.run_id == run.id).all()
    payload = _run_row(run, full=True)
    payload["orders"] = [
        {
            "symbol": o.symbol, "side": o.side, "qty": o.qty,
            "limit_price": o.limit_price, "decision_price": o.decision_price,
            "status": o.status, "reason": o.reason,
        }
        for o in orders
    ]
    return payload


@router.get("/runs")
def list_runs(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    rows = db.query(ProductionRun).order_by(ProductionRun.id.desc()).limit(50).all()
    return {"runs": [_run_row(r) for r in rows]}


@router.get("/runs/{run_id}")
def run_detail(run_id: int, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    r = db.get(ProductionRun, run_id)
    if r is None:
        raise HTTPException(status_code=404, detail="No such run")
    payload = _run_row(r, full=True)
    orders = db.query(ProductionOrder).filter(ProductionOrder.run_id == r.id).all()
    payload["orders"] = [
        {
            "symbol": o.symbol, "side": o.side, "qty": o.qty,
            "limit_price": o.limit_price, "decision_price": o.decision_price,
            "status": o.status, "reason": o.reason, "broker_id": o.broker_id,
            "fill_qty": o.fill_qty, "fill_price": o.fill_price,
            "fees": o.fees, "filled_at": o.filled_at,
        }
        for o in orders
    ]
    return payload


@router.post("/capture")
def capture_snapshots(
    req: CaptureRequest,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Archive today's raw payloads + derived features. Needs no vintage."""
    symbols = resolve_capture_universe(db, req.symbols or None)
    try:
        result = archive_current_snapshots(symbols, db)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "as_of": result["as_of"],
        "symbols": len(symbols),
        "feature_rows": len(result["captured"]),
        "raw_rows": result["raw_rows"],
        "rate_limited_symbols": result.get("rate_limited_symbols", []),
        "warnings": result["warnings"][:25],
    }


@router.get("/observations")
def observation_summary(
    _: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    """What the raw archive holds: row counts by day and by source."""
    by_day = (
        db.query(RawObservation.as_of_date, func.count(RawObservation.id))
        .group_by(RawObservation.as_of_date)
        .order_by(RawObservation.as_of_date.desc())
        .limit(30)
        .all()
    )
    by_source = (
        db.query(RawObservation.source, func.count(RawObservation.id))
        .group_by(RawObservation.source)
        .order_by(func.count(RawObservation.id).desc())
        .all()
    )
    total = db.query(func.count(RawObservation.id)).scalar() or 0
    return {
        "total_rows": int(total),
        "days": [{"as_of": d, "rows": int(n)} for d, n in by_day],
        "sources": [{"source": s, "rows": int(n)} for s, n in by_source],
    }


@router.post("/reconcile")
def run_reconcile(
    req: ReconcileRequest,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Ingest fills, rebuild ledger positions, compare with the broker."""
    try:
        return reconcile(db, broker_kind=req.broker)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
