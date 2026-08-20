import numpy as np
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.backtest.multisource_research import (
    FeaturePanels,
    _load_archived_panels,
    build_multisource_signal_library,
    multisource_signal_catalog,
)
from backend.backtest.signal_research import research_signal_suite
from backend.models import ResearchFeatureSnapshot


def _prices(days: int = 700, names: int = 8, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=days)
    common = rng.normal(0.0002, 0.008, days)
    out = {}
    for i in range(names):
        idio = rng.normal(0, 0.006, days)
        ret = (0.7 + i * 0.04) * common + idio
        out[f"S{i}"] = 100 * np.cumprod(1 + ret)
    return pd.DataFrame(out, index=idx)


def _features(prices: pd.DataFrame) -> FeaturePanels:
    idx, cols = prices.index, prices.columns
    t = np.arange(len(idx), dtype=float)
    base_volume = pd.DataFrame(
        {c: 1_000_000 * (1 + 0.1 * np.sin(t / 13 + i)) for i, c in enumerate(cols)},
        index=idx,
    )
    pe = pd.DataFrame({c: 12 + i + 0.5 * np.sin(t / 30) for i, c in enumerate(cols)}, index=idx)
    ps = pe / 4
    ev = pe * 0.7
    fcf = 1 / pe
    surprise = pd.DataFrame(0.0, index=idx, columns=cols)
    analyst = pd.DataFrame(0.0, index=idx, columns=cols)
    for i, c in enumerate(cols):
        surprise.loc[idx[250 + i * 2], c] = (i - 3) / 10
        analyst.loc[idx[320 + i], c] = 1 if i % 2 == 0 else -1
    # Archive-style panels intentionally start late.
    rev = pd.DataFrame(np.nan, index=idx, columns=cols)
    iv_gap = pd.DataFrame(np.nan, index=idx, columns=cols)
    rev.loc[idx[450]:] = np.tile(np.linspace(-0.3, 0.3, len(cols)), (len(idx) - 450, 1))
    iv_gap.loc[idx[500]:] = np.tile(np.linspace(0.05, 0.25, len(cols)), (len(idx) - 500, 1))
    return FeaturePanels(
        panels={
            "volume": base_volume,
            "fcf_yield": fcf,
            "pe_trailing": pe,
            "ps_trailing": ps,
            "ev_ebitda": ev,
            "earnings_surprise_decay": surprise,
            "analyst_action_decay": analyst,
            "eps_revision_breadth": rev,
            "iv_realized_gap": iv_gap,
        },
        source_status={"synthetic": {"available": True}},
    )


def test_catalog_marks_archive_only_sources():
    catalog = multisource_signal_catalog()
    by_name = {row["name"]: row for row in catalog}
    assert len(catalog) >= 25
    assert by_name["eps_revision_breadth"]["archive_required"] is True
    assert by_name["iv_richness"]["archive_required"] is True
    assert by_name["fcf_yield_value"]["archive_required"] is False
    assert by_name["peer_spread_reversal"]["source"] == "cross_sectional_relationships"


def test_multisource_library_contains_available_families_and_same_shape():
    prices = _prices()
    built = build_multisource_signal_library(prices, features=_features(prices))
    expected = {
        "volume_confirmed_momentum",
        "fcf_yield_value",
        "post_earnings_surprise",
        "eps_revision_breadth",
        "iv_richness",
        "peer_spread_reversal",
    }
    assert expected.issubset(built.library.components)
    for name in expected:
        assert built.library.components[name].shape == prices.shape


def test_external_feature_mutation_does_not_change_prior_signal_rows():
    prices = _prices()
    features = _features(prices)
    first = build_multisource_signal_library(
        prices,
        features=features,
        signals=["eps_revision_breadth", "iv_richness", "peer_spread_reversal"],
    )
    cutoff = prices.index[-60]
    changed_panels = {name: frame.copy() for name, frame in features.panels.items()}
    changed_panels["eps_revision_breadth"].loc[cutoff:, "S0"] = 99.0
    changed_panels["iv_realized_gap"].loc[cutoff:, "S1"] = -99.0
    second = build_multisource_signal_library(
        prices,
        features=FeaturePanels(changed_panels, features.source_status),
        signals=["eps_revision_breadth", "iv_richness", "peer_spread_reversal"],
    )
    before = prices.index < cutoff
    pd.testing.assert_frame_equal(
        first.library.components["eps_revision_breadth"].loc[before],
        second.library.components["eps_revision_breadth"].loc[before],
    )
    pd.testing.assert_frame_equal(
        first.library.components["iv_richness"].loc[before],
        second.library.components["iv_richness"].loc[before],
    )


def test_generic_oos_evaluator_accepts_multisource_library():
    prices = _prices()
    built = build_multisource_signal_library(
        prices,
        features=_features(prices),
        signals=["volume_confirmed_momentum", "fcf_yield_value", "peer_spread_reversal"],
    )
    report = research_signal_suite(
        prices,
        horizons=(1, 5, 10),
        primary_horizon=5,
        train_days=252,
        test_days=63,
        purge_days=5,
        library=built.library,
        signal_specs=built.specs,
    )
    assert {row["name"] for row in report["signals"]} == set(built.library.components)
    assert all(row["source"] in {"volume", "fundamentals", "cross_sectional_relationships"}
               for row in report["signals"])


def test_archived_panels_never_backfill_before_capture_date():
    prices = _prices(days=80, names=3)
    engine = create_engine("sqlite:///:memory:")
    ResearchFeatureSnapshot.__table__.create(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    capture_date = prices.index[30].date().isoformat()
    db.add(
        ResearchFeatureSnapshot(
            as_of_date=capture_date,
            symbol="S0",
            family="estimates",
            provider="test",
            features={"eps_revision_breadth": 0.5},
        )
    )
    db.commit()
    panels, status = _load_archived_panels(prices, db, {"estimate_archive_ffill_days": 5})
    panel = panels["eps_revision_breadth"]
    assert panel.loc[prices.index[:30], "S0"].isna().all()
    assert panel.loc[prices.index[30], "S0"] == 0.5
    assert panel.loc[prices.index[36], "S0"] != 0.5  # stale snapshot expired
    assert status["estimates_archive"]["rows"] == 1


def test_weekend_snapshot_surfaces_on_next_session_not_dropped():
    prices = _prices(days=80, names=3)
    engine = create_engine("sqlite:///:memory:")
    ResearchFeatureSnapshot.__table__.create(engine)
    db = sessionmaker(bind=engine)()
    friday = prices.index[4]  # 2022-01-07
    saturday = "2022-01-08"
    db.add(
        ResearchFeatureSnapshot(
            as_of_date=saturday,
            symbol="S0",
            family="estimates",
            provider="test",
            features={"eps_revision_breadth": 0.7},
        )
    )
    db.commit()
    panels, _ = _load_archived_panels(prices, db, {"estimate_archive_ffill_days": 5})
    panel = panels["eps_revision_breadth"]
    assert panel.loc[:friday, "S0"].isna().all()  # never visible before capture
    assert panel.loc[prices.index[5], "S0"] == 0.7  # next session after Saturday


def test_peer_signals_are_calendar_anchored():
    prices = _prices()
    names = ["peer_spread_reversal", "peer_catchup"]
    full = build_multisource_signal_library(prices, features=FeaturePanels(), signals=names)
    shifted = build_multisource_signal_library(
        prices.iloc[1:], features=FeaturePanels(), signals=names
    )
    tail = prices.index[-30:]
    for name in names:
        a = full.library.components[name].loc[tail].fillna(0.0)
        b = shifted.library.components[name].loc[tail].fillna(0.0)
        assert np.allclose(a, b, atol=1e-12)
