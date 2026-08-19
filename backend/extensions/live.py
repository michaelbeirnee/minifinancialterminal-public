"""Live prices as a command: a short sample of the streaming feed, one row per symbol.

The web UI streams over ``/api/stream/quotes``; this is the same data reached
the way every other command is reached — REST, Python, CLI — for callers that
want a snapshot rather than a subscription.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import pandas as pd

from ..config import settings
from ..core.errors import EmptyDataError, MissingCredentialError, ProviderError
from ..core.http import get_json
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import norm_symbols
from ..providers import yahoo
from ..stream.sources import YAHOO_URL, alpaca_symbol, decode_yahoo, _ssl_context

_LIVE_COLUMNS = ["symbol", "price", "change", "change_percent", "bid", "ask", "bid_size",
                 "ask_size", "size", "volume", "time", "exchange", "market_hours", "source"]

ALPACA_SNAPSHOT_URL = "https://data.alpaca.markets/v2/stocks/snapshots"


def _sample_yahoo(symbols: List[str], wait: float) -> Dict[str, Dict[str, Any]]:
    """Open Yahoo's streamer, listen for ``wait`` seconds, keep the last tick per symbol."""
    from websockets.sync.client import connect

    latest: Dict[str, Dict[str, Any]] = {}
    deadline = time.monotonic() + wait
    try:
        with connect(YAHOO_URL, ssl=_ssl_context(), open_timeout=10) as ws:
            ws.send(json.dumps({"subscribe": symbols}))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    raw = ws.recv(timeout=remaining)
                except TimeoutError:
                    break
                try:
                    frame = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if frame.get("type") != "pricing":
                    continue
                try:
                    tick = decode_yahoo(frame.get("message", ""))
                except Exception:  # noqa: BLE001 - skip one bad frame
                    continue
                if tick.get("symbol") in symbols and tick.get("price") is not None:
                    latest[tick["symbol"]] = tick
    except Exception as exc:  # noqa: BLE001 - the caller reports and falls back to quotes
        raise ProviderError("Yahoo streamer: {}: {}".format(type(exc).__name__, exc))
    return latest


def _alpaca_snapshot(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    if not (settings.alpaca_api_key and settings.alpaca_api_secret):
        raise MissingCredentialError(
            "Alpaca needs a free app key: create one at https://alpaca.markets and set "
            "MFT_ALPACA_API_KEY and MFT_ALPACA_API_SECRET. Key-free live prices are "
            "available with provider=yahoo (last price only, no bid/ask)."
        )
    mapping = {}
    for s in symbols:
        a = alpaca_symbol(s)
        if a:
            mapping[a] = s
    if not mapping:
        return {}
    feed = settings.alpaca_feed if settings.alpaca_feed != "test" else "iex"
    data = get_json(
        ALPACA_SNAPSHOT_URL,
        params={"symbols": ",".join(sorted(mapping)), "feed": feed},
        headers={"APCA-API-KEY-ID": settings.alpaca_api_key,
                 "APCA-API-SECRET-KEY": settings.alpaca_api_secret},
        use_cache=False,
    )
    out: Dict[str, Dict[str, Any]] = {}
    for a_sym, snap in (data or {}).items():
        sym = mapping.get(str(a_sym).upper())
        if not sym or not isinstance(snap, dict):
            continue
        trade = snap.get("latestTrade") or {}
        quote = snap.get("latestQuote") or {}
        day = snap.get("dailyBar") or {}
        prev = snap.get("prevDailyBar") or {}
        price = trade.get("p")
        prev_close = prev.get("c")
        change = (price - prev_close) if (price is not None and prev_close) else None
        out[sym] = {
            "symbol": sym, "price": price, "change": change,
            "change_percent": (change / prev_close) if (change is not None and prev_close) else None,
            "bid": quote.get("bp"), "ask": quote.get("ap"),
            "bid_size": quote.get("bs"), "ask_size": quote.get("as"),
            "size": trade.get("s"), "volume": day.get("v"),
            "time": trade.get("t") or quote.get("t"), "exchange": trade.get("x"),
            "market_hours": None, "source": "alpaca",
        }
    return out


@command("/equity/price/live", providers=("yahoo", "alpaca"),
         summary="Live last trade (and bid/ask on Alpaca) sampled from the streaming feed",
         examples=("symbol=AAPL,SPY", "symbol=AAPL&provider=alpaca"))
def price_live(symbol: str, wait: float = 3.0, provider: Optional[str] = None) -> Result:
    """A snapshot of the live feed, one row per symbol.

    ``yahoo`` (default, key-free) listens to Yahoo's streamer for ``wait``
    seconds (max 10) and returns the last print seen per symbol; a symbol that
    does not print in the window — every equity, outside market hours — falls
    back to the delayed quote and says so in ``source``. ``alpaca`` returns
    Alpaca's snapshot (last trade + NBBO-style bid/ask on the IEX feed) and
    needs a free key.
    """
    src = resolve_provider(provider, ("yahoo", "alpaca"))
    symbols = norm_symbols(symbol)
    wait = max(0.5, min(float(wait), 10.0))
    warnings: List[str] = []
    rows: Dict[str, Dict[str, Any]] = {}
    if src == "alpaca":
        rows = _alpaca_snapshot(symbols)
        skipped = [s for s in symbols if alpaca_symbol(s) is None]
        if skipped:
            warnings.append("Alpaca carries US stocks and ETFs only; no data for {}".format(
                ", ".join(skipped)))
    else:
        try:
            rows = {s: dict(t, source="stream") for s, t in _sample_yahoo(symbols, wait).items()}
        except ProviderError as exc:
            warnings.append(str(exc))
        missing = [s for s in symbols if s not in rows]
        if missing:
            for s in missing:
                try:
                    q = yahoo.quote(s)
                except Exception as exc:  # noqa: BLE001 - keep the rest
                    warnings.append("{}: {}".format(s, exc))
                    continue
                rows[s] = {
                    "symbol": s, "price": q.get("last_price"), "change": q.get("change"),
                    "change_percent": q.get("change_percent"), "volume": q.get("volume"),
                    "exchange": q.get("exchange"), "source": "quote",
                }
            warnings.append(
                "No live print within {:.0f}s for {} — showing the last quote instead "
                "(equities only stream during market hours).".format(wait, ", ".join(missing)))
    if not rows:
        raise EmptyDataError("No live data returned for {}. {}".format(symbol, "; ".join(warnings)))
    df = pd.DataFrame([rows[s] for s in symbols if s in rows])
    for col in _LIVE_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[_LIVE_COLUMNS]
    return Result(df, provider=src, warnings=warnings,
                  extra={"sampled_seconds": wait if src == "yahoo" else None})
