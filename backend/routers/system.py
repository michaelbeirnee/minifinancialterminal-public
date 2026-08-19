"""System / ops endpoints: health and cache controls."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth import get_current_user
from ..cache import cache
from ..config import settings
from ..models import User

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/health")
def health() -> dict:
    from ..core.registry import REGISTRY

    return {
        "status": "ok",
        "app": settings.app_name,
        "data_source": "yfinance",
        "platform_commands": len(REGISTRY),
    }


@router.get("/coverage")
def coverage_report() -> dict:
    """Command counts per menu and per provider."""
    from ..core.registry import coverage

    return coverage()


@router.get("/providers")
def providers() -> dict:
    """Every data provider, what it covers and whether a key is configured."""
    from ..providers import provider_table

    key_state = {
        "fred": bool(settings.fred_api_key),
        "eia": bool(settings.eia_api_key),
        "bls": bool(settings.bls_api_key),
        "nasdaq": bool(settings.nasdaq_api_key),
        "alpaca": bool(settings.alpaca_api_key and settings.alpaca_api_secret),
    }
    rows = []
    for row in provider_table():
        row = dict(row)
        row["key_configured"] = key_state.get(row["name"])
        rows.append(row)
    return {"providers": rows, "count": len(rows)}


@router.get("/database")
def database_overview(_: User = Depends(get_current_user)) -> dict:
    """Tables, columns and row counts — what the terminal is persisting."""
    from ..database import engine, schema_overview

    return {
        "dialect": engine.dialect.name,
        "url": str(engine.url.render_as_string(hide_password=True)),
        "tables": schema_overview(),
    }


@router.get("/cache")
def cache_stats(_: User = Depends(get_current_user)) -> dict:
    return cache.stats()


@router.post("/cache/clear")
def cache_clear(_: User = Depends(get_current_user)) -> dict:
    removed = cache.clear()
    return {"cleared": removed}
