import numpy as np
import pandas as pd

from backend.backtest.alpha_risk import (
    BorrowPanels,
    build_alpha_sleeve_plan,
    build_borrow_panels,
    daily_borrow_costs,
    project_portfolio_constraints,
)
from backend.backtest.signal_research import SignalLibraryOutput, SignalSpec


def _library(days: int = 260, names: int = 6):
    rng = np.random.default_rng(77)
    idx = pd.bdate_range("2024-01-02", periods=days)
    cols = [f"S{i}" for i in range(names)]
    ret = pd.DataFrame(rng.normal(0.0001, 0.01, (days, names)), index=idx, columns=cols)
    prices = 100.0 * (1.0 + ret).cumprod()
    base = pd.DataFrame(rng.normal(size=(days, names)), index=idx, columns=cols)
    # Two families intentionally share the same score path so their sleeve PnLs
    # are near-identical and should land in the same return-correlation cluster.
    components = {
        "mom": base,
        "rev": base.copy(),
        "evt": pd.DataFrame(rng.normal(size=(days, names)), index=idx, columns=cols),
    }
    beta = pd.DataFrame(1.0, index=idx, columns=cols)
    residual = ret.copy()
    library = SignalLibraryOutput(components=components, beta=beta, residual_returns=residual)
    specs = {
        "mom": SignalSpec("mom", "momentum", "test"),
        "rev": SignalSpec("rev", "reversal", "test"),
        "evt": SignalSpec("evt", "event", "test"),
    }
    report = {
        "recommended_blend": [
            {"signal": "mom", "weight": 0.35},
            {"signal": "rev", "weight": 0.35},
            {"signal": "evt", "weight": 0.30},
        ]
    }
    return prices, library, specs, report


def test_sleeve_plan_caps_correlated_cluster_and_event_budget():
    prices, library, specs, report = _library()
    plan = build_alpha_sleeve_plan(
        prices,
        library,
        report,
        specs,
        lookback=126,
        correlation_threshold=0.95,
        max_sleeve_budget=0.80,
        max_cluster_budget=0.40,
        event_budget_cap=0.15,
    )
    by_name = {row["name"]: row for row in plan["sleeves"]}
    assert set(by_name) == {"momentum", "reversal", "event"}
    assert by_name["event"]["risk_budget"] <= 0.15 + 1e-12
    correlated = by_name["momentum"]["risk_budget"] + by_name["reversal"]["risk_budget"]
    assert correlated <= 0.40 + 1e-12
    assert plan["budget_sum"] <= 1.0 + 1e-12
    mom_cluster = by_name["momentum"]["correlation_cluster"]
    assert mom_cluster == by_name["reversal"]["correlation_cluster"]


def test_constraint_projection_blocks_unshortable_and_caps_crowded_short():
    names = pd.Index(["A", "B", "C", "D"])
    desired = pd.Series([-0.30, -0.20, 0.25, 0.25], index=names)
    beta = pd.Series(1.0, index=names)
    shortable = pd.Series([False, True, True, True], index=names)
    crowding = pd.Series([0.0, 0.90, 0.0, 0.0], index=names)
    projected, info = project_portfolio_constraints(
        desired,
        beta,
        gross_limit=1.0,
        max_name_weight=0.30,
        shortable=shortable,
        crowding_score=crowding,
        crowded_short_threshold=0.65,
        max_crowded_short_gross=0.10,
    )
    assert info["status"] == "ready"
    assert projected["A"] >= -1e-10
    assert max(-projected["B"], 0.0) <= 0.10 + 1e-7
    assert abs(projected.sum()) < 1e-8
    assert abs((projected * beta).sum()) < 1e-8
    assert projected.abs().max() <= 0.30 + 1e-8


def test_borrow_panel_never_backfills_future_crowding():
    idx = pd.bdate_range("2025-01-02", periods=12)
    cols = ["A", "B", "C"]
    prices = pd.DataFrame(100.0, index=idx, columns=cols)
    short_float = pd.DataFrame(np.nan, index=idx, columns=cols)
    short_ratio = pd.DataFrame(np.nan, index=idx, columns=cols)
    short_float.loc[idx[-3]:, "A"] = 0.50
    short_ratio.loc[idx[-3]:, "A"] = 20.0
    panels = build_borrow_panels(
        prices,
        {"short_percent_float": short_float, "short_ratio": short_ratio},
        base_borrow_bps=30.0,
        crowding_surcharge_bps=900.0,
        hard_to_borrow_short_float=0.35,
        hard_to_borrow_days_to_cover=15.0,
    )
    assert panels.annual_borrow_bps.loc[idx[0], "A"] == 30.0
    assert bool(panels.shortable.loc[idx[0], "A"]) is True
    assert bool(panels.shortable.loc[idx[-1], "A"]) is False
    assert panels.annual_borrow_bps.loc[idx[-1], "A"] > 30.0
    assert panels.source_status["mode"] == "archived_crowding_proxy"


def test_daily_borrow_cost_charges_short_notional_only():
    idx = pd.bdate_range("2025-01-02", periods=2)
    weights = pd.DataFrame(
        [[0.25, -0.25, 0.0], [0.50, -0.25, -0.25]],
        index=idx,
        columns=["A", "B", "C"],
    )
    annual = pd.DataFrame(25200.0, index=idx, columns=weights.columns)
    borrow = BorrowPanels(
        annual_borrow_bps=annual,
        shortable=pd.DataFrame(True, index=idx, columns=weights.columns),
        crowding_score=pd.DataFrame(0.0, index=idx, columns=weights.columns),
        source_status={"mode": "test"},
    )
    costs = daily_borrow_costs(weights, borrow)
    # 25,200 bp / 252 = 100 bp per day on 100% short gross.
    assert abs(costs.iloc[0] - 0.0025) < 1e-12
    assert abs(costs.iloc[1] - 0.0050) < 1e-12
