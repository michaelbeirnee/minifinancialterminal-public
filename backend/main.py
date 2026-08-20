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
    modeling,
    playground,
    portfolio,
    reports,
    stream,
    system,
    thesis,
    trading,
    user,
)
from .playground import manager as playground_manager
from .trading import manager as paper_manager
from .stream import shutdown_all as shutdown_streams
from .thesis import scheduler

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A deployment that keeps the shipped secret key signs its login tokens with
    # a value published in this repo, so anyone can mint one for any account.
    # Refusing to boot is the only way that failure gets noticed: it is silent
    # and total otherwise. MFT_DEBUG=true marks a local run and waives the check.
    if settings.using_dev_secret and not settings.debug:
        raise RuntimeError(
            "MFT_SECRET_KEY is still the shipped development value. Generate one "
            "with: python3 -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    init_db()
    grader = scheduler.start()
    # Recording from boot: free real-time data is ephemeral, so an operator
    # who names symbols here starts owning their history at startup.
    if settings.record_symbols.strip():
        from .stream import recorder as tick_recorder
        from .stream.hub import normalise_symbols

        try:
            await tick_recorder.start_recording(normalise_symbols([settings.record_symbols]))
        except Exception as exc:  # noqa: BLE001 - boot must not die on a bad ticker list
            import logging

            logging.getLogger("mft.stream").warning("tick recorder autostart failed: %s", exc)
    try:
        yield
    finally:
        await scheduler.stop(grader)
        # Close any upstream quote sockets so the process exits cleanly.
        await shutdown_streams()
        # And kill any playground kernels — they are child processes.
        playground_manager.shutdown()
        from .stream import recorder as tick_recorder

        await tick_recorder.stop_recording()
        await paper_manager.shutdown()


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

# The image serves the UI and the API from one origin, where CORS never comes
# into play, so the default is to register nothing at all rather than to
# advertise the API to every website. Set MFT_CORS_ORIGINS when the frontend is
# hosted separately. Credentials stay off deliberately: the browser authenticates
# with a bearer token from localStorage, never a cookie.
if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

for r in (auth, user, portfolio, hedge, data, factors, backtest, reports, system, thesis,
          modeling, assistant, stream, playground, trading):
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
