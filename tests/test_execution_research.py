import numpy as np
import pandas as pd

from backend.backtest.execution_research import (
    build_execution_panels,
    current_execution_book,
    signal_execution_diagnostics,
    signal_target_weights,
)
from backend.backtest.signal_research import research_signal_suite


def _fixture(days: int = 520, names: int = 8, seed: int = 17):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=days)
    common = rng.normal(0.0002, 0.008, days)
    prices = {}
    for i in range(names):
        ret = (0.7 + 0.04 * i) * common + rng.normal(0.0, 0.006, days)
        prices[f"S{i}"] = 100 * np.cumprod(1 + ret)
    prices = pd.DataFrame(prices, index=idx)
    volume = pd.DataFrame(
        {c: 1_000_000 + 100_000 * np.sin(np.arange(days) / 17 + i)
         for i, c in enumerate(prices.columns)},
        index=idx,
    )
    high = prices * 1.006
    low = prices * 0.994
    return prices, {"volume": volume, "high": high, "low": low}


def test_signal_targets_are_unit_gross_and_dollar_neutral():
    idx = pd.bdate_range("2024-01-02", periods=30)
    cols = [f"S{i}" for i in range(10)]
    signal = pd.DataFrame(
        np.tile(np.arange(len(cols), dtype=float), (len(idx), 1)),
        index=idx,
        columns=cols,
    )
    weights = signal_target_weights(signal, quantile=0.2, min_names=5)
    active = weights.abs().sum(axis=1) > 0
    assert active.any()
    assert np.allclose(weights.loc[active].sum(axis=1), 0.0)
    assert np.allclose(weights.loc[active].abs().sum(axis=1), 1.0)


def test_capacity_fill_falls_as_research_capital_grows():
    prices, features = _fixture(days=120, names=8)
    panels = build_execution_panels(prices, features)
    # Rotate the cross-sectional ordering to create real daily turnover.
    base = np.arange(prices.shape[1], dtype=float)
    signal = pd.DataFrame(
        np.vstack([np.roll(base, i % len(base)) for i in range(len(prices))]),
        index=prices.index,
        columns=prices.columns,
    )
    future = prices.shift(-5).div(prices) - 1.0
    mask = pd.Series(False, index=prices.index)
    mask.iloc[40:100] = True

    small = signal_execution_diagnostics(
        signal, future, mask, panels,
        capital_dollars=1_000_000,
        max_adv_participation=0.01,
        min_names=5,
    )
    large = signal_execution_diagnostics(
        signal, future, mask, panels,
        capital_dollars=500_000_000,
        max_adv_participation=0.01,
        min_names=5,
    )
    assert small["capacity_fill"] > large["capacity_fill"]
    assert small["cost_bps"] > 0
    assert large["cost_bps"] >= small["cost_bps"]
    assert small["net_alpha_bps"] <= small["gross_alpha_bps"]


def test_future_liquidity_edits_do_not_change_prior_execution_inputs():
    prices, features = _fixture(days=180, names=8)
    first = build_execution_panels(prices, features)
    cutoff = prices.index[-40]
    changed = {k: v.copy() for k, v in features.items()}
    changed["volume"].loc[cutoff:, "S0"] *= 100
    changed["high"].loc[cutoff:, "S1"] *= 1.2
    changed["low"].loc[cutoff:, "S1"] *= 0.8
    second = build_execution_panels(prices, changed)
    before = prices.index < cutoff
    pd.testing.assert_frame_equal(first.adv_dollars.loc[before], second.adv_dollars.loc[before])
    pd.testing.assert_frame_equal(first.spread_bps.loc[before], second.spread_bps.loc[before])
    pd.testing.assert_frame_equal(first.volatility_bps.loc[before], second.volatility_bps.loc[before])


def test_execution_gate_can_only_filter_statistical_survivors():
    prices, features = _fixture()
    panels = build_execution_panels(prices, features)
    report = research_signal_suite(
        prices,
        horizons=(1, 5),
        primary_horizon=5,
        train_days=252,
        test_days=63,
        purge_days=5,
        min_oos_ic=-1.0,
        min_oos_t_stat=-20.0,
        min_positive_folds=0.0,
        min_coverage=0.0,
        min_oos_observations=5,
        fdr_alpha=1.0,
        redundancy_threshold=1.0,
        execution_panels=panels,
        research_capital_dollars=10_000_000,
        max_adv_participation=0.05,
        min_capacity_fill=0.0,
        min_net_alpha_bps=10_000.0,  # deliberately impossible hurdle
    )
    assert report["research_controls"]["execution"]["enabled"] is True
    assert not any(row["validated"] for row in report["signals"])
    for row in report["signals"]:
        assert row["execution"] is not None
        assert row["execution_validated"] is False
        assert "net_alpha_after_cost" in row["exclusion_reasons"]
        assert row["selection_score"] == 0.0


def test_current_book_scales_neutral_target_to_adv_cap():
    prices, features = _fixture(days=180, names=8)
    panels = build_execution_panels(prices, features)
    idx, cols = prices.index, prices.columns
    # Two simple cross-sectional components and flat beta are enough to test
    # the capacity scaling invariant.
    a = pd.DataFrame(
        np.tile(np.linspace(-1.0, 1.0, len(cols)), (len(idx), 1)),
        index=idx, columns=cols,
    )
    b = a.iloc[:, ::-1].copy()
    b.columns = cols
    beta = pd.DataFrame(1.0, index=idx, columns=cols)
    report = {
        "recommended_blend": [
            {"signal": "a", "weight": 0.75},
            {"signal": "b", "weight": 0.25},
        ],
        "signals": [
            {"name": "a", "execution": {"gross_alpha_bps": 8.0, "net_alpha_bps": 5.0, "cost_bps": 3.0, "capacity_fill": 1.0}},
            {"name": "b", "execution": {"gross_alpha_bps": 6.0, "net_alpha_bps": 4.0, "cost_bps": 2.0, "capacity_fill": 1.0}},
        ],
    }
    book = current_execution_book(
        prices, {"a": a, "b": b}, beta, report, panels,
        capital_dollars=500_000_000, max_adv_participation=0.005,
    )
    assert book["status"] == "ready"
    assert 0.0 < book["capacity_scale"] < 1.0
    assert abs(book["net_exposure"]) < 1e-8
    assert abs(book["beta_exposure"]) < 1e-8
    assert book["executable_gross_exposure"] <= book["target_gross_exposure"] + 1e-12
    for row in book["positions"]:
        if row["entry_participation"] is not None:
            assert row["entry_participation"] <= 0.005 + 1e-8
