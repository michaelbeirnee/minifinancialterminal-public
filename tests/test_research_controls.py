import numpy as np
import pandas as pd

from backend.backtest.research_controls import (
    benjamini_hochberg,
    correlation_clusters,
    group_neutral_ic,
    one_sided_positive_p_value,
    signal_correlation_matrix,
)
from backend.backtest.signal_research import (
    _metric_block,
    _t_stat,
    cross_sectional_ic,
    research_signal_suite,
)


def test_fdr_p_value_uses_non_overlapping_ic_observations():
    """At horizon h, daily ICs overlap h-1 bars; the FDR input must not treat
    them as independent evidence."""
    rng = np.random.default_rng(9)
    idx = pd.bdate_range("2023-01-02", periods=200)
    # Autocorrelated IC series, the shape overlapping forward returns produce.
    ic = pd.Series(rng.normal(0, 0.05, len(idx)), index=idx).rolling(5, min_periods=1).mean() + 0.01
    spread = pd.Series(np.nan, index=idx)

    block = _metric_block(ic, spread, horizon=5)
    sub = ic.dropna().iloc[::5]
    expected = one_sided_positive_p_value(_t_stat(sub), len(sub))
    assert block["ic_p_value_observations"] == len(sub)
    assert abs(block["ic_p_value"] - round(float(expected), 8)) < 1e-12
    # The naive full-series p-value counts every overlapping day as independent
    # evidence and comes out (wrongly) far more significant.
    naive = one_sided_positive_p_value(_t_stat(ic.dropna()), len(ic.dropna()))
    assert naive < block["ic_p_value"]
    # Horizon 1 has no overlap, so the p-value uses the full series unchanged.
    block_h1 = _metric_block(ic, spread, horizon=1)
    assert block_h1["ic_p_value_observations"] == len(ic.dropna())
    assert abs(block_h1["ic_p_value"] - round(float(naive), 8)) < 1e-12


def test_group_neutral_ic_removes_pure_sector_move():
    idx = pd.bdate_range("2024-01-02", periods=40)
    cols = ["A1", "A2", "A3", "B1", "B2", "B3"]
    signal = pd.DataFrame(
        np.tile([1.0, 1.0, 1.0, -1.0, -1.0, -1.0], (len(idx), 1)),
        index=idx,
        columns=cols,
    )
    future = pd.DataFrame(
        np.tile([0.01, 0.01, 0.01, -0.01, -0.01, -0.01], (len(idx), 1)),
        index=idx,
        columns=cols,
    )
    groups = {c: ("sector_a" if c.startswith("A") else "sector_b") for c in cols}

    raw = cross_sectional_ic(signal, future, min_names=3)
    neutral = group_neutral_ic(signal, future, groups, min_names=3, min_group_names=2)

    assert raw.dropna().mean() > 0.99
    assert neutral.dropna().empty


def test_benjamini_hochberg_controls_many_hypotheses():
    q = benjamini_hochberg({"a": 0.001, "b": 0.02, "c": 0.20, "d": 0.80})
    assert abs(q["a"] - 0.004) < 1e-12
    assert abs(q["b"] - 0.04) < 1e-12
    assert q["c"] > 0.10
    assert q["d"] > 0.10


def test_signal_correlation_clusters_near_duplicates():
    rng = np.random.default_rng(4)
    idx = pd.bdate_range("2023-01-03", periods=80)
    cols = [f"S{i}" for i in range(8)]
    base = pd.DataFrame(rng.normal(size=(len(idx), len(cols))), index=idx, columns=cols)
    duplicate = base + pd.DataFrame(
        rng.normal(scale=0.01, size=base.shape), index=idx, columns=cols
    )
    independent = pd.DataFrame(rng.normal(size=base.shape), index=idx, columns=cols)
    corr = signal_correlation_matrix(
        {"base": base, "duplicate": duplicate, "independent": independent},
        min_overlap=100,
    )
    clusters = correlation_clusters(corr, threshold=0.95)

    assert any(set(cluster) == {"base", "duplicate"} for cluster in clusters)
    assert any(cluster == ["independent"] for cluster in clusters)


def test_research_report_exposes_sector_fdr_and_redundancy_controls():
    rng = np.random.default_rng(12)
    idx = pd.bdate_range("2020-01-02", periods=700)
    common = rng.normal(0.0002, 0.008, len(idx))
    prices = {}
    for i in range(10):
        ret = (0.7 + 0.04 * i) * common + rng.normal(0.0, 0.006, len(idx))
        prices[f"S{i}"] = 100 * np.cumprod(1 + ret)
    panel = pd.DataFrame(prices, index=idx)
    groups = {f"S{i}": ("A" if i < 5 else "B") for i in range(10)}

    report = research_signal_suite(
        panel,
        horizons=(1, 5, 10),
        primary_horizon=5,
        train_days=252,
        test_days=63,
        purge_days=5,
        groups=groups,
        group_label="sector",
        fdr_alpha=0.10,
        redundancy_threshold=0.80,
    )

    controls = report["research_controls"]
    assert controls["group_neutralization"]["enabled"] is True
    assert controls["group_neutralization"]["label"] == "sector"
    assert controls["false_discovery"]["method"] == "benjamini_hochberg"
    assert controls["redundancy"]["clusters"]
    for row in report["signals"]:
        assert "group_neutral_primary" in row
        assert "fdr" in row and "q_value" in row["fdr"]
        assert "redundancy" in row and "representative" in row["redundancy"]
        if row["validated"]:
            assert row["raw_validated"] is True
            assert row["group_neutral_validated"] is True
            assert row["fdr"]["passed"] is True
            assert row["redundancy"]["passed"] is True


def test_requested_group_neutralization_fails_closed_when_classification_is_empty():
    rng = np.random.default_rng(21)
    idx = pd.bdate_range("2020-01-02", periods=500)
    common = rng.normal(0.0002, 0.008, len(idx))
    panel = pd.DataFrame(
        {
            f"S{i}": 100
            * np.cumprod(1 + (0.75 + 0.03 * i) * common + rng.normal(0, 0.006, len(idx)))
            for i in range(8)
        },
        index=idx,
    )
    report = research_signal_suite(
        panel,
        horizons=(1, 5),
        primary_horizon=5,
        train_days=252,
        test_days=63,
        purge_days=5,
        groups={},
        group_label="sector",
    )
    assert report["research_controls"]["group_neutralization"]["enabled"] is True
    assert report["research_controls"]["group_neutralization"]["classification_coverage"] == 0.0
    assert not any(row["validated"] for row in report["signals"])
    assert all(row["group_neutral_validated"] is False for row in report["signals"])
