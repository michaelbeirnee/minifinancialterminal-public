"""The paper order machine: explicit states, positions, cash, P&L.

Orders move through the lifecycle a real broker would put them through::

    CREATED -> SUBMITTED -> ACKNOWLEDGED -> FILLED
                                 |
                                 +-> CANCELLED
    (rejected by risk) -> REJECTED

``send`` never touches a position — a position changes only when ``match``
produces a fill against an actual tick, which is the discipline the whole
package exists to practice. Fills are all-or-nothing in v1 (the state machine
carries ``filled_qty`` so partials are an extension, not a rewrite).

Fill prices: a buy crosses the ask and a sell hits the bid when the tick has
them (Alpaca does); otherwise the last price is worsened by ``slippage_bps``.
A limit order fills only when that executable price is inside the limit.
"""
from __future__ import annotations

import itertools
from typing import Any, Dict, List, Optional

# Order states
CREATED = "CREATED"
SUBMITTED = "SUBMITTED"
ACKNOWLEDGED = "ACKNOWLEDGED"
FILLED = "FILLED"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"

OPEN_STATES = (SUBMITTED, ACKNOWLEDGED)


class PaperOMS:
    def __init__(self, cash: float, slippage_bps: float = 2.0,
                 commission_per_share: float = 0.0) -> None:
        self.starting_cash = float(cash)
        self.cash = float(cash)
        self.slippage_bps = float(slippage_bps)
        self.commission_per_share = float(commission_per_share)
        self.positions: Dict[str, Dict[str, float]] = {}
        self.orders: List[Dict[str, Any]] = []
        self.fills: List[Dict[str, Any]] = []
        self.commissions_paid = 0.0
        self._ids = itertools.count(1)

    # ---- order entry ------------------------------------------------------ #
    def create(self, symbol: str, qty: float, limit: Optional[float] = None,
               ts: Optional[str] = None, note: str = "") -> Dict[str, Any]:
        if not qty:
            raise ValueError("qty must be non-zero (positive buys, negative sells)")
        order = {
            "id": next(self._ids), "symbol": symbol.upper(), "qty": float(qty),
            "side": "buy" if qty > 0 else "sell", "limit": limit,
            "state": CREATED, "created_at": ts, "note": note,
            "fill_price": None, "filled_qty": 0.0, "filled_at": None, "reason": None,
        }
        self.orders.append(order)
        return order

    def send(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """Mark the order on its way. The *executor* acknowledges it — the
        internal one instantly, the broker one when the venue accepts."""
        order["state"] = SUBMITTED
        return order

    def reject(self, order: Dict[str, Any], reason: str) -> Dict[str, Any]:
        order["state"] = REJECTED
        order["reason"] = reason
        return order

    def open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        return [o for o in self.orders
                if o["state"] in OPEN_STATES and (symbol is None or o["symbol"] == symbol)]

    def cancel_open(self, symbol: Optional[str] = None) -> int:
        n = 0
        for o in self.open_orders(symbol):
            o["state"] = CANCELLED
            n += 1
        return n

    # ---- matching ---------------------------------------------------------- #
    def _exec_price(self, order: Dict[str, Any], tick: Dict[str, Any]) -> Optional[float]:
        buy = order["qty"] > 0
        quoted = tick.get("ask") if buy else tick.get("bid")
        if quoted:
            return float(quoted)
        last = tick.get("price")
        if last is None:
            return None
        slip = self.slippage_bps / 10_000.0
        return float(last) * (1 + slip if buy else 1 - slip)

    def match(self, tick: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fill open orders for this tick's symbol. Returns the fills."""
        fills = []
        for order in self.open_orders(str(tick.get("symbol", "")).upper()):
            price = self._exec_price(order, tick)
            if price is None:
                continue
            limit = order["limit"]
            if limit is not None:
                if order["qty"] > 0 and price > limit:
                    continue
                if order["qty"] < 0 and price < limit:
                    continue
            fills.append(self._fill(order, price, tick.get("time")))
        return fills

    def _fill(self, order: Dict[str, Any], price: float, ts: Any) -> Dict[str, Any]:
        qty = order["qty"]
        commission = abs(qty) * self.commission_per_share
        pos = self.positions.setdefault(order["symbol"], {"qty": 0.0, "avg_cost": 0.0, "realized": 0.0})
        old_qty, avg = pos["qty"], pos["avg_cost"]
        realized = 0.0
        if old_qty * qty >= 0:
            # Same direction (or flat): average in.
            new_qty = old_qty + qty
            pos["avg_cost"] = ((abs(old_qty) * avg + abs(qty) * price) / abs(new_qty)) if new_qty else 0.0
            pos["qty"] = new_qty
        else:
            # Reducing / crossing through flat: realise P&L on the closed part.
            closed = min(abs(qty), abs(old_qty))
            realized = closed * (price - avg) * (1 if old_qty > 0 else -1)
            pos["realized"] += realized
            new_qty = old_qty + qty
            pos["qty"] = new_qty
            if old_qty * new_qty < 0:      # crossed through zero: remainder opens at price
                pos["avg_cost"] = price
            elif new_qty == 0:
                pos["avg_cost"] = 0.0
        self.cash -= qty * price + commission
        self.commissions_paid += commission
        order.update(state=FILLED, fill_price=round(price, 6), filled_qty=qty, filled_at=ts)
        fill = {"order_id": order["id"], "symbol": order["symbol"], "qty": qty,
                "price": round(price, 6), "commission": round(commission, 6),
                "realized": round(realized, 6), "time": ts, "note": order.get("note", "")}
        self.fills.append(fill)
        return fill

    def apply_fill(self, order: Dict[str, Any], price: float, ts: Any,
                   qty: Optional[float] = None) -> Dict[str, Any]:
        """Record an execution reported from outside (a broker fill)."""
        if qty is not None and qty != order["qty"]:
            # The broker filled a different quantity than requested (paper
            # rarely partials, but the book must follow the venue, not hope).
            order["qty"] = qty
            order["side"] = "buy" if qty > 0 else "sell"
        return self._fill(order, float(price), ts)

    # ---- marks ------------------------------------------------------------- #
    def unrealized(self, last_prices: Dict[str, float]) -> float:
        total = 0.0
        for sym, pos in self.positions.items():
            px = last_prices.get(sym)
            if px is not None and pos["qty"]:
                total += pos["qty"] * (px - pos["avg_cost"])
        return total

    def gross_notional(self, last_prices: Dict[str, float]) -> float:
        return sum(abs(p["qty"]) * last_prices.get(s, p["avg_cost"])
                   for s, p in self.positions.items() if p["qty"])

    def equity(self, last_prices: Dict[str, float]) -> float:
        return self.cash + sum(p["qty"] * last_prices.get(s, p["avg_cost"])
                               for s, p in self.positions.items() if p["qty"])

    def snapshot(self, last_prices: Dict[str, float]) -> Dict[str, Any]:
        equity = self.equity(last_prices)
        return {
            "cash": round(self.cash, 2),
            "equity": round(equity, 2),
            "pnl": round(equity - self.starting_cash, 2),
            "realized": round(sum(p["realized"] for p in self.positions.values()), 2),
            "unrealized": round(self.unrealized(last_prices), 2),
            "commissions": round(self.commissions_paid, 2),
            "positions": [
                {"symbol": s, "qty": p["qty"], "avg_cost": round(p["avg_cost"], 4),
                 "last": last_prices.get(s),
                 "market_value": round(p["qty"] * last_prices.get(s, p["avg_cost"]), 2),
                 "unrealized": round(p["qty"] * (last_prices.get(s, p["avg_cost"]) - p["avg_cost"]), 2),
                 "realized": round(p["realized"], 2)}
                for s, p in sorted(self.positions.items()) if p["qty"]
            ],
            "open_orders": len(self.open_orders()),
            "orders_total": len(self.orders),
            "fills_total": len(self.fills),
        }
