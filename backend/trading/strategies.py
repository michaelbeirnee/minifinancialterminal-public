"""Built-in strategies, and the (gated) loader for user-written ones.

The built-ins are deliberately plain — a buy-and-hold, an SMA cross, a tick
z-score reverter. They exist to prove the plumbing and to be copied: each is
a complete, honest example of the interface, not a claim of edge.

Custom strategies are Python source defining a ``Strategy`` subclass. That is
arbitrary code executing in the server process, so it rides the same switch
as the playground (``settings.playground_on``) — one decision about running
your own code on your own machine, made once.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Dict, Optional, Type

from ..config import settings
from .strategy import Strategy


class BuyAndHold(Strategy):
    description = "Split the book equally across the symbols at the first bar and sit still."
    params = {"size_pct": 0.9}

    def on_start(self, ctx):
        self._bought = set()

    def on_bar(self, ctx, bar):
        sym = bar["symbol"]
        if sym in self._bought:
            return
        budget = ctx.equity * float(self.params["size_pct"]) / max(len(ctx.symbols), 1)
        qty = int(budget / bar["close"]) if bar["close"] else 0
        if qty > 0:
            self._bought.add(sym)
            ctx.buy(sym, qty, note="initial allocation")
            ctx.log("allocating {} x{} (~{:,.0f})".format(sym, qty, qty * bar["close"]))


class SmaCross(Strategy):
    description = "Long a symbol while its fast SMA is above the slow; flat otherwise."
    params = {"fast": 10, "slow": 30, "size_pct": 0.3}

    def on_start(self, ctx):
        slow = int(self.params["slow"])
        self._closes = {s: deque(maxlen=slow) for s in ctx.symbols}

    def on_bar(self, ctx, bar):
        sym, closes = bar["symbol"], self._closes.get(bar["symbol"])
        if closes is None:
            return
        closes.append(bar["close"])
        fast_n, slow_n = int(self.params["fast"]), int(self.params["slow"])
        if len(closes) < slow_n:
            return
        seq = list(closes)
        fast = sum(seq[-fast_n:]) / fast_n
        slow = sum(seq) / slow_n
        pos = ctx.position(sym)
        if fast > slow and pos <= 0:
            budget = ctx.equity * float(self.params["size_pct"])
            qty = int(budget / bar["close"]) if bar["close"] else 0
            if pos < 0:
                qty += int(-pos)
            if qty > 0:
                ctx.buy(sym, qty, note="fast SMA crossed above slow")
        elif fast < slow and pos > 0:
            ctx.sell(sym, pos, note="fast SMA crossed below slow")


class TickReversion(Strategy):
    description = ("Tick-driven: fade a z-score move of the last print against its own "
                   "rolling window; exit near the mean. Needs a live (or tick) feed.")
    params = {"window": 120, "entry_z": 2.0, "exit_z": 0.5, "qty": 10}

    def on_start(self, ctx):
        self._prices = {s: deque(maxlen=int(self.params["window"])) for s in ctx.symbols}

    def on_tick(self, ctx, tick):
        sym = tick["symbol"]
        window = self._prices.get(sym)
        if window is None:
            return
        window.append(float(tick["price"]))
        n = len(window)
        if n < max(20, int(self.params["window"]) // 2):
            return
        mean = sum(window) / n
        var = sum((p - mean) ** 2 for p in window) / n
        if var <= 0:
            return
        z = (window[-1] - mean) / var ** 0.5
        pos = ctx.position(sym)
        qty = float(self.params["qty"])
        if z > float(self.params["entry_z"]) and pos >= 0:
            ctx.sell(sym, qty + pos, note="z={:.2f} rich".format(z))
        elif z < -float(self.params["entry_z"]) and pos <= 0:
            ctx.buy(sym, qty - pos, note="z={:.2f} cheap".format(z))
        elif abs(z) < float(self.params["exit_z"]) and pos:
            ctx.order(sym, -pos, note="z={:.2f} home".format(z))


BUILTINS: Dict[str, Type[Strategy]] = {
    "buy_and_hold": BuyAndHold,
    "sma_cross": SmaCross,
    "tick_reversion": TickReversion,
}


def catalog() -> list:
    return [
        {"name": name, "description": cls.description, "params": dict(cls.params),
         "driven_by": "ticks" if name == "tick_reversion" else "bars"}
        for name, cls in BUILTINS.items()
    ]


def build(name: Optional[str] = None, params: Optional[Dict[str, Any]] = None,
          code: Optional[str] = None) -> Strategy:
    """A strategy instance from a built-in name, or from user source code."""
    if code:
        if not settings.playground_on:
            raise PermissionError(
                "Custom strategy code executes as the server user and follows the "
                "playground switch: set MFT_PLAYGROUND_ENABLED=true to allow it here.")
        import numpy as np
        import pandas as pd

        namespace: Dict[str, Any] = {"Strategy": Strategy, "np": np, "pd": pd,
                                     "deque": deque, "__name__": "__mft_strategy__"}
        exec(compile(code, "<strategy>", "exec"), namespace)  # noqa: S102 - the gated feature
        classes = [v for v in namespace.values()
                   if isinstance(v, type) and issubclass(v, Strategy) and v is not Strategy]
        if not classes:
            raise ValueError("The code must define a class that subclasses Strategy")
        return classes[-1](params)
    if not name or name not in BUILTINS:
        raise ValueError("Unknown strategy {!r}. Built-ins: {}".format(
            name, ", ".join(sorted(BUILTINS))))
    return BUILTINS[name](params)
