from datetime import date

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.backtest.alpha_risk import build_alpha_sleeve_plan
from backend.backtest.execution_research import build_execution_panels
from backend.backtest.multisource_research import FeaturePanels, build_multisource_signal_library
from backend.backtest.signal_research import research_signal_suite
from backend.models import (
    Base,
    ProductionOrder,
    ProductionRun,
)
from backend.trading import production
from backend.trading.production import (
    LedgerBroker,
    promote_vintage,
    reconcile,
    run_daily_cycle,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _panel(days: int = 520, names: int = 8, seed: int = 55) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=days)
    common = rng.normal(0.0002, 0.008, days)
    out = {}
    for i in range(names):
        ret = (0.65 + 0.05 * i) * common + rng.normal(0.0, 0.006, days)
        out[f"S{i}"] = 100.0 * np.cumprod(1.0 + ret)
    return pd.DataFrame(out, index=idx)


def _features(prices: pd.DataFrame) -> FeaturePanels:
    t = np.arange(len(prices), dtype=float)
    volume = pd.DataFrame(
        {c: 900_000 * (1.0 + 0.15 * np.sin(t / 13.0 + i)) for i, c in enumerate(prices.columns)},
        index=prices.index,
    )
    pe = pd.DataFrame(
        {c: 12.0 + i + 0.25 * np.sin(t / 29.0) for i, c in enumerate(prices.columns)},
        index=prices.index,
    )
    return FeaturePanels(
        panels={
            "volume": volume,
            "high": prices * 1.006,
            "low": prices * 0.994,
            "fcf_yield": 1.0 / pe,
            "pe_trailing": pe,
        },
        source_status={"synthetic": {"available": True}},
    )


def _promoted(db, prices, features):
    """Research once with loose gates and freeze the result as a vintage."""
    signals = ["volume_confirmed_momentum", "fcf_yield_value", "peer_spread_reversal"]
    built = build_multisource_signal_library(prices, features=features, signals=signals)
    execution = build_execution_panels(prices, features.panels)
    report = research_signal_suite(
        prices,
        horizons=(5,),
        primary_horizon=5,
        train_days=126,
        test_days=42,
        purge_days=5,
        min_oos_ic=-1.0,
        min_oos_t_stat=-20.0,
        min_positive_folds=0.0,
        min_coverage=0.0,
        min_oos_observations=5,
        fdr_alpha=1.0,
        redundancy_threshold=1.0,
        library=built.library,
        signal_specs=built.specs,
        execution_panels=execution,
        min_capacity_fill=0.0,
        min_net_alpha_bps=-10_000.0,
    )
    plan = build_alpha_sleeve_plan(prices, built.library, report, built.specs)
    return promote_vintage(
        db,
        symbols=list(prices.columns),
        as_of=report["as_of"],
        params={},
        report=report,
        sleeve_plan=plan,
    )


def _cycle(db, prices, features, *, orders_enabled=False, as_of=None, config=None):
    dt = pd.Timestamp(as_of) if as_of is not None else prices.index[-1]
    return run_daily_cycle(
        db,
        orders_enabled=orders_enabled,
        broker_kind="ledger",
        as_of=str(dt.date()),
        today=dt.date(),
        prices=prices,
        features=features,
        config=config,
    )


def test_cycle_blocks_without_an_approved_vintage(db):
    prices = _panel()
    run = _cycle(db, prices, _features(prices))
    assert run.status == "blocked"
    assert run.stages[0]["stage"] == "vintage"


def test_vintage_promotion_freezes_blend_and_retires_predecessors(db):
    prices = _panel()
    features = _features(prices)
    first = _promoted(db, prices, features)
    second = _promoted(db, prices, features)
    db.refresh(first)
    assert first.status == "retired" and first.retired_at is not None
    assert second.status == "approved"
    assert second.blend and second.sleeves
    assert all("primary" in row for row in second.evidence)


def test_record_only_cycle_plans_orders_but_submits_nothing(db):
    prices = _panel()
    features = _features(prices)
    _promoted(db, prices, features)
    run = _cycle(db, prices, features, orders_enabled=False)
    assert run.status == "recorded"
    checks = {row["check"]: row["passed"] for row in run.gateway}
    assert checks["kill_switch"] is False  # MFT_TRADING_ENABLED defaults off
    assert checks["orders_enabled"] is False
    orders = db.query(ProductionOrder).filter(ProductionOrder.run_id == run.id).all()
    assert orders, "a record-only run still plans hypothetical orders"
    assert all(o.status == "planned" for o in orders)
    # The target book is neutral and inside its risk limits.
    target = pd.Series(run.target, dtype=float)
    assert abs(target.sum()) < 1e-6
    assert target.abs().sum() <= 1.0 + 1e-6
    assert run.risk["diagnostics"]["predicted_annual_volatility"] <= run.config["target_annual_volatility"] + 1e-6


def test_order_math_uses_nav_price_and_min_notional(db):
    prices = _panel()
    features = _features(prices)
    _promoted(db, prices, features)
    run = _cycle(db, prices, features, config={"min_order_notional": 500.0})
    marks = prices.iloc[-1]
    for order in db.query(ProductionOrder).filter(ProductionOrder.run_id == run.id):
        weight = run.target.get(order.symbol, 0.0)
        expected = abs(weight) * run.nav / float(marks[order.symbol])
        assert order.qty == pytest.approx(expected, rel=1e-3)
        assert order.qty * order.decision_price >= 500.0 - 1e-6
        assert (order.side == "buy") == (weight > 0)


def test_information_cutoff_ignores_future_rows(db):
    prices = _panel()
    features = _features(prices)
    _promoted(db, prices, features)
    cutoff = prices.index[-40]

    run_a = _cycle(db, prices, features, as_of=str(cutoff.date()))
    orders_a = [
        (o.symbol, o.side, round(o.qty, 4))
        for o in db.query(ProductionOrder).filter(ProductionOrder.run_id == run_a.id)
    ]

    tampered = prices.copy()
    tail = tampered.index > cutoff
    tampered.loc[tail, "S0"] *= np.linspace(1.0, 2.0, int(tail.sum()))
    run_b = _cycle(db, tampered, _features(tampered), as_of=str(cutoff.date()))
    orders_b = [
        (o.symbol, o.side, round(o.qty, 4))
        for o in db.query(ProductionOrder).filter(ProductionOrder.run_id == run_b.id)
    ]
    assert run_a.as_of == run_b.as_of == str(cutoff.date())
    assert orders_a == orders_b


def test_submit_fill_and_reconcile_round_trip(db, monkeypatch):
    prices = _panel()
    features = _features(prices)
    _promoted(db, prices, features)
    monkeypatch.setattr(production.settings, "trading_enabled", True)
    # The ledger broker fills at the next session's open, served from the panel.
    opens = prices * 1.001

    def fake_history(symbol, start, end=None):
        frame = pd.DataFrame({"open": opens[symbol], "close": prices[symbol]})
        return frame.loc[frame.index >= pd.Timestamp(start)]

    monkeypatch.setattr(production, "get_history", fake_history)

    decision = prices.index[-2]  # leave one session for the fill
    run = _cycle(db, prices, features, orders_enabled=True, as_of=str(decision.date()))
    assert run.status == "submitted"
    submitted = db.query(ProductionOrder).filter(ProductionOrder.run_id == run.id).all()
    assert submitted and all(o.status == "submitted" for o in submitted)

    next_day = prices.index[-1].date()
    result = reconcile(db, broker_kind="ledger", today=next_day,
                       marks=prices.iloc[-1].to_dict())
    assert result["fills_ingested"] == len(submitted)
    assert result["discrepancies"] == []
    for order in submitted:
        db.refresh(order)
        side = 1.0 if order.side == "buy" else -1.0
        expected = float(opens.loc[prices.index[-1], order.symbol]) * (1.0 + side * 2.0 / 1e4)
        assert order.fill_price == pytest.approx(expected, abs=1e-5)  # stored at 6dp
        assert order.fees > 0.0

    # Ledger positions now match the fills, and the book is dollar-balanced.
    positions = LedgerBroker(db, 1_000_000.0).positions()
    assert positions
    net_value = sum(qty * float(prices.iloc[-1][sym]) for sym, qty in positions.items())
    assert abs(net_value) < 0.01 * 1_000_000.0


def test_gateway_blocks_on_position_discrepancy(db, monkeypatch):
    prices = _panel()
    features = _features(prices)
    _promoted(db, prices, features)
    # For the ledger broker both books derive from the same fills, so simulate
    # a broker drift at the reconciliation seam the cycle actually consults.
    monkeypatch.setattr(
        production, "reconcile",
        lambda *a, **k: {"discrepancies": [{"symbol": "S0", "ledger_qty": 0.0, "broker_qty": 999.0}],
                         "fills_ingested": 0},
    )
    blocked = _cycle(db, prices, features)
    assert blocked.status == "blocked"
    assert any(
        row["check"] == "reconciliation_clean" and not row["passed"] for row in blocked.gateway
    )
    assert not db.query(ProductionOrder).filter(ProductionOrder.run_id == blocked.id).count()


def test_stale_vintage_blocks_the_cycle(db):
    prices = _panel()
    features = _features(prices)
    vintage = _promoted(db, prices, features)
    vintage.as_of = "2020-01-02"  # far older than max_vintage_age_days
    db.commit()
    run = _cycle(db, prices, features)
    assert run.status == "blocked"
    assert any(
        row["check"] == "vintage_fresh" and not row["passed"] for row in run.gateway
    )


def test_partial_fill_on_cancelled_order_still_moves_the_ledger(db):
    run = ProductionRun(as_of="2024-01-02", status="submitted", broker="ledger",
                        orders_enabled=True, config={})
    db.add(run)
    db.flush()
    db.add(ProductionOrder(
        run_id=run.id, symbol="S0", side="buy", qty=100.0,
        status="cancelled", reason="broker: expired (40 of 100 filled)",
        fill_qty=40.0, fill_price=50.0, fees=0.2,
    ))
    db.commit()
    broker = LedgerBroker(db, 1_000_000.0)
    assert broker.positions() == {"S0": 40.0}
    assert broker.cash() == pytest.approx(1_000_000.0 - 40.0 * 50.0 - 0.2)


def test_unshortable_orders_are_dropped(db):
    prices = _panel()
    features = _features(prices)
    _promoted(db, prices, features)
    # Crowding snapshot marks S0 hard-to-borrow from the start of the panel.
    crowded = _features(prices)
    short_float = pd.DataFrame(np.nan, index=prices.index, columns=prices.columns)
    short_float.loc[:, "S0"] = 0.60
    crowded.panels["short_percent_float"] = short_float
    run = _cycle(db, prices, crowded)
    orders = db.query(ProductionOrder).filter(ProductionOrder.run_id == run.id).all()
    assert all(not (o.symbol == "S0" and o.side == "sell") for o in orders)
    # The projection already keeps S0 out of the short book entirely.
    assert run.target.get("S0", 0.0) >= -1e-9
