import numpy as np
import pandas as pd

from backend.data.provider import get_price_panel
from backend.factors.models import analyze_universe, build_factors, factor_regression
from backend.reports.generator import compute_metrics


def test_build_factors_columns():
    panel = get_price_panel(["AAPL", "MSFT", "NVDA", "AMZN", "SPY"], "2022-01-01", "2024-01-01")
    factors = build_factors(panel)
    assert set(factors.columns) == {"MKT", "MOM", "LOWVOL"}
    assert len(factors) > 100


def test_factor_regression_recovers_beta():
    """A synthetic asset built as 1.5*MKT should regress to beta ~1.5."""
    idx = pd.bdate_range("2022-01-01", periods=400)
    rng = np.random.default_rng(0)
    mkt = pd.Series(rng.normal(0.0003, 0.01, len(idx)), index=idx)
    mom = pd.Series(rng.normal(0, 0.008, len(idx)), index=idx)
    factors = pd.DataFrame({"MKT": mkt, "MOM": mom})
    asset = 1.5 * mkt + 0.0001 + rng.normal(0, 0.001, len(idx))
    asset.index = idx

    res = factor_regression(asset, factors)
    assert abs(res["betas"]["MKT"] - 1.5) < 0.1
    assert res["r_squared"] > 0.8


def test_analyze_universe_structure():
    panel = get_price_panel(["AAPL", "MSFT", "SPY"], "2022-01-01", "2024-01-01")
    out = analyze_universe(panel)
    assert "factors" in out and "assets" in out
    for sym in panel.columns:
        assert sym in out["assets"]


def test_compute_metrics_basic():
    eq = pd.Series(np.linspace(100, 200, 252), index=pd.bdate_range("2022-01-01", periods=252))
    m = compute_metrics(eq)
    assert m["total_return"] > 0
    assert m["max_drawdown"] <= 0
    assert "sharpe" in m


def test_factors_endpoint(auth_client):
    r = auth_client.post(
        "/api/factors/analyze",
        json={"symbols": ["AAPL", "MSFT", "NVDA", "SPY"], "start": "2022-01-01", "end": "2024-01-01"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "factors" in body
    assert "AAPL" in body["assets"]
