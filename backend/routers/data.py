"""Market-data endpoints (historical bars + quotes)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import get_current_user
from ..data.provider import get_history, latest_quote
from ..models import User

router = APIRouter(prefix="/api/data", tags=["data"])


@router.get("/history/{symbol}")
def history(
    symbol: str,
    start: str = Query("2022-01-01"),
    end: Optional[str] = Query(None),
    _: User = Depends(get_current_user),
) -> dict:
    try:
        df = get_history(symbol, start, end)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return {
        "symbol": symbol.upper(),
        "source": df.attrs.get("source", "unknown"),
        "rows": len(df),
        "bars": [
            {
                "date": idx.date().isoformat() if hasattr(idx, "date") else str(idx),
                "open": round(float(r.open), 4),
                "high": round(float(r.high), 4),
                "low": round(float(r.low), 4),
                "close": round(float(r.close), 4),
                "volume": int(r.volume),
            }
            for idx, r in df.iterrows()
        ],
    }


@router.get("/quote/{symbol}")
def quote(symbol: str, _: User = Depends(get_current_user)) -> dict:
    try:
        return latest_quote(symbol)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"Quote unavailable: {exc}")


@router.get("/suggest")
def suggest_symbols(
    q: str = Query(..., min_length=1, description="Partial ticker or company name"),
    limit: int = Query(10, ge=1, le=25),
    _: User = Depends(get_current_user),
) -> dict:
    """Ticker type-ahead across equities, ETFs, indices, futures, FX and crypto.

    Served from a locally cached directory (SEC register + curated non-equity
    symbols), so it is fast enough to call on every keystroke. Kept off the
    /api/v1 registry on purpose — those calls are logged to per-user history,
    and autocomplete would flood it.
    """
    from ..providers.symbols import suggest

    return {"results": suggest(q, limit)}


@router.get("/quotes")
def quotes(
    symbols: str = Query(..., description="Comma-separated tickers"),
    _: User = Depends(get_current_user),
) -> dict:
    out = []
    for sym in [s.strip() for s in symbols.split(",") if s.strip()][:25]:
        try:
            out.append(latest_quote(sym))
        except Exception:  # noqa: BLE001
            continue
    return {"quotes": out}
