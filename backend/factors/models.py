"""Factor models.

Builds a small set of equity style factors from a price panel and runs OLS
factor-exposure regressions (alpha, betas, t-stats, R^2) per asset. Factors are
constructed cross-sectionally from the supplied universe so the module is fully
self-contained (no external factor-data feed required), while remaining
methodologically close to academic style factors (market / momentum / low-vol).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import statsmodels.api as sm


def build_factors(prices: pd.DataFrame, rf_annual: float = 0.0) -> pd.DataFrame:
    """Construct daily factor returns from a wide price panel.

    Returns a DataFrame with columns:
      - ``MKT``  : market (equal-weight universe) excess return
      - ``MOM``  : momentum, long top / short bottom by trailing 12-1m return
      - ``LOWVOL``: low-volatility minus high-volatility
    """
    rets = prices.pct_change().dropna()
    if rets.empty:
        raise ValueError("Not enough price history to build factors")

    rf_daily = rf_annual / 252.0

    # Market: equal-weighted mean of the universe.
    mkt = rets.mean(axis=1) - rf_daily

    # Momentum: trailing ~12m skip last month, ranked cross-sectionally.
    lookback = min(252, max(40, len(rets) // 2))
    skip = 21
    mom_signal = prices.pct_change(lookback).shift(skip)

    # Low-vol: trailing 60d realized vol (lower = preferred).
    vol_signal = rets.rolling(63).std()

    mom = _long_short(rets, mom_signal, high_is_long=True)
    lowvol = _long_short(rets, vol_signal, high_is_long=False)

    factors = pd.DataFrame({"MKT": mkt, "MOM": mom, "LOWVOL": lowvol}).dropna()
    return factors


def _long_short(rets: pd.DataFrame, signal: pd.DataFrame, high_is_long: bool) -> pd.Series:
    """Daily return of a dollar-neutral long-short portfolio.

    Each day, rank assets by ``signal`` (lagged one day to avoid look-ahead),
    go long the top tercile and short the bottom tercile.
    """
    signal = signal.shift(1).reindex(rets.index)
    out = pd.Series(0.0, index=rets.index)
    n_assets = rets.shape[1]
    k = max(1, n_assets // 3)

    for dt in rets.index:
        row = signal.loc[dt].dropna()
        if len(row) < 2:
            continue
        ranked = row.sort_values(ascending=not high_is_long)
        longs = ranked.index[:k]
        shorts = ranked.index[-k:]
        long_ret = rets.loc[dt, longs].mean()
        short_ret = rets.loc[dt, shorts].mean()
        out.loc[dt] = float(long_ret - short_ret)
    return out


def factor_regression(
    asset_returns: pd.Series, factors: pd.DataFrame, rf_annual: float = 0.0
) -> dict:
    """OLS of asset excess returns on factors. Returns alpha/betas/stats."""
    rf_daily = rf_annual / 252.0
    df = pd.concat([asset_returns.rename("y"), factors], axis=1).dropna()
    if len(df) < len(factors.columns) + 5:
        return {"error": "insufficient overlapping observations"}

    y = df["y"] - rf_daily
    X = sm.add_constant(df[factors.columns])
    model = sm.OLS(y, X).fit()

    betas = {name: round(float(model.params[name]), 6) for name in factors.columns}
    tstats = {name: round(float(model.tvalues[name]), 4) for name in factors.columns}

    return {
        "alpha_daily": round(float(model.params["const"]), 8),
        "alpha_annual": round(float(model.params["const"]) * 252, 6),
        "alpha_tstat": round(float(model.tvalues["const"]), 4),
        "betas": betas,
        "tstats": tstats,
        "r_squared": round(float(model.rsquared), 4),
        "r_squared_adj": round(float(model.rsquared_adj), 4),
        "n_obs": int(model.nobs),
    }


def analyze_universe(prices: pd.DataFrame, rf_annual: float = 0.0) -> dict:
    """Run factor regressions for every asset in the panel."""
    factors = build_factors(prices, rf_annual)
    rets = prices.pct_change().dropna()
    results = {}
    for sym in prices.columns:
        results[sym] = factor_regression(rets[sym], factors, rf_annual)
    return {
        "factors": list(factors.columns),
        "factor_means_annual": {
            c: round(float(factors[c].mean() * 252), 6) for c in factors.columns
        },
        "factor_corr": factors.corr().round(4).to_dict(),
        "assets": results,
    }
