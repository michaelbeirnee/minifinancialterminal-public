"""The strategy interface, and the context every hook receives.

A strategy is a class with five optional hooks::

    class Momentum(Strategy):
        params = {"fast": 10, "slow": 30}

        def on_start(self, ctx): ...
        def on_tick(self, ctx, tick): ...       # every live print (or replay open)
        def on_bar(self, ctx, bar): ...         # completed bars
        def on_fill(self, ctx, fill): ...       # its own executions
        def on_stop(self, ctx): ...

The same instance runs in replay and live — the hooks cannot tell which feed
is driving them, and that is the designed property. Everything a strategy may
do to the world goes through ``ctx``, and every order it places passes the
risk gate before the OMS sees it.

Tick shape: the stream hub's (symbol, price, bid/ask when the source has
them, time, ...). Bar shape: ``{"symbol", "open", "high", "low", "close",
"volume", "start", "end"}``. Fill shape: the OMS's (order_id, symbol, qty,
price, realized, ...).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


class Strategy:
    """Base class. Subclass, override the hooks you need."""

    #: Parameter defaults; instances get ``self.params`` = defaults + overrides.
    params: Dict[str, Any] = {}
    #: One-line description shown in the strategy list.
    description: str = ""

    def __init__(self, params: Optional[Dict[str, Any]] = None) -> None:
        merged = {**type(self).params}
        for key, value in (params or {}).items():
            if key in merged:
                # Keep the default's type where it has one, so "10" from a
                # form becomes 10 and a typo'd string fails loudly here.
                default = merged[key]
                merged[key] = type(default)(value) if default is not None else value
            else:
                merged[key] = value
        self.params = merged

    def on_start(self, ctx: "Context") -> None: ...
    def on_tick(self, ctx: "Context", tick: Dict[str, Any]) -> None: ...
    def on_bar(self, ctx: "Context", bar: Dict[str, Any]) -> None: ...
    def on_fill(self, ctx: "Context", fill: Dict[str, Any]) -> None: ...
    def on_timer(self, ctx: "Context", ts: float) -> None: ...
    def on_stop(self, ctx: "Context") -> None: ...


class Context:
    """What a strategy is allowed to see and do. One per engine."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    # ---- acting ----------------------------------------------------------- #
    def order(self, symbol: str, qty: float, limit: Optional[float] = None,
              note: str = "") -> Optional[Dict[str, Any]]:
        """Signed order: positive buys, negative sells. None if risk refused it."""
        return self._engine.place_order(symbol, qty, limit=limit, note=note)

    def buy(self, symbol: str, qty: float, limit: Optional[float] = None,
            note: str = "") -> Optional[Dict[str, Any]]:
        return self.order(symbol, abs(qty), limit=limit, note=note)

    def sell(self, symbol: str, qty: float, limit: Optional[float] = None,
             note: str = "") -> Optional[Dict[str, Any]]:
        return self.order(symbol, -abs(qty), limit=limit, note=note)

    def cancel_open(self, symbol: Optional[str] = None) -> int:
        return self._engine.oms.cancel_open(symbol)

    def log(self, message: str) -> None:
        self._engine.log(str(message))

    # ---- seeing ----------------------------------------------------------- #
    def position(self, symbol: str) -> float:
        return self._engine.oms.positions.get(symbol.upper(), {}).get("qty", 0.0)

    @property
    def positions(self) -> Dict[str, float]:
        return {s: p["qty"] for s, p in self._engine.oms.positions.items() if p["qty"]}

    @property
    def cash(self) -> float:
        return self._engine.oms.cash

    @property
    def equity(self) -> float:
        return self._engine.oms.equity(self._engine.last_prices())

    def last(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self._engine.last_tick.get(symbol.upper())

    def price(self, symbol: str) -> Optional[float]:
        tick = self.last(symbol)
        return tick.get("price") if tick else None

    @property
    def symbols(self) -> List[str]:
        return list(self._engine.symbols)

    @property
    def now(self) -> Optional[Any]:
        return self._engine.now
