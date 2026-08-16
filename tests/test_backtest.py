import numpy as np
import pandas as pd
import pytest

from backend.backtest.analysis import (
    benchmark_attribution,
    cost_sensitivity,
    monte_carlo,
    sweep,
    walk_forward,
)
from backend.backtest.engine import (
    CostModel,
    EventDrivenEngine,
    VectorizedBacktester,
    run_backtest,
)
from backend.backtest.sizing import apply_stop_loss, apply_vol_target
from backend.backtest.strategies import REGISTRY, get_strategy
from backend.data.provider import get_price_panel


@pytest.fixture()
def panel():
    return get_price_panel(["AAPL", "MSFT", "NVDA", "SPY"], "2022-01-01", "2024-01-01")


def test_all_strategies_produce_valid_weights(panel):
    for name in REGISTRY:
        w = get_strategy(name)(panel, {})
        assert w.shape[0] == panel.shape[0]
        # Gross exposure never exceeds 1 (allowing float slack).
        assert (w.abs().sum(axis=1) <= 1.0 + 1e-9).all()


def test_vectorized_backtest_runs(panel):
    w = get_strategy("sma_crossover")(panel, {"fast": 10, "slow": 30})
    res = VectorizedBacktester(CostModel()).run(panel, w)
    assert len(res.equity) == len(panel)
    assert "sharpe" in res.metrics
    assert res.engine == "vectorized"


def test_event_driven_generates_trades(panel):
    w = get_strategy("sma_crossover")(panel, {"fast": 10, "slow": 30})
    res = EventDrivenEngine(CostModel()).run(panel, w)
    assert res.engine == "event_driven"
    assert len(res.trades) > 0
    assert res.total_costs >= 0


def test_costs_reduce_returns(panel):
    """Higher transaction costs should not improve net performance."""
    cheap = run_backtest(panel, "mean_reversion", {"window": 10}, commission_bps=0, slippage_bps=0)
    pricey = run_backtest(panel, "mean_reversion", {"window": 10}, commission_bps=20, slippage_bps=20)
    assert pricey.metrics["total_return"] <= cheap.metrics["total_return"] + 1e-9


def test_unknown_strategy_raises():
    with pytest.raises(KeyError):
        get_strategy("does_not_exist")


def test_to_dict_includes_trades(panel):
    w = get_strategy("sma_crossover")(panel, {"fast": 10, "slow": 30})
    res = EventDrivenEngine(CostModel()).run(panel, w)
    d = res.to_dict(max_trades=5)
    assert d["num_trades"] == len(res.trades)
    assert len(d["trades"]) == 5
    assert d["trades_truncated"] is True
    assert {"date", "symbol", "shares", "price", "side"} <= set(d["trades"][0])


def test_sweep_ranks_by_metric(panel):
    out = sweep(panel, "sma_crossover", {"fast": [10, 20], "slow": [50, 100]})
    assert out["num_combinations"] == 4
    sharpes = [r["metrics"]["sharpe"] for r in out["results"]]
    assert sharpes == sorted(sharpes, reverse=True)
    assert out["best"]["params"] == out["results"][0]["params"]


def test_sweep_rejects_oversized_grid(panel):
    with pytest.raises(ValueError):
        sweep(panel, "sma_crossover", {"fast": list(range(30)), "slow": list(range(30))})


def test_walk_forward_stitches_oos_curve(panel):
    out = walk_forward(
        panel,
        "sma_crossover",
        param_grid={"fast": [10, 20], "slow": [50]},
        train_days=200,
        test_days=60,
        purge_days=5,
    )
    assert out["num_folds"] >= 2
    for fold in out["folds"]:
        assert fold["params"]["slow"] == 50
        assert fold["train_end"] < fold["test_start"]  # purge gap keeps them apart
    assert "sharpe" in out["oos_metrics"]
    n_days = len(out["oos_equity_curve"]["dates"])
    assert n_days == out["num_folds"] * 60


def test_walk_forward_needs_enough_data(panel):
    with pytest.raises(ValueError):
        walk_forward(panel.iloc[:100], "buy_and_hold", train_days=252, test_days=63)


def test_cost_sensitivity_monotone(panel):
    out = cost_sensitivity(panel, "mean_reversion", {"window": 10}, multipliers=(0.0, 1.0, 4.0))
    returns = [lvl["metrics"]["total_return"] for lvl in out["levels"]]
    assert returns[0] >= returns[1] >= returns[2]
    assert out["levels"][0]["total_costs"] == 0.0


def _synthetic_panel(days=300, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=days)
    px = 100 * np.cumprod(1 + rng.normal(0.0004, 0.01, size=(days, 2)), axis=0)
    return pd.DataFrame(px, index=idx, columns=["AAA", "BBB"])


def test_vol_target_caps_leverage_and_scales():
    prices = _synthetic_panel()
    raw = pd.DataFrame(0.5, index=prices.index, columns=prices.columns)
    scaled = apply_vol_target(prices, raw, target_vol=0.05, lookback=20, max_leverage=2.0)
    gross = scaled.abs().sum(axis=1)
    assert (gross <= 2.0 + 1e-9).all()
    # A ~16% vol book targeted at 5% should be scaled well below raw exposure.
    assert gross.iloc[50:].mean() < raw.abs().sum(axis=1).iloc[50:].mean()


def test_stop_loss_exits_after_breach():
    idx = pd.bdate_range("2022-01-03", periods=10)
    # Steady decline: -5% per bar breaches a 10% trailing stop on bar 3.
    prices = pd.DataFrame({"AAA": 100 * 0.95 ** np.arange(10)}, index=idx)
    raw = pd.DataFrame(1.0, index=idx, columns=["AAA"])
    stopped = apply_stop_loss(prices, raw, stop_pct=0.10, trailing=True)
    assert stopped["AAA"].iloc[0] == 1.0  # in the market before the breach
    assert (stopped["AAA"].iloc[3:] == 0.0).all()  # flat after it


def test_stop_loss_ignores_winners():
    idx = pd.bdate_range("2022-01-03", periods=10)
    prices = pd.DataFrame({"AAA": 100 * 1.01 ** np.arange(10)}, index=idx)
    raw = pd.DataFrame(1.0, index=idx, columns=["AAA"])
    stopped = apply_stop_loss(prices, raw, stop_pct=0.10)
    assert stopped.equals(stopped * 0 + raw)  # untouched


def test_monte_carlo_deterministic_and_sane():
    rng = np.random.default_rng(11)
    returns = pd.Series(rng.normal(0.0005, 0.01, 500), index=pd.bdate_range("2022-01-03", periods=500))
    a = monte_carlo(returns, n_paths=200, seed=7)
    b = monte_carlo(returns, n_paths=200, seed=7)
    assert a == b  # seeded → reproducible
    t = a["terminal_equity"]
    assert t["p5"] <= t["p25"] <= t["p50"] <= t["p75"] <= t["p95"]
    assert 0.0 <= a["prob_loss"] <= 1.0
    assert a["max_drawdown"]["p50"] <= 0.0
    assert a["cvar_95"] <= a["var_95"]


def test_benchmark_attribution_self_is_identity():
    rng = np.random.default_rng(5)
    r = pd.Series(rng.normal(0.0004, 0.01, 300), index=pd.bdate_range("2022-01-03", periods=300))
    stats = benchmark_attribution(r, r)
    assert stats["beta"] == pytest.approx(1.0)
    assert stats["alpha_annual"] == pytest.approx(0.0, abs=1e-9)
    assert stats["tracking_error"] == pytest.approx(0.0, abs=1e-9)
    assert stats["up_capture"] == pytest.approx(1.0)
    assert stats["down_capture"] == pytest.approx(1.0)


def test_run_backtest_with_overlays(panel):
    plain = run_backtest(panel, "sma_crossover", {"fast": 10, "slow": 30})
    sized = run_backtest(
        panel,
        "sma_crossover",
        {"fast": 10, "slow": 30},
        vol_target=0.05,
        stop_loss=0.10,
    )
    assert len(sized.equity) == len(plain.equity)
    # A 5% vol target should tamp down realized volatility vs. the raw strategy.
    assert sized.metrics["annual_volatility"] < plain.metrics["annual_volatility"]


def test_backtest_endpoint_persists(auth_client):
    body = {
        "strategy": "sma_crossover",
        "symbols": ["AAPL", "MSFT", "SPY"],
        "start": "2022-01-01",
        "end": "2024-01-01",
        "engine": "vectorized",
        "params": {"fast": 20, "slow": 50},
    }
    r = auth_client.post("/api/backtest/run", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "run_id" in data
    assert "equity_curve" in data
    assert "metrics" in data

    hist = auth_client.get("/api/backtest/history").json()
    assert any(run["id"] == data["run_id"] for run in hist["runs"])


def test_run_endpoint_attribution_and_monte_carlo(auth_client):
    body = {
        "strategy": "sma_crossover",
        "symbols": ["AAPL", "MSFT"],
        "start": "2022-01-01",
        "end": "2024-01-01",
        "benchmark": "SPY",
        "monte_carlo": True,
        "vol_target": 0.10,
        "stop_loss": 0.15,
    }
    r = auth_client.post("/api/backtest/run", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "trades" in data
    assert "beta" in data["benchmark"]["attribution"]
    assert "terminal_equity" in data["monte_carlo"]


def test_sweep_endpoint(auth_client):
    body = {
        "strategy": "sma_crossover",
        "symbols": ["AAPL", "MSFT"],
        "start": "2022-01-01",
        "end": "2024-01-01",
        "param_grid": {"fast": [10, 20], "slow": [50]},
    }
    r = auth_client.post("/api/backtest/sweep", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["num_combinations"] == 2
    assert data["best"]["params"] in [res["params"] for res in data["results"]]


def test_walk_forward_endpoint(auth_client):
    body = {
        "strategy": "buy_and_hold",
        "symbols": ["AAPL", "SPY"],
        "start": "2021-01-01",
        "end": "2024-01-01",
        "train_days": 252,
        "test_days": 63,
        "purge_days": 5,
    }
    r = auth_client.post("/api/backtest/walk_forward", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["num_folds"] >= 1
    assert "sharpe" in data["oos_metrics"]


def test_cost_sensitivity_endpoint(auth_client):
    body = {
        "strategy": "sma_crossover",
        "symbols": ["AAPL", "MSFT"],
        "start": "2022-01-01",
        "end": "2024-01-01",
        "multipliers": [0.0, 2.0],
    }
    r = auth_client.post("/api/backtest/cost_sensitivity", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["levels"]) == 2
    assert data["levels"][0]["commission_bps"] == 0.0
