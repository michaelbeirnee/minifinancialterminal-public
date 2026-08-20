import numpy as np
import pandas as pd

from backend.backtest.signal_research import (
    build_adaptive_stat_arb,
    build_signal_library,
    research_signal_suite,
    signal_catalog,
)
from backend.backtest.strategies import get_strategy


def _panel(days: int = 900, names: int = 12, seed: int = 41) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2021-01-04", periods=days)
    common = rng.normal(0.00025, 0.008, days)
    prices = {}
    for i in range(names):
        beta = 0.55 + 0.06 * i
        idio = rng.normal(0.0, 0.0055 + 0.00025 * i, days)
        for t in range(2, days):
            idio[t] += 0.08 * idio[t - 1] - 0.05 * idio[t - 2]
        returns = beta * common + idio
        prices[f"S{i}"] = 100.0 * np.cumprod(1.0 + returns)
    return pd.DataFrame(prices, index=idx)


def test_signal_catalog_and_library_are_extensible():
    prices = _panel()
    catalog = signal_catalog()
    library = build_signal_library(prices)

    assert len(catalog) >= 9
    assert {row["name"] for row in catalog} == set(library.components)
    assert {row["source"] for row in catalog} == {"price"}
    for frame in library.components.values():
        assert frame.shape == prices.shape


def test_signal_research_reports_oos_decay_and_folds():
    report = research_signal_suite(
        _panel(),
        horizons=(1, 5, 10, 21),
        primary_horizon=5,
        train_days=252,
        test_days=63,
        purge_days=5,
    )

    assert report["fold_config"]["folds"] > 0
    assert len(report["signals"]) >= 9
    assert set(report["signals"][0]["decay"]) == {"1", "5", "10", "21"}
    assert report["signals"][0]["status"] in {"validated", "watch", "reject"}
    assert all(0.0 <= row["coverage"] <= 1.0 for row in report["signals"])
    assert all(0.0 <= row["score_turnover"] <= 1.0 for row in report["signals"])


def test_adaptive_stat_arb_is_registered_neutral_and_gross_constrained():
    prices = _panel()
    params = {"quality_window": 126, "quality_min_periods": 40, "rebalance_days": 1}
    out = build_adaptive_stat_arb(prices, params)
    strategy_weights = get_strategy("stat_arb_research")(prices, params)

    pd.testing.assert_frame_equal(out.weights, strategy_weights)
    active = out.weights.abs().sum(axis=1) > 1e-12
    assert active.any()
    assert (out.weights.abs().sum(axis=1) <= 1.0 + 1e-9).all()
    assert out.weights.loc[active].sum(axis=1).abs().max() < 1e-8
    beta_exposure = (out.weights.loc[active] * out.beta.loc[active]).sum(axis=1).abs()
    assert beta_exposure.max() < 1e-7
    assert (out.signal_weights.sum(axis=1) <= 1.0 + 1e-9).all()


def test_adaptive_quality_does_not_use_unrealized_future_labels():
    prices = _panel()
    params = {
        "quality_horizon": 5,
        "quality_window": 126,
        "quality_min_periods": 40,
        "rebalance_days": 1,
    }
    original = build_adaptive_stat_arb(prices, params)

    changed = prices.copy()
    cutoff = changed.index[-45]
    tail = changed.index >= cutoff
    changed.loc[tail, "S0"] *= np.linspace(1.0, 1.9, int(tail.sum()))
    changed.loc[tail, "S1"] *= np.linspace(1.0, 0.55, int(tail.sum()))
    revised = build_adaptive_stat_arb(changed, params)

    before = original.weights.index < cutoff
    pd.testing.assert_frame_equal(original.weights.loc[before], revised.weights.loc[before])
    pd.testing.assert_frame_equal(
        original.signal_weights.loc[before], revised.signal_weights.loc[before]
    )
