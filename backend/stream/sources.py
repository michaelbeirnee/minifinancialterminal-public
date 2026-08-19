"""Upstream websocket sources for the streaming hub.

Each source turns a vendor's wire format into the one tick shape the hub
publishes::

    {"symbol": "AAPL", "price": 189.12, "change": 1.02, "change_percent": 0.0054,
     "bid": 189.10, "ask": 189.13, "bid_size": 200, "ask_size": 100,
     "size": 100, "volume": 41_233_100, "time": "2026-08-18T14:30:01.123Z",
     "exchange": "NMS", "market_hours": "regular", "kind": "trade",
     "provider": "yahoo"}

Fields a vendor cannot supply are simply absent (Yahoo has no bid/ask on most
names; Alpaca has no day-change), and the hub carries earlier values forward
so a reader always sees a complete row.

Both sources reconnect with capped backoff. Alpaca stops retrying on an
authentication error, since no amount of waiting fixes a bad key.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import ssl
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

log = logging.getLogger("mft.stream")

RECONNECT_MIN = 1.0
RECONNECT_MAX = 30.0


def _ssl_context() -> ssl.SSLContext:
    """A TLS context that trusts certifi's bundle when the interpreter's own is empty.

    python.org builds on macOS ship without system CAs wired in; every HTTPS
    call in this codebase already goes through requests/httpx, which bring
    certifi, so websocket connections should trust the same roots.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - fall back to the interpreter's defaults
        return ssl.create_default_context()


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class Source:
    """Interface the hub drives. Subclasses implement the vendor protocol."""

    name = "base"

    def __init__(self) -> None:
        self.hub: Any = None
        self.connected = False
        self.last_error: Optional[str] = None
        self.last_message_at: Optional[float] = None
        self.reconnects = 0
        self._stopping = False
        self._symbols: Set[str] = set()

    def attach(self, hub: Any) -> None:
        self.hub = hub

    async def run(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def set_symbols(self, symbols: Set[str]) -> None:
        """The hub's current union of watched symbols. Push the delta upstream."""
        added = symbols - self._symbols
        removed = self._symbols - symbols
        self._symbols = set(symbols)
        if self.connected:
            try:
                await self._send_delta(added, removed)
            except Exception as exc:  # noqa: BLE001 - the run loop will reconnect
                log.debug("%s: delta send failed: %s", self.name, exc)

    async def _send_delta(self, added: Set[str], removed: Set[str]) -> None:  # pragma: no cover
        raise NotImplementedError

    async def stop(self) -> None:
        self._stopping = True

    def status(self) -> Dict[str, Any]:
        return {
            "source": self.name,
            "connected": self.connected,
            "last_error": self.last_error,
            "last_message_at": _iso(self.last_message_at),
            "reconnects": self.reconnects,
        }

    def _publish(self, tick: Dict[str, Any]) -> None:
        self.last_message_at = time.time()
        if self.hub is not None:
            self.hub.publish(tick)


# --------------------------------------------------------------------------- #
# Yahoo Finance streamer
# --------------------------------------------------------------------------- #
YAHOO_URL = "wss://streamer.finance.yahoo.com/?version=2"
#: Yahoo drops a subscription that is not refreshed; yfinance re-sends every 15 s.
YAHOO_HEARTBEAT = 15.0
_YAHOO_HOURS = {0: "pre", 1: "regular", 2: "post", 3: "extended",
                "PRE_MARKET": "pre", "REGULAR_MARKET": "regular",
                "POST_MARKET": "post", "EXTENDED_HOURS_MARKET": "extended"}


def decode_yahoo(payload_b64: str) -> Dict[str, Any]:
    """Decode one base64 protobuf ``PricingData`` frame into a tick.

    Uses yfinance's generated protobuf class so we track Yahoo's schema
    without carrying our own copy of the .proto.
    """
    from google.protobuf.json_format import MessageToDict
    from yfinance.pricing_pb2 import PricingData

    msg = PricingData()
    msg.ParseFromString(base64.b64decode(payload_b64))
    d = MessageToDict(msg, preserving_proto_field_name=True)
    return yahoo_tick(d)


def yahoo_tick(d: Dict[str, Any]) -> Dict[str, Any]:
    """Map a decoded Yahoo pricing dict onto the hub's tick shape."""
    ts = d.get("time")
    try:
        ts_s = int(ts) / 1000.0 if ts is not None else None
    except (TypeError, ValueError):
        ts_s = None
    pct = d.get("change_percent")
    tick: Dict[str, Any] = {
        "symbol": str(d.get("id", "")).upper(),
        "price": d.get("price"),
        "change": d.get("change"),
        # Yahoo sends percent units (0.46 == 0.46 %); the platform uses fractions.
        "change_percent": (float(pct) / 100.0) if pct is not None else None,
        "volume": d.get("day_volume"),
        "day_high": d.get("day_high"),
        "day_low": d.get("day_low"),
        "prev_close": d.get("previous_close"),
        "time": _iso(ts_s),
        "exchange": d.get("exchange"),
        "market_hours": _YAHOO_HOURS.get(d.get("market_hours"), d.get("market_hours")),
        "kind": "trade",
        "provider": "yahoo",
    }
    # Bid/ask are on the schema and occasionally populated (mostly FX/futures).
    for k in ("bid", "ask", "bid_size", "ask_size", "last_size"):
        if d.get(k) not in (None, 0, "0"):
            tick["size" if k == "last_size" else k] = d[k]
    # protobuf int64s arrive as strings through MessageToDict.
    for k in ("volume", "size", "bid_size", "ask_size"):
        if tick.get(k) is not None:
            try:
                tick[k] = int(float(tick[k]))
            except (TypeError, ValueError):
                tick[k] = None
    for k in ("day_high", "day_low", "prev_close", "bid", "ask"):
        if tick.get(k) is not None:
            try:
                tick[k] = float(tick[k])
            except (TypeError, ValueError):
                tick[k] = None
    return tick


class YahooSource(Source):
    name = "yahoo"

    def __init__(self, url: str = YAHOO_URL) -> None:
        super().__init__()
        self.url = url
        self._ws: Any = None

    async def _send(self, obj: Dict[str, Any]) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(obj))

    async def _send_delta(self, added: Set[str], removed: Set[str]) -> None:
        if removed:
            await self._send({"unsubscribe": sorted(removed)})
        if added:
            await self._send({"subscribe": sorted(added)})

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(YAHOO_HEARTBEAT)
            if self._symbols:
                await self._send({"subscribe": sorted(self._symbols)})

    async def run(self) -> None:
        from websockets.asyncio.client import connect

        self._stopping = False
        backoff = RECONNECT_MIN
        while not self._stopping:
            beat: Optional[asyncio.Task] = None
            try:
                async with connect(self.url, ssl=_ssl_context(), open_timeout=15,
                                   ping_interval=20, ping_timeout=20) as ws:
                    self._ws = ws
                    self.connected = True
                    self.last_error = None
                    backoff = RECONNECT_MIN
                    if self._symbols:
                        await self._send({"subscribe": sorted(self._symbols)})
                    beat = asyncio.get_running_loop().create_task(self._heartbeat())
                    async for raw in ws:
                        if self._stopping:
                            break
                        try:
                            frame = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        if frame.get("type") != "pricing":
                            continue
                        try:
                            tick = decode_yahoo(frame.get("message", ""))
                        except Exception as exc:  # noqa: BLE001 - one bad frame
                            log.debug("yahoo: undecodable frame: %s", exc)
                            continue
                        if tick.get("symbol") and tick.get("price") is not None:
                            self._publish(tick)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect
                self.last_error = "{}: {}".format(type(exc).__name__, exc)
                log.info("yahoo stream: %s (retry in %.0fs)", self.last_error, backoff)
            finally:
                if beat is not None:
                    beat.cancel()
                self._ws = None
                self.connected = False
            if self._stopping:
                break
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)


# --------------------------------------------------------------------------- #
# Alpaca Markets data stream (IEX feed on the free plan)
# --------------------------------------------------------------------------- #
ALPACA_URL = "wss://stream.data.alpaca.markets/v2/{feed}"
ALPACA_FEEDS = ("iex", "sip", "delayed_sip", "test")


def alpaca_symbol(symbol: str) -> Optional[str]:
    """Translate a Yahoo-style ticker into Alpaca's, or ``None`` if not a US stock.

    Alpaca is stocks-and-ETFs only, so indices (``^GSPC``), futures (``ES=F``),
    FX (``EURUSD=X``) and crypto (``BTC-USD``) have no equivalent. Share
    classes use a dot: Yahoo ``BRK-B`` is Alpaca ``BRK.B``.
    """
    s = symbol.upper()
    if s.startswith("^") or "=" in s or s.endswith("-USD"):
        return None
    s = s.replace("-", ".")
    head, _, tail = s.partition(".")
    if not head.isalpha() or not (1 <= len(head) <= 6):
        return None
    if tail and not (tail.isalpha() and len(tail) <= 2):
        return None
    return s


def _alpaca_ts(value: Any) -> Optional[str]:
    # RFC-3339 with nanoseconds; keep it as-is, trimmed to microseconds for JS.
    if not isinstance(value, str):
        return None
    if "." in value:
        head, _, tail = value.partition(".")
        digits = "".join(ch for ch in tail if ch.isdigit())[:6]
        return "{}.{}Z".format(head, digits) if digits else head + "Z"
    return value


def alpaca_tick(msg: Dict[str, Any], back: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Map one Alpaca stream message onto the hub tick shape (or ``None`` to skip)."""
    kind = msg.get("T")
    sym = back.get(str(msg.get("S", "")).upper())
    if not sym:
        return None
    if kind == "t":
        return {
            "symbol": sym, "price": msg.get("p"), "size": msg.get("s"),
            "exchange": msg.get("x"), "time": _alpaca_ts(msg.get("t")),
            "kind": "trade", "provider": "alpaca",
        }
    if kind == "q":
        return {
            "symbol": sym, "bid": msg.get("bp"), "bid_size": msg.get("bs"),
            "ask": msg.get("ap"), "ask_size": msg.get("as"),
            "time": _alpaca_ts(msg.get("t")), "kind": "quote", "provider": "alpaca",
        }
    return None


class AlpacaSource(Source):
    name = "alpaca"

    def __init__(self, key: str, secret: str, feed: str = "iex") -> None:
        super().__init__()
        feed = (feed or "iex").lower()
        if feed not in ALPACA_FEEDS:
            raise ValueError("MFT_ALPACA_FEED must be one of {}".format(", ".join(ALPACA_FEEDS)))
        self.key, self.secret, self.feed = key, secret, feed
        self.url = ALPACA_URL.format(feed=feed)
        self._ws: Any = None
        self._back: Dict[str, str] = {}  # alpaca symbol -> requested symbol
        self.fatal = False  # auth failure: do not keep retrying

    def _translate(self, symbols: Set[str]) -> List[str]:
        out = []
        for s in symbols:
            a = alpaca_symbol(s)
            if a:
                self._back[a] = s
                out.append(a)
        return sorted(out)

    async def _send(self, obj: Dict[str, Any]) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(obj))

    async def _send_delta(self, added: Set[str], removed: Set[str]) -> None:
        rem = self._translate(removed)
        add = self._translate(added)
        if rem:
            await self._send({"action": "unsubscribe", "trades": rem, "quotes": rem})
        if add:
            await self._send({"action": "subscribe", "trades": add, "quotes": add})

    async def run(self) -> None:
        from websockets.asyncio.client import connect

        self._stopping = False
        backoff = RECONNECT_MIN
        while not self._stopping and not self.fatal:
            try:
                async with connect(self.url, ssl=_ssl_context(), open_timeout=15,
                                   ping_interval=20, ping_timeout=20) as ws:
                    self._ws = ws
                    # Handshake: connected -> auth -> authenticated.
                    await self._expect(ws, "connected")
                    await self._send({"action": "auth", "key": self.key, "secret": self.secret})
                    await self._expect(ws, "authenticated")
                    self.connected = True
                    self.last_error = None
                    backoff = RECONNECT_MIN
                    syms = self._translate(self._symbols)
                    if syms:
                        await self._send({"action": "subscribe", "trades": syms, "quotes": syms})
                    async for raw in ws:
                        if self._stopping:
                            break
                        try:
                            frames = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        if isinstance(frames, dict):
                            frames = [frames]
                        for msg in frames:
                            if not isinstance(msg, dict):
                                continue
                            if msg.get("T") == "error":
                                self._on_error(msg)
                                continue
                            tick = alpaca_tick(msg, self._back)
                            if tick is not None:
                                self._publish(tick)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect unless fatal
                if not self.fatal:
                    self.last_error = "{}: {}".format(type(exc).__name__, exc)
                log.info("alpaca stream: %s%s", self.last_error,
                         "" if self.fatal else " (retry in {:.0f}s)".format(backoff))
            finally:
                self._ws = None
                self.connected = False
            if self._stopping or self.fatal:
                break
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)

    async def _expect(self, ws: Any, msg: str) -> None:
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        frames = json.loads(raw)
        if isinstance(frames, dict):
            frames = [frames]
        for f in frames:
            if f.get("T") == "error":
                self._on_error(f)
                raise RuntimeError(self.last_error or "alpaca error")
            if f.get("T") == "success" and f.get("msg") == msg:
                return
        raise RuntimeError("alpaca: expected {!r}, got {}".format(msg, raw[:200]))

    def _on_error(self, msg: Dict[str, Any]) -> None:
        code = msg.get("code")
        text = "alpaca error {}: {}".format(code, msg.get("msg"))
        self.last_error = text
        # 401 not authenticated, 402 auth failed, 403 already authenticated,
        # 404 auth timeout, 406 connection limit exceeded, 409 insufficient
        # subscription. The key-shaped ones will not heal on retry.
        if code in (402, 409):
            self.fatal = True
        log.warning("alpaca stream: %s", text)

    def status(self) -> Dict[str, Any]:
        out = super().status()
        out.update({"feed": self.feed, "fatal": self.fatal})
        return out
