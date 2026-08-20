"""Live paper sessions: one per user, driven by the stream hub.

A session is an asyncio task on the server's loop that drains a hub
subscription into a :class:`PaperEngine` — the same engine ``replay`` uses.
It closes bars on the wall clock (so a quiet symbol still finishes its bar),
snapshots equity every few seconds, and dies cleanly on stop, kill, or server
shutdown. State is in-memory and ephemeral, exactly like a playground kernel:
a paper book that outlives the process would be pretending to be a broker.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from .broker import build_executor
from .engine import PaperEngine
from .strategies import build

#: Wall-clock seconds between equity snapshots in the live curve.
EQUITY_EVERY = 5.0


class LiveSession:
    def __init__(self, engine: PaperEngine, provider: Optional[str]) -> None:
        self.engine = engine
        self.provider = provider
        self.state = "starting"          # starting | running | stopped | killed
        self.started_at: Optional[float] = None
        self.stopped_at: Optional[float] = None
        self.error: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._sub = None

    async def start(self) -> None:
        from ..stream.hub import get_hub

        hub = get_hub(self.provider)
        self.provider = hub.provider
        self._sub = await hub.subscribe(self.engine.symbols)
        self.engine.start()
        self.started_at = time.time()
        self.state = "running"
        self._task = asyncio.get_running_loop().create_task(
            self._run(), name="mft-paper-session")

    async def _run(self) -> None:
        last_equity = 0.0
        loop = asyncio.get_running_loop()
        try:
            while True:
                ticks = await self._sub.drain(timeout=1.0)
                for tick in ticks:
                    self.engine.process_tick(tick)
                await self._broker_sync(loop)
                now = time.time()
                self.engine.close_due_bars()
                self.engine.on_timer(now)
                if now - last_equity >= EQUITY_EVERY and self.engine.last_tick:
                    last_equity = now
                    self.engine.mark_equity(time.strftime("%H:%M:%S", time.gmtime(now)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a session dies visibly, not silently
            self.error = "{}: {}".format(type(exc).__name__, exc)
            self.state = "stopped"
            self.engine.log("session crashed: {}".format(self.error))

    async def _broker_sync(self, loop) -> None:
        """Flush queued orders to the broker and book its fills — off the loop."""
        executor = self.engine.executor
        if executor.local_fills:
            return
        fills = await loop.run_in_executor(
            None, executor.flush_and_poll, self.engine.oms.open_orders())
        for order, price, qty, ts in fills:
            self.engine.apply_external_fill(order, price, qty, ts)

    async def stop(self, state: str = "stopped") -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._sub is not None:
            await self._sub.close()
            self._sub = None
        if self.state in ("running", "starting"):
            self.engine.stop()
        self.state = state
        self.stopped_at = time.time()

    async def kill(self, flatten: bool = False) -> Dict[str, Any]:
        """The red button: no more strategy orders, cancel the rest, optionally flatten."""
        self.engine.risk.engage_kill("kill switch pressed")
        executor = self.engine.executor
        loop = asyncio.get_running_loop()
        if not executor.local_fills:
            # Cancel at the venue first, then locally, then flatten through it.
            await loop.run_in_executor(None, executor.cancel_all)
        cancelled = self.engine.oms.cancel_open()
        flattened: List[Dict[str, Any]] = []
        if flatten and not executor.local_fills:
            for symbol, pos in list(self.engine.oms.positions.items()):
                if pos["qty"]:
                    self.engine.place_order(symbol, -pos["qty"],
                                            note="kill switch flatten", override=True)
            # Give the broker a few rounds to fill the flattening orders.
            for _ in range(8):
                fills = await loop.run_in_executor(
                    None, executor.flush_and_poll, self.engine.oms.open_orders())
                for order, price, qty, ts in fills:
                    flattened.append(self.engine.apply_external_fill(order, price, qty, ts))
                if not any(p["qty"] for p in self.engine.oms.positions.values()):
                    break
                await asyncio.sleep(1.0)
        elif flatten:
            for symbol, pos in list(self.engine.oms.positions.items()):
                qty = pos["qty"]
                if not qty:
                    continue
                order = self.engine.place_order(symbol, -qty, note="kill switch flatten",
                                                override=True)
                if order is not None:
                    tick = self.engine.last_tick.get(symbol)
                    if tick:
                        # Flatten fills against the last known print immediately:
                        # waiting for the next tick with the switch pulled would
                        # be ceremony, not caution.
                        fills = self.engine.oms.match(tick)
                        flattened.extend(fills)
        if flatten and not executor.local_fills:
            left = [s for s, p in self.engine.oms.positions.items() if p["qty"]]
            if left:
                self.engine.log("KILL SWITCH: {} not confirmed flat at the broker yet — "
                                "check the paper account".format(", ".join(left)))
        self.engine.log("KILL SWITCH: {} open orders cancelled{}".format(
            cancelled, ", book flattened" if flatten else ""))
        await self.stop(state="killed")
        return {"cancelled": cancelled, "flattened": flattened}

    def status(self) -> Dict[str, Any]:
        snap = self.engine.snapshot()
        snap.update({
            "state": self.state,
            "provider": self.provider,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "error": self.error,
        })
        return snap


class SessionManager:
    """One live session per user id."""

    def __init__(self) -> None:
        self._sessions: Dict[int, LiveSession] = {}

    def get(self, user_id: int) -> Optional[LiveSession]:
        return self._sessions.get(user_id)

    async def start(
        self,
        user_id: int,
        strategy_name: Optional[str],
        params: Optional[Dict[str, Any]],
        code: Optional[str],
        symbols: List[str],
        cash: float,
        limits: Optional[Dict[str, float]],
        bar_seconds: int,
        provider: Optional[str],
        execution: Optional[str] = None,
    ) -> LiveSession:
        existing = self._sessions.get(user_id)
        if existing is not None and existing.state == "running":
            raise RuntimeError("A paper session is already running — stop it first")
        strategy = build(strategy_name, params, code)
        executor = build_executor(execution)
        executor.validate_symbols(symbols)
        engine = PaperEngine(strategy, symbols, cash=cash, limits=limits,
                             bar_seconds=bar_seconds, executor=executor)
        session = LiveSession(engine, provider)
        await session.start()
        self._sessions[user_id] = session
        return session

    async def stop(self, user_id: int) -> Optional[Dict[str, Any]]:
        session = self._sessions.get(user_id)
        if session is None:
            return None
        await session.stop()
        return session.status()

    async def kill(self, user_id: int, flatten: bool) -> Optional[Dict[str, Any]]:
        session = self._sessions.get(user_id)
        if session is None:
            return None
        result = await session.kill(flatten)
        result["status"] = session.status()
        return result

    async def shutdown(self) -> None:
        for session in list(self._sessions.values()):
            if session.state == "running":
                await session.stop()
        self._sessions.clear()


manager = SessionManager()
