"""Quantitative menu: distribution tests, rolling statistics, risk metrics, CAPM."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd
from scipy import stats as sps

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import date_window, norm_symbols
from ..providers import yahoo

TRADING_DAYS = 252


def series_frame(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    target: str = "returns",
) -> pd.DataFrame:
    """Wide frame (columns = symbols) of prices, simple returns or log returns."""
    symbols = norm_symbols(symbol)
    start, end = date_window(start_date, end_date)
    panel = yahoo.close_panel(symbols, str(start), str(end))
    if target == "close":
        return panel.dropna(how="all")
    if target == "log_returns":
        return np.log(panel / panel.shift(1)).dropna(how="all")
    if target == "returns":
        return panel.pct_change().dropna(how="all")
    raise ValueError("target must be returns, log_returns or close")


def _long(df: pd.DataFrame, value_name: str) -> List[Dict[str, Any]]:
    return [{"symbol": col, value_name: None if pd.isna(v) else float(v)} for col, v in df.items()]


# --------------------------------------------------------------------------- #
# Descriptive statistics
# --------------------------------------------------------------------------- #
@command("/quantitative/summary", providers=("yahoo",), summary="Full descriptive statistics")
def summary(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
            target: str = "returns", provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    df = series_frame(symbol, start_date, end_date, target)
    rows = []
    for col in df.columns:
        s = df[col].dropna()
        if s.empty:
            continue
        rows.append(
            {
                "symbol": col, "observations": int(s.size), "mean": float(s.mean()),
                "std": float(s.std(ddof=1)), "variance": float(s.var(ddof=1)),
                "skew": float(sps.skew(s)), "kurtosis": float(sps.kurtosis(s)),
                "min": float(s.min()), "p5": float(s.quantile(0.05)), "median": float(s.median()),
                "p95": float(s.quantile(0.95)), "max": float(s.max()),
                "annualised_mean": float(s.mean() * TRADING_DAYS),
                "annualised_volatility": float(s.std(ddof=1) * np.sqrt(TRADING_DAYS)),
            }
        )
    if not rows:
        raise EmptyDataError("No observations to summarise")
    return Result(rows, provider=src)


_STATS: Dict[str, Callable[[pd.Series], float]] = {
    "mean": lambda s: float(s.mean()),
    "std": lambda s: float(s.std(ddof=1)),
    "var": lambda s: float(s.var(ddof=1)),
    "skew": lambda s: float(sps.skew(s)),
    "kurtosis": lambda s: float(sps.kurtosis(s)),
}


def _make_stat(name: str, func: Callable[[pd.Series], float]):
    def fn(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
           target: str = "returns", provider: Optional[str] = None) -> Result:
        src = resolve_provider(provider, ("yahoo",))
        df = series_frame(symbol, start_date, end_date, target)
        return Result([{"symbol": c, name: func(df[c].dropna())} for c in df.columns], provider=src)

    fn.__name__ = "stats_" + name
    return fn


for _name, _func in _STATS.items():
    command("/quantitative/stats/" + _name, providers=("yahoo",),
            summary="Sample {} of the series".format(_name))(_make_stat(_name, _func))


@command("/quantitative/stats/quantile", providers=("yahoo",), summary="Sample quantile")
def stats_quantile(symbol: str, quantile: float = 0.05, start_date: Optional[str] = None,
                   end_date: Optional[str] = None, target: str = "returns",
                   provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    df = series_frame(symbol, start_date, end_date, target)
    return Result([{"symbol": c, "quantile": quantile, "value": float(df[c].dropna().quantile(quantile))}
                   for c in df.columns], provider=src)


# --------------------------------------------------------------------------- #
# Rolling statistics
# --------------------------------------------------------------------------- #
_ROLLING: Dict[str, str] = {"mean": "mean", "std": "std", "var": "var", "skew": "skew",
                            "kurtosis": "kurt"}


def _make_rolling(name: str, method: str):
    def fn(symbol: str, window: int = 21, start_date: Optional[str] = None,
           end_date: Optional[str] = None, target: str = "returns", limit: int = 500,
           provider: Optional[str] = None) -> Result:
        src = resolve_provider(provider, ("yahoo",))
        df = series_frame(symbol, start_date, end_date, target)
        rolled = getattr(df.rolling(window), method)()
        rolled = rolled.dropna(how="all").tail(limit)
        if rolled.empty:
            raise EmptyDataError("Window of {} is longer than the available history".format(window))
        return Result(rolled, provider=src, index_name="date")

    fn.__name__ = "rolling_" + name
    return fn


for _name, _method in _ROLLING.items():
    command("/quantitative/rolling/" + _name, providers=("yahoo",),
            summary="Rolling {}".format(_name))(_make_rolling(_name, _method))


@command("/quantitative/rolling/quantile", providers=("yahoo",), summary="Rolling quantile")
def rolling_quantile(symbol: str, window: int = 21, quantile: float = 0.05,
                     start_date: Optional[str] = None, end_date: Optional[str] = None,
                     target: str = "returns", limit: int = 500,
                     provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    df = series_frame(symbol, start_date, end_date, target)
    out = df.rolling(window).quantile(quantile).dropna(how="all").tail(limit)
    if out.empty:
        raise EmptyDataError("Window of {} is longer than the available history".format(window))
    return Result(out, provider=src, index_name="date")


# --------------------------------------------------------------------------- #
# Normality
# --------------------------------------------------------------------------- #
@command("/quantitative/normality", providers=("yahoo",), summary="Battery of normality tests")
def normality(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
              target: str = "returns", provider: Optional[str] = None) -> Result:
    """Jarque-Bera, Shapiro-Wilk, Kolmogorov-Smirnov, D'Agostino, plus moments."""
    src = resolve_provider(provider, ("yahoo",))
    df = series_frame(symbol, start_date, end_date, target)
    rows = []
    for col in df.columns:
        s = df[col].dropna()
        if s.size < 8:
            continue
        jb_stat, jb_p = sps.jarque_bera(s)
        sw_stat, sw_p = sps.shapiro(s.iloc[:5000])
        # Standardise before the KS test rather than passing loc/scale through
        # `args`: scipy now dispatches "norm" to a CDF that takes no parameters.
        sigma = s.std(ddof=1)
        ks_stat, ks_p = sps.kstest((s - s.mean()) / sigma, "norm") if sigma else (float("nan"),) * 2
        da_stat, da_p = sps.normaltest(s)
        rows.append(
            {
                "symbol": col, "observations": int(s.size),
                "skew": float(sps.skew(s)), "kurtosis": float(sps.kurtosis(s)),
                "jarque_bera_stat": float(jb_stat), "jarque_bera_p": float(jb_p),
                "shapiro_stat": float(sw_stat), "shapiro_p": float(sw_p),
                "kolmogorov_stat": float(ks_stat), "kolmogorov_p": float(ks_p),
                "dagostino_stat": float(da_stat), "dagostino_p": float(da_p),
                "normal_at_5pct": bool(jb_p > 0.05),
            }
        )
    if not rows:
        raise EmptyDataError("Not enough observations for normality testing")
    return Result(rows, provider=src)


# --------------------------------------------------------------------------- #
# Unit root
# --------------------------------------------------------------------------- #
@command("/quantitative/unitroot", providers=("yahoo",), summary="ADF and KPSS stationarity tests")
def unitroot(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
             target: str = "close", regression: str = "c", provider: Optional[str] = None) -> Result:
    """ADF null = unit root (non-stationary); KPSS null = stationary."""
    from statsmodels.tsa.stattools import adfuller, kpss

    src = resolve_provider(provider, ("yahoo",))
    df = series_frame(symbol, start_date, end_date, target)
    rows = []
    for col in df.columns:
        s = df[col].dropna()
        if s.size < 20:
            continue
        adf_stat, adf_p, adf_lags, adf_n, adf_crit, _ = adfuller(s, regression=regression)
        kpss_stat, kpss_p, kpss_lags, kpss_crit = kpss(s, regression=regression, nlags="auto")
        rows.append(
            {
                "symbol": col, "adf_stat": float(adf_stat), "adf_p": float(adf_p),
                "adf_lags": int(adf_lags), "adf_critical_5pct": float(adf_crit["5%"]),
                "adf_stationary_at_5pct": bool(adf_p < 0.05),
                "kpss_stat": float(kpss_stat), "kpss_p": float(kpss_p),
                "kpss_critical_5pct": float(kpss_crit["5%"]),
                "kpss_stationary_at_5pct": bool(kpss_p > 0.05),
            }
        )
    if not rows:
        raise EmptyDataError("Not enough observations for unit-root testing")
    return Result(rows, provider=src)


# --------------------------------------------------------------------------- #
# CAPM & risk
# --------------------------------------------------------------------------- #
@command("/quantitative/capm", providers=("yahoo",), summary="CAPM alpha, beta and R-squared")
def capm(symbol: str, benchmark: str = "SPY", start_date: Optional[str] = None,
         end_date: Optional[str] = None, risk_free_rate: float = 0.0,
         provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    symbols = norm_symbols(symbol)
    df = series_frame(",".join(symbols + [benchmark.upper()]), start_date, end_date, "returns")
    bench_col = benchmark.upper()
    if bench_col not in df.columns:
        raise EmptyDataError("No return data for benchmark {}".format(benchmark))
    daily_rf = risk_free_rate / TRADING_DAYS
    bench_excess = df[bench_col] - daily_rf
    rows = []
    for col in symbols:
        if col not in df.columns or col == bench_col:
            continue
        paired = pd.concat([df[col] - daily_rf, bench_excess], axis=1).dropna()
        if len(paired) < 20:
            continue
        y, x = paired.iloc[:, 0], paired.iloc[:, 1]
        slope, intercept, r, p, stderr = sps.linregress(x, y)
        rows.append(
            {
                "symbol": col, "benchmark": bench_col, "observations": int(len(paired)),
                "alpha_daily": float(intercept), "alpha_annualised": float(intercept * TRADING_DAYS),
                "beta": float(slope), "beta_stderr": float(stderr), "r_squared": float(r**2),
                "p_value": float(p),
                "correlation": float(r),
                "tracking_error": float((y - x).std(ddof=1) * np.sqrt(TRADING_DAYS)),
            }
        )
    if not rows:
        raise EmptyDataError("Not enough overlapping observations to estimate CAPM")
    return Result(rows, provider=src)


def _drawdown(returns: pd.Series) -> pd.Series:
    curve = (1 + returns.fillna(0)).cumprod()
    return curve / curve.cummax() - 1


def risk_metrics(returns: pd.Series, risk_free_rate: float = 0.0,
                 var_level: float = 0.05) -> Dict[str, Any]:
    """Risk/return statistics for one return stream.

    Kept apart from the command below because a return series does not have to
    come from a ticker: the portfolio layer feeds its own time-weighted returns
    through exactly this function, so a holdings-level Sharpe and a symbol-level
    Sharpe are computed by the same code.
    """
    s = returns.dropna()
    if s.size < 2:
        return {}
    daily_rf = risk_free_rate / TRADING_DAYS
    excess = s - daily_rf
    downside = excess[excess < 0]
    dd = _drawdown(s)
    max_dd = float(dd.min())
    ann_return = float((1 + s).prod() ** (TRADING_DAYS / len(s)) - 1)
    ann_vol = float(s.std(ddof=1) * np.sqrt(TRADING_DAYS))
    var = float(s.quantile(var_level))
    gains = excess.clip(lower=0).sum()
    losses = -excess.clip(upper=0).sum()
    return {
        "observations": int(s.size),
        "total_return": float((1 + s).prod() - 1),
        "cagr": ann_return,
        "annualised_volatility": ann_vol,
        "sharpe": float(excess.mean() / s.std(ddof=1) * np.sqrt(TRADING_DAYS))
        if s.std(ddof=1) else None,
        "sortino": float(excess.mean() / downside.std(ddof=1) * np.sqrt(TRADING_DAYS))
        if len(downside) > 1 and downside.std(ddof=1) else None,
        "calmar": float(ann_return / abs(max_dd)) if max_dd else None,
        "omega": float(gains / losses) if losses else None,
        "max_drawdown": max_dd,
        "ulcer_index": float(np.sqrt((dd**2).mean())),
        "value_at_risk": var,
        "conditional_var": float(s[s <= var].mean()) if (s <= var).any() else None,
        "win_rate": float((s > 0).mean()),
        "skew": float(sps.skew(s)),
        "kurtosis": float(sps.kurtosis(s)),
    }


@command("/quantitative/performance", providers=("yahoo",),
         summary="Risk/return metrics: Sharpe, Sortino, Calmar, Omega, VaR, drawdown")
def performance(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
                risk_free_rate: float = 0.0, var_level: float = 0.05,
                provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    df = series_frame(symbol, start_date, end_date, "returns")
    rows = []
    for col in df.columns:
        s = df[col].dropna()
        if s.size < 20:
            continue
        rows.append({"symbol": col, **risk_metrics(s, risk_free_rate, var_level)})
    if not rows:
        raise EmptyDataError("Not enough observations to compute performance metrics")
    return Result(rows, provider=src)


@command("/quantitative/drawdown", providers=("yahoo",), summary="Drawdown series")
def drawdown(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
             limit: int = 1000, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    df = series_frame(symbol, start_date, end_date, "returns")
    out = pd.DataFrame({col: _drawdown(df[col]) for col in df.columns}).tail(limit)
    return Result(out, provider=src, index_name="date")
