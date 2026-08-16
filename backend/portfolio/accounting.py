"""Cost-basis accounting.

The transaction log is the source of truth. Replaying it through
:func:`build_holdings` produces every derived number a portfolio has: share
count, cost basis, realised P&L, fees and dividends per symbol, plus the cash
balance. :func:`rebuild_positions` writes that replay into the ``positions``
table so the API can read holdings without replaying anything.

Rebuilding from scratch rather than incrementing counters is deliberate — it
means an edited or deleted transaction cannot leave a position wrong, and a
change of ``cost_basis_method`` re-states history correctly.

Two matching rules are supported:

``fifo``
    Sales are matched against the oldest open lot first (what most brokers
    default to, and what a tax lot report expects).
``average``
    Open lots are collapsed into one lot at the weighted-average price, so
    every sale realises against the same basis.

Short positions fall out of the same code: a sale with nothing to close opens a
negative lot, and a later purchase closes it.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..models import Portfolio, Position, Transaction

#: Quantities below this are treated as flat — protects against float dust
#: left behind by partial closes of fractional shares.
EPSILON = 1e-9


@dataclass
class Lot:
    """An open tax lot. ``quantity`` is signed: positive long, negative short."""

    quantity: float
    price: float
    opened_at: Optional[datetime] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "quantity": round(self.quantity, 10),
            "price": round(self.price, 10),
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
        }


@dataclass
class Holding:
    """Everything the log says about one symbol."""

    symbol: str
    asset_type: str = "equity"
    quantity: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    dividends: float = 0.0
    first_trade_at: Optional[datetime] = None
    last_trade_at: Optional[datetime] = None
    lots: Deque[Lot] = field(default_factory=deque)

    @property
    def cost_basis(self) -> float:
        """Money tied up in the open lots (negative for a short)."""
        return sum(lot.quantity * lot.price for lot in self.lots)

    @property
    def avg_cost(self) -> float:
        if abs(self.quantity) < EPSILON:
            return 0.0
        return self.cost_basis / self.quantity

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "asset_type": self.asset_type,
            "quantity": round(self.quantity, 10),
            "avg_cost": round(self.avg_cost, 10),
            "cost_basis": round(self.cost_basis, 10),
            "realized_pnl": round(self.realized_pnl, 10),
            "fees_paid": round(self.fees_paid, 10),
            "dividends": round(self.dividends, 10),
            "lots": [lot.as_dict() for lot in self.lots],
            "first_trade_at": self.first_trade_at,
            "last_trade_at": self.last_trade_at,
        }


@dataclass
class Ledger:
    """The full derived state of a portfolio."""

    holdings: Dict[str, Holding] = field(default_factory=dict)
    cash: float = 0.0
    deposits: float = 0.0
    withdrawals: float = 0.0
    dividends: float = 0.0
    fees: float = 0.0
    realized_pnl: float = 0.0

    @property
    def open_holdings(self) -> List[Holding]:
        return [h for h in self.holdings.values() if abs(h.quantity) > EPSILON]

    @property
    def net_deposits(self) -> float:
        return self.deposits - self.withdrawals


def _effective_price(quantity: float, price: float, fees: float, is_buy: bool) -> float:
    """Per-share price with the commission folded in.

    Capitalising fees into the lot on the way in, and netting them out of the
    proceeds on the way out, is what makes realised P&L come out net of costs
    without a separate fee adjustment.
    """
    if quantity <= 0:
        return price
    return price + (fees / quantity if is_buy else -fees / quantity)


def _close_lots(holding: Holding, quantity: float, price: float) -> Tuple[float, float]:
    """Close ``quantity`` units against open lots.

    ``quantity`` is signed the same way as the incoming trade, so a buy closing
    a short and a sale closing a long take the same path. Returns
    ``(realised P&L, quantity left over)`` — the leftover is what has to open a
    new lot, which is how a sale larger than the holding flips it short.
    """
    realized = 0.0
    remaining = quantity
    step = 1.0 if quantity > 0 else -1.0
    while abs(remaining) > EPSILON and holding.lots:
        lot = holding.lots[0]
        if (lot.quantity > 0) == (remaining > 0):
            break  # same direction — extending, not closing
        matched = min(abs(remaining), abs(lot.quantity))
        # Long lot: gain when the exit is above the entry. Short lot: reversed.
        direction = 1.0 if lot.quantity > 0 else -1.0
        realized += matched * (price - lot.price) * direction
        lot.quantity += matched * step
        remaining -= matched * step
        if abs(lot.quantity) < EPSILON:
            holding.lots.popleft()
    return realized, remaining


def _collapse_to_average(holding: Holding) -> None:
    """Merge open lots into a single weighted-average lot."""
    if len(holding.lots) < 2:
        return
    total_qty = sum(lot.quantity for lot in holding.lots)
    if abs(total_qty) < EPSILON:
        holding.lots.clear()
        return
    basis = sum(lot.quantity * lot.price for lot in holding.lots)
    opened = min((lot.opened_at for lot in holding.lots if lot.opened_at), default=None)
    holding.lots = deque([Lot(quantity=total_qty, price=basis / total_qty, opened_at=opened)])


def apply_transaction(ledger: Ledger, txn: Transaction, method: str = "fifo") -> None:
    """Fold one transaction into ``ledger``."""
    side = txn.side
    quantity = float(txn.quantity or 0.0)
    price = float(txn.price if txn.price is not None else 1.0)
    fees = float(txn.fees or 0.0)
    ledger.cash += txn.cash_flow

    if side == "deposit":
        ledger.deposits += quantity * price
        ledger.fees += fees
        return
    if side == "withdraw":
        ledger.withdrawals += quantity * price
        ledger.fees += fees
        return
    if side == "interest":
        ledger.dividends += quantity * price
        ledger.fees += fees
        return

    symbol = (txn.symbol or "").strip().upper()
    holding = ledger.holdings.get(symbol)
    if holding is None:
        holding = Holding(symbol=symbol, asset_type=txn.asset_type or "equity")
        ledger.holdings[symbol] = holding
    if txn.asset_type:
        holding.asset_type = txn.asset_type

    ledger.fees += fees
    holding.fees_paid += fees
    if holding.first_trade_at is None:
        holding.first_trade_at = txn.trade_date
    holding.last_trade_at = txn.trade_date

    if side == "dividend":
        # Cash paid on a holding: income, not a change in share count.
        holding.dividends += quantity * price
        ledger.dividends += quantity * price
        return
    if side == "fee":
        return

    signed = quantity if side == "buy" else -quantity
    exec_price = _effective_price(quantity, price, fees, is_buy=(side == "buy"))

    realized, leftover = _close_lots(holding, signed, exec_price)
    holding.realized_pnl += realized
    ledger.realized_pnl += realized

    # Whatever the close did not consume opens a lot — extending the position,
    # or flipping it through zero when a sale exceeds the shares held.
    if abs(leftover) > EPSILON:
        holding.lots.append(Lot(quantity=leftover, price=exec_price, opened_at=txn.trade_date))

    holding.quantity = sum(lot.quantity for lot in holding.lots)
    if abs(holding.quantity) < EPSILON:
        holding.quantity = 0.0
        holding.lots.clear()
    elif method == "average":
        _collapse_to_average(holding)


def naive_utc(stamp: Optional[datetime]) -> datetime:
    """A comparable timestamp.

    Trade dates arrive from three places — a client payload, a column default,
    and SQLite (which hands back naive datetimes) — so the log routinely mixes
    aware and naive values. Everything is compared in naive UTC.
    """
    if stamp is None:
        return datetime.min
    if stamp.tzinfo is not None:
        return stamp.astimezone(timezone.utc).replace(tzinfo=None)
    return stamp


def build_ledger(
    transactions: Iterable[Transaction], method: str = "fifo"
) -> Ledger:
    """Replay a transaction log into a :class:`Ledger`."""
    ledger = Ledger()
    ordered = sorted(transactions, key=lambda t: (naive_utc(t.trade_date), t.id or 0))
    for txn in ordered:
        apply_transaction(ledger, txn, method)
    return ledger


def rebuild_positions(db: Session, portfolio: Portfolio) -> Ledger:
    """Re-derive ``positions`` and the cash balance from the log.

    Called after every mutation of the blotter. Positions that closed out are
    kept with ``quantity = 0`` so their realised P&L stays visible.
    """
    transactions = (
        db.query(Transaction).filter(Transaction.portfolio_id == portfolio.id).all()
    )
    ledger = build_ledger(transactions, portfolio.cost_basis_method or "fifo")

    existing = {p.symbol: p for p in portfolio.positions}
    for symbol, holding in ledger.holdings.items():
        row = existing.pop(symbol, None)
        if row is None:
            row = Position(portfolio_id=portfolio.id, symbol=symbol)
            db.add(row)
            portfolio.positions.append(row)
        row.asset_type = holding.asset_type
        row.quantity = holding.quantity
        row.avg_cost = holding.avg_cost
        row.cost_basis = holding.cost_basis
        row.realized_pnl = holding.realized_pnl
        row.fees_paid = holding.fees_paid
        row.dividends = holding.dividends
        row.lots = [lot.as_dict() for lot in holding.lots]
        row.first_trade_at = holding.first_trade_at
        row.last_trade_at = holding.last_trade_at

    # Symbols that no longer appear in the log at all (their trades were deleted).
    for orphan in existing.values():
        db.delete(orphan)

    portfolio.cash = ledger.cash
    return ledger
