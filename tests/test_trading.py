"""Paper trading: the OMS, the risk gate, the engine, replay/live parity.

All offline. The live path is exercised against the same FakeSource hub the
stream tests use; replay runs on synthetic bars. The one property tested
hardest is the package's reason to exist: the same strategy object, fed the
same prices through the live tick path and the replay path, produces the
same fills.
"""
from __future__ import annotations

import asyncio
import uuid

import pandas as pd
import pytest

from backend.config import settings
from backend.stream import hub as hub_mod
from backend.stream.hub import StreamHub
from backend.trading import PaperEngine, PaperOMS, RiskGate, Strategy, replay
from backend.trading import manager as live_manager
from backend.trading.strategies import build
from tests.test_stream import FakeSource, _run


def tick(symbol, price, **extra):
    return {"symbol": symbol, "price": price, "time": extra.pop("time", "t"), **extra}


# --------------------------------------------------------------------------- #
# The OMS
# --------------------------------------------------------------------------- #
def test_order_lifecycle_and_no_same_tick_fill():
    oms = PaperOMS(cash=10_000, slippage_bps=0)
    order = oms.send(oms.create("AAPL", 10, ts="t0"))
    assert order["state"] == "SUBMITTED"   # the *executor* acknowledges, not send()
    assert oms.positions == {}          # sending is not filling
    fills = oms.match(tick("AAPL", 100.0))
    assert len(fills) == 1 and order["state"] == "FILLED"
    assert order["fill_price"] == 100.0
    assert oms.positions["AAPL"]["qty"] == 10
    assert oms.cash == 10_000 - 1_000


def test_fills_cross_the_spread_when_quoted():
    oms = PaperOMS(cash=10_000, slippage_bps=50)  # slippage must be ignored when quoted
    oms.send(oms.create("AAPL", 5))
    (fill,) = oms.match(tick("AAPL", 100.0, bid=99.9, ask=100.1))
    assert fill["price"] == 100.1       # buys pay the ask
    oms.send(oms.create("AAPL", -5))
    (fill2,) = oms.match(tick("AAPL", 100.0, bid=99.9, ask=100.1))
    assert fill2["price"] == 99.9       # sells hit the bid


def test_slippage_applies_without_quotes():
    oms = PaperOMS(cash=10_000, slippage_bps=10)
    oms.send(oms.create("X", 1))
    (fill,) = oms.match(tick("X", 100.0))
    assert fill["price"] == pytest.approx(100.10)


def test_limit_orders_wait_to_be_marketable():
    oms = PaperOMS(cash=10_000, slippage_bps=0)
    oms.send(oms.create("X", 10, limit=99.0))
    assert oms.match(tick("X", 100.0)) == []          # too expensive, rests
    (fill,) = oms.match(tick("X", 98.5))
    assert fill["price"] == 98.5
    assert oms.open_orders() == []


def test_realized_pnl_and_crossing_flat():
    oms = PaperOMS(cash=100_000, slippage_bps=0)
    oms.send(oms.create("X", 100)); oms.match(tick("X", 10.0))
    oms.send(oms.create("X", -150)); oms.match(tick("X", 12.0))   # close 100, short 50
    pos = oms.positions["X"]
    assert pos["realized"] == pytest.approx(100 * 2.0)
    assert pos["qty"] == -50 and pos["avg_cost"] == 12.0
    snap = oms.snapshot({"X": 12.0})
    assert snap["realized"] == pytest.approx(200.0)


def test_cancel_open():
    oms = PaperOMS(cash=1_000)
    oms.send(oms.create("A", 1, limit=1)); oms.send(oms.create("B", 1, limit=1))
    assert oms.cancel_open() == 2
    assert all(o["state"] == "CANCELLED" for o in oms.orders)


# --------------------------------------------------------------------------- #
# The risk gate
# --------------------------------------------------------------------------- #
def test_risk_caps_and_counting():
    oms = PaperOMS(cash=100_000)
    gate = RiskGate({"max_order_notional": 1_000, "stale_seconds": 0})
    ok, why = gate.check("X", 100, 50.0, oms, {})
    assert not ok and "order notional" in why
    ok, _ = gate.check("X", 10, 50.0, oms, {})
    assert ok
    assert gate.status()["approved"] == 1
    assert sum(gate.status()["rejections"].values()) == 1


def test_risk_position_and_gross_caps():
    oms = PaperOMS(cash=1_000_000)
    oms.send(oms.create("X", 100)); oms.match(tick("X", 100.0))
    gate = RiskGate({"max_position_notional": 12_000, "max_gross_notional": 15_000,
                     "stale_seconds": 0})
    ok, why = gate.check("X", 30, 100.0, oms, {"X": 100.0})
    assert not ok and "position" in why
    ok, why = gate.check("Y", 60, 100.0, oms, {"X": 100.0})
    assert not ok and "gross" in why
    ok, _ = gate.check("Y", 40, 100.0, oms, {"X": 100.0})
    assert ok


def test_loss_limit_engages_the_kill_switch():
    oms = PaperOMS(cash=100_000)
    oms.send(oms.create("X", 100)); oms.match(tick("X", 100.0))
    gate = RiskGate({"max_loss": 500, "stale_seconds": 0})
    ok, why = gate.check("X", 1, 90.0, oms, {"X": 90.0})   # book is down 1,000
    assert not ok and gate.killed and "loss limit" in gate.kill_reason
    # and once killed, everything is refused — except the override path
    assert gate.check("X", 1, 90.0, oms, {"X": 90.0})[0] is False
    assert gate.check("X", -100, 90.0, oms, {"X": 90.0}, override=True)[0] is True


def test_stale_data_is_refused():
    oms = PaperOMS(cash=100_000)
    gate = RiskGate({"stale_seconds": 10})
    ok, why = gate.check("X", 1, 100.0, oms, {}, tick_age_seconds=60)
    assert not ok and "stale" in why


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
class BuyOnFirstBar(Strategy):
    params = {"qty": 10}

    def on_bar(self, ctx, bar):
        if not ctx.position(bar["symbol"]):
            ctx.buy(bar["symbol"], self.params["qty"])


def test_orders_fill_on_the_next_event_never_the_same_one():
    eng = PaperEngine(BuyOnFirstBar(), ["X"], cash=10_000, bar_seconds=60,
                      limits={"stale_seconds": 0}, slippage_bps=0)
    eng.replay_mode = True
    eng.start()
    eng.last_tick["X"] = tick("X", 100.0)
    eng.process_bar({"symbol": "X", "open": 99, "high": 101, "low": 98, "close": 100.0,
                     "start": "t1", "end": "t1"})
    assert eng.oms.open_orders()                      # placed, not filled
    fills = eng.process_tick(tick("X", 105.0))        # the next print fills it
    assert len(fills) == 1 and fills[0]["price"] == 105.0


def test_strategy_param_coercion_follows_default_types():
    s = build("sma_cross", {"fast": "5", "slow": "12", "size_pct": "0.5"})
    assert s.params["fast"] == 5 and isinstance(s.params["fast"], int)
    assert s.params["size_pct"] == 0.5


def test_custom_code_follows_the_playground_switch(monkeypatch):
    code = "class Mine(Strategy):\n    def on_bar(self, ctx, bar): pass\n"
    monkeypatch.setattr(settings, "playground_enabled", False)
    with pytest.raises(PermissionError):
        build(code=code)
    monkeypatch.setattr(settings, "playground_enabled", True)
    assert type(build(code=code)).__name__ == "Mine"


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #
def _trend_bars(symbol="X", n=60, start=100.0, step=1.0):
    rows = []
    price = start
    for i in range(n):
        date = pd.Timestamp("2026-01-01") + pd.Timedelta(i, unit="D")
        rows.append({"date": date, "symbol": symbol, "open": price, "high": price + 0.5,
                     "low": price - 0.5, "close": price + step * 0.5, "volume": 1000})
        price += step
    return pd.DataFrame(rows)


def test_replay_sma_cross_trades_a_reversal():
    up = _trend_bars(n=40, start=100, step=1.0)
    down = _trend_bars(n=40, start=139.5, step=-1.0)
    down["date"] = pd.Timestamp("2026-02-10") + pd.to_timedelta(range(40), unit="D")
    bars = pd.concat([up, down], ignore_index=True)
    result = replay(build("sma_cross", {"fast": 5, "slow": 15, "size_pct": 0.5}), bars,
                    cash=100_000)
    m = result["metrics"]
    assert m["fills"] >= 2                       # bought the uptrend, sold the break
    assert m["bars"] == 80
    sides = [("buy" if f["qty"] > 0 else "sell") for f in result["fills"]]
    assert sides[0] == "buy" and "sell" in sides
    assert len(result["equity_curve"]) == 80
    assert m["final_equity"] == result["equity_curve"][-1][1]


def test_replay_buy_and_hold_matches_hand_arithmetic():
    bars = _trend_bars(n=10, start=100, step=1.0)
    result = replay(build("buy_and_hold", {"size_pct": 0.5}), bars, cash=100_000,
                    slippage_bps=0)
    (fill,) = result["fills"]
    # Order placed on bar 1 (close 100.5), fills at bar 2's open = 101.
    assert fill["price"] == pytest.approx(101.0)
    assert fill["qty"] == int(100_000 * 0.5 / 100.5)
    final_close = bars.iloc[-1]["close"]
    expected = 100_000 + fill["qty"] * (final_close - 101.0)
    assert result["metrics"]["final_equity"] == pytest.approx(expected, abs=0.01)


def test_replay_rejects_wrong_columns():
    with pytest.raises(ValueError):
        replay(build("buy_and_hold"), pd.DataFrame({"date": [], "close": []}))


# --------------------------------------------------------------------------- #
# Parity: the same strategy, the same prices, both feeds
# --------------------------------------------------------------------------- #
def test_replay_and_live_paths_produce_identical_fills():
    bars = _trend_bars(n=30, start=100, step=1.0)
    params = {"fast": 3, "slow": 8, "size_pct": 0.4}

    replayed = replay(build("sma_cross", params), bars, cash=50_000, slippage_bps=0)

    # The live path: drive the engine by hand exactly as a session would —
    # a tick, then the completed bar — using the same open/close sequence.
    eng = PaperEngine(build("sma_cross", params), ["X"], cash=50_000,
                      limits={"stale_seconds": 0}, slippage_bps=0)
    eng.replay_mode = True     # bars come from us, not the wall clock
    eng.start()
    for row in bars.itertuples(index=False):
        label = str(row.date.date())
        eng.process_tick({"symbol": "X", "price": float(row.open), "time": label})
        eng.last_tick["X"] = {"symbol": "X", "price": float(row.close), "time": label}
        eng.process_bar({"symbol": "X", "open": float(row.open), "high": float(row.high),
                         "low": float(row.low), "close": float(row.close),
                         "start": label, "end": label})
    eng.stop()

    live_fills = [(f["symbol"], f["qty"], f["price"]) for f in eng.oms.fills]
    replay_fills = [(f["symbol"], f["qty"], f["price"]) for f in replayed["fills"]]
    assert live_fills == replay_fills and live_fills   # same trades, and there were some


# --------------------------------------------------------------------------- #
# The live session on a fake hub
# --------------------------------------------------------------------------- #
class BuyFirstTick(Strategy):
    params = {"qty": 5}

    def on_tick(self, ctx, t):
        if not ctx.position(t["symbol"]):
            ctx.buy(t["symbol"], self.params["qty"])


def test_live_session_start_trade_kill(monkeypatch):
    monkeypatch.setattr(settings, "playground_enabled", True)

    async def scenario():
        src = FakeSource()
        hub = StreamHub("yahoo", src)
        hub_mod.register_hub("yahoo", hub)
        mgr = live_manager.__class__() if False else None  # noqa: F841 - keep module manager
        from backend.trading.manager import SessionManager

        sessions = SessionManager()
        session = await sessions.start(
            user_id=1, strategy_name=None, params={"qty": 5},
            code="class T(Strategy):\n"
                 "    params = {'qty': 5}\n"
                 "    def on_tick(self, ctx, t):\n"
                 "        if not ctx.position(t['symbol']): ctx.buy(t['symbol'], self.params['qty'])\n",
            symbols=["AAPL"], cash=10_000, limits={"stale_seconds": 0},
            bar_seconds=60, provider="yahoo")
        assert session.state == "running"
        src.emit("AAPL", 100.0)          # first tick: order placed, rests
        await asyncio.sleep(0.05)
        src.emit("AAPL", 101.0)          # second tick: fills at 101
        await asyncio.sleep(0.1)
        snap = session.status()
        assert snap["book"]["positions"] and snap["book"]["positions"][0]["qty"] == 5
        assert snap["fills"][-1]["price"] == pytest.approx(101.0 * 1.0002)

        # a second session for the same user is refused while running
        with pytest.raises(RuntimeError):
            await sessions.start(1, "buy_and_hold", {}, None, ["SPY"], 1_000, None, 60, "yahoo")

        result = await sessions.kill(1, flatten=True)
        assert result["status"]["state"] == "killed"
        assert result["flattened"] and result["flattened"][0]["qty"] == -5
        assert session.engine.risk.killed
        assert not session.engine.oms.positions["AAPL"]["qty"]
        await sessions.shutdown()
        await hub.shutdown()

    _run(scenario())
    hub_mod._HUBS.pop("yahoo", None)


# --------------------------------------------------------------------------- #
# The endpoints
# --------------------------------------------------------------------------- #
def test_endpoints_require_auth(client):
    assert client.get("/api/trading/strategies").status_code == 401
    assert client.post("/api/trading/replay", json={"symbols": "X"}).status_code == 401


def test_strategy_catalog(auth_client):
    body = auth_client.get("/api/trading/strategies").json()
    names = {s["name"] for s in body["strategies"]}
    assert {"buy_and_hold", "sma_cross", "tick_reversion"} <= names
    assert all("params" in s and "description" in s for s in body["strategies"])


def test_paper_status_when_none(auth_client):
    assert auth_client.get("/api/trading/paper").json()["session"] is None
    assert auth_client.post("/api/trading/paper/stop").status_code == 404


def test_replay_endpoint_runs_offline(auth_client, monkeypatch):
    from types import SimpleNamespace

    from backend.core import registry

    bars = _trend_bars(n=20, start=100, step=1.0)
    rows = [dict(date=str(r.date.date()), symbol=r.symbol, open=r.open, high=r.high,
                 low=r.low, close=r.close, volume=r.volume)
            for r in bars.itertuples(index=False)]
    monkeypatch.setattr(registry, "execute",
                        lambda path, **kw: SimpleNamespace(results=rows, provider="test"))
    body = auth_client.post("/api/trading/replay", json={
        "strategy": "buy_and_hold", "symbols": "X", "cash": 50_000,
        "slippage_bps": 0}).json()
    assert body["metrics"]["fills"] == 1
    assert body["provider"] == "test"
    assert body["equity_curve"]


def test_replay_endpoint_rejects_unknown_strategy(auth_client):
    r = auth_client.post("/api/trading/replay", json={"strategy": "nope", "symbols": "X"})
    assert r.status_code == 422
