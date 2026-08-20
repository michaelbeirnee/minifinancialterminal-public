"""The risk gate: every order passes through it, and the kill switch lives here.

The checks are deliberately blunt — notional caps, a loss floor, stale data,
an open-order cap — because blunt checks are the ones that still work when a
strategy is doing something its author did not anticipate, which is the only
time a risk gate earns its keep. Every rejection is counted by reason, so a
strategy that keeps walking into the same wall is visible.

The kill switch is one-way for the session: once engaged (manually or by the
daily-loss check) no further strategy order passes; the session cancels its
open orders and, on request, flattens with override orders that only the kill
path may send.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

def defaults_for(cash: float) -> Dict[str, float]:
    """Limits scaled to the book, so a $10k and a $1m session both make sense.

    Deliberately loose enough that a vanilla long-only allocation passes —
    the gate exists to catch a strategy running away, not to veto sane
    sizing. Tighten per session via ``limits=``.
    """
    return {
        "max_order_notional": float(cash),        # one order's |qty| * price
        "max_position_notional": float(cash),     # one symbol's post-fill |exposure|
        "max_gross_notional": 2.0 * float(cash),  # book-wide sum of |exposures|
        "max_loss": 0.10 * float(cash),           # equity below start - this => kill
        "max_open_orders": 20,
        "stale_seconds": 120.0,                   # live only; replay passes 0
    }


class RiskGate:
    def __init__(self, limits: Optional[Dict[str, float]] = None,
                 cash: float = 100_000.0) -> None:
        self.limits = {**defaults_for(cash), **(limits or {})}
        self.killed = False
        self.kill_reason: Optional[str] = None
        self.approved = 0
        self.rejections: Dict[str, int] = {}

    def engage_kill(self, reason: str) -> None:
        if not self.killed:
            self.killed = True
            self.kill_reason = reason

    def _reject(self, reason: str) -> Tuple[bool, str]:
        self.rejections[reason] = self.rejections.get(reason, 0) + 1
        return False, reason

    def check(
        self,
        symbol: str,
        qty: float,
        price: Optional[float],
        oms: Any,
        last_prices: Dict[str, float],
        tick_age_seconds: Optional[float] = None,
        override: bool = False,
    ) -> Tuple[bool, str]:
        """Approve or refuse one order before the OMS sees it."""
        if override:
            # The kill path flattening its own book — size checks would be
            # self-defeating here; the override exists for exactly this.
            self.approved += 1
            return True, "override"
        if self.killed:
            return self._reject("kill switch engaged: {}".format(self.kill_reason))
        if price is None or price <= 0:
            return self._reject("no price for {} yet — order refused, not guessed".format(symbol))
        lim = self.limits
        if tick_age_seconds is not None and lim.get("stale_seconds") \
                and tick_age_seconds > lim["stale_seconds"]:
            return self._reject("stale data: last {} tick is {:.0f}s old".format(
                symbol, tick_age_seconds))
        if len(oms.open_orders()) >= lim["max_open_orders"]:
            return self._reject("open-order cap ({:.0f}) reached".format(lim["max_open_orders"]))
        order_notional = abs(qty) * price
        if order_notional > lim["max_order_notional"]:
            return self._reject("order notional {:,.0f} over cap {:,.0f}".format(
                order_notional, lim["max_order_notional"]))
        pos_qty = oms.positions.get(symbol, {}).get("qty", 0.0)
        post_notional = abs(pos_qty + qty) * price
        if post_notional > lim["max_position_notional"]:
            return self._reject("{} position would be {:,.0f}, over cap {:,.0f}".format(
                symbol, post_notional, lim["max_position_notional"]))
        gross_after = oms.gross_notional(last_prices) + order_notional
        if gross_after > lim["max_gross_notional"]:
            return self._reject("gross notional would be {:,.0f}, over cap {:,.0f}".format(
                gross_after, lim["max_gross_notional"]))
        # The loss check: engage the kill switch, not just refuse one order.
        equity = oms.equity(last_prices)
        if equity < oms.starting_cash - lim["max_loss"]:
            self.engage_kill("loss limit: equity {:,.0f} is more than {:,.0f} below start".format(
                equity, lim["max_loss"]))
            return self._reject(self.kill_reason)
        self.approved += 1
        return True, "ok"

    def status(self) -> Dict[str, Any]:
        return {
            "killed": self.killed,
            "kill_reason": self.kill_reason,
            "limits": self.limits,
            "approved": self.approved,
            "rejections": dict(sorted(self.rejections.items())),
        }
