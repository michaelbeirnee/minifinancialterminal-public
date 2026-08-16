"""Mini Financial Terminal — FastAPI application entry point."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import extensions  # noqa: F401 - importing registers every platform command
from .config import settings
from .core.api import build_router
from .core.errors import MFTError
from .core.registry import REGISTRY
from .database import init_db
from .routers import (
    assistant,
    auth,
    backtest,
    data,
    factors,
    hedge,
    portfolio,
    reports,
    system,
    thesis,
    user,
)
from .thesis import scheduler

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    grader = scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop(grader)


app = FastAPI(
    title=settings.app_name,
    version="2.0.0",
    description=(
        "An open-source financial research terminal: {} data commands across "
        "equities, ETFs, crypto, FX, derivatives, macro, fixed income and "
        "regulatory filings — plus factor models, backtesting and reports. "
        "Every data source is free or public-domain.".format(len(REGISTRY))
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (auth, user, portfolio, hedge, data, factors, backtest, reports, system, thesis, assistant):
    app.include_router(r.router)
# The single-name hedge simulator is not book-scoped, so it carries its own prefix.
app.include_router(hedge.simulate_router)

# The auto-generated data platform: one route per registered command.
app.include_router(build_router())


@app.exception_handler(MFTError)
async def platform_error_handler(request: Request, exc: MFTError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


# --- Frontend (single-page terminal UI) ---
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.middleware("http")
    async def _frontend_no_store_cache(request: Request, call_next):
        """Make browsers revalidate the UI files on every load.

        Without this a browser can serve a *fresh* index.html against a *stale*
        cached app.js — new panels in the DOM, old code driving them — which
        shows up as the page stuck on its "Loading…" placeholders.
        """
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        # Stamp asset URLs with their mtime so any edit is a brand-new URL —
        # a cache can never pair old JS with new HTML.
        html = (FRONTEND_DIR / "index.html").read_text()
        for name in ("app.js", "styles.css"):
            asset = FRONTEND_DIR / name
            if asset.exists():
                html = html.replace(
                    "/static/{}".format(name),
                    "/static/{}?v={}".format(name, int(asset.stat().st_mtime)),
                )
        return HTMLResponse(html, headers={"Cache-Control": "no-cache, must-revalidate"})
