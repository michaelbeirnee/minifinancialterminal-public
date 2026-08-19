"""The fan-out hub: one upstream socket per provider, many subscribers.

A :class:`StreamHub` owns a single :class:`~backend.stream.sources.Source`. It
keeps a reference count per symbol so the source only ever carries the union
of what somebody is watching, remembers the latest tick per symbol so a new
subscriber sees a price before the next print arrives, and coalesces updates
per subscriber — a slow reader gets the newest tick for each symbol, never a
backlog of stale ones.

Everything here runs on the server's asyncio loop. Sources are started lazily
on the first subscription and stopped after a short linger once the last one
is gone, so an idle server holds no upstream connection.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Set

from ..config import settings

log = logging.getLogger("mft.stream")

#: The normalised tick shape every source publishes. ``change_percent`` is a
#: fraction (0.0123 = +1.23 %) to match ``/equity/price/quote``.
Tick = Dict[str, Any]

# Yahoo-style tickers, which is the vocabulary the rest of the terminal uses:
# ``AAPL``, ``BRK-B``, ``^GSPC``, ``BTC-USD``, ``ES=F``, ``EURUSD=X``, ``DX-Y.NYB``.
_SYMBOL_RE = re.compile(r"^[A-Z0-9^.=-]{1,20}$")

#: How long the upstream socket stays open after its last subscriber leaves.
#: Long enough that a browser navigating between two live views does not
#: pay a reconnect; short enough that an idle server is idle.
LINGER_SECONDS = 20.0

#: Symbols one subscription may carry. Yahoo copes with far more, but a
#: quote monitor is not a market-wide scanner.
MAX_SYMBOLS = 100


def normalise_symbols(raw: Iterable[str]) -> List[str]:
    """Upper-case, de-duplicate and validate a symbol list, preserving order."""
    out: List[str] = []
    seen: Set[str] = set()
    for item in raw:
        for part in str(item).split(","):
            sym = part.strip().upper()
            if not sym or sym in seen:
                continue
            if not _SYMBOL_RE.match(sym):
                raise ValueError("Invalid symbol {!r}".format(part.strip()))
            seen.add(sym)
            out.append(sym)
    if len(out) > MAX_SYMBOLS:
        raise ValueError("At most {} symbols per stream".format(MAX_SYMBOLS))
    return out


class Subscription:
    """One downstream reader's view of the hub.

    ``pending`` holds the newest tick per symbol since the reader last drained;
    ``event`` wakes the reader. Draining is O(symbols), not O(ticks), which is
    the whole point.
    """

    __slots__ = ("symbols", "pending", "event", "hub", "created_at")

    def __init__(self, hub: "StreamHub", symbols: List[str]) -> None:
        self.hub = hub
        self.symbols: Set[str] = set(symbols)
        self.pending: Dict[str, Tick] = {}
        self.event = asyncio.Event()
        self.created_at = time.time()

    def push(self, tick: Tick) -> None:
        self.pending[tick["symbol"]] = tick
        self.event.set()

    async def drain(self, timeout: Optional[float] = None) -> List[Tick]:
        """Wait for at least one tick (or ``timeout``) and return what arrived.

        An empty list means the timeout elapsed — the caller uses that to send
        a keep-alive rather than treat it as an error.
        """
        if not self.pending:
            try:
                await asyncio.wait_for(self.event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return []
        self.event.clear()
        ticks = list(self.pending.values())
        self.pending.clear()
        return ticks

    async def close(self) -> None:
        await self.hub.unsubscribe(self)


class StreamHub:
    """Reference-counted fan-out in front of one :class:`Source`."""

    def __init__(self, provider: str, source: Any) -> None:
        self.provider = provider
        self.source = source
        self.latest: Dict[str, Tick] = {}
        self._refs: Dict[str, int] = {}
        self._subs: Set[Subscription] = set()
        self._task: Optional[asyncio.Task] = None
        self._linger: Optional[asyncio.TimerHandle] = None
        self._lock = asyncio.Lock()
        self.ticks_seen = 0
        self.started_at: Optional[float] = None
        source.attach(self)

    # ---- subscriber side ------------------------------------------------ #
    async def subscribe(self, symbols: Iterable[str]) -> Subscription:
        syms = normalise_symbols(symbols)
        if not syms:
            raise ValueError("Give at least one symbol")
        sub = Subscription(self, syms)
        async with self._lock:
            self._subs.add(sub)
            added = []
            for s in syms:
                self._refs[s] = self._refs.get(s, 0) + 1
                if self._refs[s] == 1:
                    added.append(s)
            self._cancel_linger()
            self._ensure_running()
            if added:
                await self.source.set_symbols(set(self._refs))
        # Replay what we already know so the reader has a price immediately.
        for s in syms:
            tick = self.latest.get(s)
            if tick is not None:
                sub.pending[s] = tick
        if sub.pending:
            sub.event.set()
        return sub

    async def unsubscribe(self, sub: Subscription) -> None:
        async with self._lock:
            if sub not in self._subs:
                return
            self._subs.discard(sub)
            removed = []
            for s in sub.symbols:
                n = self._refs.get(s, 0) - 1
                if n <= 0:
                    self._refs.pop(s, None)
                    removed.append(s)
                else:
                    self._refs[s] = n
            if removed:
                await self.source.set_symbols(set(self._refs))
            if not self._subs:
                self._schedule_linger()

    # ---- source side ---------------------------------------------------- #
    def publish(self, tick: Tick) -> None:
        """Called by the source for every normalised tick."""
        sym = tick.get("symbol")
        if not sym or sym not in self._refs:
            return
        tick.setdefault("provider", self.provider)
        prev = self.latest.get(sym)
        # A quote-only message (Alpaca ``q``) has no price; carry the last
        # trade forward so every tick a reader sees is a complete row.
        if prev is not None:
            for k, v in prev.items():
                if k not in tick or tick[k] is None:
                    tick[k] = v
        self.latest[sym] = tick
        self.ticks_seen += 1
        for sub in self._subs:
            if sym in sub.symbols:
                sub.push(tick)

    def symbols(self) -> List[str]:
        return sorted(self._refs)

    # ---- lifecycle ------------------------------------------------------ #
    def _ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self.started_at = time.time()
            self._task = asyncio.get_running_loop().create_task(
                self.source.run(), name="mft-stream-{}".format(self.provider)
            )

    def _schedule_linger(self) -> None:
        self._cancel_linger()
        loop = asyncio.get_running_loop()
        self._linger = loop.call_later(LINGER_SECONDS, lambda: loop.create_task(self._stop_if_idle()))

    def _cancel_linger(self) -> None:
        if self._linger is not None:
            self._linger.cancel()
            self._linger = None

    async def _stop_if_idle(self) -> None:
        async with self._lock:
            if self._subs:
                return
            await self._stop_task()

    async def _stop_task(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        await self.source.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
            pass

    async def shutdown(self) -> None:
        self._cancel_linger()
        async with self._lock:
            self._subs.clear()
            self._refs.clear()
            await self._stop_task()

    def status(self) -> Dict[str, Any]:
        src = self.source.status()
        return {
            "provider": self.provider,
            "running": self._task is not None and not self._task.done(),
            "subscribers": len(self._subs),
            "symbols": self.symbols(),
            "ticks_seen": self.ticks_seen,
            "started_at": self.started_at,
            **src,
        }


# --------------------------------------------------------------------------- #
# Registry of hubs — one per provider, created on first use
# --------------------------------------------------------------------------- #
_HUBS: Dict[str, StreamHub] = {}


def available_providers() -> Dict[str, Dict[str, Any]]:
    """Which stream providers this deployment can serve, and why not if not."""
    from . import sources

    alpaca_ok = bool(settings.alpaca_api_key and settings.alpaca_api_secret)
    return {
        "yahoo": {
            "available": True,
            "requires_key": False,
            "fields": ["price", "change", "volume"],
            "coverage": "US stocks, ETFs, indices, futures, FX, crypto",
            "note": "Yahoo Finance public streamer; last price only, no bid/ask.",
        },
        "alpaca": {
            "available": alpaca_ok,
            "requires_key": True,
            "fields": ["price", "size", "bid", "ask", "bid_size", "ask_size"],
            "coverage": "US stocks and ETFs ({} feed)".format(settings.alpaca_feed),
            "note": (
                "Licensed trades and quotes over Alpaca's free IEX feed."
                if alpaca_ok
                else "Set MFT_ALPACA_API_KEY and MFT_ALPACA_API_SECRET (free at alpaca.markets) "
                "to stream licensed bid/ask; Yahoo carries on without them."
            ),
        },
    }


def default_provider() -> str:
    """The provider used when a request names none.

    Yahoo unless the operator has both set MFT_STREAM_DEFAULT_PROVIDER=alpaca
    *and* configured keys; a preference for a source that cannot connect is
    ignored rather than honoured into a wall of errors.
    """
    pref = (settings.stream_default_provider or "yahoo").strip().lower()
    if pref == "alpaca" and available_providers()["alpaca"]["available"]:
        return "alpaca"
    return "yahoo"


def get_hub(provider: Optional[str] = None) -> StreamHub:
    """The process-wide hub for ``provider`` (created on first call)."""
    from ..core.errors import UnknownProviderError
    from . import sources

    key = (provider or default_provider()).strip().lower()
    if key not in ("yahoo", "alpaca"):
        raise UnknownProviderError(
            "Unknown stream provider {!r}. Available: yahoo, alpaca".format(provider)
        )
    if key == "alpaca" and not available_providers()["alpaca"]["available"]:
        raise UnknownProviderError(
            "Alpaca streaming needs MFT_ALPACA_API_KEY and MFT_ALPACA_API_SECRET "
            "(free at https://alpaca.markets). Use provider=yahoo for key-free prices."
        )
    hub = _HUBS.get(key)
    if hub is None:
        source = sources.YahooSource() if key == "yahoo" else sources.AlpacaSource(
            settings.alpaca_api_key or "", settings.alpaca_api_secret or "", settings.alpaca_feed
        )
        hub = _HUBS[key] = StreamHub(key, source)
    return hub


def register_hub(provider: str, hub: StreamHub) -> None:
    """Install a hub under ``provider`` — tests use this to swap in a fake source."""
    _HUBS[provider] = hub


async def shutdown_all() -> None:
    for hub in list(_HUBS.values()):
        await hub.shutdown()
    _HUBS.clear()
