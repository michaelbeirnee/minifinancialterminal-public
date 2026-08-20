import numpy as np
import pandas as pd

from backend.backtest.execution_research import build_execution_panels
from backend.backtest.multisource_research import FeaturePanels, build_multisource_signal_library
from backend.backtest.walkforward_research import walk_forward_multisource_portfolio


def _fixture(days: int = 520, names: int = 8, seed: int = 101):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=days)
    common = rng.normal(0.0002, 0.008, days)
    prices = {}
    for i in range(names):
        idio = rng.normal(0.0, 0.006, days)
        returns = (0.65 + 0.05 * i) * common + idio
        prices[f"S{i}"] = 100.0 * np.cumprod(1.0 + returns)
    prices = pd.DataFrame(prices, index=idx)
    t = np.arange(days, dtype=float)
    volume = pd.DataFrame(
        {c: 900_000 * (1.0 + 0.15 * np.sin(t / 13.0 + i)) for i, c in enumerate(prices.columns)},
        index=idx,
    )
    pe = pd.DataFrame(
        {c: 12.0 + i + 0.25 * np.sin(t / 29.0) for i, c in enumerate(prices.columns)},
        index=idx,
    )
    features = FeaturePanels(
        panels={
            "volume": volume,
            "high": prices * 1.006,
            "low": prices * 0.994,
            "fcf_yield": 1.0 / pe,
            "pe_trailing": pe,
            "ps_trailing": pe / 4.0,
            "ev_ebitda": pe * 0.7,
        },
        source_status={"synthetic": {"available": True}},
    )
    return prices, features


def _run(prices: pd.DataFrame, features: FeaturePanels, *, capital: float = 1_000_000.0):
    signals = ["volume_confirmed_momentum", "fcf_yield_value", "peer_spread_reversal"]
    built = build_multisource_signal_library(prices, features=features, signals=signals)
    execution = build_execution_panels(prices, features.panels)
    return walk_forward_multisource_portfolio(
        prices,
        features=features,
        built=built,
        execution_panels=execution,
        primary_horizon=5,
        train_days=126,
        test_days=42,
        purge_days=5,
        min_names=3,
        min_oos_ic=-1.0,
        min_oos_t_stat=-20.0,
        min_positive_folds=0.0,
        min_coverage=0.0,
        min_oos_observations=5,
        fdr_alpha=1.0,
        redundancy_threshold=1.0,
        min_capacity_fill=0.0,
        min_net_alpha_bps=-10_000.0,
        research_refresh_days=42,
        portfolio_rebalance_days=5,
        research_capital_dollars=capital,
        initial_capital=capital,
        max_adv_participation=0.05,
    )


def test_walk_forward_uses_one_bar_execution_lag_and_prior_vintage():
    prices, features = _fixture()
    out = _run(prices, features)
    assert out.research_vintages
    assert out.decisions

    first = out.decisions[0]
    signal_dt = pd.Timestamp(first["signal_date"])
    execution_dt = pd.Timestamp(first["execution_date"])
    assert pd.Timestamp(first["research_as_of"]) <= signal_dt
    assert out.held_weights.loc[signal_dt].abs().sum() == 0.0
    pd.testing.assert_series_equal(
        out.held_weights.loc[execution_dt],
        out.target_weights.loc[signal_dt],
        check_names=False,
    )


def test_future_price_edits_do_not_change_prior_walk_forward_decisions():
    prices, features = _fixture()
    first = _run(prices, features)
    cutoff = prices.index[-80]

    changed_prices = prices.copy()
    tail = changed_prices.index >= cutoff
    changed_prices.loc[tail, "S0"] *= np.linspace(1.0, 1.8, int(tail.sum()))
    changed_prices.loc[tail, "S1"] *= np.linspace(1.0, 0.6, int(tail.sum()))
    changed_panels = {name: frame.copy() for name, frame in features.panels.items()}
    changed_panels["high"] = changed_prices * 1.006
    changed_panels["low"] = changed_prices * 0.994
    changed = _run(
        changed_prices,
        FeaturePanels(changed_panels, features.source_status),
    )

    before = prices.index < cutoff
    pd.testing.assert_frame_equal(
        first.target_weights.loc[before],
        changed.target_weights.loc[before],
    )
    prior_first = [row for row in first.decisions if pd.Timestamp(row["signal_date"]) < cutoff]
    prior_changed = [row for row in changed.decisions if pd.Timestamp(row["signal_date"]) < cutoff]
    assert prior_first == prior_changed


def test_capacity_scales_trade_vector_without_creating_net_exposure():
    prices, features = _fixture()
    tiny = features.panels["volume"] * 0.001
    constrained_features = FeaturePanels(
        {**features.panels, "volume": tiny}, features.source_status
    )
    out = _run(prices, constrained_features, capital=500_000_000.0)
    assert any(row["capacity_scale"] < 1.0 for row in out.decisions)
    assert out.target_weights.sum(axis=1).abs().max() < 1e-8
    assert out.daily_costs.ge(0.0).all()
    assert out.to_dict()["simulation"]["capacity_constrained_decisions"] > 0


def test_net_equity_includes_walk_forward_execution_costs():
    prices, features = _fixture()
    out = _run(prices, features)
    payload = out.to_dict()
    assert out.daily_costs.sum() > 0.0
    assert out.equity.iloc[-1] <= out.gross_equity.iloc[-1] + 1e-8
    assert payload["total_costs"] > 0.0
    assert payload["simulation"]["research_vintages"] == len(out.research_vintages)
    assert payload["selection_summary"]
