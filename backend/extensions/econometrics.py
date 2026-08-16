"""Econometrics menu: regression, causality, cointegration, panel models.

Commands accept either a POSTed ``data`` table (list of row objects) or a
``symbol`` list, in which case the frame of daily returns is built for you.
Panel estimators need ``data`` because they require entity and time columns.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from .quantitative import series_frame

POST = ("POST",)


def _frame(
    data: Optional[List[Dict[str, Any]]],
    symbol: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
    target: str = "returns",
) -> pd.DataFrame:
    if data:
        df = pd.DataFrame(data)
        if df.empty:
            raise EmptyDataError("The supplied data table is empty")
        return df
    if not symbol:
        raise ValueError("Supply either a data table in the request body or symbol=")
    return series_frame(symbol, start_date, end_date, target)


def _numeric(df: pd.DataFrame) -> pd.DataFrame:
    numeric = df.select_dtypes(include=[np.number])
    if numeric.empty:
        raise EmptyDataError("No numeric columns to model")
    return numeric


def _xy(df: pd.DataFrame, y_column: Optional[str], x_columns: Optional[str]):
    numeric = _numeric(df)
    y_name = y_column or numeric.columns[0]
    if y_name not in numeric.columns:
        raise ValueError("y_column {!r} is not a numeric column. Have: {}".format(
            y_name, ", ".join(numeric.columns)))
    x_names = ([c.strip() for c in x_columns.split(",") if c.strip()] if x_columns
               else [c for c in numeric.columns if c != y_name])
    missing = [c for c in x_names if c not in numeric.columns]
    if missing:
        raise ValueError("Unknown x column(s): {}".format(", ".join(missing)))
    if not x_names:
        raise ValueError("Need at least one explanatory column")
    paired = numeric[[y_name] + x_names].dropna()
    if len(paired) <= len(x_names) + 1:
        raise EmptyDataError("Not enough complete rows to fit the regression")
    return paired[y_name], paired[x_names], y_name, x_names


# --------------------------------------------------------------------------- #
# Correlation
# --------------------------------------------------------------------------- #
@command("/econometrics/correlation_matrix", providers=("yahoo",), methods=POST,
         summary="Pairwise correlation matrix")
def correlation_matrix(data: Optional[List[Dict[str, Any]]] = None, symbol: Optional[str] = None,
                       start_date: Optional[str] = None, end_date: Optional[str] = None,
                       method: str = "pearson", target: str = "returns",
                       provider: Optional[str] = None) -> Result:
    """``method``: pearson, kendall or spearman."""
    src = resolve_provider(provider, ("yahoo",))
    df = _numeric(_frame(data, symbol, start_date, end_date, target))
    corr = df.corr(method=method)
    corr.index.name = "column"
    return Result(corr, provider=src, index_name="column")


@command("/econometrics/covariance_matrix", providers=("yahoo",), methods=POST,
         summary="Pairwise covariance matrix")
def covariance_matrix(data: Optional[List[Dict[str, Any]]] = None, symbol: Optional[str] = None,
                      start_date: Optional[str] = None, end_date: Optional[str] = None,
                      target: str = "returns", annualise: bool = False,
                      provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    df = _numeric(_frame(data, symbol, start_date, end_date, target))
    cov = df.cov() * (252 if annualise else 1)
    cov.index.name = "column"
    return Result(cov, provider=src, index_name="column")


# --------------------------------------------------------------------------- #
# OLS
# --------------------------------------------------------------------------- #
@command("/econometrics/ols_regression", providers=("yahoo",), methods=POST,
         summary="OLS coefficients with t-stats")
def ols_regression(data: Optional[List[Dict[str, Any]]] = None, symbol: Optional[str] = None,
                   y_column: Optional[str] = None, x_columns: Optional[str] = None,
                   start_date: Optional[str] = None, end_date: Optional[str] = None,
                   target: str = "returns", provider: Optional[str] = None) -> Result:
    import statsmodels.api as sm

    src = resolve_provider(provider, ("yahoo",))
    y, X, y_name, x_names = _xy(_frame(data, symbol, start_date, end_date, target),
                                y_column, x_columns)
    model = sm.OLS(y, sm.add_constant(X)).fit()
    rows = [
        {
            "term": term, "coefficient": float(model.params[term]),
            "std_error": float(model.bse[term]), "t_stat": float(model.tvalues[term]),
            "p_value": float(model.pvalues[term]),
            "ci_lower": float(model.conf_int().loc[term, 0]),
            "ci_upper": float(model.conf_int().loc[term, 1]),
        }
        for term in model.params.index
    ]
    return Result(
        rows, provider=src,
        extra={"dependent": y_name, "observations": int(model.nobs),
               "r_squared": float(model.rsquared), "adj_r_squared": float(model.rsquared_adj),
               "f_statistic": float(model.fvalue), "f_pvalue": float(model.f_pvalue),
               "aic": float(model.aic), "bic": float(model.bic),
               "durbin_watson": float(sm.stats.durbin_watson(model.resid))},
    )


@command("/econometrics/ols_regression_summary", providers=("yahoo",), methods=POST,
         summary="Full statsmodels OLS summary text")
def ols_regression_summary(data: Optional[List[Dict[str, Any]]] = None, symbol: Optional[str] = None,
                           y_column: Optional[str] = None, x_columns: Optional[str] = None,
                           start_date: Optional[str] = None, end_date: Optional[str] = None,
                           target: str = "returns", provider: Optional[str] = None) -> Result:
    import statsmodels.api as sm

    src = resolve_provider(provider, ("yahoo",))
    y, X, y_name, _ = _xy(_frame(data, symbol, start_date, end_date, target), y_column, x_columns)
    model = sm.OLS(y, sm.add_constant(X)).fit()
    return Result({"dependent": y_name, "summary": str(model.summary())}, provider=src)


@command("/econometrics/vif", providers=("yahoo",), methods=POST,
         summary="Variance inflation factors (multicollinearity)")
def vif(data: Optional[List[Dict[str, Any]]] = None, symbol: Optional[str] = None,
        x_columns: Optional[str] = None, start_date: Optional[str] = None,
        end_date: Optional[str] = None, target: str = "returns",
        provider: Optional[str] = None) -> Result:
    import statsmodels.api as sm
    from statsmodels.stats.outliers_influence import variance_inflation_factor

    src = resolve_provider(provider, ("yahoo",))
    df = _numeric(_frame(data, symbol, start_date, end_date, target))
    cols = [c.strip() for c in x_columns.split(",")] if x_columns else list(df.columns)
    X = sm.add_constant(df[cols].dropna())
    if X.shape[1] < 3:
        raise ValueError("VIF needs at least two explanatory columns")
    rows = [
        {"column": name, "vif": float(variance_inflation_factor(X.values, i))}
        for i, name in enumerate(X.columns) if name != "const"
    ]
    return Result(rows, provider=src)


# --------------------------------------------------------------------------- #
# Time-series tests
# --------------------------------------------------------------------------- #
@command("/econometrics/causality", providers=("yahoo",), methods=POST,
         summary="Granger causality test")
def causality(data: Optional[List[Dict[str, Any]]] = None, symbol: Optional[str] = None,
              y_column: Optional[str] = None, x_column: Optional[str] = None,
              lags: int = 5, start_date: Optional[str] = None, end_date: Optional[str] = None,
              target: str = "returns", provider: Optional[str] = None) -> Result:
    """Tests whether lags of ``x_column`` help predict ``y_column``."""
    from statsmodels.tsa.stattools import grangercausalitytests

    src = resolve_provider(provider, ("yahoo",))
    df = _numeric(_frame(data, symbol, start_date, end_date, target))
    y_name = y_column or df.columns[0]
    x_name = x_column or (df.columns[1] if len(df.columns) > 1 else None)
    if not x_name:
        raise ValueError("Need two columns: pass y_column and x_column")
    paired = df[[y_name, x_name]].dropna()
    if len(paired) < lags * 3 + 10:
        raise EmptyDataError("Need more observations than 3x the lag count")
    results = grangercausalitytests(paired, maxlag=lags)
    rows = []
    for lag, (tests, _) in results.items():
        f_stat, f_p = tests["ssr_ftest"][0], tests["ssr_ftest"][1]
        chi2, chi2_p = tests["ssr_chi2test"][0], tests["ssr_chi2test"][1]
        rows.append(
            {
                "lag": int(lag), "f_stat": float(f_stat), "f_p_value": float(f_p),
                "chi2_stat": float(chi2), "chi2_p_value": float(chi2_p),
                "causal_at_5pct": bool(f_p < 0.05),
            }
        )
    return Result(rows, provider=src, extra={"cause": x_name, "effect": y_name})


@command("/econometrics/cointegration", providers=("yahoo",), methods=POST,
         summary="Engle-Granger cointegration test for every pair")
def cointegration(data: Optional[List[Dict[str, Any]]] = None, symbol: Optional[str] = None,
                  start_date: Optional[str] = None, end_date: Optional[str] = None,
                  target: str = "close", provider: Optional[str] = None) -> Result:
    from statsmodels.tsa.stattools import coint

    src = resolve_provider(provider, ("yahoo",))
    df = _numeric(_frame(data, symbol, start_date, end_date, target)).dropna()
    cols = list(df.columns)
    if len(cols) < 2:
        raise ValueError("Cointegration needs at least two series")
    rows = []
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            stat, p_value, crit = coint(df[a], df[b])
            rows.append(
                {
                    "series_1": a, "series_2": b, "test_stat": float(stat),
                    "p_value": float(p_value), "critical_1pct": float(crit[0]),
                    "critical_5pct": float(crit[1]), "critical_10pct": float(crit[2]),
                    "cointegrated_at_5pct": bool(p_value < 0.05),
                }
            )
    return Result(rows, provider=src)


@command("/econometrics/unit_root", providers=("yahoo",), methods=POST,
         summary="Augmented Dickey-Fuller test per column")
def unit_root(data: Optional[List[Dict[str, Any]]] = None, symbol: Optional[str] = None,
              start_date: Optional[str] = None, end_date: Optional[str] = None,
              target: str = "close", regression: str = "c",
              provider: Optional[str] = None) -> Result:
    from statsmodels.tsa.stattools import adfuller

    src = resolve_provider(provider, ("yahoo",))
    df = _numeric(_frame(data, symbol, start_date, end_date, target))
    rows = []
    for col in df.columns:
        s = df[col].dropna()
        if s.size < 20:
            continue
        stat, p, lags, nobs, crit, _ = adfuller(s, regression=regression)
        rows.append(
            {
                "column": col, "adf_stat": float(stat), "p_value": float(p), "lags": int(lags),
                "observations": int(nobs), "critical_5pct": float(crit["5%"]),
                "stationary_at_5pct": bool(p < 0.05),
            }
        )
    if not rows:
        raise EmptyDataError("Not enough observations for a unit-root test")
    return Result(rows, provider=src)


@command("/econometrics/autocorrelation", providers=("yahoo",), methods=POST,
         summary="Ljung-Box, Durbin-Watson and Breusch-Godfrey diagnostics")
def autocorrelation(data: Optional[List[Dict[str, Any]]] = None, symbol: Optional[str] = None,
                    y_column: Optional[str] = None, x_columns: Optional[str] = None,
                    lags: int = 10, start_date: Optional[str] = None,
                    end_date: Optional[str] = None, target: str = "returns",
                    provider: Optional[str] = None) -> Result:
    """Durbin-Watson and Breusch-Godfrey run on the residuals of an OLS fit;
    Ljung-Box runs on the dependent series itself."""
    import statsmodels.api as sm
    from statsmodels.stats.diagnostic import acorr_breusch_godfrey, acorr_ljungbox

    src = resolve_provider(provider, ("yahoo",))
    df = _frame(data, symbol, start_date, end_date, target)
    y, X, y_name, _ = _xy(df, y_column, x_columns)
    model = sm.OLS(y, sm.add_constant(X)).fit()
    lb = acorr_ljungbox(y, lags=[lags], return_df=True)
    bg_stat, bg_p, bg_f, bg_fp = acorr_breusch_godfrey(model, nlags=lags)
    return Result(
        {
            "dependent": y_name, "lags": lags,
            "durbin_watson": float(sm.stats.durbin_watson(model.resid)),
            "ljung_box_stat": float(lb["lb_stat"].iloc[0]),
            "ljung_box_p": float(lb["lb_pvalue"].iloc[0]),
            "breusch_godfrey_lm": float(bg_stat), "breusch_godfrey_p": float(bg_p),
            "breusch_godfrey_f": float(bg_f), "breusch_godfrey_f_p": float(bg_fp),
            "residual_autocorrelation_at_5pct": bool(bg_p < 0.05),
        },
        provider=src,
    )


# --------------------------------------------------------------------------- #
# Panel models
# --------------------------------------------------------------------------- #
def _panel_inputs(data: List[Dict[str, Any]], entity_column: str, time_column: str,
                  y_column: str, x_columns: Optional[str]):
    if not data:
        raise ValueError("Panel models need a data table with entity and time columns")
    df = pd.DataFrame(data)
    for col in (entity_column, time_column, y_column):
        if col not in df.columns:
            raise ValueError("Column {!r} is missing from the data".format(col))
    x_names = ([c.strip() for c in x_columns.split(",") if c.strip()] if x_columns
               else [c for c in df.select_dtypes(include=[np.number]).columns
                     if c not in (y_column, time_column, entity_column)])
    if not x_names:
        raise ValueError("Need at least one explanatory column")
    frame = df[[entity_column, time_column, y_column] + x_names].dropna()
    if frame.empty:
        raise EmptyDataError("No complete rows in the panel")
    return frame, x_names


def _ols_rows(y: pd.Series, X: pd.DataFrame, label: str, extra: Dict[str, Any]) -> Result:
    import statsmodels.api as sm

    model = sm.OLS(y, sm.add_constant(X, has_constant="add")).fit()
    rows = [
        {"term": t, "coefficient": float(model.params[t]), "std_error": float(model.bse[t]),
         "t_stat": float(model.tvalues[t]), "p_value": float(model.pvalues[t])}
        for t in model.params.index
    ]
    return Result(rows, provider="yahoo",
                  extra=dict(model=label, observations=int(model.nobs),
                             r_squared=float(model.rsquared), **extra))


_PANEL_MODELS = ("pooled", "fixed", "between", "first_difference", "fama_macbeth")


@command("/econometrics/panel", providers=("yahoo",), methods=POST,
         summary="Panel regression: pooled, fixed effects, between, first difference, Fama-MacBeth")
def panel(data: List[Dict[str, Any]], y_column: str, entity_column: str = "entity",
          time_column: str = "date", x_columns: Optional[str] = None,
          model: str = "fixed", provider: Optional[str] = None) -> Result:
    """Estimated directly with OLS on the appropriate transform.

    * ``pooled`` — OLS ignoring the panel structure.
    * ``fixed`` — within (entity-demeaned) estimator.
    * ``between`` — OLS on entity means.
    * ``first_difference`` — OLS on within-entity first differences.
    * ``fama_macbeth`` — cross-sectional regressions per period, averaged, with
      Fama-MacBeth standard errors.
    """
    resolve_provider(provider, ("yahoo",))
    if model not in _PANEL_MODELS:
        raise ValueError("model must be one of {}".format(", ".join(_PANEL_MODELS)))
    frame, x_names = _panel_inputs(data, entity_column, time_column, y_column, x_columns)
    entities = frame[entity_column].nunique()
    periods = frame[time_column].nunique()
    meta = {"entities": int(entities), "periods": int(periods)}

    if model == "pooled":
        return _ols_rows(frame[y_column], frame[x_names], "pooled", meta)

    if model == "between":
        means = frame.groupby(entity_column)[[y_column] + x_names].mean()
        return _ols_rows(means[y_column], means[x_names], "between", meta)

    if model == "fixed":
        grouped = frame.groupby(entity_column)
        demeaned = frame[[y_column] + x_names] - grouped[[y_column] + x_names].transform("mean")
        return _ols_rows(demeaned[y_column], demeaned[x_names], "fixed effects (within)", meta)

    if model == "first_difference":
        ordered = frame.sort_values([entity_column, time_column])
        diffs = ordered.groupby(entity_column)[[y_column] + x_names].diff().dropna()
        if diffs.empty:
            raise EmptyDataError("Every entity has a single observation — cannot difference")
        return _ols_rows(diffs[y_column], diffs[x_names], "first difference", meta)

    # Fama-MacBeth: one cross-sectional regression per period, then average.
    import statsmodels.api as sm

    per_period: List[pd.Series] = []
    for _, chunk in frame.groupby(time_column):
        if len(chunk) <= len(x_names) + 1:
            continue
        fit = sm.OLS(chunk[y_column], sm.add_constant(chunk[x_names], has_constant="add")).fit()
        per_period.append(fit.params)
    if len(per_period) < 2:
        raise EmptyDataError("Need at least two periods with enough cross-sectional observations")
    coefs = pd.DataFrame(per_period)
    n = len(coefs)
    rows = []
    for term in coefs.columns:
        mean = float(coefs[term].mean())
        se = float(coefs[term].std(ddof=1) / np.sqrt(n))
        rows.append({"term": term, "coefficient": mean, "std_error": se,
                     "t_stat": (mean / se) if se else None,
                     "p_value": None})
    return Result(rows, provider="yahoo",
                  extra=dict(model="fama-macbeth", cross_sections=int(n), **meta))
