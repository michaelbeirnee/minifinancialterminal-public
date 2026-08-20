import numpy as np
import pandas as pd

from backend.backtest.stat_arb import build_stat_arb, stat_arb_snapshot
from backend.backtest.strategies import get_strategy


def _panel(days: int = 460, names: int = 10, seed: int = 17) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=days)
    common = rng.normal(0.0003, 0.008, days)
    prices = {}
    for i in range(names):
        beta = 0.55 + 0.1 * i
        idio = rng.normal(0.0, 0.006 + 0.0004 * i, days)
        ret = beta * common + idio
        prices[f"S{i}"] = 100.0 * np.cumprod(1.0 + ret)
    return pd.DataFrame(prices, index=idx)


def test_stat_arb_registered_and_gross_constrained():
    prices = _panel()
    weights = get_strategy("stat_arb")(prices, {"rebalance_days": 1})
    assert weights.shape == prices.shape
    assert (weights.abs().sum(axis=1) <= 1.0 + 1e-9).all()
    assert np.isfinite(weights.to_numpy()).all()


def test_stat_arb_is_dollar_and_beta_neutral_on_rebalance_dates():
    prices = _panel()
    out = build_stat_arb(prices, {"rebalance_days": 1})
    active = out.weights.abs().sum(axis=1) > 1e-12
    assert active.any()

    net = out.weights.loc[active].sum(axis=1).abs()
    beta_exposure = (out.weights.loc[active] * out.beta.loc[active]).sum(axis=1).abs()
    assert net.max() < 1e-9
    assert beta_exposure.max() < 1e-8


def test_stat_arb_rebalance_schedule_is_calendar_anchored():
    """The same date must get the same target no matter where the panel starts.

    A positional (row-count) schedule fails this: trimming one leading bar
    shifts every rebalance date, so snapshots depend on the query's start.
    """
    prices = _panel()
    params = {"rebalance_days": 5}
    full = build_stat_arb(prices, params).weights
    shifted = build_stat_arb(prices.iloc[1:], params).weights

    tail = full.index[-30:]
    assert np.allclose(full.loc[tail], shifted.loc[tail], atol=1e-12)


def test_stat_arb_does_not_look_into_future():
    prices = _panel()
    params = {"rebalance_days": 1, "smooth_span": 3}
    original = build_stat_arb(prices, params).weights

    changed = prices.copy()
    cutoff = changed.index[-30]
    changed.loc[cutoff:, "S0"] *= np.linspace(1.0, 1.8, len(changed.loc[cutoff:]))
    changed.loc[cutoff:, "S1"] *= np.linspace(1.0, 0.65, len(changed.loc[cutoff:]))
    revised = build_stat_arb(changed, params).weights

    pd.testing.assert_frame_equal(original.loc[original.index < cutoff], revised.loc[revised.index < cutoff])


def test_stat_arb_snapshot_explains_positions():
    snap = stat_arb_snapshot(_panel(), {"rebalance_days": 1})
    assert snap["gross_exposure"] <= 1.0 + 1e-9
    assert abs(snap["net_exposure"]) < 1e-8
    assert abs(snap["beta_exposure"]) < 1e-7
    assert len(snap["positions"]) == 10
    assert {
        "symbol",
        "side",
        "weight",
        "score",
        "beta",
        "residual_reversal",
        "residual_momentum",
        "low_idio_vol",
    } <= set(snap["positions"][0])
