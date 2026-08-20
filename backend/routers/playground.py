"""The Python playground's endpoints: status, run, reset.

Auth is the same bearer token as everything else, and every kernel is scoped
to the authenticated user's id — two accounts never share a namespace. On an
internet-reachable deployment the whole surface is off unless
``MFT_PLAYGROUND_ENABLED=true`` was set deliberately (see ``config.py``).
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import get_current_user
from ..config import settings
from ..models import User
from ..playground import manager

router = APIRouter(prefix="/api/playground", tags=["playground"])

#: A script, not a book. Anything real at this size belongs in a file.
MAX_CODE_CHARS = 100_000


class RunRequest(BaseModel):
    code: str = Field(..., max_length=MAX_CODE_CHARS)


def _require_enabled() -> None:
    if not settings.playground_on:
        raise HTTPException(
            status_code=403,
            detail="The playground is switched off on this deployment. It executes "
                   "arbitrary Python as the server user, so it is disabled when "
                   "MFT_DEBUG=false unless MFT_PLAYGROUND_ENABLED=true is set deliberately.",
        )


@router.get("/status")
def status(user: User = Depends(get_current_user)) -> Dict[str, Any]:
    """Whether the playground is on, and the caller's kernel state if any."""
    packages = {}
    for mod, name in (("numpy", "numpy"), ("pandas", "pandas"), ("scipy", "scipy"),
                      ("statsmodels", "statsmodels"), ("sklearn", "scikit-learn")):
        try:
            packages[name] = __import__(mod).__version__
        except Exception:  # noqa: BLE001 - absence is a reportable state
            packages[name] = None
    return {
        "enabled": settings.playground_on,
        "timeout_seconds": settings.playground_timeout_seconds,
        "kernel": manager.status(user.id),
        "packages": packages,
    }


@router.post("/run")
def run(payload: RunRequest, user: User = Depends(get_current_user)) -> Dict[str, Any]:
    """Execute code in the caller's kernel (started on first use)."""
    _require_enabled()
    if not payload.code.strip():
        raise HTTPException(status_code=422, detail="Give some code to run")
    try:
        return manager.run(user.id, payload.code)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/reset")
def reset(user: User = Depends(get_current_user)) -> Dict[str, Any]:
    """Kill the caller's kernel; the next run starts a clean namespace."""
    _require_enabled()
    return {"killed": manager.reset(user.id)}
