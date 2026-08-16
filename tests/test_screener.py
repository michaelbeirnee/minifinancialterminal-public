"""Screener extension: synthetic-data unit tests plus live end-to-end runs."""
import numpy as np
import pandas as pd
import pytest

from backend.core.registry import REGISTRY
from backend.extensions.screener import (
    TIMEFRAMES,
    UNIVERSES,
    _apply_filters,
    _bounded,
    _capm,
    _norm_sym,
    _rsi,
    _side_of,
    _sorted_rows,
    screener_run,
)


def _row(**over):
    base = {"symbol": "AAA", "name": "Alpha Co", "sector": "Tech", "last_price": 100.0,
            "market_cap": 50e9, "one_day": 0.01, "one_month": 0.05, "ytd": 0.10,
            "volatility": 0.30, "beta": 1.1, "alpha": 0.04,
            "ma50_dist": 0.02, "ma200_dist": 0.08, "high52_dist": -0.05,
            "low52_dist": 0.40, "rsi14": 55.0}
    base.update(over)
    return base


def test_commands_registered():
    assert "/screener/run" in REGISTRY
    assert "/screener/indexes" in REGISTRY


def test_norm_sym_joins_source_spellings():
    assert _norm_sym("brk.b") == "BRK-B"
    assert _norm_sym("BRK/B ") == "BRK-B"
    assert _norm_sym("AAPL") == "AAPL"


def test_bounded_unknown_fails_only_bounded_filters():
    assert _bounded(None, None, None)
    assert not _bounded(None, 1.0, None)
    assert _bounded(5.0, 1.0, 10.0)
    assert not _bounded(0.5, 1.0, 10.0)


def test_side_of_unknown_fails_only_when_required():
    assert _side_of(None, None)
    assert not _side_of(None, True)
    assert _side_of(0.05, True) and not _side_of(0.05, False)
    assert _side_of(-0.05, False) and not _side_of(-0.05, True)


def test_filter_direction_and_min_move():
    rows = [_row(symbol="UP", one_month=0.08), _row(symbol="DN", one_month=-0.08),
            _row(symbol="FLAT", one_month=0.001), _row(symbol="NA", one_month=None)]
    up = _apply_filters(rows, "one_month", "up", min_move=5.0)
    assert [r["symbol"] for r in up] == ["UP"]
    down = _apply_filters(rows, "one_month", "down")
    assert [r["symbol"] for r in down] == ["DN"]
    any_dir = _apply_filters(rows, "one_month", "any")
    assert len(any_dir) == 4  # unknown moves survive an unbounded screen


def test_filter_units_are_billions_and_percent():
    rows = [_row(symbol="BIG", market_cap=200e9, volatility=0.50, alpha=0.12),
            _row(symbol="SMALL", market_cap=2e9, volatility=0.20, alpha=-0.05)]
    got = _apply_filters(rows, "one_month", "any", mcap=(100.0, None),
                         vol=(40.0, None), alpha=(5.0, None))
    assert [r["symbol"] for r in got] == ["BIG"]


def test_filter_sector_and_trend_and_rsi():
    rows = [_row(symbol="T1", sector="Tech", ma200_dist=0.10, rsi14=75.0),
            _row(symbol="T2", sector="Tech", ma200_dist=-0.10, rsi14=25.0),
            _row(symbol="H1", sector="Health Care", ma200_dist=0.05, rsi14=50.0)]
    assert [r["symbol"] for r in _apply_filters(rows, "one_month", "any", sector="tech")] == ["T1", "T2"]
    assert [r["symbol"] for r in _apply_filters(rows, "one_month", "any", above_ma200=True)] == ["T1", "H1"]
    assert [r["symbol"] for r in _apply_filters(rows, "one_month", "any", above_ma200=False)] == ["T2"]
    assert [r["symbol"] for r in _apply_filters(rows, "one_month", "any", rsi=(None, 30.0))] == ["T2"]
    assert [r["symbol"] for r in _apply_filters(rows, "one_month", "any", rsi=(70.0, None))] == ["T1"]


def test_sort_puts_unknown_last_both_directions():
    rows = [_row(symbol="A", beta=0.5), _row(symbol="B", beta=None), _row(symbol="C", beta=1.5)]
    desc = _sorted_rows(rows, "beta", ascending=False)
    assert [r["symbol"] for r in desc] == ["C", "A", "B"]
    asc = _sorted_rows(rows, "beta", ascending=True)
    assert [r["symbol"] for r in asc] == ["A", "C", "B"]


def test_capm_recovers_known_beta():
    idx = pd.bdate_range("2024-01-01", periods=300)
    rng = np.random.default_rng(1)
    bench = pd.Series(rng.normal(0.0004, 0.01, len(idx)), index=idx)
    asset = 1.4 * bench + 0.0002 + rng.normal(0, 0.002, len(idx))
    beta, alpha = _capm(asset, bench)
    assert abs(beta - 1.4) < 0.1
    assert alpha is not None and abs(alpha - 0.0002 * 252) < 0.15


def test_capm_thin_data_returns_none():
    idx = pd.bdate_range("2024-01-01", periods=10)
    s = pd.Series(0.01, index=idx)
    assert _capm(s, s) == (None, None)


def test_rsi_extremes_and_thin_data():
    idx = pd.bdate_range("2024-01-01", periods=60)
    rising = pd.Series(np.linspace(100, 160, len(idx)), index=idx)
    falling = pd.Series(np.linspace(160, 100, len(idx)), index=idx)
    assert _rsi(rising) == 100.0
    assert _rsi(falling) < 1.0
    assert _rsi(rising.head(10)) is None
    # Alternating gains/losses of equal size should sit near the midline.
    flat = pd.Series(100 + np.tile([0, 1], 30)[: len(idx)], index=idx, dtype=float)
    assert 40.0 < _rsi(flat) < 60.0


def test_run_rejects_bad_inputs():
    with pytest.raises(ValueError):
        screener_run(index="ftse100")
    with pytest.raises(ValueError):
        screener_run(timeframe="two_weeks")
    with pytest.raises(ValueError):
        screener_run(direction="sideways")
    with pytest.raises(ValueError):
        screener_run(sort="pe_ratio")


def test_universes_use_known_timeframes():
    assert set(UNIVERSES) == {"sp500", "nasdaq100", "dowjones", "sp400", "sp600", "russell1000"}
    assert "ytd" in TIMEFRAMES


def test_nasdaq100_membership_live():
    """Wikipedia dropped the Nasdaq-100 components table in 2026; the
    slickcharts fallback must keep the universe available."""
    from backend.providers import markets

    df = markets.index_constituents("nasdaq100")
    assert len(df) > 90
    assert "symbol" in df.columns and "name" in df.columns


def test_run_endpoint_live(auth_client):
    """End-to-end on the smallest universe (Dow 30 + one benchmark download)."""
    r = auth_client.get(
        "/api/v1/screener/run",
        params={"index": "dowjones", "timeframe": "one_month", "sort": "one_month", "limit": 5},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert 0 < len(body["results"]) <= 5
    row = body["results"][0]
    for field in ("symbol", "last_price", "one_month", "volatility", "beta", "alpha",
                  "ma50_dist", "ma200_dist", "high52_dist", "low52_dist", "rsi14"):
        assert field in row
    assert body["extra"]["benchmark"] == "DIA"
    assert body["extra"]["universe_size"] >= 25
    assert body["extra"]["sectors"], "distinct sector list should be populated"
    # Sorted by the one-month move, biggest first, unknowns last.
    moves = [x["one_month"] for x in body["results"] if x["one_month"] is not None]
    assert moves == sorted(moves, reverse=True)


def test_run_custom_symbols_live(auth_client):
    """A custom list (the watchlist path) benchmarks against SPY."""
    r = auth_client.get(
        "/api/v1/screener/run",
        params={"symbols": "AAPL,MSFT,NVDA", "timeframe": "one_month", "sort": "symbol",
                "ascending": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["symbol"] for x in body["results"]] == ["AAPL", "MSFT", "NVDA"]
    assert body["extra"]["benchmark"] == "SPY"
    assert body["extra"]["index"] == "custom"
