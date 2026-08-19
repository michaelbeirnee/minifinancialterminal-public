"""Live quote streaming over Server-Sent Events.

``GET /api/stream/quotes?symbols=AAPL,SPY`` holds the response open and
writes one SSE frame per batch of ticks. SSE rather than a websocket because
the browser already talks to this API with a bearer header on ``fetch``, and a
one-way price feed needs nothing a websocket adds.

Frames, each ``data: <json>``::

    {"type": "hello", "provider": "yahoo", "symbols": [...], "fields": [...]}
    {"type": "ticks", "ticks": [{"symbol": "AAPL", "price": ..., ...}, ...]}
    {"type": "status", "connected": true|false, "last_error": ...}
    : ping                      (a comment, every 15 s of silence)

``provider`` picks the source: ``yahoo`` (default, key-free) or ``alpaca``
(licensed bid/ask; needs keys). ``/api/stream/status`` reports both.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..auth import get_current_user
from ..core.errors import MFTError
from ..models import User
from ..stream import available_providers, get_hub
from ..stream.hub import _HUBS, default_provider

router = APIRouter(prefix="/api/stream", tags=["stream"])

#: Seconds of silence before a keep-alive comment goes out. Proxies and
#: browsers both give up on a stream that says nothing for too long.
KEEPALIVE_SECONDS = 15.0
#: Ticks are coalesced per symbol and flushed at most this often, so a busy
#: name cannot flood a slow client.
FLUSH_INTERVAL = 0.25

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _frame(obj: Dict[str, Any]) -> str:
    return "data: {}\n\n".format(json.dumps(obj, separators=(",", ":")))


@router.get("/status")
def stream_status(_: User = Depends(get_current_user)) -> Dict[str, Any]:
    """Which providers can stream here, and the state of any live hub."""
    return {
        "default_provider": default_provider(),
        "providers": available_providers(),
        "hubs": {name: hub.status() for name, hub in _HUBS.items()},
    }


@router.get("/snapshot")
async def stream_snapshot(
    symbols: str = Query(..., description="Comma-separated tickers"),
    provider: Optional[str] = Query(None, description="yahoo (default) or alpaca"),
    _: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """The latest tick the hub holds for each symbol — no waiting, no stream.

    Symbols nobody is streaming have no tick yet; the row is simply absent.
    """
    try:
        hub = get_hub(provider)
    except MFTError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    from ..stream.hub import normalise_symbols

    try:
        syms = normalise_symbols([symbols])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    rows = [hub.latest[s] for s in syms if s in hub.latest]
    return {"provider": hub.provider, "results": rows, "count": len(rows)}


@router.get("/quotes")
async def stream_quotes(
    symbols: str = Query(..., description="Comma-separated tickers, up to 100"),
    provider: Optional[str] = Query(None, description="yahoo (default) or alpaca"),
    _: User = Depends(get_current_user),
) -> StreamingResponse:
    """Server-Sent Events: live ticks for ``symbols`` until the client hangs up."""
    try:
        hub = get_hub(provider)
    except MFTError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    try:
        sub = await hub.subscribe([symbols])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    async def events() -> AsyncIterator[str]:
        try:
            yield _frame({
                "type": "hello",
                "provider": hub.provider,
                "symbols": sorted(sub.symbols),
                "fields": available_providers()[hub.provider]["fields"],
            })
            last_status = None
            while True:
                ticks = await sub.drain(timeout=KEEPALIVE_SECONDS)
                status = hub.source.status()
                state = (status.get("connected"), status.get("last_error"))
                if state != last_status:
                    last_status = state
                    yield _frame({"type": "status", "connected": state[0], "last_error": state[1]})
                if ticks:
                    yield _frame({"type": "ticks", "ticks": ticks})
                    # Let a burst accumulate before the next flush.
                    await asyncio.sleep(FLUSH_INTERVAL)
                else:
                    yield ": ping\n\n"
        finally:
            await sub.close()

    return StreamingResponse(events(), media_type="text/event-stream", headers=_SSE_HEADERS)
