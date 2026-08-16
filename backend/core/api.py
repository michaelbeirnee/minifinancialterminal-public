"""Turn the command registry into REST endpoints.

Every registered command becomes ``/api/v1<command path>`` with its function
signature exposed as query parameters (or a JSON body for POST commands), so
the OpenAPI docs at ``/docs`` stay in sync with the registry automatically.

Each call is also written to ``command_runs``, which is what powers the user's
history, "most used" stats, and re-running something from last week.
"""
from __future__ import annotations

import inspect
import json
import logging
import time
import typing
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..auth import get_current_user, get_optional_user
from ..config import settings
from ..database import get_db
from .errors import MFTError
from .registry import REGISTRY, CommandSpec, coverage, execute, search

log = logging.getLogger(__name__)

# Parameters bigger than this are summarised rather than stored verbatim — an
# econometrics POST can carry a whole panel of data.
MAX_RECORDED_PARAM_BYTES = 4096


def _typed_signature(func: Callable[..., Any]) -> inspect.Signature:
    """Resolve string annotations to real types.

    ``from __future__ import annotations`` leaves every annotation as a string.
    FastAPI would try to evaluate those against *this* module's globals rather
    than the extension module they were written in, so we resolve them here
    while the owning module is still in scope.
    """
    sig = inspect.signature(func)
    try:
        hints = typing.get_type_hints(func)
    except Exception:  # noqa: BLE001
        hints = {}
    params = [
        p.replace(annotation=hints.get(name, p.annotation))
        for name, p in sig.parameters.items()
    ]
    return sig.replace(parameters=params, return_annotation=inspect.Signature.empty)


def _endpoint_signature(func: Callable[..., Any]) -> inspect.Signature:
    """The command's signature plus the two dependencies the wrapper needs."""
    sig = _typed_signature(func)
    # Reuse the router-level auth dependency when it is active so FastAPI's
    # per-request cache resolves the user once rather than twice.
    user_dep = get_current_user if settings.platform_require_auth else get_optional_user
    extra = [
        inspect.Parameter(
            "_user", inspect.Parameter.KEYWORD_ONLY,
            default=Depends(user_dep), annotation=Optional[Any],
        ),
        inspect.Parameter(
            "_db", inspect.Parameter.KEYWORD_ONLY,
            default=Depends(get_db), annotation=Session,
        ),
    ]
    return sig.replace(parameters=list(sig.parameters.values()) + extra)


def _recordable(params: Dict[str, Any]) -> Dict[str, Any]:
    """Drop unset parameters and shrink anything oversized."""
    trimmed = {k: v for k, v in params.items() if v is not None}
    try:
        if len(json.dumps(trimmed, default=str)) <= MAX_RECORDED_PARAM_BYTES:
            return trimmed
    except (TypeError, ValueError):
        pass
    summary: Dict[str, Any] = {}
    for key, value in trimmed.items():
        if isinstance(value, (list, tuple, dict)):
            summary[key] = "<{} with {} entries>".format(type(value).__name__, len(value))
        else:
            summary[key] = str(value)[:200]
    return summary


def _record_run(
    db: Optional[Session],
    user: Any,
    path: str,
    params: Dict[str, Any],
    provider: Optional[str],
    status: str,
    row_count: Optional[int],
    duration_ms: int,
    error: Optional[str],
) -> None:
    """Persist one command execution. Never lets a logging failure break the call."""
    if db is None:
        return
    from ..models import CommandRun

    try:
        run = CommandRun(
            user_id=getattr(user, "id", None),
            command_path=path,
            parameters=_recordable(params),
            provider=provider,
            status=status,
            row_count=row_count,
            duration_ms=duration_ms,
            error=(error[:2000] if error else None),
        )
        db.add(run)
        db.commit()
        # Cheap amortised pruning: trim only every 25th write.
        if run.id and run.id % 25 == 0 and run.user_id is not None:
            _prune_history(db, run.user_id)
    except Exception as exc:  # noqa: BLE001 - history is best-effort
        log.warning("Could not record command run for %s: %s", path, exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _prune_history(db: Session, user_id: int) -> None:
    db.execute(
        text(
            "DELETE FROM command_runs WHERE user_id = :uid AND id NOT IN "
            "(SELECT id FROM command_runs WHERE user_id = :uid ORDER BY id DESC LIMIT :cap)"
        ),
        {"uid": user_id, "cap": settings.max_history_rows_per_user},
    )
    db.commit()


def _make_endpoint(spec: CommandSpec) -> Callable[..., Dict[str, Any]]:
    """Build a FastAPI-compatible view function for a command."""

    def endpoint(**kwargs: Any) -> Dict[str, Any]:
        user = kwargs.pop("_user", None)
        db = kwargs.pop("_db", None)
        started = time.perf_counter()

        def elapsed_ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        try:
            result = execute(spec.path, **kwargs)
        except MFTError as exc:
            _record_run(db, user, spec.path, kwargs, kwargs.get("provider"),
                        "error", None, elapsed_ms(), str(exc))
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
        except (ValueError, TypeError, KeyError) as exc:
            detail = "{}: {}".format(type(exc).__name__, exc)
            _record_run(db, user, spec.path, kwargs, kwargs.get("provider"),
                        "error", None, elapsed_ms(), detail)
            raise HTTPException(status_code=400, detail=detail)

        _record_run(db, user, spec.path, kwargs, result.provider,
                    "ok", len(result), elapsed_ms(), None)
        return result.to_dict()

    endpoint.__name__ = "_".join(spec.parts)
    endpoint.__doc__ = inspect.getdoc(spec.func) or spec.summary
    endpoint.__signature__ = _endpoint_signature(spec.func)  # type: ignore[attr-defined]
    return endpoint


def build_router() -> APIRouter:
    """Assemble the ``/api/v1`` router from every registered command."""
    dependencies = [Depends(get_current_user)] if settings.platform_require_auth else []
    router = APIRouter(prefix="/api/v1", dependencies=dependencies)

    for spec in sorted(REGISTRY.values(), key=lambda s: s.path):
        router.add_api_route(
            spec.path,
            _make_endpoint(spec),
            methods=list(spec.methods),
            name=spec.path,
            summary=spec.description,
            description=inspect.getdoc(spec.func) or spec.description,
            tags=[spec.tag],
            response_model=None,
        )

    # --- registry introspection (handy for the CLI and the web UI) --------
    @router.get("/_registry", tags=["_meta"], summary="Every registered command")
    def _registry(menu: Optional[str] = Query(None, description="Filter by path prefix")) -> Dict[str, Any]:
        from .docs import MENU_GUIDES

        specs = sorted(REGISTRY.values(), key=lambda s: s.path)
        if menu:
            prefix = "/" + menu.strip("/")
            specs = [s for s in specs if s.path.startswith(prefix)]
        return {"results": [_describe(s) for s in specs], "count": len(specs),
                "guides": MENU_GUIDES}

    @router.get("/_search", tags=["_meta"], summary="Search commands by name or description")
    def _search(query: str = Query(..., min_length=1), limit: int = 50) -> Dict[str, Any]:
        hits = search(query, limit=limit)
        return {"results": [_describe(s) for s in hits], "count": len(hits)}

    @router.get("/_coverage", tags=["_meta"], summary="Command counts per menu and provider")
    def _coverage() -> Dict[str, Any]:
        return coverage()

    return router


def _describe(spec: CommandSpec) -> Dict[str, Any]:
    from .docs import describe_param, example_for, full_doc

    parameters = [dict(p, description=describe_param(spec, p["name"]))
                  for p in spec.parameters]
    return {
        "path": spec.path,
        "endpoint": "/api/v1" + spec.path,
        "menu": spec.menu,
        "name": spec.name,
        "description": spec.description,
        "doc": full_doc(spec),
        "example": example_for(spec),
        "providers": list(spec.providers),
        "methods": list(spec.methods),
        "parameters": parameters,
    }


def describe_all() -> List[Dict[str, Any]]:
    return [_describe(s) for s in sorted(REGISTRY.values(), key=lambda s: s.path)]
