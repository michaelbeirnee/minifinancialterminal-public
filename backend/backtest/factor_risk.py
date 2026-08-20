"""Point-in-time stock-level factor/covariance risk for research portfolios.

The walk-forward research engine already controls signal quality, capacity,
borrow and sleeve concentration.  This module adds the security-level risk
layer that a market-neutral book still needs:

* trailing market / momentum / low-vol factor exposures per stock;
* factor covariance plus shrunk residual covariance;
* a positive-semidefinite total covariance matrix;
* portfolio volatility, factor/residual decomposition and marginal/component
  risk contributions;
* weighted pair-covariance diagnostics for correlated-position risk; and
* transparent factor and single-name stress scenarios.

Every estimator accepts an ``as_of`` date and slices the supplied price panel
before doing any work.  Future rows therefore cannot alter an old risk model.
The factors are built from the supplied universe, so this remains a research
proxy rather than a vendor risk model with proprietary industry/style data.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd

from ..factors.models import build_factors

TRADING_DAYS = 252.0


@dataclass(frozen=True)
class FactorRiskModel:
    as_of: pd.Timestamp
    exposures: pd.DataFrame
    factor_covariance: pd.DataFrame
    residual_covariance: pd.DataFrame
    covariance: pd.DataFrame
    residual_volatility: pd.Series
    factor_returns: pd.DataFrame
    observations: dict[str, int]
    source_status: dict[str, Any]


def _nearest_psd(matrix: pd.DataFrame, floor: float = 1e-10) -> pd.DataFrame:
    """Symmetrise and clip eigenvalues so numerical noise cannot create <0 risk."""

    if matrix.empty:
        return matrix.copy()
    values = matrix.to_numpy(dtype=float)
    values = np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)
    values = (values + values.T) / 2.0
    eigvals, eigvecs = np.linalg.eigh(values)
    scale = max(1.0, float(np.max(np.abs(eigvals))) if len(eigvals) else 1.0)
    eigvals = np.maximum(eigvals, max(float(floor), 1e-12 * scale))
    repaired = eigvecs @ np.diag(eigvals) @ eigvecs.T
    repaired = (repaired + repaired.T) / 2.0
    return pd.DataFrame(repaired, index=matrix.index, columns=matrix.columns)


def build_factor_risk_model(
    prices: pd.DataFrame,
    *,
    as_of: pd.Timestamp | str | None = None,
    lookback: int = 252,
    min_obs: int = 80,
    residual_shrinkage: float = 0.50,
    covariance_floor: float = 1e-10,
) -> FactorRiskModel:
    """Estimate a trailing factor + residual stock covariance model.

    ``lookback`` applies to the regression/factor-covariance sample, while the
    factor constructor may inspect the earlier prefix needed to form momentum
    and volatility signals.  The entire input is first truncated at ``as_of``.
    """

    if prices is None or prices.empty:
        raise ValueError("factor risk model requires non-empty prices")
    panel = prices.sort_index().astype(float)
    if as_of is not None:
        stamp = pd.Timestamp(as_of)
        panel = panel.loc[panel.index <= stamp]
    if len(panel) < max(60, int(min_obs) + 2):
        raise ValueError("insufficient history for factor risk model")
    if panel.shape[1] < 3:
        raise ValueError("factor risk model requires at least 3 securities")

    lookback = max(40, int(lookback))
    min_obs = max(20, int(min_obs))
    shrink = min(1.0, max(0.0, float(residual_shrinkage)))

    factors = build_factors(panel)
    if factors.empty:
        raise ValueError("factor construction returned no observations")
    factors = factors.tail(lookback).replace([np.inf, -np.inf], np.nan).dropna(how="any")
    returns = panel.pct_change(fill_method=None).reindex(factors.index)

    names: list[str] = []
    exposure_rows: dict[str, np.ndarray] = {}
    residuals: dict[str, pd.Series] = {}
    obs: dict[str, int] = {}
    factor_names = list(factors.columns)

    for symbol in panel.columns:
        frame = pd.concat([returns[symbol].rename("asset"), factors], axis=1).dropna()
        if len(frame) < min_obs:
            continue
        y = frame["asset"].to_numpy(dtype=float)
        x_factors = frame[factor_names].to_numpy(dtype=float)
        x = np.column_stack([np.ones(len(frame)), x_factors])
        coef, *_ = np.linalg.lstsq(x, y, rcond=None)
        fitted = x @ coef
        resid = pd.Series(y - fitted, index=frame.index, dtype=float)
        names.append(str(symbol))
        exposure_rows[str(symbol)] = coef[1:].astype(float)
        residuals[str(symbol)] = resid
        obs[str(symbol)] = int(len(frame))

    if len(names) < 3:
        raise ValueError("fewer than 3 securities have enough factor-risk observations")

    exposures = pd.DataFrame.from_dict(
        exposure_rows, orient="index", columns=factor_names, dtype=float
    ).reindex(names)
    residual_frame = pd.DataFrame(residuals).reindex(columns=names)

    # Use the pairwise sample, then shrink off-diagonals toward zero.  This is
    # intentionally conservative for a small research universe and greatly
    # reduces unstable residual correlations.
    residual_cov = residual_frame.cov(min_periods=max(20, min_obs // 2)) * TRADING_DAYS
    residual_cov = residual_cov.reindex(index=names, columns=names).fillna(0.0)
    diag = np.diag(np.diag(residual_cov.to_numpy(dtype=float)))
    shrunk_values = (1.0 - shrink) * residual_cov.to_numpy(dtype=float) + shrink * diag
    residual_cov = pd.DataFrame(shrunk_values, index=names, columns=names)
    residual_cov = _nearest_psd(residual_cov, floor=covariance_floor)

    factor_cov = factors.cov() * TRADING_DAYS
    factor_cov = factor_cov.reindex(index=factor_names, columns=factor_names).fillna(0.0)
    factor_cov = _nearest_psd(factor_cov, floor=covariance_floor)

    b = exposures.to_numpy(dtype=float)
    f = factor_cov.to_numpy(dtype=float)
    total = b @ f @ b.T + residual_cov.to_numpy(dtype=float)
    covariance = pd.DataFrame(total, index=names, columns=names)
    covariance = _nearest_psd(covariance, floor=covariance_floor)

    residual_vol = pd.Series(
        np.sqrt(np.maximum(np.diag(residual_cov.to_numpy(dtype=float)), 0.0)),
        index=names,
        dtype=float,
    )

    return FactorRiskModel(
        as_of=pd.Timestamp(panel.index[-1]),
        exposures=exposures,
        factor_covariance=factor_cov,
        residual_covariance=residual_cov,
        covariance=covariance,
        residual_volatility=residual_vol,
        factor_returns=factors,
        observations=obs,
        source_status={
            "mode": "universe_internal_factor_model",
            "point_in_time": True,
            "as_of": str(pd.Timestamp(panel.index[-1]).date()),
            "factor_names": factor_names,
            "lookback_days": lookback,
            "min_observations": min_obs,
            "residual_shrinkage": round(shrink, 6),
            "security_count": len(names),
            "factor_observations": int(len(factors)),
            "note": (
                "Market, momentum and low-vol factors are reconstructed from the supplied "
                "universe; residual covariance is shrunk toward diagonal and PSD-repaired."
            ),
        },
    )


def _factor_stress_scenarios(model: FactorRiskModel) -> dict[str, dict[str, float]]:
    factors = list(model.exposures.columns)
    zero = {name: 0.0 for name in factors}
    scenarios: dict[str, dict[str, float]] = {}
    if "MKT" in zero:
        shock = dict(zero)
        shock["MKT"] = -0.05
        scenarios["market_down_5pct"] = shock
    if "MOM" in zero:
        shock = dict(zero)
        shock["MOM"] = -0.04
        scenarios["momentum_reversal_4pct"] = shock
    if "LOWVOL" in zero:
        shock = dict(zero)
        shock["LOWVOL"] = -0.04
        scenarios["lowvol_unwind_4pct"] = shock

    tail = {}
    for name in factors:
        series = model.factor_returns[name].dropna()
        tail[name] = float(series.quantile(0.01)) if not series.empty else 0.0
    if tail:
        scenarios["joint_factor_1pct_tail"] = tail
    return scenarios


def stress_portfolio(
    weights: pd.Series,
    model: FactorRiskModel,
    *,
    scenarios: Mapping[str, Mapping[str, float]] | None = None,
) -> list[dict[str, Any]]:
    """Apply transparent factor shocks plus a worst held-name 2-sigma shock."""

    names = model.covariance.index
    w = weights.reindex(names).fillna(0.0).astype(float)
    factor_exposure = model.exposures.T.dot(w)
    scenario_map = dict(scenarios or _factor_stress_scenarios(model))
    rows: list[dict[str, Any]] = []
    for label, shock_map in scenario_map.items():
        shock = pd.Series({name: float(shock_map.get(name, 0.0)) for name in model.exposures.columns})
        pnl = float((factor_exposure * shock).sum())
        rows.append({
            "scenario": str(label),
            "portfolio_return": round(pnl, 8),
            "factor_shocks": {name: round(float(shock[name]), 6) for name in shock.index},
        })

    if (w.abs() > 1e-12).any():
        # One security receives an adverse two daily residual-sigma move.  This
        # is a concentration diagnostic, not a claim about an actual event.
        daily_resid = model.residual_volatility.reindex(names).fillna(0.0) / np.sqrt(TRADING_DAYS)
        adverse = -2.0 * w.abs() * daily_resid
        symbol = str(adverse.idxmin())
        rows.append({
            "scenario": "largest_single_name_residual_2sigma",
            "portfolio_return": round(float(adverse.loc[symbol]), 8),
            "symbol": symbol,
            "residual_sigma_daily": round(float(daily_resid.loc[symbol]), 8),
        })
    return sorted(rows, key=lambda row: float(row.get("portfolio_return", 0.0)))


def portfolio_risk_diagnostics(
    weights: pd.Series,
    model: FactorRiskModel,
    *,
    top_n: int = 10,
) -> dict[str, Any]:
    """Return stock/factor/correlation risk decomposition for one target book."""

    names = model.covariance.index
    full_weights = weights.astype(float).fillna(0.0)
    unmodeled = [name for name in full_weights.index if name not in names and abs(float(full_weights.loc[name])) > 1e-12]
    unmodeled_gross = float(full_weights.reindex(unmodeled).abs().sum()) if unmodeled else 0.0
    w = full_weights.reindex(names).fillna(0.0).astype(float)
    cov = model.covariance.to_numpy(dtype=float)
    arr = w.to_numpy(dtype=float)
    variance = max(0.0, float(arr @ cov @ arr))
    vol = float(np.sqrt(variance))

    cov_w = cov @ arr
    if vol > 1e-14:
        marginal = cov_w / vol
        component = arr * marginal
        risk_share = component / vol
    else:
        marginal = np.zeros_like(arr)
        component = np.zeros_like(arr)
        risk_share = np.zeros_like(arr)

    contribution_rows = []
    for i, symbol in enumerate(names):
        if abs(arr[i]) <= 1e-12:
            continue
        contribution_rows.append({
            "symbol": str(symbol),
            "weight": round(float(arr[i]), 8),
            "marginal_volatility": round(float(marginal[i]), 8),
            "component_volatility": round(float(component[i]), 8),
            "risk_share": round(float(risk_share[i]), 8),
        })
    contribution_rows.sort(key=lambda row: abs(float(row["component_volatility"])), reverse=True)

    factor_exposure = model.exposures.T.dot(w)
    factor_cov = model.factor_covariance.to_numpy(dtype=float)
    e = factor_exposure.to_numpy(dtype=float)
    factor_variance = max(0.0, float(e @ factor_cov @ e))
    residual_variance = max(0.0, float(arr @ model.residual_covariance.to_numpy(dtype=float) @ arr))
    factor_component = e * (factor_cov @ e) if len(e) else np.array([])
    factor_rows = []
    for i, name in enumerate(factor_exposure.index):
        factor_rows.append({
            "factor": str(name),
            "exposure": round(float(factor_exposure.iloc[i]), 8),
            "variance_contribution": round(float(factor_component[i]), 10),
            "variance_share": round(float(factor_component[i] / variance), 8) if variance > 1e-14 else 0.0,
        })

    pair_rows: list[dict[str, Any]] = []
    for i in range(len(names)):
        if abs(arr[i]) <= 1e-12:
            continue
        for j in range(i + 1, len(names)):
            if abs(arr[j]) <= 1e-12:
                continue
            pair = 2.0 * arr[i] * arr[j] * cov[i, j]
            denom = np.sqrt(max(cov[i, i], 0.0) * max(cov[j, j], 0.0))
            corr = float(cov[i, j] / denom) if denom > 1e-14 else 0.0
            pair_rows.append({
                "left": str(names[i]),
                "right": str(names[j]),
                "correlation": round(corr, 6),
                "variance_contribution": round(float(pair), 10),
            })
    pair_rows.sort(key=lambda row: abs(float(row["variance_contribution"])), reverse=True)

    positive_shares = np.maximum(risk_share, 0.0)
    positive_total = float(positive_shares.sum())
    if positive_total > 1e-14:
        p = positive_shares / positive_total
        effective_risk_names = float(1.0 / np.sum(p * p))
    else:
        effective_risk_names = 0.0

    stress = stress_portfolio(w, model)
    worst_stress = min((float(row["portfolio_return"]) for row in stress), default=0.0)
    max_positive_share = max((float(row["risk_share"]) for row in contribution_rows), default=0.0)

    return {
        "status": "ready",
        "as_of": str(model.as_of.date()),
        "unmodeled_gross_exposure": round(unmodeled_gross, 8),
        "unmodeled_names": [str(name) for name in unmodeled],
        "predicted_annual_volatility": round(vol, 8),
        "predicted_annual_variance": round(variance, 10),
        "factor_variance": round(factor_variance, 10),
        "residual_variance": round(residual_variance, 10),
        "factor_variance_share": round(factor_variance / variance, 8) if variance > 1e-14 else 0.0,
        "residual_variance_share": round(residual_variance / variance, 8) if variance > 1e-14 else 0.0,
        "max_positive_name_risk_share": round(max_positive_share, 8),
        "effective_risk_names": round(effective_risk_names, 6),
        "factor_exposures": {name: round(float(value), 8) for name, value in factor_exposure.items()},
        "factor_risk": factor_rows,
        "top_name_risk": contribution_rows[: max(1, int(top_n))],
        "top_correlated_pairs": pair_rows[: max(1, int(top_n))],
        "stress_tests": stress,
        "worst_stress_return": round(worst_stress, 8),
        "model": dict(model.source_status),
    }


def factor_risk_model_summary(model: FactorRiskModel, *, top_pairs: int = 20) -> dict[str, Any]:
    """Compact API representation of the stock risk model without an NxN dump."""

    cov = model.covariance
    diag = np.sqrt(np.maximum(np.diag(cov.to_numpy(dtype=float)), 0.0))
    pair_rows: list[dict[str, Any]] = []
    for i, left in enumerate(cov.index):
        for j in range(i + 1, len(cov.index)):
            right = cov.index[j]
            denom = float(diag[i] * diag[j])
            corr = float(cov.iloc[i, j] / denom) if denom > 1e-14 else 0.0
            pair_rows.append({"left": str(left), "right": str(right), "correlation": round(corr, 6)})
    pair_rows.sort(key=lambda row: abs(float(row["correlation"])), reverse=True)

    exposure_rows = []
    for symbol, row in model.exposures.iterrows():
        exposure_rows.append({
            "symbol": str(symbol),
            "residual_annual_volatility": round(float(model.residual_volatility.loc[symbol]), 8),
            "factors": {name: round(float(value), 8) for name, value in row.items()},
            "observations": int(model.observations.get(str(symbol), 0)),
        })

    return {
        "as_of": str(model.as_of.date()),
        "source_status": dict(model.source_status),
        "factors": list(model.exposures.columns),
        "factor_covariance": {
            str(left): {str(right): round(float(value), 10) for right, value in row.items()}
            for left, row in model.factor_covariance.iterrows()
        },
        "securities": exposure_rows,
        "top_stock_correlations": pair_rows[: max(1, int(top_pairs))],
    }
