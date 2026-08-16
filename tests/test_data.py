import pandas as pd

from backend.data.provider import get_history, get_price_panel, latest_quote


def test_history_shape_and_source():
    a = get_history("AAPL", "2022-01-01", "2023-01-01")
    assert isinstance(a, pd.DataFrame)
    assert list(a.columns) == ["open", "high", "low", "close", "volume"]
    assert a.attrs.get("source") == "yfinance"
    assert len(a) > 0
    # high >= low for every bar
    assert (a["high"] >= a["low"]).all()
    # Second call is served from cache and matches the first.
    b = get_history("AAPL", "2022-01-01", "2023-01-01")
    assert a["close"].equals(b["close"])


def test_price_panel_alignment():
    panel = get_price_panel(["AAPL", "MSFT", "SPY"], "2022-01-01", "2023-01-01")
    assert list(panel.columns) == ["AAPL", "MSFT", "SPY"]
    assert not panel.isna().any().any()


def test_latest_quote():
    q = latest_quote("MSFT")
    assert q["symbol"] == "MSFT"
    assert q["price"] > 0
    assert "change_pct" in q


def test_history_endpoint(auth_client):
    r = auth_client.get("/api/data/history/NVDA?start=2022-01-01&end=2023-01-01")
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "NVDA"
    assert body["rows"] > 0
    assert body["source"] == "yfinance"


def test_quotes_endpoint(auth_client):
    r = auth_client.get("/api/data/quotes?symbols=AAPL,MSFT,SPY")
    assert r.status_code == 200
    assert len(r.json()["quotes"]) == 3
