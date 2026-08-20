"""The tick recorder: buffer, flush, store layout, read-back, endpoints.

Offline: the recorder subscribes to a hub backed by the FakeSource from the
stream tests, and the store lives in a temp directory patched into settings.
"""
from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from backend.config import settings
from backend.stream import hub as hub_mod
from backend.stream import recorder as rec
from backend.stream.hub import StreamHub
from tests.test_stream import FakeSource, _run


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "tick_store_dir", str(tmp_path / "ticks"))
    yield tmp_path / "ticks"


@pytest.fixture()
def fake_hub():
    src = FakeSource()
    hub = StreamHub("yahoo", src)
    hub_mod.register_hub("yahoo", hub)
    yield src, hub
    _run(hub.shutdown())
    hub_mod._HUBS.pop("yahoo", None)


def test_record_flush_and_read_back(store, fake_hub):
    src, hub = fake_hub

    async def scenario():
        recorder = await rec.start_recording(["AAPL", "BTC-USD"], provider="yahoo")
        assert recorder.running
        assert hub.symbols() == ["AAPL", "BTC-USD"]   # the recorder holds the subscription
        src.emit("AAPL", 100.5, change_percent=0.01, volume=1000, time="2026-08-19T14:30:00Z")
        src.emit("BTC-USD", 68000.0, time="2026-08-19T14:30:01Z")
        src.emit("MSFT", 1.0)                          # not subscribed -> hub drops it
        await asyncio.sleep(0.1)                       # let the drain loop pick them up
        final = await rec.stop_recording()             # stop flushes the buffer
        assert final["rows_written"] == 2
        assert final["files_written"] >= 1
        assert hub.symbols() == []                     # subscription released

    _run(scenario())

    parts = list(store.glob("date=*/yahoo-*.parquet"))
    assert parts, "no part files written"
    df = rec.read_ticks("2000-01-01", "2100-01-01")
    assert len(df) == 2
    assert set(df["symbol"]) == {"AAPL", "BTC-USD"}
    row = df[df.symbol == "AAPL"].iloc[0]
    assert row["price"] == 100.5 and row["volume"] == 1000
    assert row["recorded_at"]  # stamped on write

    # symbol and date filters
    only = rec.read_ticks("2000-01-01", "2100-01-01", symbols=["AAPL"])
    assert list(only["symbol"]) == ["AAPL"]
    assert rec.read_ticks("1999-01-01", "1999-12-31").empty


def test_flush_produces_stable_schema(store, fake_hub):
    src, hub = fake_hub

    async def scenario():
        await rec.start_recording(["AAPL"], provider="yahoo")
        src.emit("AAPL", 1.0)                    # bare tick: most columns absent
        await asyncio.sleep(0.05)
        await rec.stop_recording()

    _run(scenario())
    (part,) = list(store.glob("date=*/*.parquet"))
    df = pd.read_parquet(part)
    assert list(df.columns) == rec.COLUMNS       # fixed order, even when sparse
    assert pd.isna(df["bid"]).all()


def test_read_ticks_validates_dates(store):
    with pytest.raises(ValueError):
        rec.read_ticks("19-08-2026")


def test_store_overview_counts_files(store, fake_hub):
    src, hub = fake_hub

    async def scenario():
        await rec.start_recording(["AAPL"], provider="yahoo")
        src.emit("AAPL", 1.0)
        await asyncio.sleep(0.05)
        await rec.stop_recording()

    _run(scenario())
    overview = rec.store_overview()
    assert overview["total_files"] == 1
    assert overview["dates"][0]["files"] == 1
    assert overview["total_bytes"] > 0


def test_ticks_command_errors_helpfully_when_empty(store):
    from backend.core.errors import EmptyDataError
    from backend.extensions.live import price_ticks

    with pytest.raises(EmptyDataError) as exc:
        price_ticks(start_date="2000-01-02")
    assert "MFT_RECORD_SYMBOLS" in str(exc.value)


def test_recorder_endpoints(auth_client, store, fake_hub):
    src, hub = fake_hub
    body = auth_client.get("/api/stream/recorder").json()
    assert body["recorder"] is None and body["store"]["total_files"] == 0

    started = auth_client.post("/api/stream/recorder/start?symbols=AAPL,SPY").json()
    assert started["running"] is True and started["symbols"] == ["AAPL", "SPY"]

    src.emit("SPY", 550.0)
    stopped = auth_client.post("/api/stream/recorder/stop").json()
    assert stopped["running"] is False
    assert stopped["rows_written"] == 1

    again = auth_client.post("/api/stream/recorder/stop").json()
    assert again == {"running": False, "note": "The recorder was not running."}

    assert auth_client.post("/api/stream/recorder/start?symbols=bad;sym").status_code == 422


def test_recorder_endpoints_need_auth(client):
    assert client.get("/api/stream/recorder").status_code == 401
    assert client.post("/api/stream/recorder/start?symbols=AAPL").status_code == 401
