"""The Alpaca paper executor (against a fake HTTP layer) and DuckDB tick analytics.

No sockets anywhere: the executor's ``_request`` is replaced with an
in-memory paper venue, and the tick store is written into a temp directory.
"""
from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from backend.config import settings
from backend.stream import tickdb
from backend.trading import PaperEngine, Strategy
from backend.trading.broker import (AlpacaPaperExecutor, InternalExecutor,
                                    build_executor)
from backend.trading.manager import LiveSession
from tests.test_stream import _run


# --------------------------------------------------------------------------- #
# A fake Alpaca paper venue
# --------------------------------------------------------------------------- #
class FakeVenue:
    """Answers the executor's HTTP calls like paper-api would."""

    def __init__(self, fill_price=101.0, reject=False):
        self.orders = {}
        self.calls = []
        self.fill_price = fill_price
        self.reject = reject
        self._n = 0

    def request(self, method, path, json_body=None):
        self.calls.append((method, path))
        if method == "POST" and path == "/v2/orders":
            if self.reject:
                raise RuntimeError("alpaca POST /v2/orders -> HTTP 403 insufficient buying power")
            self._n += 1
            oid = "brk-{}".format(self._n)
            self.orders[oid] = {"id": oid, "status": "accepted", **json_body}
            return self.orders[oid]
        if method == "GET" and path.startswith("/v2/orders/"):
            order = self.orders[path.rsplit("/", 1)[1]]
            # First poll: filled at the venue's price.
            order["status"] = "filled"
            order["filled_avg_price"] = str(self.fill_price)
            order["filled_qty"] = order["qty"]
            order["filled_at"] = "2026-08-19T15:00:00Z"
            return order
        if method == "GET" and path == "/v2/account":
            return {"equity": "100100.5", "cash": "50000", "buying_power": "200000"}
        if method == "GET" and path == "/v2/positions":
            return [{"symbol": "AAPL", "qty": "5", "avg_entry_price": "101",
                     "market_value": "505"}]
        if method == "DELETE" and path == "/v2/orders":
            for o in self.orders.values():
                if o["status"] == "accepted":
                    o["status"] = "canceled"
            return None
        raise AssertionError("unexpected call {} {}".format(method, path))


def make_executor(venue) -> AlpacaPaperExecutor:
    ex = AlpacaPaperExecutor("PKTEST", "secret")
    ex._request = venue.request
    ex._last_account = -1e9   # force the account snapshot on first poll
    return ex


# --------------------------------------------------------------------------- #
# The executor
# --------------------------------------------------------------------------- #
def test_paper_host_is_the_only_host_allowed():
    with pytest.raises(ValueError) as exc:
        AlpacaPaperExecutor("k", "s", base="https://api.alpaca.markets")
    assert "paper-api.alpaca.markets" in str(exc.value)
    with pytest.raises(ValueError):
        AlpacaPaperExecutor("", "", base="https://paper-api.alpaca.markets")  # no keys


def test_symbol_validation_refuses_non_stocks():
    ex = make_executor(FakeVenue())
    ex.validate_symbols(["AAPL", "BRK-B"])
    with pytest.raises(ValueError) as exc:
        ex.validate_symbols(["AAPL", "BTC-USD"])
    assert "BTC-USD" in str(exc.value)


def test_build_executor_selection(monkeypatch):
    assert isinstance(build_executor(None), InternalExecutor)
    assert isinstance(build_executor("internal"), InternalExecutor)
    monkeypatch.setattr(settings, "alpaca_api_key", "k")
    monkeypatch.setattr(settings, "alpaca_api_secret", "s")
    assert isinstance(build_executor("alpaca"), AlpacaPaperExecutor)
    monkeypatch.setattr(settings, "alpaca_api_key", None)
    with pytest.raises(ValueError):
        build_executor("alpaca")


class BuyFirstTick(Strategy):
    params = {"qty": 5}

    def on_start(self, ctx):
        self.fills = []

    def on_tick(self, ctx, t):
        if not ctx.position(t["symbol"]) and not ctx._engine.oms.orders:
            ctx.buy(t["symbol"], self.params["qty"])

    def on_fill(self, ctx, fill):
        self.fills.append(fill)


def test_broker_round_trip_books_the_venue_fill():
    venue = FakeVenue(fill_price=101.5)
    ex = make_executor(venue)
    strategy = BuyFirstTick()
    eng = PaperEngine(strategy, ["AAPL"], cash=10_000, limits={"stale_seconds": 0},
                      executor=ex)
    eng.start()
    eng.process_tick({"symbol": "AAPL", "price": 100.0, "time": "t1"})
    (order,) = eng.oms.orders
    assert order["state"] == "SUBMITTED"          # queued, not yet at the venue
    assert eng.oms.positions == {}

    fills = ex.flush_and_poll(eng.oms.open_orders())
    assert order["state"] == "ACKNOWLEDGED" and order["broker_id"] == "brk-1"
    # a later tick must NOT fill it locally — the venue owns execution now
    assert eng.process_tick({"symbol": "AAPL", "price": 99.0, "time": "t2"}) == []
    for o, price, qty, ts in fills:
        eng.apply_external_fill(o, price, qty, ts)
    assert order["state"] == "FILLED" and order["fill_price"] == 101.5
    assert eng.oms.positions["AAPL"]["qty"] == 5
    assert strategy.fills and strategy.fills[0]["price"] == 101.5
    assert ex.status()["account"]["equity"] == 100100.5
    assert ex.status()["broker_positions"][0]["symbol"] == "AAPL"


def test_broker_rejection_lands_on_the_order():
    venue = FakeVenue(reject=True)
    ex = make_executor(venue)
    eng = PaperEngine(BuyFirstTick(), ["AAPL"], cash=10_000,
                      limits={"stale_seconds": 0}, executor=ex)
    eng.start()
    eng.process_tick({"symbol": "AAPL", "price": 100.0, "time": "t1"})
    ex.flush_and_poll(eng.oms.open_orders())
    (order,) = eng.oms.orders
    assert order["state"] == "REJECTED"
    assert "buying power" in order["reason"]


def test_session_kill_flattens_through_the_broker(monkeypatch):
    monkeypatch.setattr(settings, "playground_enabled", True)

    async def scenario():
        venue = FakeVenue(fill_price=102.0)
        ex = make_executor(venue)
        eng = PaperEngine(BuyFirstTick(), ["AAPL"], cash=10_000,
                          limits={"stale_seconds": 0}, executor=ex)
        session = LiveSession(eng, provider=None)
        session.state = "running"
        eng.start()
        eng.process_tick({"symbol": "AAPL", "price": 100.0, "time": "t1"})
        await session._broker_sync(asyncio.get_running_loop())
        assert eng.oms.positions["AAPL"]["qty"] == 5

        result = await session.kill(flatten=True)
        assert ("DELETE", "/v2/orders") in venue.calls        # venue cancel first
        assert result["flattened"] and result["flattened"][0]["qty"] == -5
        assert eng.oms.positions["AAPL"]["qty"] == 0
        assert session.state == "killed"

    _run(scenario())


# --------------------------------------------------------------------------- #
# DuckDB over the tick store
# --------------------------------------------------------------------------- #
@pytest.fixture()
def seeded_store(tmp_path, monkeypatch):
    """A store with two symbols, two minutes of deterministic ticks."""
    monkeypatch.setattr(settings, "tick_store_dir", str(tmp_path / "ticks"))
    rows = []
    for i in range(120):  # one tick per second, 2026-08-19 14:30:00..14:31:59
        ts = "2026-08-19T14:{}:{:02d}Z".format(30 + i // 60, i % 60)
        rows.append({"symbol": "AAA", "price": 100.0 + i * 0.1, "time": ts,
                     "volume": 1000 + i, "provider": "yahoo", "kind": "trade"})
        rows.append({"symbol": "BBB", "price": 50.0 - i * 0.05, "time": ts,
                     "volume": None, "provider": "yahoo", "kind": "trade"})
    df = pd.DataFrame(rows)
    part_dir = tmp_path / "ticks" / "date=2026-08-19"
    part_dir.mkdir(parents=True)
    df.iloc[:120].to_parquet(part_dir / "yahoo-a.parquet", index=False)
    df.iloc[120:].to_parquet(part_dir / "yahoo-b.parquet", index=False)
    return df


def test_bars_from_ticks_ohlc_is_exact(seeded_store):
    bars = tickdb.bars("2026-08-19", symbols=["AAA"], bar_seconds=60)
    assert len(bars) == 2
    first, second = bars.iloc[0], bars.iloc[1]
    assert first["open"] == pytest.approx(100.0)
    assert first["close"] == pytest.approx(105.9)      # tick 59: 100 + 5.9
    assert first["high"] == pytest.approx(105.9) and first["low"] == pytest.approx(100.0)
    assert first["ticks"] == 60
    assert first["volume"] == 59                        # cumulative delta within the bar
    assert second["open"] == pytest.approx(106.0)
    assert str(first["date"]) < str(second["date"])


def test_bars_span_part_files_and_symbols(seeded_store):
    bars = tickdb.bars("2026-08-19", bar_seconds=120)
    assert set(bars["symbol"]) == {"AAA", "BBB"}
    aaa = bars[bars.symbol == "AAA"].iloc[0]
    assert aaa["ticks"] == 120                          # both parquet parts scanned
    bbb = bars[bars.symbol == "BBB"].iloc[0]
    assert bbb["open"] == pytest.approx(50.0) and bbb["close"] == pytest.approx(50.0 - 119 * 0.05)


def test_day_stats(seeded_store):
    stats = tickdb.day_stats("2026-08-19")
    assert len(stats) == 2
    aaa = stats[stats.symbol == "AAA"].iloc[0]
    assert aaa["prints"] == 120
    assert aaa["first_price"] == pytest.approx(100.0)
    assert aaa["tick_ret_stddev"] is not None


def test_tickdb_validates_inputs(seeded_store):
    with pytest.raises(ValueError):
        tickdb.bars("19-08-2026")
    with pytest.raises(ValueError):
        tickdb.bars("2026-08-19", bar_seconds=0)


def test_empty_store_reports_helpfully(tmp_path, monkeypatch):
    from backend.core.errors import EmptyDataError

    monkeypatch.setattr(settings, "tick_store_dir", str(tmp_path / "nothing"))
    with pytest.raises(EmptyDataError) as exc:
        tickdb.connect()
    assert "recorder" in str(exc.value)


def test_commands_are_registered():
    from backend.core.registry import REGISTRY

    assert "/equity/price/bars_from_ticks" in REGISTRY
    assert "/equity/price/tick_stats" in REGISTRY


def test_replay_from_recorded_ticks_endpoint(auth_client, seeded_store, monkeypatch):
    """The loop closes: record -> build bars -> run the strategy on your own tape."""
    monkeypatch.setattr(settings, "playground_enabled", True)
    code = ("class T(Strategy):\n"
            "    def on_bar(self, ctx, bar):\n"
            "        if bar['symbol'] == 'AAA' and not ctx.position('AAA'):\n"
            "            ctx.buy('AAA', 10)\n")
    body = auth_client.post("/api/trading/replay", json={
        "code": code, "symbols": "AAA,BBB", "source": "ticks", "bar_seconds": 30,
        "cash": 10_000, "slippage_bps": 0}).json()
    assert body["provider"] == "tick_store (30s bars)"
    assert body["metrics"]["bars"] == 8                # 2 symbols x 4 x 30s bars
    (fill,) = body["fills"]
    # Bought on the first AAA bar, filled at the second bar's open: tick 30.
    assert fill["price"] == pytest.approx(103.0)
    assert body["metrics"]["final_equity"] > 10_000    # AAA trended up all window


def test_replay_ticks_source_with_empty_store(auth_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "tick_store_dir", str(tmp_path / "none"))
    r = auth_client.post("/api/trading/replay", json={
        "strategy": "buy_and_hold", "symbols": "AAA", "source": "ticks"})
    assert r.status_code == 404
    assert "recorder" in r.json()["detail"]
