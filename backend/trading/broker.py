"""Execution adapters: internal paper fills, or Alpaca's paper-trading account.

The engine owns the book either way; an executor only decides how an
ACKNOWLEDGED order becomes a fill.

``InternalExecutor``
    The default. Orders rest in the OMS and fill against the next tick the
    engine sees — no network, works with zero keys.

``AlpacaPaperExecutor``
    Routes orders to Alpaca's **paper-trading** API, so fills come from a
    real matching venue's simulation instead of our own next-tick rule —
    queue-ish behaviour, their slippage model, an account that exists outside
    this process. Orders are queued in-process and flushed off the event loop
    (``flush_and_poll`` runs in a worker thread), and fills are polled and
    applied back to the local OMS, which keeps its own state machine as the
    reconciliation record: what we requested, what the broker acknowledged,
    what actually filled.

Hard safety line: the adapter asserts the host is ``paper-api.alpaca.markets``
and refuses to start otherwise. This project places no real-money orders —
that is a property of the code, not a configuration default.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from ..config import settings
from .oms import ACKNOWLEDGED, CANCELLED, REJECTED, SUBMITTED

log = logging.getLogger("mft.trading")

PAPER_HOST = "paper-api.alpaca.markets"
#: How often the executor refreshes the broker account/positions snapshot.
ACCOUNT_EVERY = 10.0


class InternalExecutor:
    """Fills happen locally, against the next tick. The zero-key default."""

    name = "internal"
    local_fills = True

    def submit(self, order: Dict[str, Any]) -> None:
        order["state"] = ACKNOWLEDGED     # the in-process broker always hears us

    def flush_and_poll(self, open_orders: List[Dict[str, Any]]) -> List[Tuple]:
        return []

    def cancel_all(self) -> None: ...

    def validate_symbols(self, symbols: List[str]) -> None: ...

    def status(self) -> Dict[str, Any]:
        return {"execution": "internal", "note": "fills against the next local tick"}


class AlpacaPaperExecutor:
    """Orders out to, and fills back from, an Alpaca paper account."""

    name = "alpaca"
    local_fills = False

    def __init__(self, key: str, secret: str, base: Optional[str] = None) -> None:
        base = (base or settings.alpaca_paper_base).rstrip("/")
        host = urlparse(base).hostname or ""
        if host != PAPER_HOST:
            raise ValueError(
                "Refusing to trade against {!r}: this adapter only speaks to {} — "
                "no real-money endpoint, by design.".format(host, PAPER_HOST))
        if not (key and secret):
            raise ValueError(
                "Alpaca execution needs MFT_ALPACA_API_KEY and MFT_ALPACA_API_SECRET "
                "(free at https://alpaca.markets; use the keys of a *paper* account).")
        self.base = base
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self._outbox: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self.broker_account: Optional[Dict[str, Any]] = None
        self.broker_positions: List[Dict[str, Any]] = []
        self.last_error: Optional[str] = None
        self._last_account = 0.0

    # ---- HTTP (worker thread only) ----------------------------------------- #
    def _request(self, method: str, path: str, json_body: Any = None) -> Any:
        import httpx

        resp = httpx.request(method, self.base + path, headers=self._headers,
                             json=json_body, timeout=15.0)
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("message", "")
            except Exception:  # noqa: BLE001 - the status code carries the story
                pass
            raise RuntimeError("alpaca {} {} -> HTTP {} {}".format(
                method, path, resp.status_code, detail).strip())
        return resp.json() if resp.content else None

    # ---- the executor interface -------------------------------------------- #
    def validate_symbols(self, symbols: List[str]) -> None:
        from ..stream.sources import alpaca_symbol

        bad = [s for s in symbols if alpaca_symbol(s) is None]
        if bad:
            raise ValueError(
                "Alpaca paper execution trades US stocks and ETFs only — {} cannot be "
                "routed. Use internal execution for those.".format(", ".join(bad)))

    def submit(self, order: Dict[str, Any]) -> None:
        """Queue only — called on the event loop, must not block."""
        with self._lock:
            self._outbox.append(order)

    def flush_and_poll(self, open_orders: List[Dict[str, Any]]) -> List[Tuple]:
        """Send queued orders, poll the open ones. Runs in a worker thread.

        Returns ``(order, fill_price, fill_qty, filled_at)`` tuples for orders
        the broker reports filled; mutates order state for acks, rejections
        and cancellations directly.
        """
        from ..stream.sources import alpaca_symbol

        with self._lock:
            outbox, self._outbox = self._outbox, []
        for order in outbox:
            body = {
                "symbol": alpaca_symbol(order["symbol"]),
                "qty": str(abs(order["qty"])),
                "side": order["side"],
                "type": "limit" if order.get("limit") is not None else "market",
                "time_in_force": "day",
            }
            if order.get("limit") is not None:
                body["limit_price"] = str(order["limit"])
            try:
                resp = self._request("POST", "/v2/orders", body)
                order["broker_id"] = resp["id"]
                order["state"] = ACKNOWLEDGED
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - the order carries the reason
                order["state"] = REJECTED
                order["reason"] = "broker: {}".format(exc)
                self.last_error = str(exc)

        fills: List[Tuple] = []
        for order in open_orders:
            broker_id = order.get("broker_id")
            if not broker_id or order["state"] not in (SUBMITTED, ACKNOWLEDGED):
                continue
            try:
                resp = self._request("GET", "/v2/orders/{}".format(broker_id))
            except Exception as exc:  # noqa: BLE001 - poll again next round
                self.last_error = str(exc)
                continue
            status = resp.get("status")
            if status == "filled":
                qty = float(resp.get("filled_qty") or abs(order["qty"]))
                signed = qty if order["qty"] > 0 else -qty
                fills.append((order, float(resp["filled_avg_price"]), signed,
                              resp.get("filled_at")))
            elif status in ("canceled", "expired", "done_for_day"):
                order["state"] = CANCELLED
                order["reason"] = "broker: {}".format(status)
            elif status == "rejected":
                order["state"] = REJECTED
                order["reason"] = "broker: rejected"

        now = time.monotonic()
        if now - self._last_account >= ACCOUNT_EVERY:
            self._last_account = now
            try:
                acct = self._request("GET", "/v2/account") or {}
                self.broker_account = {
                    "equity": float(acct.get("equity", 0)),
                    "cash": float(acct.get("cash", 0)),
                    "buying_power": float(acct.get("buying_power", 0)),
                }
                self.broker_positions = [
                    {"symbol": p.get("symbol"), "qty": float(p.get("qty", 0)),
                     "avg_entry": float(p.get("avg_entry_price", 0)),
                     "market_value": float(p.get("market_value", 0))}
                    for p in (self._request("GET", "/v2/positions") or [])
                ]
            except Exception as exc:  # noqa: BLE001 - the snapshot is advisory
                self.last_error = str(exc)
        return fills

    def cancel_all(self) -> None:
        try:
            self._request("DELETE", "/v2/orders")
        except Exception as exc:  # noqa: BLE001 - reported via status
            self.last_error = str(exc)

    def status(self) -> Dict[str, Any]:
        return {
            "execution": "alpaca-paper",
            "endpoint": self.base,
            "queued": len(self._outbox),
            "account": self.broker_account,
            "broker_positions": self.broker_positions,
            "last_error": self.last_error,
            "note": ("fills come from Alpaca's paper matching; the local book is the "
                     "reconciliation record"),
        }


def build_executor(execution: Optional[str]) -> Any:
    """``internal`` (default) or ``alpaca`` — the latter needs the keys."""
    kind = (execution or "internal").strip().lower()
    if kind in ("internal", "paper", ""):
        return InternalExecutor()
    if kind == "alpaca":
        return AlpacaPaperExecutor(settings.alpaca_api_key or "",
                                   settings.alpaca_api_secret or "")
    raise ValueError("Unknown execution {!r}: internal or alpaca".format(execution))


def alpaca_execution_available() -> bool:
    return bool(settings.alpaca_api_key and settings.alpaca_api_secret)
