"""Saved-actions endpoints: settings, saved commands, history, watchlists, alerts.

Everything here is scoped to the authenticated user — each query filters on
``user_id`` so one account can never read or mutate another's rows.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..core.errors import MFTError
from ..core.registry import execute, get_spec
from ..database import get_db
from ..models import (
    Alert,
    BacktestRun,
    CommandRun,
    SavedCommand,
    SavedResult,
    User,
    UserSetting,
    Watchlist,
    WatchlistItem,
)
from ..schemas import (
    AlertCreate,
    AlertOut,
    AlertUpdate,
    CommandRunOut,
    SavedCommandCreate,
    SavedCommandOut,
    SavedCommandUpdate,
    SavedResultCreate,
    SavedResultFull,
    SavedResultOut,
    SettingIn,
    SettingOut,
    WatchlistCreate,
    WatchlistItemIn,
    WatchlistItemOut,
    WatchlistOut,
    WatchlistUpdate,
)

router = APIRouter(prefix="/api/user", tags=["user data"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@router.get("/settings", response_model=List[SettingOut])
def list_settings(
    current: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> List[UserSetting]:
    return db.query(UserSetting).filter(UserSetting.user_id == current.id).all()


@router.put("/settings", response_model=SettingOut)
def put_setting(
    payload: SettingIn,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserSetting:
    """Upsert one preference."""
    setting = (
        db.query(UserSetting)
        .filter(UserSetting.user_id == current.id, UserSetting.key == payload.key)
        .first()
    )
    if setting is None:
        setting = UserSetting(user_id=current.id, key=payload.key)
        db.add(setting)
    setting.value = payload.value
    db.commit()
    db.refresh(setting)
    return setting


@router.delete("/settings/{key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_setting(
    key: str, current: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    deleted = (
        db.query(UserSetting)
        .filter(UserSetting.user_id == current.id, UserSetting.key == key)
        .delete()
    )
    db.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="No setting called {!r}".format(key))


# --------------------------------------------------------------------------- #
# Saved commands
# --------------------------------------------------------------------------- #
@router.get("/saved", response_model=List[SavedCommandOut])
def list_saved(
    favorites_only: bool = False,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[SavedCommand]:
    query = db.query(SavedCommand).filter(SavedCommand.user_id == current.id)
    if favorites_only:
        query = query.filter(SavedCommand.is_favorite.is_(True))
    return query.order_by(SavedCommand.is_favorite.desc(), SavedCommand.name).all()


@router.post("/saved", response_model=SavedCommandOut, status_code=status.HTTP_201_CREATED)
def create_saved(
    payload: SavedCommandCreate,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SavedCommand:
    try:
        spec = get_spec(payload.command_path)
    except MFTError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    accepted = {p["name"] for p in spec.parameters}
    unknown = set(payload.parameters) - accepted
    if unknown:
        raise HTTPException(
            status_code=400,
            detail="{} does not accept: {}".format(spec.path, ", ".join(sorted(unknown))),
        )
    if _saved_by_name(db, current.id, payload.name):
        raise HTTPException(status_code=400, detail="You already saved something called that")

    saved = SavedCommand(
        user_id=current.id,
        name=payload.name,
        command_path=spec.path,
        parameters=payload.parameters,
        description=payload.description,
        is_favorite=payload.is_favorite,
    )
    db.add(saved)
    db.commit()
    db.refresh(saved)
    return saved


def _log_run(
    db: Session,
    user_id: int,
    saved: SavedCommand,
    clock: float,
    result: Any,
    status: str,
    error: Optional[str],
) -> None:
    """Mirror a saved-command run into history, so recent activity is complete."""
    db.add(
        CommandRun(
            user_id=user_id,
            command_path=saved.command_path,
            parameters=dict(saved.parameters or {}),
            provider=getattr(result, "provider", None),
            status=status,
            row_count=(len(result) if result is not None else None),
            duration_ms=int((time.perf_counter() - clock) * 1000),
            error=(error[:2000] if error else None),
        )
    )


def _saved_by_name(db: Session, user_id: int, name: str) -> Optional[SavedCommand]:
    return (
        db.query(SavedCommand)
        .filter(SavedCommand.user_id == user_id, SavedCommand.name == name)
        .first()
    )


def _owned_saved(db: Session, user_id: int, saved_id: int) -> SavedCommand:
    saved = (
        db.query(SavedCommand)
        .filter(SavedCommand.id == saved_id, SavedCommand.user_id == user_id)
        .first()
    )
    if saved is None:
        raise HTTPException(status_code=404, detail="Saved command not found")
    return saved


@router.patch("/saved/{saved_id}", response_model=SavedCommandOut)
def update_saved(
    saved_id: int,
    payload: SavedCommandUpdate,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SavedCommand:
    saved = _owned_saved(db, current.id, saved_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(saved, field, value)
    db.commit()
    db.refresh(saved)
    return saved


@router.delete("/saved/{saved_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_saved(
    saved_id: int, current: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    db.delete(_owned_saved(db, current.id, saved_id))
    db.commit()


@router.post("/saved/{saved_id}/run")
def run_saved(
    saved_id: int,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Execute a saved command and bump its usage counters."""
    saved = _owned_saved(db, current.id, saved_id)
    started = _utcnow()
    clock = time.perf_counter()
    try:
        result = execute(saved.command_path, **(saved.parameters or {}))
    except MFTError as exc:
        _log_run(db, current.id, saved, clock, None, "error", str(exc))
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    except (ValueError, TypeError, KeyError) as exc:
        detail = "{}: {}".format(type(exc).__name__, exc)
        _log_run(db, current.id, saved, clock, None, "error", detail)
        raise HTTPException(status_code=400, detail=detail)

    saved.last_run_at = started
    saved.run_count = (saved.run_count or 0) + 1
    _log_run(db, current.id, saved, clock, result, "ok", None)
    db.commit()
    payload = result.to_dict()
    payload["saved_command"] = {"id": saved.id, "name": saved.name, "path": saved.command_path}
    return payload


# --------------------------------------------------------------------------- #
# Saved results — snapshots of command output
# --------------------------------------------------------------------------- #
# Guardrails so one snapshot cannot bloat the database: rows beyond the cap are
# dropped (and flagged), and grossly oversized payloads are rejected outright.
MAX_RESULT_ROWS = 5000
MAX_RESULT_BYTES = 2_000_000


@router.get("/results", response_model=List[SavedResultOut])
def list_results(
    current: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> List[SavedResult]:
    return (
        db.query(SavedResult)
        .filter(SavedResult.user_id == current.id)
        .order_by(SavedResult.created_at.desc(), SavedResult.id.desc())
        .limit(200)
        .all()
    )


@router.post("/results", response_model=SavedResultOut, status_code=status.HTTP_201_CREATED)
def create_result(
    payload: SavedResultCreate,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SavedResult:
    import json

    rows = payload.results
    truncated = len(rows) > MAX_RESULT_ROWS
    if truncated:
        rows = rows[:MAX_RESULT_ROWS]
    size = len(json.dumps(rows, default=str))
    if size > MAX_RESULT_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Result set is {:.1f} MB — too large to store. Narrow the query "
                   "or use the CSV download instead.".format(size / 1e6),
        )
    saved = SavedResult(
        user_id=current.id,
        name=payload.name,
        command_path=payload.command_path,
        parameters=payload.parameters,
        provider=payload.provider,
        row_count=len(rows),
        truncated=truncated,
        results=rows,
        note=payload.note,
    )
    db.add(saved)
    db.commit()
    db.refresh(saved)
    return saved


def _owned_result(db: Session, user_id: int, result_id: int) -> SavedResult:
    result = (
        db.query(SavedResult)
        .filter(SavedResult.id == result_id, SavedResult.user_id == user_id)
        .first()
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Saved result not found")
    return result


@router.get("/results/{result_id}", response_model=SavedResultFull)
def get_result(
    result_id: int, current: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> SavedResult:
    return _owned_result(db, current.id, result_id)


@router.delete("/results/{result_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_result(
    result_id: int, current: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    db.delete(_owned_result(db, current.id, result_id))
    db.commit()


# --------------------------------------------------------------------------- #
# Command history
# --------------------------------------------------------------------------- #
@router.get("/history", response_model=List[CommandRunOut])
def list_history(
    limit: int = Query(50, ge=1, le=500),
    command_path: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[CommandRun]:
    query = db.query(CommandRun).filter(CommandRun.user_id == current.id)
    if command_path:
        query = query.filter(CommandRun.command_path.like("{}%".format(command_path)))
    if status_filter:
        query = query.filter(CommandRun.status == status_filter)
    return query.order_by(CommandRun.created_at.desc(), CommandRun.id.desc()).limit(limit).all()


@router.delete("/history")
def clear_history(
    current: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Dict[str, int]:
    deleted = db.query(CommandRun).filter(CommandRun.user_id == current.id).delete()
    db.commit()
    return {"deleted": deleted}


@router.get("/stats")
def stats(current: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Everything this account has stored, at a glance."""
    runs = db.query(CommandRun).filter(CommandRun.user_id == current.id)
    top = (
        db.query(CommandRun.command_path, func.count(CommandRun.id).label("runs"))
        .filter(CommandRun.user_id == current.id)
        .group_by(CommandRun.command_path)
        .order_by(func.count(CommandRun.id).desc())
        .limit(10)
        .all()
    )
    return {
        "username": current.username,
        "member_since": current.created_at,
        "last_login_at": current.last_login_at,
        "login_count": current.login_count,
        "saved_commands": db.query(SavedCommand).filter(SavedCommand.user_id == current.id).count(),
        "saved_results": db.query(SavedResult).filter(SavedResult.user_id == current.id).count(),
        "watchlists": db.query(Watchlist).filter(Watchlist.user_id == current.id).count(),
        "alerts": db.query(Alert).filter(Alert.user_id == current.id).count(),
        "backtests": db.query(BacktestRun).filter(BacktestRun.owner_id == current.id).count(),
        "command_runs": runs.count(),
        "failed_runs": runs.filter(CommandRun.status != "ok").count(),
        "most_used": [{"command_path": path, "runs": count} for path, count in top],
    }


# --------------------------------------------------------------------------- #
# Watchlists
# --------------------------------------------------------------------------- #
def _owned_watchlist(db: Session, user_id: int, watchlist_id: int) -> Watchlist:
    watchlist = (
        db.query(Watchlist)
        .filter(Watchlist.id == watchlist_id, Watchlist.user_id == user_id)
        .first()
    )
    if watchlist is None:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    return watchlist


@router.get("/watchlists", response_model=List[WatchlistOut])
def list_watchlists(
    current: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> List[Watchlist]:
    return (
        db.query(Watchlist)
        .filter(Watchlist.user_id == current.id)
        .order_by(Watchlist.is_default.desc(), Watchlist.name)
        .all()
    )


@router.post("/watchlists", response_model=WatchlistOut, status_code=status.HTTP_201_CREATED)
def create_watchlist(
    payload: WatchlistCreate,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Watchlist:
    clash = (
        db.query(Watchlist)
        .filter(Watchlist.user_id == current.id, Watchlist.name == payload.name)
        .first()
    )
    if clash:
        raise HTTPException(status_code=400, detail="You already have a watchlist with that name")

    watchlist = Watchlist(
        user_id=current.id,
        name=payload.name,
        description=payload.description,
        is_default=payload.is_default,
    )
    for position, symbol in enumerate(_clean_symbols(payload.symbols)):
        watchlist.items.append(WatchlistItem(symbol=symbol, position=position))
    if payload.is_default:
        _clear_default(db, current.id)
    db.add(watchlist)
    db.commit()
    db.refresh(watchlist)
    return watchlist


def _clean_symbols(symbols: List[str]) -> List[str]:
    out: List[str] = []
    for raw in symbols:
        symbol = str(raw).strip().upper()
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def _clear_default(db: Session, user_id: int) -> None:
    db.query(Watchlist).filter(
        Watchlist.user_id == user_id, Watchlist.is_default.is_(True)
    ).update({"is_default": False})


@router.get("/watchlists/{watchlist_id}", response_model=WatchlistOut)
def get_watchlist(
    watchlist_id: int, current: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Watchlist:
    return _owned_watchlist(db, current.id, watchlist_id)


@router.patch("/watchlists/{watchlist_id}", response_model=WatchlistOut)
def update_watchlist(
    watchlist_id: int,
    payload: WatchlistUpdate,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Watchlist:
    watchlist = _owned_watchlist(db, current.id, watchlist_id)
    fields = payload.model_dump(exclude_unset=True)
    if fields.get("is_default"):
        _clear_default(db, current.id)
    for field, value in fields.items():
        setattr(watchlist, field, value)
    db.commit()
    db.refresh(watchlist)
    return watchlist


@router.delete("/watchlists/{watchlist_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_watchlist(
    watchlist_id: int, current: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    db.delete(_owned_watchlist(db, current.id, watchlist_id))
    db.commit()


@router.post("/watchlists/{watchlist_id}/items", response_model=WatchlistItemOut,
             status_code=status.HTTP_201_CREATED)
def add_watchlist_item(
    watchlist_id: int,
    payload: WatchlistItemIn,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WatchlistItem:
    watchlist = _owned_watchlist(db, current.id, watchlist_id)
    symbol = payload.symbol.strip().upper()
    if any(item.symbol == symbol for item in watchlist.items):
        raise HTTPException(status_code=400, detail="{} is already on this list".format(symbol))
    item = WatchlistItem(
        watchlist_id=watchlist.id,
        symbol=symbol,
        asset_type=payload.asset_type,
        note=payload.note,
        position=len(watchlist.items),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/watchlists/{watchlist_id}/items/{symbol}", status_code=status.HTTP_204_NO_CONTENT)
def remove_watchlist_item(
    watchlist_id: int,
    symbol: str,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    watchlist = _owned_watchlist(db, current.id, watchlist_id)
    deleted = (
        db.query(WatchlistItem)
        .filter(
            WatchlistItem.watchlist_id == watchlist.id,
            WatchlistItem.symbol == symbol.strip().upper(),
        )
        .delete()
    )
    db.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="{} is not on this list".format(symbol))


@router.get("/watchlists/{watchlist_id}/quotes")
def watchlist_quotes(
    watchlist_id: int, current: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Live quotes for every symbol on the list."""
    watchlist = _owned_watchlist(db, current.id, watchlist_id)
    symbols = [item.symbol for item in watchlist.items]
    if not symbols:
        return {"watchlist": watchlist.name, "results": [], "warnings": ["Watchlist is empty"]}
    result = execute("/equity/price/quote", symbol=",".join(symbols))
    notes = {item.symbol: item.note for item in watchlist.items}
    rows = result.to_records()
    for row in rows:
        row["note"] = notes.get(row.get("symbol"))
    return {"watchlist": watchlist.name, "results": rows, "warnings": result.warnings}


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
def _owned_alert(db: Session, user_id: int, alert_id: int) -> Alert:
    alert = db.query(Alert).filter(Alert.id == alert_id, Alert.user_id == user_id).first()
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    return alert


@router.get("/alerts", response_model=List[AlertOut])
def list_alerts(
    active_only: bool = False,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[Alert]:
    query = db.query(Alert).filter(Alert.user_id == current.id)
    if active_only:
        query = query.filter(Alert.is_active.is_(True))
    return query.order_by(Alert.symbol).all()


@router.post("/alerts", response_model=AlertOut, status_code=status.HTTP_201_CREATED)
def create_alert(
    payload: AlertCreate,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Alert:
    alert = Alert(
        user_id=current.id,
        symbol=payload.symbol.strip().upper(),
        condition=payload.condition,
        threshold=payload.threshold,
        note=payload.note,
        is_active=payload.is_active,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return alert


@router.patch("/alerts/{alert_id}", response_model=AlertOut)
def update_alert(
    alert_id: int,
    payload: AlertUpdate,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Alert:
    alert = _owned_alert(db, current.id, alert_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(alert, field, value)
    db.commit()
    db.refresh(alert)
    return alert


@router.delete("/alerts/{alert_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_alert(
    alert_id: int, current: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    db.delete(_owned_alert(db, current.id, alert_id))
    db.commit()


@router.post("/alerts/evaluate")
def evaluate_alerts(
    current: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Check every active alert against a live quote.

    Evaluated on demand rather than by a background scheduler — there is no
    worker process here, so a "triggered" result reflects this moment only.
    """
    alerts = db.query(Alert).filter(Alert.user_id == current.id, Alert.is_active.is_(True)).all()
    if not alerts:
        return {"checked": 0, "triggered": [], "results": []}

    symbols = sorted({alert.symbol for alert in alerts})
    quotes: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    try:
        result = execute("/equity/price/quote", symbol=",".join(symbols))
        warnings = list(result.warnings)
        quotes = {row.get("symbol"): row for row in result.to_records()}
    except MFTError as exc:
        warnings.append(str(exc))

    now = _utcnow()
    rows: List[Dict[str, Any]] = []
    for alert in alerts:
        quote = quotes.get(alert.symbol)
        price = quote.get("last_price") if quote else None
        change_pct = (quote.get("change_percent") if quote else None)
        observed = price if alert.condition.startswith("price") else (
            None if change_pct is None else change_pct * 100
        )
        triggered = False
        if observed is not None:
            if alert.condition in ("price_above", "pct_change_above"):
                triggered = observed > alert.threshold
            else:
                triggered = observed < alert.threshold

        alert.last_checked_at = now
        alert.last_value = observed
        if triggered:
            alert.last_triggered_at = now
            alert.trigger_count = (alert.trigger_count or 0) + 1

        rows.append(
            {
                "id": alert.id, "symbol": alert.symbol, "condition": alert.condition,
                "threshold": alert.threshold, "observed": observed, "price": price,
                "triggered": triggered, "note": alert.note,
                "unavailable": observed is None,
            }
        )
    db.commit()
    return {
        "checked": len(rows),
        "triggered": [r for r in rows if r["triggered"]],
        "results": rows,
        "warnings": warnings,
    }
