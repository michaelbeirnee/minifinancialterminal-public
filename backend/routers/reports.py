"""Report endpoints: render an HTML tearsheet for a stored backtest run."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..backtest.engine import run_backtest
from ..data.provider import get_history, get_price_panel
from ..database import get_db
from ..models import BacktestRun, User
from ..reports.generator import render_html_report

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/{run_id}", response_class=HTMLResponse)
def report(
    run_id: int,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    run = (
        db.query(BacktestRun)
        .filter(BacktestRun.id == run_id, BacktestRun.owner_id == current.id)
        .first()
    )
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    symbols = run.symbols.split(",")
    params = json.loads(run.params_json or "{}")

    # Re-run deterministically to rebuild the equity curve for rendering.
    prices = get_price_panel(symbols, run.start, run.end or None)
    result = run_backtest(prices=prices, strategy=run.strategy, params=params)

    bench = None
    try:
        b = get_history("SPY", run.start, run.end or None)["close"]
        b = b.reindex(result.equity.index).ffill()
        bench = result.equity.iloc[0] * b / b.iloc[0]
    except Exception:  # noqa: BLE001
        bench = None

    title = f"{run.strategy.upper()} | {run.symbols} | {run.start}..{run.end or 'now'}"
    html = render_html_report(title, result.metrics, result.equity, bench)
    return HTMLResponse(content=html)
