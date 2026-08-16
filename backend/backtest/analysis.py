"""Backtest analysis toolkit.

Everything here consumes the same inputs as :func:`~backend.backtest.engine.run_backtest`
(a close-price panel plus a strategy name/params) or its outputs (a daily net
return series), and layers research workflows on top of the engines:

* :func:`sweep` — grid-search strategy parameters on the vectorized engine.
* :func:`walk_forward` — rolling train/test evaluation with a purge gap
  between fit and evaluation windows; re-picks parameters each fold and
  stitches the out-of-sample equity curve.
* :func:`benchmark_attribution` — CAPM alpha/beta, tracking error,
  information ratio and up/down capture against a benchmark return series.
* :func:`cost_sensitivity` — re-run one configuration across a ladder of
  cost assumptions to see how quickly the edge decays.
* :func:`monte_carlo` — block-bootstrap the realized daily returns to put
  confidence bands on terminal wealth and drawdown.
"""
from __future__ import annotations

from itertools import product

import numpy as np
import pandas as pd

from ..reports.generator import compute_metrics
from .engine import CostModel, VectorizedBacktester
from .strategies import get_strategy

TRADING_DAYS = 252

#: Upper bound on grid size so a single request can't pin the server.
MAX_SWEEP_COMBOS = 200

_SUMMARY_KEYS = ("sharpe", "sortino", "total_return", "cagr", "max_drawdown", "annual_volatility")


def _expand_grid(param_grid: dict) -> list[dict]:
    """{'fast': [10, 20], 'slow': 50} -> [{'fast': 10, 'slow': 50}, ...]"""
    if not param_grid:
        return [{}]
    keys = sorted(param_grid)
    value_lists = [
        list(v) if isinstance(v, (list, tuple)) else [v] for v in (param_grid[k] for k in keys)
    ]
    combos = [dict(zip(keys, values)) for values in product(*value_lists)]
    if len(combos) > MAX_SWEEP_COMBOS:
        raise ValueError(
            f"Parameter grid expands to {len(combos)} combinations; max is {MAX_SWEEP_COMBOS}."
        )
    return combos


def _summarize(metrics: dict) -> dict:
    return {k: metrics.get(k) for k in _SUMMARY_KEYS if k in metrics}


def sweep(
    prices: pd.DataFrame,
    strategy: str,
    param_grid: dict,
    metric: str = "sharpe",
    commission_bps: float = 1.0,
    slippage_bps: float = 2.0,
    initial_capital: float = 100_000.0,
) -> dict:
    """Run ``strategy`` once per point of ``param_grid``, ranked by ``metric``.

    Uses the vectorized engine throughout — this is the parameter-sweeping
    workflow its docstring promises.
    """
    combos = _expand_grid(param_grid)
    strategy_fn = get_strategy(strategy)
    backtester = VectorizedBacktester(
        CostModel(commission_bps=commission_bps, slippage_bps=slippage_bps), initial_capital
    )

    results = []
    for params in combos:
        res = backtester.run(prices, strategy_fn(prices, params))
        results.append(
            {
                "params": params,
                "metrics": _summarize(res.metrics),
                "turnover": round(res.turnover, 4),
                "total_costs": round(res.total_costs, 2),
            }
        )

    results.sort(key=lambda r: r["metrics"].get(metric) or float("-inf"), reverse=True)
    return {
        "strategy": strategy,
        "metric": metric,
        "num_combinations": len(results),
        "best": results[0] if results else None,
        "results": results,
    }


def walk_forward(
    prices: pd.DataFrame,
    strategy: str,
    params: dict | None = None,
    param_grid: dict | None = None,
    train_days: int = 252,
    test_days: int = 63,
    purge_days: int = 5,
    metric: str = "sharpe",
    commission_bps: float = 1.0,
    slippage_bps: float = 2.0,
    initial_capital: float = 100_000.0,
) -> dict:
    """Rolling out-of-sample evaluation.

    Each fold fits on ``train_days`` bars, skips ``purge_days`` bars (an
    embargo so a signal's lookback can't straddle the fit/evaluate boundary),
    then evaluates on the next ``test_days`` bars. With a ``param_grid``, the
    grid point that maximizes ``metric`` in-sample is what trades out of
    sample; otherwise the fixed ``params`` are used and the folds simply
    measure stability. Test-window returns are stitched into one
    out-of-sample equity curve. Signals inside a fold see only that fold's
    window, so per-fold turnover at the boundary is approximate.
    """
    prices = prices.sort_index()
    fold_span = train_days + purge_days + test_days
    if len(prices) < fold_span:
        raise ValueError(
            f"Need at least {fold_span} bars for one fold "
            f"(train {train_days} + purge {purge_days} + test {test_days}); got {len(prices)}."
        )

    params = params or {}
    strategy_fn = get_strategy(strategy)
    backtester = VectorizedBacktester(
        CostModel(commission_bps=commission_bps, slippage_bps=slippage_bps), initial_capital
    )

    folds = []
    oos_returns: list[pd.Series] = []
    start = 0
    while start + fold_span <= len(prices):
        train_slice = prices.iloc[start : start + train_days]
        window = prices.iloc[start : start + fold_span]
        test_index = window.index[train_days + purge_days :]

        if param_grid:
            fitted = sweep(
                train_slice,
                strategy,
                param_grid,
                metric=metric,
                commission_bps=commission_bps,
                slippage_bps=slippage_bps,
                initial_capital=initial_capital,
            )["best"]
            fold_params = fitted["params"]
            train_metric = fitted["metrics"].get(metric)
        else:
            fold_params = params
            train_res = backtester.run(train_slice, strategy_fn(train_slice, fold_params))
            train_metric = train_res.metrics.get(metric)

        window_res = backtester.run(window, strategy_fn(window, fold_params))
        test_returns = window_res.returns.loc[test_index]
        test_metrics = compute_metrics(initial_capital * (1 + test_returns).cumprod())

        folds.append(
            {
                "train_start": train_slice.index[0].date().isoformat(),
                "train_end": train_slice.index[-1].date().isoformat(),
                "test_start": test_index[0].date().isoformat(),
                "test_end": test_index[-1].date().isoformat(),
                "params": fold_params,
                f"train_{metric}": train_metric,
                f"test_{metric}": test_metrics.get(metric),
            }
        )
        oos_returns.append(test_returns)
        start += test_days

    stitched = pd.concat(oos_returns)
    oos_equity = initial_capital * (1 + stitched).cumprod()
    return {
        "strategy": strategy,
        "num_folds": len(folds),
        "train_days": train_days,
        "test_days": test_days,
        "purge_days": purge_days,
        "folds": folds,
        "oos_metrics": compute_metrics(oos_equity),
        "oos_equity_curve": {
            "dates": [d.date().isoformat() for d in oos_equity.index],
            "values": [round(float(v), 4) for v in oos_equity.values],
        },
    }


def benchmark_attribution(
    returns: pd.Series,
    benchmark_returns: pd.Series,
    rf_annual: float = 0.0,
    periods_per_year: int = TRADING_DAYS,
) -> dict:
    """Benchmark-relative statistics for a daily return series."""
    joined = pd.concat([returns, benchmark_returns], axis=1, join="inner").dropna()
    if len(joined) < 20:
        return {"error": "insufficient overlapping data"}
    r = joined.iloc[:, 0]
    b = joined.iloc[:, 1]
    rf_period = rf_annual / periods_per_year

    bench_var = float(b.var(ddof=1))
    beta = float(r.cov(b) / bench_var) if bench_var > 0 else 0.0
    alpha_annual = float(
        ((r.mean() - rf_period) - beta * (b.mean() - rf_period)) * periods_per_year
    )

    active = r - b
    tracking_error = float(active.std(ddof=1) * np.sqrt(periods_per_year))
    information_ratio = (
        float(active.mean() * periods_per_year / tracking_error) if tracking_error > 0 else 0.0
    )

    up, down = b > 0, b < 0
    up_capture = float(r[up].mean() / b[up].mean()) if up.any() and b[up].mean() != 0 else 0.0
    down_capture = (
        float(r[down].mean() / b[down].mean()) if down.any() and b[down].mean() != 0 else 0.0
    )

    return {
        "beta": round(beta, 4),
        "alpha_annual": round(alpha_annual, 6),
        "correlation": round(float(r.corr(b)), 4),
        "tracking_error": round(tracking_error, 6),
        "information_ratio": round(information_ratio, 4),
        "active_return_annual": round(float(active.mean() * periods_per_year), 6),
        "up_capture": round(up_capture, 4),
        "down_capture": round(down_capture, 4),
        "num_periods": int(len(joined)),
    }


def cost_sensitivity(
    prices: pd.DataFrame,
    strategy: str,
    params: dict | None = None,
    multipliers: tuple[float, ...] = (0.0, 0.5, 1.0, 2.0, 4.0),
    commission_bps: float = 1.0,
    slippage_bps: float = 2.0,
    initial_capital: float = 100_000.0,
) -> dict:
    """Re-run one configuration with costs scaled by each multiplier.

    Signals don't depend on costs, so the weight panel is built once and only
    the cost pass is repeated.
    """
    params = params or {}
    weights = get_strategy(strategy)(prices, params)

    levels = []
    for m in multipliers:
        model = CostModel(commission_bps=commission_bps * m, slippage_bps=slippage_bps * m)
        res = VectorizedBacktester(model, initial_capital).run(prices, weights)
        levels.append(
            {
                "multiplier": m,
                "commission_bps": round(model.commission_bps, 4),
                "slippage_bps": round(model.slippage_bps, 4),
                "metrics": _summarize(res.metrics),
                "total_costs": round(res.total_costs, 2),
            }
        )
    return {
        "strategy": strategy,
        "params": params,
        "base_commission_bps": commission_bps,
        "base_slippage_bps": slippage_bps,
        "levels": levels,
    }


def monte_carlo(
    returns: pd.Series,
    n_paths: int = 500,
    horizon_days: int | None = None,
    block_days: int = 10,
    initial_capital: float = 100_000.0,
    seed: int = 7,
) -> dict:
    """Moving-block bootstrap of realized daily returns.

    Resampling in blocks preserves short-range autocorrelation that i.i.d.
    resampling would destroy. Returns percentile bands for terminal wealth and
    max drawdown, plus loss probability and 95% VaR/CVaR on the terminal
    return distribution.
    """
    r = returns.dropna().to_numpy(dtype=float)
    if len(r) < max(20, block_days):
        return {"error": "insufficient data for bootstrap"}

    horizon = int(horizon_days or len(r))
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(horizon / block_days))
    starts = rng.integers(0, len(r) - block_days + 1, size=(n_paths, n_blocks))
    idx = (starts[:, :, None] + np.arange(block_days)[None, None, :]).reshape(n_paths, -1)
    paths = r[idx[:, :horizon]]

    equity = initial_capital * np.cumprod(1.0 + paths, axis=1)
    terminal = equity[:, -1]
    running_max = np.maximum.accumulate(equity, axis=1)
    max_dd = (equity / running_max - 1.0).min(axis=1)
    terminal_return = terminal / initial_capital - 1.0

    def bands(values: np.ndarray, digits: int) -> dict:
        pcts = np.percentile(values, [5, 25, 50, 75, 95])
        return {f"p{p}": round(float(v), digits) for p, v in zip((5, 25, 50, 75, 95), pcts)}

    var_95 = float(np.percentile(terminal_return, 5))
    tail = terminal_return[terminal_return <= var_95]
    return {
        "n_paths": n_paths,
        "horizon_days": horizon,
        "block_days": block_days,
        "terminal_equity": bands(terminal, 2),
        "terminal_return": bands(terminal_return, 6),
        "max_drawdown": bands(max_dd, 6),
        "prob_loss": round(float((terminal < initial_capital).mean()), 4),
        "var_95": round(var_95, 6),
        "cvar_95": round(float(tail.mean()) if len(tail) else var_95, 6),
    }
