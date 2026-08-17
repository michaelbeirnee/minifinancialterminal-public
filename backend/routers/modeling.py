"""Modeling endpoints: seed a DCF, value it, save it, come back to it.

Three verbs, in the order they get used:

* ``seed`` reads the filed statements and proposes every assumption,
* ``value`` runs a set of assumptions and returns the projection, the answer
  and a sensitivity grid — without saving anything, so the sliders can move
  freely,
* the ``models`` CRUD keeps the ones worth keeping.

Everything is scoped to the authenticated user; each query filters on
``user_id`` so one account can never read or mutate another's models.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..core.errors import MFTError
from ..database import get_db
from ..models import User, ValuationModel
from ..providers import yahoo
from ..schemas import (
    DCFAssumptions,
    ValuationModelCreate,
    ValuationModelFull,
    ValuationModelOut,
    ValuationModelUpdate,
    ValuationRequest,
)
from ..valuation import dcf, seed as seeding

router = APIRouter(prefix="/api/modeling", tags=["modeling"])

# The grid either side of the model's own discount rate and terminal
# assumption: five steps is enough to read a gradient off, and few enough to
# stay legible on one screen.
_RATE_STEPS = (-0.02, -0.01, 0.0, 0.01, 0.02)
_GROWTH_STEPS = (-0.01, -0.005, 0.0, 0.005, 0.01)
_MULTIPLE_STEPS = (-4.0, -2.0, 0.0, 2.0, 4.0)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _price(symbol: str) -> Optional[float]:
    """Last price, for the upside line. Never fatal — a model is still a model."""
    try:
        quote = yahoo.quote(symbol)
    except Exception:  # noqa: BLE001
        return None
    for key in ("last_price", "price", "regularMarketPrice"):
        value = quote.get(key) if isinstance(quote, dict) else None
        if value:
            return float(value)
    return None


def _run(symbol: str, assumptions: DCFAssumptions, with_sensitivity: bool) -> Dict[str, Any]:
    """Value one set of assumptions and dress the result for display."""
    payload = assumptions.model_dump()
    try:
        model = dcf.Assumptions(**payload)
        result = dcf.value(model)
    except dcf.ModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    price = _price(symbol)
    out: Dict[str, Any] = result.to_dict()
    out["symbol"] = symbol.upper()
    out["price"] = price
    out["upside"] = (result.value_per_share / price - 1.0) if price else None
    out["assumptions"] = payload

    if with_sensitivity:
        rate = result.discount_rate
        rates = [round(rate + step, 6) for step in _RATE_STEPS if rate + step > 0]
        if assumptions.terminal_method == "exit_multiple":
            axis = [round(assumptions.exit_multiple + s, 4) for s in _MULTIPLE_STEPS
                    if assumptions.exit_multiple + s > 0]
        else:
            axis = [round(assumptions.terminal_growth + s, 6) for s in _GROWTH_STEPS]
        out["sensitivity"] = dcf.sensitivity(model, rates, axis)
    return out


# --------------------------------------------------------------------------- #
# Building a model
# --------------------------------------------------------------------------- #
@router.get("/seed", summary="Assumptions pre-filled from the company's own filings")
def seed_model(
    symbol: str = Query(..., min_length=1, max_length=32),
    years: int = Query(5, ge=1, le=20),
    _user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """A DCF opened on this company, already filled in.

    Returns the proposed ``assumptions``, the ``evidence`` each one was read
    off, and the ``valuation`` those assumptions produce — so the screen has
    something on it before the operator has touched a control.
    """
    try:
        assumptions, evidence = seeding.seed(symbol, years=years)
    except MFTError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - a bad ticker must not 500
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        parsed = DCFAssumptions(**assumptions)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=422,
            detail="{} does not report enough to seed a DCF: {}".format(symbol.upper(), exc),
        )
    return {
        "symbol": symbol.upper(),
        "assumptions": parsed.model_dump(),
        "evidence": evidence,
        "valuation": _run(symbol, parsed, with_sensitivity=True),
    }


@router.post("/value", summary="Run a set of assumptions without saving them")
def value_model(
    body: ValuationRequest,
    _user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    return _run(body.symbol, body.assumptions, with_sensitivity=body.sensitivity)


# --------------------------------------------------------------------------- #
# Saved models
# --------------------------------------------------------------------------- #
@router.get("/models", response_model=List[ValuationModelOut], summary="Every saved model")
def list_models(
    symbol: Optional[str] = Query(None, max_length=32),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[ValuationModel]:
    query = db.query(ValuationModel).filter(ValuationModel.user_id == user.id)
    if symbol:
        query = query.filter(ValuationModel.symbol == symbol.upper().strip())
    return query.order_by(ValuationModel.updated_at.desc()).all()


def _owned(model_id: int, user: User, db: Session) -> ValuationModel:
    model = (
        db.query(ValuationModel)
        .filter(ValuationModel.id == model_id, ValuationModel.user_id == user.id)
        .first()
    )
    if model is None:
        raise HTTPException(status_code=404, detail="No saved model with id {}".format(model_id))
    return model


@router.get("/models/{model_id}", response_model=ValuationModelFull,
            summary="One saved model, as it was saved")
def get_model(
    model_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ValuationModel:
    return _owned(model_id, user, db)


@router.get("/models/{model_id}/rerun", summary="Re-run a saved model against today's market")
def rerun_model(
    model_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """The same assumptions, valued now.

    The stored answer is left untouched: this returns ``saved`` beside ``now``
    so a model can be compared against itself rather than quietly rewritten.
    """
    model = _owned(model_id, user, db)
    assumptions = DCFAssumptions(**(model.assumptions or {}))
    return {
        "id": model.id,
        "symbol": model.symbol,
        "name": model.name,
        "saved": {
            "value_per_share": model.value_per_share,
            "price": model.price_at_save,
            "at": model.updated_at.isoformat() if model.updated_at else None,
        },
        "now": _run(model.symbol, assumptions, with_sensitivity=True),
    }


def _refuse_duplicate_name(name: str, user: User, db: Session,
                           exclude_id: Optional[int] = None) -> None:
    """Names are unique per user in the database; say so in HTTP, not in a 500."""
    query = db.query(ValuationModel).filter(
        ValuationModel.user_id == user.id, ValuationModel.name == name)
    if exclude_id is not None:
        query = query.filter(ValuationModel.id != exclude_id)
    if query.first() is not None:
        raise HTTPException(
            status_code=409,
            detail="You already have a model called {!r}. Rename it or update that one."
            .format(name),
        )


@router.post("/models", response_model=ValuationModelFull,
             status_code=status.HTTP_201_CREATED, summary="Save a model")
def create_model(
    body: ValuationModelCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ValuationModel:
    symbol = body.symbol.upper().strip()
    _refuse_duplicate_name(body.name, user, db)
    valuation = _run(symbol, body.assumptions, with_sensitivity=False)
    model = ValuationModel(
        user_id=user.id,
        name=body.name,
        symbol=symbol,
        kind="dcf",
        assumptions=body.assumptions.model_dump(),
        valuation=valuation,
        value_per_share=valuation.get("value_per_share"),
        price_at_save=valuation.get("price"),
        note=body.note,
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    return model


@router.put("/models/{model_id}", response_model=ValuationModelFull, summary="Update a model")
def update_model(
    model_id: int,
    body: ValuationModelUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ValuationModel:
    model = _owned(model_id, user, db)
    if body.name is not None:
        _refuse_duplicate_name(body.name, user, db, exclude_id=model_id)
        model.name = body.name
    if body.note is not None:
        model.note = body.note
    if body.assumptions is not None:
        valuation = _run(model.symbol, body.assumptions, with_sensitivity=False)
        model.assumptions = body.assumptions.model_dump()
        model.valuation = valuation
        model.value_per_share = valuation.get("value_per_share")
        model.price_at_save = valuation.get("price")
    model.updated_at = _utcnow()
    db.commit()
    db.refresh(model)
    return model


@router.delete("/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Delete a model")
def delete_model(
    model_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    db.delete(_owned(model_id, user, db))
    db.commit()
