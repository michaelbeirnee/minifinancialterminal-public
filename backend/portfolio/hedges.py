"""Hedge-construction analytics — exposure estimators.

Step 1 of docs/hedge-construction.md: turn a portfolio snapshot into the three
measured exposures a hedge can target — beta-dollars, single-name
concentration, and tail loss — each with the uncertainty of the estimate, so
downstream sizing never treats a noisy number as exact.

Everything here is a pure function of series and rows already in the
snapshot: no fetching, no session state, deterministic (the bootstrap is
seeded), offline-testable.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
from scipy import stats as sps

from . import analytics

TRADING_DAYS = 252

#: Stamped on every /hedge response so a stored hedge decision records which
#: estimator produced its numbers (see the lifecycle-log design).
ESTIMATOR_VERSION = "exposures-v1"

#: Fewest paired daily observations either estimator will accept.
MIN_OBSERVATIONS = 30

#: A single name at or above this share of portfolio risk is flagged as a
#: concentration hedge target.
DOMINANT_RISK_SHARE = 0.25


def market_exposure(
    returns: pd.Series,
    benchmark_returns: Optional[pd.Series],
    value: float,
) -> Optional[Dict[str, Any]]:
    """Linear market exposure: beta vs the benchmark, in dollars, with error.

    OLS of daily portfolio returns on daily benchmark returns. ``beta_dollars``
    is the notional a linear hedge (short futures / short ETF) would offset;
    the confidence interval is the slope's ±1.96·SE, propagated to dollars.
    Returns None when there is no benchmark series or too little overlap —
    absence, not a guess.
    """
    if benchmark_returns is None:
        return None
    paired = pd.concat(
        [returns.rename("p"), benchmark_returns.rename("b")], axis=1
    ).dropna()
    if len(paired) < MIN_OBSERVATIONS or not float(paired["b"].std(ddof=1)):
        return None

    fit = sps.linregress(paired["b"], paired["p"])
    beta, se = float(fit.slope), float(fit.stderr)
    half = 1.96 * se
    active = paired["p"] - paired["b"]
    tracking_error = float(active.std(ddof=1) * np.sqrt(TRADING_DAYS))
    return {
        "observations": int(len(paired)),
        "beta": round(beta, 4),
        "beta_se": round(se, 4),
        "beta_ci95": [round(beta - half, 4), round(beta + half, 4)],
        "correlation": round(float(fit.rvalue), 4),
        "r_squared": round(float(fit.rvalue) ** 2, 4),
        "tracking_error_annual": round(tracking_error, 6),
        "beta_dollars": round(beta * value, 2),
        "beta_dollars_ci95": [
            round((beta - half) * value, 2),
            round((beta + half) * value, 2),
        ],
    }


def tail_loss(
    returns: pd.Series,
    value: float,
    var_level: float = 0.05,
    horizon_days: int = 21,
    bootstrap_draws: int = 1000,
    seed: int = 0,
) -> Optional[Dict[str, Any]]:
    """Historical VaR/CVaR of the book in dollars, with sampling error.

    Daily tail statistics scaled by √horizon (the same convention ``/risk``
    uses), negative = loss. The bootstrap CI is the point of this function:
    a CVaR estimated from a few hundred days is noisy, and hedge sizing must
    see that width rather than a single exact-looking number. IID resampling
    ignores autocorrelation — a named approximation, disclosed in the output.
    """
    clean = returns.dropna()
    n = int(len(clean))
    if n < MIN_OBSERVATIONS:
        return None

    scale = float(np.sqrt(horizon_days))
    var_daily = float(clean.quantile(var_level))
    tail = clean[clean <= var_daily]
    cvar_daily = float(tail.mean()) if len(tail) else var_daily

    out: Dict[str, Any] = {
        "observations": n,
        "confidence": round(1 - var_level, 4),
        "horizon_days": horizon_days,
        "var_pct": round(var_daily * scale, 6),
        "var_amount": round(var_daily * scale * value, 2),
        "cvar_pct": round(cvar_daily * scale, 6),
        "cvar_amount": round(cvar_daily * scale * value, 2),
        "method": "historical daily tail × sqrt(horizon); negative = loss",
    }

    if bootstrap_draws:
        rng = np.random.default_rng(seed)
        arr = clean.to_numpy()
        samples = rng.choice(arr, size=(bootstrap_draws, n), replace=True)
        quantiles = np.quantile(samples, var_level, axis=1)
        mask = samples <= quantiles[:, None]
        cvars = (samples * mask).sum(axis=1) / mask.sum(axis=1)
        low, high = (float(q) for q in np.quantile(cvars, [0.025, 0.975]))
        out["cvar_pct_ci95"] = [round(low * scale, 6), round(high * scale, 6)]
        out["cvar_amount_ci95"] = [
            round(low * scale * value, 2),
            round(high * scale * value, 2),
        ]
        out["bootstrap"] = {
            "draws": bootstrap_draws,
            "seed": seed,
            "method": "iid resample of daily returns",
        }
    return out


def concentration_exposure(
    rows: Sequence[Dict[str, Any]], panel: pd.DataFrame
) -> Dict[str, Any]:
    """Which single names a collar or per-name put would actually be for.

    Reuses the exact concentration and risk-contribution decompositions the
    ``/risk`` endpoint reports, and flags any position carrying at least
    ``DOMINANT_RISK_SHARE`` of portfolio risk as a hedge target.
    """
    summary = analytics.concentration(rows)
    contributions = analytics.risk_contribution(rows, panel)
    positions: List[Dict[str, Any]] = []
    values = {r["symbol"]: r["market_value"] for r in rows}
    for contribution in contributions[:5]:
        symbol = contribution["symbol"]
        positions.append(
            {
                "symbol": symbol,
                "market_value": round(float(values.get(symbol, 0.0)), 2),
                "weight_of_gross": contribution["weight"],
                "pct_of_risk": contribution["pct_of_risk"],
                "dominant": contribution["pct_of_risk"] >= DOMINANT_RISK_SHARE,
            }
        )
    return {
        **summary,
        "dominant_threshold": DOMINANT_RISK_SHARE,
        "positions": positions,
    }
