"""Historical-shock distribution engine for hedge evaluation.

Step 3 of docs/hedge-construction.md, and the reason the cost table can rank
on a tail metric at all: a payoff grid has no probabilities, so CVaR reduction
must come from repricing *today's* contract under *prior* joint shocks. No
claim is made that the contract could have been traded historically — the
shocks are the empirical distribution, the contract is the one on the screen
now.

Everything here is pure: inputs are price series already fetched elsewhere,
the bootstrap is seeded, and every approximation is written into
``ShockSet.notes`` rather than silently defaulted:

* Overlapping horizon windows are serially dependent — ``n_independent``
  (≈ windows / horizon) is reported next to the raw count.
* The vol dimension maps ΔVIX points / 100 to an additive sticky-strike IV
  shift on every leg. Frozen IV (no vol series) understates put protection
  and is flagged loudly.
* Holdings with short history fall back to beta × benchmark + a resampled
  residual, per the design doc.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from . import pricing

TRADING_DAYS = 252

#: Fewest overlapping daily observations needed to estimate a holding's beta.
MIN_DAILY_OBS = 20

#: VIX is quoted in vol points; option IVs are decimals.
VOL_POINTS_SCALE = 100.0


# --------------------------------------------------------------------------- #
# Shock set
# --------------------------------------------------------------------------- #
@dataclass
class ShockSet:
    """Joint horizon shocks: one row per historical window.

    ``holding_returns`` columns are per-symbol horizon returns,
    ``benchmark_return`` the index move over the same window, ``iv_shift`` the
    additive IV change to apply when repricing options under that window.
    """

    holding_returns: pd.DataFrame
    benchmark_return: pd.Series
    iv_shift: pd.Series
    horizon_sessions: int
    benchmark: str
    period: Tuple[str, str]
    #: Daily beta vs the benchmark per symbol (also used by the display grid).
    betas: Dict[str, float]
    #: Symbols whose windows were filled by the beta + residual fallback.
    fallback_symbols: List[str]
    notes: List[str]

    @property
    def n_windows(self) -> int:
        return int(len(self.benchmark_return))

    @property
    def n_independent(self) -> int:
        """Rough count of non-overlapping windows — the honest sample size."""
        return max(1, self.n_windows // self.horizon_sessions)


def horizon_end(as_of: date, horizon_sessions: int) -> date:
    """Calendar date ``horizon_sessions`` trading sessions past ``as_of``."""
    return as_of + timedelta(days=round(horizon_sessions * 365.0 / TRADING_DAYS))


def _window_returns(values: np.ndarray, horizon: int) -> np.ndarray:
    """Overlapping ``horizon``-session returns; NaN where either end is NaN."""
    with np.errstate(invalid="ignore", divide="ignore"):
        return values[horizon:] / values[:-horizon] - 1.0


def build_shocks(
    panel: pd.DataFrame,
    benchmark_closes: pd.Series,
    horizon_sessions: int,
    benchmark: str = "SPY",
    vol_closes: Optional[pd.Series] = None,
    seed: int = 0,
) -> ShockSet:
    """Horizon-matched joint shocks from the holdings panel and index history.

    The window calendar is the benchmark's: holdings are aligned onto it, and
    a holding missing a window (listed too recently) gets
    ``beta × benchmark + resampled own residual`` — the design doc's fallback
    — with the symbol reported in ``fallback_symbols``.
    """
    if len(benchmark_closes) <= horizon_sessions:
        raise ValueError(
            "Need more than {} sessions of benchmark history to build "
            "{}-session shocks".format(horizon_sessions, horizon_sessions)
        )
    notes: List[str] = [
        "overlapping windows are serially dependent; n_independent is the honest sample size",
        "today's contracts are repriced under prior shocks — no claim of historical tradability",
    ]
    index = benchmark_closes.dropna().index
    window_index = index[horizon_sessions:]

    bench_values = benchmark_closes.dropna().to_numpy(dtype=float)
    bench_windows = pd.Series(
        _window_returns(bench_values, horizon_sessions), index=window_index
    )
    bench_daily = pd.Series(bench_values, index=index).pct_change()

    aligned = panel.reindex(index)
    rng = np.random.default_rng(seed)
    columns: Dict[str, pd.Series] = {}
    betas: Dict[str, float] = {}
    fallback: List[str] = []

    for symbol in aligned.columns:
        series = aligned[symbol].astype(float)
        windows = pd.Series(
            _window_returns(series.to_numpy(), horizon_sessions), index=window_index
        )
        betas[symbol] = _daily_beta(series.pct_change(), bench_daily, notes, symbol)
        missing = windows.isna()
        if missing.any():
            fallback.append(symbol)
            residuals = (windows - betas[symbol] * bench_windows).dropna().to_numpy()
            fill = betas[symbol] * bench_windows[missing]
            if residuals.size:
                fill = fill + rng.choice(residuals, size=int(missing.sum()))
            else:
                notes.append(
                    "{}: no observed windows, filled with beta x benchmark only".format(symbol)
                )
            windows[missing] = fill
        columns[symbol] = windows

    if vol_closes is not None and len(vol_closes.dropna()):
        vol = vol_closes.reindex(index).ffill().to_numpy(dtype=float)
        iv_shift = pd.Series(
            (vol[horizon_sessions:] - vol[:-horizon_sessions]) / VOL_POINTS_SCALE,
            index=window_index,
        ).fillna(0.0)
        notes.append(
            "IV shift = window change in the vol index / 100, applied sticky-strike to every leg"
        )
    else:
        iv_shift = pd.Series(0.0, index=window_index)
        notes.append(
            "no vol index history: IV frozen across shocks — put protection is UNDERSTATED "
            "and rankings tilt toward linear hedges"
        )

    return ShockSet(
        holding_returns=pd.DataFrame(columns, index=window_index),
        benchmark_return=bench_windows,
        iv_shift=iv_shift,
        horizon_sessions=horizon_sessions,
        benchmark=benchmark.upper(),
        period=(index[0].date().isoformat(), index[-1].date().isoformat()),
        betas=betas,
        fallback_symbols=fallback,
        notes=notes,
    )


def _daily_beta(
    returns: pd.Series, bench_returns: pd.Series, notes: List[str], symbol: str
) -> float:
    """Cov/var beta on overlapping daily returns; 1.0 (flagged) when starved."""
    paired = pd.concat([returns, bench_returns], axis=1, keys=["a", "b"]).dropna()
    if len(paired) < MIN_DAILY_OBS or not float(paired["b"].var(ddof=1)):
        notes.append("{}: too little history to estimate beta, using 1.0".format(symbol))
        return 1.0
    return float(paired["a"].cov(paired["b"]) / paired["b"].var(ddof=1))


# --------------------------------------------------------------------------- #
# P&L distributions under the shocks
# --------------------------------------------------------------------------- #
def book_pnl(shocks: ShockSet, rows: Sequence[Dict[str, Any]]) -> np.ndarray:
    """Dollar P&L of the current book under each shock window.

    ``rows`` are the snapshot's marked-to-market holdings; cash is unmoved by
    construction. A holding absent from the shock set contributes nothing and
    is recorded in the notes — silence would understate the book's risk.
    """
    pnl = np.zeros(shocks.n_windows)
    for row in rows:
        symbol = row["symbol"]
        if symbol in shocks.holding_returns.columns:
            pnl += float(row["market_value"]) * shocks.holding_returns[symbol].to_numpy()
        else:
            note = "{}: not in the shock set, its risk is missing from the distribution".format(
                symbol
            )
            if note not in shocks.notes:
                shocks.notes.append(note)
    return pnl


def underlying_windows(shocks: ShockSet, symbol: str) -> np.ndarray:
    """Horizon window returns of one underlying — the benchmark or a holding.

    Raises when the symbol has no series: a hedge cannot be evaluated against
    a distribution that does not contain what it is written on.
    """
    key = symbol.upper()
    if key == shocks.benchmark:
        return shocks.benchmark_return.to_numpy()
    if key in shocks.holding_returns.columns:
        return shocks.holding_returns[key].to_numpy()
    raise ValueError(
        "No shock series for underlying {} — hedge it via the benchmark "
        "or add it to the panel".format(key)
    )


def hedge_unit_pnl(
    shocks: ShockSet,
    structure: pricing.OptionStructure,
    spot: float,
    as_of: date,
    rate: float = 0.0,
    div_yield: float = 0.0,
) -> np.ndarray:
    """Dollar P&L of ONE unit of the structure under each shock window.

    The underlying is shocked by its own window return (the benchmark's when
    the structure is written on the benchmark), IV shifts sticky-strike by the
    window's Δvol, and tenor shrinks by the horizon. The base is today's
    *model* value: execution give-up (ask vs model) belongs to the cost table,
    not the protection distribution — mixing them double-counts.
    """
    underlying_returns = underlying_windows(shocks, structure.underlying)
    horizon_date = horizon_end(as_of, shocks.horizon_sessions)
    base = pricing.structure_value(structure, spot, as_of, 0.0, rate, div_yield)
    iv_shifts = shocks.iv_shift.to_numpy()
    values = np.array(
        [
            pricing.structure_value(
                structure,
                spot * (1.0 + underlying_returns[i]),
                horizon_date,
                float(iv_shifts[i]),
                rate,
                div_yield,
            )
            for i in range(shocks.n_windows)
        ]
    )
    return values - base


def cvar(pnls: np.ndarray, level: float = 0.05) -> float:
    """Mean of the worst ``level`` tail, in dollars (negative = loss)."""
    quantile = float(np.quantile(pnls, level))
    tail = pnls[pnls <= quantile]
    return float(tail.mean()) if tail.size else quantile


def cvar_curve(
    book_pnls: np.ndarray,
    unit_pnls: np.ndarray,
    quantities: Sequence[int],
    level: float = 0.05,
) -> List[Dict[str, Any]]:
    """CVaR before/after per integer contract count — what the solver ranks.

    ``unit_pnls`` scales linearly with quantity, so the whole curve costs one
    repricing pass however many counts are tried.
    """
    base = cvar(book_pnls, level)
    curve = []
    for quantity in quantities:
        hedged = cvar(book_pnls + quantity * unit_pnls, level)
        curve.append(
            {
                "quantity": int(quantity),
                "cvar_unhedged": round(base, 2),
                "cvar_hedged": round(hedged, 2),
                "cvar_reduction": round(hedged - base, 2),
            }
        )
    return curve


def protection_ci(
    book_pnls: np.ndarray,
    unit_pnls: np.ndarray,
    quantity: int,
    level: float = 0.05,
    draws: int = 1000,
    seed: int = 0,
) -> Dict[str, Any]:
    """Bootstrap CI on the CVaR reduction at one contract count.

    Windows are resampled jointly (same indices for book and hedge, so the
    dependence between them survives). IID resampling of overlapping windows
    understates the width — rank on the lower bound, and treat it as
    optimistic. Seeded: same inputs, same answer.
    """
    rng = np.random.default_rng(seed)
    n = len(book_pnls)
    indices = rng.integers(0, n, size=(draws, n))
    reductions = np.empty(draws)
    for draw in range(draws):
        chosen = indices[draw]
        reductions[draw] = cvar(
            book_pnls[chosen] + quantity * unit_pnls[chosen], level
        ) - cvar(book_pnls[chosen], level)
    low, high = np.quantile(reductions, [0.025, 0.975])
    point = cvar(book_pnls + quantity * unit_pnls, level) - cvar(book_pnls, level)
    return {
        "cvar_reduction": round(float(point), 2),
        "cvar_reduction_ci95": [round(float(low), 2), round(float(high), 2)],
        "draws": draws,
        "seed": seed,
        "method": "joint iid resample of overlapping windows (optimistic width)",
    }


# --------------------------------------------------------------------------- #
# Display grid — communicates, never ranks
# --------------------------------------------------------------------------- #
DEFAULT_INDEX_SHOCKS = tuple(round(s, 2) for s in np.arange(-0.30, 0.21, 0.05))
DEFAULT_IV_SHIFTS = (-0.05, 0.0, 0.10, 0.25)

#: Underlying moves the plotted curve is drawn over. Finer than the table
#: grid on purpose: a curve is read for its shape, and a 5-point step walks
#: straight over the bend where protection starts.
CURVE_SHOCKS = tuple(round(float(s), 4) for s in np.arange(-0.30, 0.2001, 0.025))


def exposure_pnl(
    rows: Sequence[Dict[str, Any]],
    shock: float,
    betas: Optional[Dict[str, float]] = None,
) -> float:
    """Dollar P&L of ``rows`` under one underlying move.

    ``betas`` maps the move onto each holding — the book's response to an
    *index* shock. Omit it (or pass a map missing the symbol) and the holding
    moves 1:1, which is the right convention when the thing being shocked is
    the position itself rather than the index it is measured against.
    """
    betas = betas or {}
    return sum(
        float(row["market_value"]) * betas.get(row["symbol"], 1.0) * float(shock)
        for row in rows
    )


def iv_response(shocks: ShockSet) -> Optional[Tuple[float, float]]:
    """Least-squares (slope, intercept) of the window IV shift on the move.

    How volatility actually travelled with the market *in this sample*, so a
    drawn curve can show what a fall does to an option instead of freezing IV
    and picturing protection the engine already knows is understated. ``None``
    when there is no vol history: the frozen case must never be drawn as
    though vol had been measured and found still.
    """
    shift = shocks.iv_shift.to_numpy(dtype=float)
    moves = shocks.benchmark_return.to_numpy(dtype=float)
    if len(shift) < 2 or not np.any(shift) or not float(np.var(moves)):
        return None
    slope, intercept = np.polyfit(moves, shift, 1)
    return float(slope), float(intercept)


def scenario_curve(
    shocks: ShockSet,
    exposure_rows: Sequence[Dict[str, Any]],
    hedge_pnl: Callable[[float, float], float],
    exposure_betas: Optional[Dict[str, float]] = None,
    underlying_shocks: Sequence[float] = CURVE_SHOCKS,
) -> List[Dict[str, Any]]:
    """Payoff-at-horizon curve: where the hedge leaves you if the move lands.

    ``hedge_pnl(shock, iv_shift)`` is the caller's already-sized structure, net
    of what it cost to put on — the picture is of money, not of model value.
    Display only, exactly like :func:`scenario_grid`: one deterministic path
    per move, carrying no probabilities, so nothing drawn here may rank a
    candidate.

    Each point is drawn twice where a vol history exists — once with IV frozen
    (the pessimistic reading of a long put) and once with IV where this
    sample's own moves put it. Frozen alone would picture a put as weaker than
    the ranking beside it says.
    """
    response = iv_response(shocks)
    points: List[Dict[str, Any]] = []
    for shock in underlying_shocks:
        shock = float(shock)
        exposure = exposure_pnl(exposure_rows, shock, exposure_betas)
        frozen = float(hedge_pnl(shock, 0.0))
        point = {
            "shock": round(shock, 4),
            "exposure_pnl": round(exposure, 2),
            "hedge_pnl": round(frozen, 2),
            "hedged_pnl": round(exposure + frozen, 2),
        }
        if response is not None:
            iv_shift = response[0] * shock + response[1]
            paired = float(hedge_pnl(shock, iv_shift))
            point["iv_shift"] = round(iv_shift, 4)
            point["hedge_pnl_iv"] = round(paired, 2)
            point["hedged_pnl_iv"] = round(exposure + paired, 2)
        points.append(point)
    return points


def scenario_grid(
    shocks: ShockSet,
    rows: Sequence[Dict[str, Any]],
    structure: pricing.OptionStructure,
    quantity: int,
    spot: float,
    as_of: date,
    rate: float = 0.0,
    div_yield: float = 0.0,
    index_shocks: Sequence[float] = DEFAULT_INDEX_SHOCKS,
    iv_shifts: Sequence[float] = DEFAULT_IV_SHIFTS,
) -> List[Dict[str, Any]]:
    """Deterministic payoff-at-horizon grid: index shock × IV shift.

    Display only — there are no probabilities here, so nothing in this grid
    may rank candidates (that is the shock distribution's job). Holdings move
    beta × index shock, an approximation the grid states in every row's
    inputs rather than a footnote.
    """
    horizon_date = horizon_end(as_of, shocks.horizon_sessions)
    base = pricing.structure_value(structure, spot, as_of, 0.0, rate, div_yield)
    grid = []
    for index_shock in index_shocks:
        book = exposure_pnl(rows, index_shock, shocks.betas)
        for iv_shift in iv_shifts:
            hedge = quantity * (
                pricing.structure_value(
                    structure,
                    spot * (1.0 + index_shock),
                    horizon_date,
                    iv_shift,
                    rate,
                    div_yield,
                )
                - base
            )
            grid.append(
                {
                    "index_shock": round(float(index_shock), 4),
                    "iv_shift": round(float(iv_shift), 4),
                    "book_pnl": round(book, 2),
                    "hedge_pnl": round(float(hedge), 2),
                    "hedged_pnl": round(book + float(hedge), 2),
                }
            )
    return grid
