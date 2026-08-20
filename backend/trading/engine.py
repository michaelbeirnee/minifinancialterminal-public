"""The feed-agnostic engine, and the historical replay built on it.

``PaperEngine`` is deliberately synchronous and loop-free: it exposes
``process_tick`` / ``process_bar`` / ``close_due_bars`` and holds the OMS,
the risk gate and the strategy. The live session (``manager.py``) drives it
from the stream hub; ``replay`` drives it from historical bars. Same engine,
same code paths — which is the parity the package promises.

Fill timing, both feeds: an order placed inside a hook rests ACKNOWLEDGED and
fills against the *next* event's price — the next tick live, the next bar's
open in replay. Nothing ever fills at the price that triggered it, which is
the smallest honest model of not being able to trade the past.
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional

import pandas as pd

from .broker import InternalExecutor
from .oms import PaperOMS
from .risk import RiskGate
from .strategy import Context, Strategy

MAX_LOG_LINES = 400
MAX_EQUITY_POINTS = 5_000


class PaperEngine:
    def __init__(
        self,
        strategy: Strategy,
        symbols: List[str],
        cash: float = 100_000.0,
        limits: Optional[Dict[str, float]] = None,
        bar_seconds: int = 60,
        slippage_bps: float = 2.0,
        commission_per_share: float = 0.0,
        executor: Optional[Any] = None,
    ) -> None:
        self.strategy = strategy
        self.symbols = [s.upper() for s in symbols]
        self.oms = PaperOMS(cash, slippage_bps, commission_per_share)
        self.risk = RiskGate(limits, cash=cash)
        self.executor = executor or InternalExecutor()
        self.ctx = Context(self)
        self.bar_seconds = int(bar_seconds)
        self.last_tick: Dict[str, Dict[str, Any]] = {}
        self.last_tick_mono: Dict[str, float] = {}
        self.now: Optional[Any] = None
        self.replay_mode = False
        self.equity_curve: List[List[Any]] = []
        self.logs: List[str] = []
        self.ticks_seen = 0
        self.bars_seen = 0
        self._building: Dict[str, Dict[str, Any]] = {}   # symbol -> partial bar
        self._started = False

    # ---- plumbing the strategy sees --------------------------------------- #
    def last_prices(self) -> Dict[str, float]:
        return {s: t["price"] for s, t in self.last_tick.items() if t.get("price") is not None}

    def log(self, message: str) -> None:
        stamp = str(self.now or "")
        self.logs.append("{} {}".format(stamp, message).strip())
        if len(self.logs) > MAX_LOG_LINES:
            del self.logs[: len(self.logs) - MAX_LOG_LINES]

    def place_order(self, symbol: str, qty: float, limit: Optional[float] = None,
                    note: str = "", override: bool = False) -> Optional[Dict[str, Any]]:
        symbol = symbol.upper()
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            self.log("order refused: qty {!r} is not a number".format(qty))
            return None
        if not qty or not math.isfinite(qty):
            return None
        price = (self.last_tick.get(symbol) or {}).get("price")
        age = None
        if not self.replay_mode and symbol in self.last_tick_mono:
            age = time.monotonic() - self.last_tick_mono[symbol]
        ok, reason = self.risk.check(symbol, qty, price, self.oms, self.last_prices(),
                                     tick_age_seconds=age, override=override)
        order = self.oms.create(symbol, qty, limit=limit, ts=self.now, note=note)
        if not ok:
            self.oms.reject(order, reason)
            self.log("REJECTED {} {} x{:g}: {}".format(order["side"], symbol, abs(qty), reason))
            return None
        self.oms.send(order)
        try:
            self.executor.submit(order)
        except Exception as exc:  # noqa: BLE001 - a broker refusal is an order state
            self.oms.reject(order, "executor: {}".format(exc))
            self.log("REJECTED {} {}: {}".format(order["side"], symbol, exc))
            return None
        return order

    # ---- feeding ----------------------------------------------------------- #
    def start(self) -> None:
        if not self._started:
            self._started = True
            self.strategy.on_start(self.ctx)

    def stop(self) -> None:
        if self._started:
            self.strategy.on_stop(self.ctx)

    def process_tick(self, tick: Dict[str, Any]) -> List[Dict[str, Any]]:
        """One tick: fill resting orders first, then let the strategy see it."""
        symbol = str(tick.get("symbol", "")).upper()
        if symbol not in self.symbols or tick.get("price") is None:
            return []
        self.ticks_seen += 1
        self.now = tick.get("time") or self.now
        # Resting orders fill against this print BEFORE the strategy reacts to
        # it — an order can never fill at the tick that caused it. When a real
        # broker executes, fills arrive from its poll instead.
        fills = self.oms.match(tick) if self.executor.local_fills else []
        self.last_tick[symbol] = tick
        self.last_tick_mono[symbol] = time.monotonic()
        for fill in fills:
            self.strategy.on_fill(self.ctx, fill)
        self._update_bar(symbol, tick)
        self.strategy.on_tick(self.ctx, tick)
        return fills

    def process_bar(self, bar: Dict[str, Any]) -> None:
        """A completed bar (built live, or historical in replay)."""
        self.bars_seen += 1
        self.now = bar.get("end") or self.now
        self.strategy.on_bar(self.ctx, bar)

    def on_timer(self, ts: float) -> None:
        self.strategy.on_timer(self.ctx, ts)

    def apply_external_fill(self, order: Dict[str, Any], price: float,
                            qty: Optional[float], ts: Any) -> Dict[str, Any]:
        """A broker-reported execution: book it, then tell the strategy."""
        fill = self.oms.apply_fill(order, price, ts, qty=qty)
        self.log("BROKER FILL {} {} x{:g} @ {}".format(
            order["side"], order["symbol"], abs(fill["qty"]), fill["price"]))
        self.strategy.on_fill(self.ctx, fill)
        return fill

    def mark_equity(self, label: Any) -> None:
        self.equity_curve.append([label, round(self.oms.equity(self.last_prices()), 2)])
        if len(self.equity_curve) > MAX_EQUITY_POINTS:
            # Thin from the front, keeping the shape: drop every other old point.
            keep = self.equity_curve[::2] if len(self.equity_curve) % 2 == 0 else self.equity_curve[1::2]
            self.equity_curve = keep

    # ---- live bar building ------------------------------------------------- #
    def _bucket(self, epoch: float) -> int:
        return int(epoch // self.bar_seconds) * self.bar_seconds

    def _update_bar(self, symbol: str, tick: Dict[str, Any]) -> None:
        if self.replay_mode:
            return  # replay feeds real historical bars via process_bar
        price = float(tick["price"])
        epoch = time.time()
        bucket = self._bucket(epoch)
        bar = self._building.get(symbol)
        if bar is not None and bar["_bucket"] != bucket:
            self._emit_bar(symbol)
            bar = None
        if bar is None:
            self._building[symbol] = {
                "_bucket": bucket, "symbol": symbol, "open": price, "high": price,
                "low": price, "close": price, "volume": tick.get("volume"),
                "ticks": 1,
                "start": pd.Timestamp(bucket, unit="s", tz="UTC").isoformat(),
                "end": pd.Timestamp(bucket + self.bar_seconds, unit="s", tz="UTC").isoformat(),
            }
            return
        bar["high"] = max(bar["high"], price)
        bar["low"] = min(bar["low"], price)
        bar["close"] = price
        bar["volume"] = tick.get("volume", bar["volume"])
        bar["ticks"] += 1

    def _emit_bar(self, symbol: str) -> None:
        bar = self._building.pop(symbol, None)
        if bar is not None:
            bar.pop("_bucket", None)
            self.process_bar(bar)

    def close_due_bars(self) -> None:
        """Emit any building bar whose window has passed (quiet symbols too)."""
        bucket_now = self._bucket(time.time())
        for symbol in list(self._building):
            if self._building[symbol]["_bucket"] < bucket_now:
                self._emit_bar(symbol)

    # ---- reporting ---------------------------------------------------------- #
    def snapshot(self) -> Dict[str, Any]:
        book = self.oms.snapshot(self.last_prices())
        return {
            "strategy": type(self.strategy).__name__,
            "params": self.strategy.params,
            "symbols": self.symbols,
            "bar_seconds": self.bar_seconds,
            "ticks_seen": self.ticks_seen,
            "bars_seen": self.bars_seen,
            "execution": self.executor.status(),
            "book": book,
            "risk": self.risk.status(),
            "orders": self.oms.orders[-50:],
            "fills": self.oms.fills[-50:],
            "log": self.logs[-40:],
            "equity_curve": self.equity_curve[-600:],
        }


# --------------------------------------------------------------------------- #
# Replay: the same engine, fed history
# --------------------------------------------------------------------------- #
def replay(
    strategy: Strategy,
    bars: pd.DataFrame,
    cash: float = 100_000.0,
    limits: Optional[Dict[str, float]] = None,
    slippage_bps: float = 2.0,
    commission_per_share: float = 0.0,
) -> Dict[str, Any]:
    """Drive ``strategy`` with historical bars through the full engine.

    ``bars`` needs columns ``date, symbol, open, high, low, close`` (volume
    optional) — the shape ``/equity/price/historical`` returns. Per timestamp,
    each symbol contributes an *open tick* (which is where resting orders
    fill: next bar's open) and then the completed bar; equity is marked at
    every timestamp's closes. Stale-data checks are off — history is allowed
    to be old.
    """
    required = {"date", "symbol", "open", "close"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError("replay needs columns {}; missing {}".format(
            sorted(required), sorted(missing)))
    limits = {**(limits or {}), "stale_seconds": 0}
    symbols = sorted(bars["symbol"].astype(str).str.upper().unique())
    engine = PaperEngine(strategy, symbols, cash=cash, limits=limits,
                         slippage_bps=slippage_bps, commission_per_share=commission_per_share)
    engine.replay_mode = True
    engine.start()

    df = bars.copy()
    df["symbol"] = df["symbol"].astype(str).str.upper()
    df = df.sort_values("date", kind="stable")
    for ts, group in df.groupby("date", sort=True):
        label = str(getattr(ts, "date", lambda: ts)())
        # Pass 1 — every symbol's open prints: resting orders fill here.
        for row in group.itertuples(index=False):
            engine.process_tick({"symbol": row.symbol, "price": float(row.open),
                                 "time": label, "kind": "replay_open"})
        # Pass 2 — the completed bars: strategy logic runs on closes; its
        # orders now rest until the next timestamp's opens.
        for row in group.itertuples(index=False):
            close = float(row.close)
            engine.last_tick[row.symbol] = {"symbol": row.symbol, "price": close, "time": label}
            engine.process_bar({
                "symbol": row.symbol, "open": float(row.open),
                "high": float(getattr(row, "high", max(row.open, row.close))),
                "low": float(getattr(row, "low", min(row.open, row.close))),
                "close": close, "volume": float(getattr(row, "volume", 0) or 0),
                "start": label, "end": label,
            })
        engine.mark_equity(label)
    engine.stop()

    curve = engine.equity_curve
    equity = [p[1] for p in curve]
    peak, max_dd = -float("inf"), 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    round_trips = [f for f in engine.oms.fills if f["realized"]]
    wins = sum(1 for f in round_trips if f["realized"] > 0)
    final = equity[-1] if equity else cash
    return {
        "metrics": {
            "starting_cash": cash,
            "final_equity": round(final, 2),
            "total_return": round(final / cash - 1, 6),
            "max_drawdown": round(max_dd, 6),
            "fills": len(engine.oms.fills),
            "orders": len(engine.oms.orders),
            "rejected": sum(engine.risk.rejections.values()),
            "round_trips": len(round_trips),
            "win_rate": round(wins / len(round_trips), 4) if round_trips else None,
            "commissions": round(engine.oms.commissions_paid, 2),
            "bars": engine.bars_seen,
        },
        "equity_curve": curve,
        "book": engine.oms.snapshot(engine.last_prices()),
        "risk": engine.risk.status(),
        "fills": engine.oms.fills[-200:],
        "log": engine.logs[-100:],
    }
