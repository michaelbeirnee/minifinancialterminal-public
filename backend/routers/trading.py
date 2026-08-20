"""Paper trading endpoints: strategies, the live session, and replay.

The live session and the replay run the same strategy object through the
same engine — the endpoints exist so the UI (and curl) can prove it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import get_current_user
from ..config import settings
from ..core.errors import MFTError
from ..models import User
from ..stream.hub import normalise_symbols
from ..trading import manager, replay
from ..trading.broker import alpaca_execution_available
from ..trading.strategies import build, catalog

router = APIRouter(prefix="/api/trading", tags=["trading"])

MAX_SYMBOLS = 20
MAX_CODE_CHARS = 100_000


class StartRequest(BaseModel):
    strategy: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    code: Optional[str] = Field(None, max_length=MAX_CODE_CHARS)
    symbols: str
    cash: float = Field(100_000.0, gt=0)
    bar_seconds: int = Field(60, ge=5, le=3600)
    provider: Optional[str] = None
    execution: str = Field("internal", pattern="^(internal|alpaca)$")
    limits: Dict[str, float] = Field(default_factory=dict)


class ReplayRequest(BaseModel):
    strategy: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    code: Optional[str] = Field(None, max_length=MAX_CODE_CHARS)
    symbols: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    interval: str = "1d"
    # "history" replays vendor bars; "ticks" builds bars of ``bar_seconds``
    # from the recorded tape — the honest backtest for tick-driven strategies.
    source: str = Field("history", pattern="^(history|ticks)$")
    bar_seconds: int = Field(60, ge=1, le=86400)
    cash: float = Field(100_000.0, gt=0)
    slippage_bps: float = Field(2.0, ge=0, le=100)
    commission_per_share: float = Field(0.0, ge=0, le=1)
    limits: Dict[str, float] = Field(default_factory=dict)


class KillRequest(BaseModel):
    flatten: bool = False


def _symbols(raw: str) -> List[str]:
    try:
        syms = normalise_symbols([raw])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not syms:
        raise HTTPException(status_code=422, detail="Give at least one symbol")
    if len(syms) > MAX_SYMBOLS:
        raise HTTPException(status_code=422,
                            detail="At most {} symbols per session".format(MAX_SYMBOLS))
    return syms


@router.get("/strategies")
def strategies(_: User = Depends(get_current_user)) -> Dict[str, Any]:
    """The built-in strategies, and whether custom code is allowed here."""
    return {"strategies": catalog(), "custom_allowed": settings.playground_on,
            "alpaca_execution_available": alpaca_execution_available()}


@router.get("/paper")
def paper_status(user: User = Depends(get_current_user)) -> Dict[str, Any]:
    """The caller's session, or an explicit none."""
    session = manager.get(user.id)
    return {"session": session.status() if session else None}


@router.post("/paper/start")
async def paper_start(payload: StartRequest, user: User = Depends(get_current_user)) -> Dict[str, Any]:
    """Start a live paper session on the streaming feed."""
    syms = _symbols(payload.symbols)
    try:
        session = await manager.start(
            user.id, payload.strategy, payload.params, payload.code, syms,
            cash=payload.cash, limits=payload.limits or None,
            bar_seconds=payload.bar_seconds, provider=payload.provider,
            execution=payload.execution)
    except (ValueError, PermissionError, RuntimeError) as exc:
        code = 403 if isinstance(exc, PermissionError) else 409 if isinstance(exc, RuntimeError) else 422
        raise HTTPException(status_code=code, detail=str(exc))
    except MFTError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return session.status()


@router.post("/paper/stop")
async def paper_stop(user: User = Depends(get_current_user)) -> Dict[str, Any]:
    final = await manager.stop(user.id)
    if final is None:
        raise HTTPException(status_code=404, detail="No paper session to stop")
    return final


@router.post("/paper/kill")
async def paper_kill(payload: KillRequest, user: User = Depends(get_current_user)) -> Dict[str, Any]:
    """The kill switch: block new orders, cancel open ones, optionally flatten."""
    result = await manager.kill(user.id, payload.flatten)
    if result is None:
        raise HTTPException(status_code=404, detail="No paper session to kill")
    return result


@router.post("/replay")
def replay_run(payload: ReplayRequest, user: User = Depends(get_current_user)) -> Dict[str, Any]:
    """The same strategy, fed history through the same engine."""
    syms = _symbols(payload.symbols)
    try:
        strategy = build(payload.strategy, payload.params, payload.code)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=403 if isinstance(exc, PermissionError) else 422,
                            detail=str(exc))
    if payload.source == "ticks":
        from ..stream import tickdb

        start = payload.start_date or "1970-01-01"
        end = payload.end_date or "2100-01-01"
        try:
            bars = tickdb.bars(start, end, syms, bar_seconds=payload.bar_seconds)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except MFTError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
        bars["date"] = bars["date"].astype(str)
        source_label = "tick_store ({}s bars)".format(payload.bar_seconds)
    else:
        from ..core.registry import execute

        try:
            obj = execute("/equity/price/historical", symbol=",".join(syms),
                          start_date=payload.start_date, end_date=payload.end_date,
                          interval=payload.interval)
        except MFTError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
        bars = pd.DataFrame(obj.results)
        if "symbol" not in bars.columns:      # single-symbol responses omit the column
            bars["symbol"] = syms[0]
        source_label = obj.provider
    try:
        result = replay(strategy, bars, cash=payload.cash, limits=payload.limits or None,
                        slippage_bps=payload.slippage_bps,
                        commission_per_share=payload.commission_per_share)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    result["symbols"] = syms
    result["interval"] = payload.interval if payload.source == "history" else "{}s".format(payload.bar_seconds)
    result["provider"] = source_label
    return result
