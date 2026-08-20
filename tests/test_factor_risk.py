import numpy as np
import pandas as pd

from backend.backtest.alpha_risk import project_portfolio_constraints
from backend.backtest.factor_risk import (
    build_factor_risk_model,
    portfolio_risk_diagnostics,
)


def _prices(days: int = 520, names: int = 8, seed: int = 91) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=days)
    market = rng.normal(0.0002, 0.009, days)
    style = rng.normal(0.0, 0.004, days)
    data = {}
    for i in range(names):
        beta = 0.7 + 0.08 * i
        loading = (-1.0 + 2.0 * i / max(1, names - 1)) * 0.5
        ret = beta * market + loading * style + rng.normal(0.0, 0.006 + 0.0005 * i, days)
        data[f"S{i}"] = 100.0 * np.cumprod(1.0 + ret)
    return pd.DataFrame(data, index=idx)


def test_factor_risk_covariance_is_psd_and_decomposes_portfolio_risk():
    prices = _prices()
    model = build_factor_risk_model(prices, lookback=252, min_obs=80, residual_shrinkage=0.5)
    eigvals = np.linalg.eigvalsh(model.covariance.to_numpy(dtype=float))
    assert eigvals.min() >= -1e-10
    weights = pd.Series(
        [0.12, 0.10, 0.08, 0.05, -0.08, -0.09, -0.09, -0.09],
        index=prices.columns,
        dtype=float,
    )
    weights -= weights.mean()
    risk = portfolio_risk_diagnostics(weights, model)
    assert risk["status"] == "ready"
    assert risk["predicted_annual_volatility"] > 0.0
    assert abs((risk["factor_variance"] + risk["residual_variance"]) - risk["predicted_annual_variance"]) < 1e-7
    shares = sum(float(row["risk_share"]) for row in risk["top_name_risk"])
    # All names fit in top_n=10 for this fixture, so Euler shares sum to one.
    assert abs(shares - 1.0) < 1e-5
    assert risk["stress_tests"]


def test_future_price_edits_do_not_change_old_factor_risk_model():
    prices = _prices()
    cutoff = prices.index[-80]
    first = build_factor_risk_model(prices, as_of=cutoff, lookback=180, min_obs=60)
    changed = prices.copy()
    changed.loc[changed.index > cutoff, "S0"] *= np.linspace(1.0, 5.0, (changed.index > cutoff).sum())
    changed.loc[changed.index > cutoff, "S1"] *= np.linspace(1.0, 0.2, (changed.index > cutoff).sum())
    second = build_factor_risk_model(changed, as_of=cutoff, lookback=180, min_obs=60)
    pd.testing.assert_frame_equal(first.exposures, second.exposures)
    pd.testing.assert_frame_equal(first.factor_covariance, second.factor_covariance)
    pd.testing.assert_frame_equal(first.residual_covariance, second.residual_covariance)
    pd.testing.assert_frame_equal(first.covariance, second.covariance)


def test_covariance_projection_respects_vol_and_factor_caps():
    prices = _prices()
    model = build_factor_risk_model(prices, lookback=252, min_obs=80)
    names = model.covariance.index
    desired = pd.Series(np.linspace(0.16, -0.16, len(names)), index=names)
    desired -= desired.mean()
    beta = pd.Series(1.0, index=names)
    caps = {name: 0.05 for name in model.exposures.columns}
    projected, info = project_portfolio_constraints(
        desired,
        beta,
        gross_limit=1.0,
        max_name_weight=0.20,
        covariance=model.covariance,
        factor_exposures=model.exposures,
        factor_exposure_caps=caps,
        target_annual_vol=0.08,
        risk_aversion=0.25,
    )
    assert info["status"] == "ready"
    assert abs(projected.sum()) < 1e-8
    assert projected.abs().max() <= 0.20 + 1e-8
    assert info["predicted_annual_volatility"] <= 0.08 + 1e-6
    for name, exposure in info["factor_exposures"].items():
        assert abs(float(exposure)) <= caps[name] + 1e-6


def test_covariance_penalty_reduces_predicted_risk_without_breaking_neutrality():
    prices = _prices()
    model = build_factor_risk_model(prices, lookback=252, min_obs=80)
    names = model.covariance.index
    desired = pd.Series([0.18, 0.16, 0.14, 0.02, -0.03, -0.12, -0.16, -0.19], index=names)
    desired -= desired.mean()
    beta = pd.Series(1.0, index=names)
    plain, _ = project_portfolio_constraints(
        desired, beta, gross_limit=1.0, max_name_weight=0.25,
    )
    aware, info = project_portfolio_constraints(
        desired,
        beta,
        gross_limit=1.0,
        max_name_weight=0.25,
        covariance=model.covariance,
        risk_aversion=1.0,
    )
    cov = model.covariance.to_numpy(dtype=float)
    plain_var = float(plain.to_numpy() @ cov @ plain.to_numpy())
    aware_var = float(aware.to_numpy() @ cov @ aware.to_numpy())
    assert info["status"] == "ready"
    assert aware_var <= plain_var + 1e-10
    assert abs(aware.sum()) < 1e-8
