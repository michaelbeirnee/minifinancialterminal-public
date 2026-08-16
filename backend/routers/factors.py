"""Factor-model endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_user
from ..data.provider import get_price_panel
from ..factors.models import analyze_universe
from ..models import User
from ..schemas import FactorRequest

router = APIRouter(prefix="/api/factors", tags=["factors"])


@router.post("/analyze")
def analyze(req: FactorRequest, _: User = Depends(get_current_user)) -> dict:
    try:
        panel = get_price_panel(req.symbols, req.start, req.end)
        if panel.shape[1] < 2:
            raise ValueError("Factor analysis needs at least 2 symbols")
        result = analyze_universe(panel, req.rf_annual)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result["symbols"] = list(panel.columns)
    result["period"] = {"start": req.start, "end": req.end, "rows": len(panel)}
    return result
