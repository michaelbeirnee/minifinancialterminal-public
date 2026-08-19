"""The live-quote streaming layer.

Offline. The hub is exercised with a fake source, the wire decoders with
captured payloads, and the SSE endpoint through the TestClient. Nothing here
opens a socket to Yahoo or Alpaca; ``test_yahoo_streamer_live`` at the bottom
does, and is skipped unless MFT_TEST_NETWORK=1.
"""
from __future__ import annotations

import asyncio
import json
import os
import pytest

from backend.config import settings
from backend.stream import hub as hub_mod
from backend.stream.hub import StreamHub, normalise_symbols
from backend.stream.sources import Source, alpaca_symbol, alpaca_tick, yahoo_tick


# --------------------------------------------------------------------------- #
# A source the tests can drive by hand
# --------------------------------------------------------------------------- #
class FakeSource(Source):
    name = "fake"

    def __init__(self) -> None:
        super().__init__()
        self.deltas = []
        self.started = 0
        self.stopped = 0

    async def run(self) -> None:
        self.started += 1
        self.connected = True
        try:
            while not self._stopping:
                await asyncio.sleep(0.01)
        finally:
            self.connected = False

    async def _send_delta(self, added, removed) -> None:
        self.deltas.append((set(added), set(removed)))

    async def stop(self) -> None:
        self.stopped += 1
        await super().stop()

    def emit(self, symbol, price, **extra):
        self._publish({"symbol": symbol, "price": price, **extra})


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------------- #
# Symbol handling
# --------------------------------------------------------------------------- #
def test_normalise_symbols_accepts_the_terminal_vocabulary():
    assert normalise_symbols(["aapl, brk-b", "^GSPC", "BTC-USD", "ES=F", "DX-Y.NYB", "AAPL"]) == [
        "AAPL", "BRK-B", "^GSPC", "BTC-USD", "ES=F", "DX-Y.NYB"]


def test_normalise_symbols_rejects_junk_and_floods():
    with pytest.raises(ValueError):
        normalise_symbols(["AAPL;DROP"])
    with pytest.raises(ValueError):
        normalise_symbols(["S{}".format(i) for i in range(hub_mod.MAX_SYMBOLS + 1)])


def test_alpaca_symbol_translation():
    assert alpaca_symbol("AAPL") == "AAPL"
    assert alpaca_symbol("BRK-B") == "BRK.B"
    assert alpaca_symbol("brk.b") == "BRK.B"
    for non_stock in ("^GSPC", "ES=F", "EURUSD=X", "BTC-USD", "DX-Y.NYB", "TOOLONGNAME"):
        assert alpaca_symbol(non_stock) is None, non_stock


# --------------------------------------------------------------------------- #
# Wire decoders
# --------------------------------------------------------------------------- #
def test_yahoo_tick_normalises_units_and_hours():
    raw = {"id": "aapl", "price": 189.12, "time": "1787107539000", "exchange": "NMS",
           "market_hours": 1, "change_percent": 0.54, "change": 1.02, "day_volume": "41233100",
           "day_high": 190.0, "day_low": 187.5, "previous_close": 188.1, "bid": 0, "ask": 0}
    tick = yahoo_tick(raw)
    assert tick["symbol"] == "AAPL"
    assert tick["price"] == 189.12
    assert tick["change_percent"] == pytest.approx(0.0054)  # percent -> fraction
    assert tick["volume"] == 41233100
    assert tick["market_hours"] == "regular"
    assert tick["time"] == "2026-08-19T02:45:39Z"
    assert "bid" not in tick and "ask" not in tick  # zeros are "not populated"
    assert tick["provider"] == "yahoo"


def test_alpaca_tick_maps_trades_and_quotes_back_to_requested_symbols():
    back = {"BRK.B": "BRK-B"}
    trade = alpaca_tick({"T": "t", "S": "BRK.B", "p": 412.5, "s": 100, "x": "V",
                         "t": "2026-08-18T14:30:01.123456789Z"}, back)
    assert trade == {"symbol": "BRK-B", "price": 412.5, "size": 100, "exchange": "V",
                     "time": "2026-08-18T14:30:01.123456Z", "kind": "trade", "provider": "alpaca"}
    quote = alpaca_tick({"T": "q", "S": "BRK.B", "bp": 412.4, "bs": 2, "ap": 412.6, "as": 3,
                         "t": "2026-08-18T14:30:01Z"}, back)
    assert quote["bid"] == 412.4 and quote["ask"] == 412.6 and quote["kind"] == "quote"
    assert alpaca_tick({"T": "t", "S": "MSFT", "p": 1}, back) is None  # not requested
    assert alpaca_tick({"T": "subscription", "trades": ["BRK.B"]}, back) is None


# --------------------------------------------------------------------------- #
# The hub
# --------------------------------------------------------------------------- #
def test_hub_refcounts_symbols_and_fans_out():
    async def scenario():
        src = FakeSource()
        hub = StreamHub("fake", src)
        a = await hub.subscribe(["AAPL", "MSFT"])
        b = await hub.subscribe(["MSFT"])
        await asyncio.sleep(0.02)  # let the run task start
        assert src.started == 1 and src.connected
        assert hub.symbols() == ["AAPL", "MSFT"]

        src.emit("MSFT", 400.0)
        src.emit("AAPL", 189.0)
        src.emit("MSFT", 401.0)  # coalesces over the first MSFT tick
        got_a = await a.drain(timeout=0.5)
        got_b = await b.drain(timeout=0.5)
        assert {t["symbol"]: t["price"] for t in got_a} == {"AAPL": 189.0, "MSFT": 401.0}
        assert [t["symbol"] for t in got_b] == ["MSFT"] and got_b[0]["price"] == 401.0
        assert got_b[0]["provider"] == "fake"

        # A late joiner is handed the latest known tick immediately.
        c = await hub.subscribe(["AAPL"])
        assert [t["price"] for t in await c.drain(timeout=0.1)] == [189.0]

        # Silence yields an empty drain (keep-alive), not an error.
        assert await a.drain(timeout=0.05) == []

        await a.close()
        assert hub.symbols() == ["AAPL", "MSFT"]  # b and c still hold them
        await b.close()
        await c.close()
        assert hub.symbols() == []
        assert src.deltas[-1] == (set(), {"AAPL", "MSFT"}) or src.deltas[-1][1]
        await hub.shutdown()
        assert src.stopped >= 1 and not src.connected

    _run(scenario())


def test_hub_carries_forward_price_into_quote_only_ticks():
    async def scenario():
        src = FakeSource()
        hub = StreamHub("fake", src)
        sub = await hub.subscribe(["AAPL"])
        src.emit("AAPL", 189.0, volume=1000)
        await sub.drain(timeout=0.2)
        src._publish({"symbol": "AAPL", "bid": 188.9, "ask": 189.1, "price": None})
        (tick,) = await sub.drain(timeout=0.2)
        assert tick["price"] == 189.0 and tick["bid"] == 188.9 and tick["volume"] == 1000
        await hub.shutdown()

    _run(scenario())


def test_hub_ignores_ticks_nobody_asked_for():
    async def scenario():
        src = FakeSource()
        hub = StreamHub("fake", src)
        sub = await hub.subscribe(["AAPL"])
        src.emit("TSLA", 1.0)
        assert await sub.drain(timeout=0.05) == []
        assert "TSLA" not in hub.latest
        await hub.shutdown()

    _run(scenario())


# --------------------------------------------------------------------------- #
# Provider selection
# --------------------------------------------------------------------------- #
def test_alpaca_requires_keys(monkeypatch):
    monkeypatch.setattr(settings, "alpaca_api_key", None)
    monkeypatch.setattr(settings, "alpaca_api_secret", None)
    hub_mod._HUBS.pop("alpaca", None)
    from backend.core.errors import UnknownProviderError

    with pytest.raises(UnknownProviderError) as exc:
        hub_mod.get_hub("alpaca")
    assert "MFT_ALPACA_API_KEY" in str(exc.value)
    assert hub_mod.available_providers()["alpaca"]["available"] is False
    # A preference for an unconfigured source is ignored, not honoured.
    monkeypatch.setattr(settings, "stream_default_provider", "alpaca")
    assert hub_mod.default_provider() == "yahoo"


def test_alpaca_hub_builds_once_keys_exist(monkeypatch):
    monkeypatch.setattr(settings, "alpaca_api_key", "k")
    monkeypatch.setattr(settings, "alpaca_api_secret", "s")
    hub_mod._HUBS.pop("alpaca", None)
    hub = hub_mod.get_hub("alpaca")
    assert hub.provider == "alpaca" and hub.source.name == "alpaca"
    assert hub.source.status()["feed"] == settings.alpaca_feed
    monkeypatch.setattr(settings, "stream_default_provider", "alpaca")
    assert hub_mod.default_provider() == "alpaca"
    hub_mod._HUBS.pop("alpaca", None)


def test_unknown_stream_provider():
    from backend.core.errors import UnknownProviderError

    with pytest.raises(UnknownProviderError):
        hub_mod.get_hub("bloomberg")


# --------------------------------------------------------------------------- #
# The SSE endpoint
# --------------------------------------------------------------------------- #
def test_stream_endpoints_need_a_token(client):
    assert client.get("/api/stream/quotes?symbols=AAPL").status_code == 401
    assert client.get("/api/stream/status").status_code == 401
    assert client.get("/api/stream/snapshot?symbols=AAPL").status_code == 401


def test_stream_status_lists_both_providers(auth_client):
    body = auth_client.get("/api/stream/status").json()
    assert set(body["providers"]) == {"yahoo", "alpaca"}
    assert body["providers"]["yahoo"]["available"] is True
    assert body["default_provider"] in ("yahoo", "alpaca")


def test_stream_quotes_rejects_bad_symbols(auth_client):
    assert auth_client.get("/api/stream/quotes?symbols=AAPL;x").status_code == 422
    assert auth_client.get("/api/stream/quotes?symbols=AAPL&provider=nope").status_code == 400


def test_stream_quotes_sse_frames():
    """hello -> status -> ticks, from the router's generator off a fake source.

    Driven directly rather than through the TestClient: an SSE body never
    ends on its own, and the test client has no way to hang up on it.
    """
    from backend.routers import stream as stream_router

    async def scenario():
        src = FakeSource()
        hub = StreamHub("yahoo", src)
        hub_mod.register_hub("yahoo", hub)
        try:
            res = await stream_router.stream_quotes(symbols="AAPL,SPY", provider=None, _=None)
            assert res.media_type == "text/event-stream"
            assert res.headers["x-accel-buffering"] == "no"
            body = res.body_iterator
            frames = []
            first = await body.__anext__()
            assert first.startswith("data: ")
            frames.append(json.loads(first[6:]))
            assert hub.symbols() == ["AAPL", "SPY"]
            src.emit("AAPL", 189.5, change_percent=0.01)
            src.emit("SPY", 552.25)
            for _ in range(6):
                chunk = await asyncio.wait_for(body.__anext__(), timeout=2)
                if chunk.startswith("data: "):
                    frames.append(json.loads(chunk[6:]))
                if any(f["type"] == "ticks" for f in frames):
                    break
            await body.aclose()  # the client hangs up -> subscription released
            kinds = [f["type"] for f in frames]
            assert kinds[0] == "hello"
            assert frames[0]["provider"] == "yahoo" and frames[0]["symbols"] == ["AAPL", "SPY"]
            assert "price" in frames[0]["fields"]
            assert "status" in kinds
            ticks = next(f for f in frames if f["type"] == "ticks")["ticks"]
            assert {t["symbol"]: t["price"] for t in ticks} == {"AAPL": 189.5, "SPY": 552.25}
            assert hub.symbols() == []
        finally:
            await hub.shutdown()
            hub_mod._HUBS.pop("yahoo", None)

    _run(scenario())


def test_stream_quotes_keepalive_when_silent(monkeypatch):
    from backend.routers import stream as stream_router

    monkeypatch.setattr(stream_router, "KEEPALIVE_SECONDS", 0.05)

    async def scenario():
        src = FakeSource()
        hub = StreamHub("yahoo", src)
        hub_mod.register_hub("yahoo", hub)
        try:
            res = await stream_router.stream_quotes(symbols="AAPL", provider=None, _=None)
            body = res.body_iterator
            chunks = [await body.__anext__() for _ in range(3)]
            await body.aclose()
            assert ": ping" in "".join(chunks)
        finally:
            await hub.shutdown()
            hub_mod._HUBS.pop("yahoo", None)

    _run(scenario())


def test_stream_snapshot_returns_latest_known(auth_client):
    src = FakeSource()
    hub = StreamHub("yahoo", src)
    hub_mod.register_hub("yahoo", hub)
    try:
        hub._refs["AAPL"] = 1  # pretend someone is streaming it
        src.emit("AAPL", 190.0)
        body = auth_client.get("/api/stream/snapshot?symbols=AAPL,MSFT").json()
        assert body["count"] == 1 and body["results"][0]["price"] == 190.0
    finally:
        hub_mod._HUBS.pop("yahoo", None)


# --------------------------------------------------------------------------- #
# The command form
# --------------------------------------------------------------------------- #
def test_live_command_is_registered_in_every_interface():
    from backend.core.registry import REGISTRY

    spec = REGISTRY["/equity/price/live"]
    assert spec.providers == ("yahoo", "alpaca")
    assert {p["name"] for p in spec.parameters} >= {"symbol", "wait", "provider"}


def test_live_command_alpaca_without_keys_names_the_alternative(monkeypatch):
    from backend.core.errors import MissingCredentialError
    from backend.extensions.live import price_live

    monkeypatch.setattr(settings, "alpaca_api_key", None)
    with pytest.raises(MissingCredentialError) as exc:
        price_live(symbol="AAPL", provider="alpaca")
    assert "provider=yahoo" in str(exc.value)


def test_live_command_falls_back_to_quote_when_nothing_prints(monkeypatch):
    from backend.extensions import live

    monkeypatch.setattr(live, "_sample_yahoo", lambda symbols, wait: {})
    monkeypatch.setattr(live.yahoo, "quote", lambda s: {
        "symbol": s, "last_price": 100.0, "change": 1.0, "change_percent": 0.01, "volume": 5})
    res = live.price_live(symbol="AAPL,MSFT", wait=1)
    assert list(res.data["symbol"]) == ["AAPL", "MSFT"]
    assert set(res.data["source"]) == {"quote"}
    assert any("last quote" in w for w in res.warnings)


def test_live_command_prefers_stream_rows(monkeypatch):
    from backend.extensions import live

    monkeypatch.setattr(live, "_sample_yahoo", lambda symbols, wait: {
        "AAPL": {"symbol": "AAPL", "price": 189.0, "change_percent": 0.005, "provider": "yahoo"}})
    monkeypatch.setattr(live.yahoo, "quote", lambda s: {"symbol": s, "last_price": 1.0})
    res = live.price_live(symbol="AAPL,MSFT")
    by = res.data.set_index("symbol")
    assert by.loc["AAPL", "source"] == "stream" and by.loc["MSFT", "source"] == "quote"


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not os.environ.get("MFT_TEST_NETWORK"), reason="opens a socket to Yahoo")
def test_yahoo_streamer_live():
    """FX and crypto print around the clock, so a short sample always sees something."""
    from backend.extensions.live import _sample_yahoo

    got = _sample_yahoo(["EURUSD=X", "BTC-USD"], wait=8)
    assert got, "no ticks from Yahoo's streamer in 8s"
    tick = next(iter(got.values()))
    assert tick["price"] and tick["time"]
