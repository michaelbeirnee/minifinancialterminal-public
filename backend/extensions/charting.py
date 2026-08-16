"""Charting menu.

Figures are emitted as Plotly-compatible JSON (``{"data": [...], "layout": {...}}``)
built by hand, so nothing here depends on a plotting library being installed —
the browser, a notebook, or ``plotly.io.from_json`` can all render the result.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import MFTObject, Result
from ..core.registry import command, execute, resolve_provider
from ..core.utils import date_window, jsonable, norm_symbols, one_symbol
from ..providers import treasury, yahoo
from .quantitative import series_frame

# Terminal palette — dark background, amber/green accents, colour-blind safe.
PALETTE = ["#e8b33a", "#3aa6e8", "#4cc38a", "#e05c5c", "#b07be0", "#57c7d4",
           "#d98b45", "#8fbf5a", "#e07bb0", "#7f8fa6"]

LAYOUT: Dict[str, Any] = {
    "template": "plotly_dark",
    "paper_bgcolor": "#0d1117",
    "plot_bgcolor": "#0d1117",
    "font": {"family": "SFMono-Regular, Menlo, monospace", "size": 12, "color": "#c9d1d9"},
    "margin": {"l": 60, "r": 30, "t": 50, "b": 45},
    "hovermode": "x unified",
    "xaxis": {"gridcolor": "#21262d", "zerolinecolor": "#30363d"},
    "yaxis": {"gridcolor": "#21262d", "zerolinecolor": "#30363d"},
    "legend": {"orientation": "h", "y": -0.18},
}


def _layout(title: str, **overrides: Any) -> Dict[str, Any]:
    layout = {k: (dict(v) if isinstance(v, dict) else v) for k, v in LAYOUT.items()}
    layout["title"] = {"text": title, "x": 0.01, "xanchor": "left"}
    layout.update(overrides)
    return layout


def _axis(values: Any) -> List[Any]:
    return [jsonable(v) for v in values]


def _figure(traces: List[Dict[str, Any]], layout: Dict[str, Any],
            extra: Optional[Dict[str, Any]] = None) -> Result:
    if not traces:
        raise EmptyDataError("Nothing to plot")
    figure = {"data": traces, "layout": layout}
    return Result(figure, provider="mft-charting", extra=extra or {})


# --------------------------------------------------------------------------- #
# Price charts
# --------------------------------------------------------------------------- #
@command("/charting/price", providers=("yahoo",),
         summary="Candlestick price chart with volume and optional moving averages")
def chart_price(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
                interval: str = "1d", moving_averages: Optional[str] = "50,200",
                chart_type: str = "candlestick", provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    sym = one_symbol(symbol)
    start, end = date_window(start_date, end_date)
    df = yahoo.history(sym, str(start), str(end), interval=interval)
    if df.empty:
        raise EmptyDataError("No price history for {}".format(sym))
    dates = _axis(df.index)
    traces: List[Dict[str, Any]] = []
    if chart_type == "line":
        traces.append({"type": "scatter", "mode": "lines", "name": sym, "x": dates,
                       "y": _axis(df["close"]), "line": {"color": PALETTE[0], "width": 1.6}})
    else:
        traces.append(
            {
                "type": "candlestick", "name": sym, "x": dates,
                "open": _axis(df["open"]), "high": _axis(df["high"]),
                "low": _axis(df["low"]), "close": _axis(df["close"]),
                "increasing": {"line": {"color": "#4cc38a"}},
                "decreasing": {"line": {"color": "#e05c5c"}},
            }
        )
    for i, window in enumerate([int(w) for w in (moving_averages or "").split(",") if w.strip()]):
        traces.append(
            {
                "type": "scatter", "mode": "lines", "name": "SMA {}".format(window),
                "x": dates, "y": _axis(df["close"].rolling(window).mean()),
                "line": {"color": PALETTE[(i + 1) % len(PALETTE)], "width": 1.2},
            }
        )
    if "volume" in df.columns:
        traces.append(
            {
                "type": "bar", "name": "Volume", "x": dates, "y": _axis(df["volume"]),
                "yaxis": "y2", "marker": {"color": "#30363d"}, "opacity": 0.6,
            }
        )
    layout = _layout(
        "{} — {} to {}".format(sym, start, end),
        yaxis={"title": "Price", "domain": [0.28, 1.0], "gridcolor": "#21262d"},
        yaxis2={"title": "Volume", "domain": [0.0, 0.22], "gridcolor": "#21262d"},
        xaxis={"rangeslider": {"visible": False}, "gridcolor": "#21262d"},
    )
    return _figure(traces, layout, {"symbol": sym, "bars": int(len(df))})


@command("/charting/compare", providers=("yahoo",), summary="Normalised multi-symbol price chart")
def chart_compare(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
                  normalise: bool = True, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    symbols = norm_symbols(symbol)
    start, end = date_window(start_date, end_date)
    panel = yahoo.close_panel(symbols, str(start), str(end)).dropna(how="all")
    if normalise:
        panel = panel / panel.ffill().bfill().iloc[0] * 100
    dates = _axis(panel.index)
    traces = [
        {"type": "scatter", "mode": "lines", "name": col, "x": dates,
         "y": _axis(panel[col]), "line": {"color": PALETTE[i % len(PALETTE)], "width": 1.6}}
        for i, col in enumerate(panel.columns)
    ]
    title = "Relative performance (rebased to 100)" if normalise else "Price comparison"
    return _figure(traces, _layout(title, yaxis={"title": "Index" if normalise else "Price",
                                                 "gridcolor": "#21262d"}))


@command("/charting/drawdown", providers=("yahoo",), summary="Underwater drawdown chart")
def chart_drawdown(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
                   provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    returns = series_frame(symbol, start_date, end_date, "returns")
    traces = []
    for i, col in enumerate(returns.columns):
        curve = (1 + returns[col].fillna(0)).cumprod()
        dd = (curve / curve.cummax() - 1) * 100
        traces.append(
            {
                "type": "scatter", "mode": "lines", "name": col, "x": _axis(dd.index),
                "y": _axis(dd), "fill": "tozeroy",
                "line": {"color": PALETTE[i % len(PALETTE)], "width": 1.2},
            }
        )
    return _figure(traces, _layout("Drawdown", yaxis={"title": "%", "gridcolor": "#21262d"}))


@command("/charting/histogram", providers=("yahoo",), summary="Return distribution histogram")
def chart_histogram(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
                    bins: int = 60, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    returns = series_frame(symbol, start_date, end_date, "returns") * 100
    traces = [
        {"type": "histogram", "name": col, "x": _axis(returns[col].dropna()),
         "nbinsx": bins, "opacity": 0.65,
         "marker": {"color": PALETTE[i % len(PALETTE)]}}
        for i, col in enumerate(returns.columns)
    ]
    return _figure(traces, _layout("Daily return distribution", barmode="overlay",
                                   xaxis={"title": "%", "gridcolor": "#21262d"},
                                   yaxis={"title": "Frequency", "gridcolor": "#21262d"}))


# --------------------------------------------------------------------------- #
# Cross-sectional charts
# --------------------------------------------------------------------------- #
@command("/charting/correlation", providers=("yahoo",), summary="Correlation heatmap")
def chart_correlation(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
                      method: str = "pearson", provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    corr = series_frame(symbol, start_date, end_date, "returns").corr(method=method)
    trace = {
        "type": "heatmap", "z": [[jsonable(v) for v in row] for row in corr.to_numpy()],
        "x": list(corr.columns), "y": list(corr.index), "zmin": -1, "zmax": 1,
        "colorscale": [[0, "#e05c5c"], [0.5, "#0d1117"], [1, "#4cc38a"]],
        "colorbar": {"title": "ρ"},
    }
    return _figure([trace], _layout("Return correlation ({})".format(method)))


@command("/charting/yield_curve", providers=("treasury",),
         summary="Treasury yield curve, optionally versus an earlier date")
def chart_yield_curve(date: Optional[str] = None, compare_date: Optional[str] = None,
                      provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("treasury",))
    traces = []
    for i, day in enumerate([d for d in (date, compare_date) if d] or [None]):
        curve = treasury.yield_curve(day)
        traces.append(
            {
                "type": "scatter", "mode": "lines+markers",
                "name": str(curve["date"].iloc[0].date()),
                "x": _axis(curve["maturity"]), "y": _axis(curve["rate"]),
                "line": {"color": PALETTE[i % len(PALETTE)], "width": 2},
            }
        )
    return _figure(traces, _layout("US Treasury par yield curve",
                                   yaxis={"title": "%", "gridcolor": "#21262d"}))


@command("/charting/performance", providers=("yahoo",), summary="Trailing-return bar chart")
def chart_performance(symbol: str, window: str = "ytd", provider: Optional[str] = None) -> Result:
    """``window``: one_day, one_week, one_month, three_month, six_month, one_year, ytd, max."""
    src = resolve_provider(provider, ("yahoo",))
    obj: MFTObject = execute("/equity/price/performance", symbol=symbol, provider="yahoo")
    rows = [r for r in obj.to_records() if r.get(window) is not None]
    if not rows:
        raise EmptyDataError("No {} performance available for those symbols".format(window))
    rows.sort(key=lambda r: r[window])
    values = [r[window] * 100 for r in rows]
    trace = {
        "type": "bar", "orientation": "h", "x": values, "y": [r["symbol"] for r in rows],
        "marker": {"color": ["#4cc38a" if v >= 0 else "#e05c5c" for v in values]},
        "name": window,
    }
    return _figure([trace], _layout("Trailing return — {}".format(window.replace("_", " ")),
                                    xaxis={"title": "%", "gridcolor": "#21262d"}))


@command("/charting/volatility_cones", providers=("yahoo",), summary="Realised volatility cone")
def chart_cones(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
                provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    obj = execute("/technical/cones", symbol=symbol, start_date=start_date, end_date=end_date,
                  provider=src)
    rows = obj.to_records()
    if not rows:
        raise EmptyDataError("No volatility cone data for {}".format(symbol))
    windows = [r["window"] for r in rows]
    traces = [
        {"type": "scatter", "mode": "lines+markers", "name": label,
         "x": windows, "y": [r[key] * 100 for r in rows],
         "line": {"color": PALETTE[i % len(PALETTE)], "width": 1.4,
                  "dash": "dot" if key in ("min", "max") else "solid"}}
        for i, (key, label) in enumerate(
            [("min", "Min"), ("p25", "25th pct"), ("median", "Median"),
             ("p75", "75th pct"), ("max", "Max"), ("realised", "Current")]
        )
    ]
    return _figure(traces, _layout("Realised volatility cone — {}".format(symbol.upper()),
                                   xaxis={"title": "Window (days)", "gridcolor": "#21262d"},
                                   yaxis={"title": "Annualised vol %", "gridcolor": "#21262d"}))


# --------------------------------------------------------------------------- #
# Generic
# --------------------------------------------------------------------------- #
@command("/charting/command", providers=("mft",), methods=("POST",),
         summary="Chart the numeric output of any other command")
def chart_command(command_path: str, parameters: Optional[Dict[str, Any]] = None,
                  x_column: Optional[str] = None, y_columns: Optional[str] = None,
                  chart_type: str = "line", title: Optional[str] = None) -> Result:
    """Run any registry command and plot its numeric columns.

    ``chart_command(command_path="/economy/cpi", parameters={"transform": "pc1"})``
    """
    obj: MFTObject = execute(command_path, **(parameters or {}))
    rows = obj.to_records()
    if not rows:
        raise EmptyDataError("{} returned no rows to plot".format(command_path))
    df = pd.DataFrame(rows)
    x_name = x_column or next(
        (c for c in ("date", "period_ending", "maturity", "symbol", "index") if c in df.columns),
        df.columns[0],
    )
    numeric = df.select_dtypes(include=[np.number])
    wanted = ([c.strip() for c in y_columns.split(",") if c.strip()] if y_columns
              else [c for c in numeric.columns if c != x_name])
    missing = [c for c in wanted if c not in df.columns]
    if missing:
        raise ValueError("Unknown column(s): {}".format(", ".join(missing)))
    if not wanted:
        raise EmptyDataError("{} returned no numeric columns".format(command_path))
    traces = [
        {
            "type": "bar" if chart_type == "bar" else "scatter",
            "mode": None if chart_type == "bar" else "lines",
            "name": col, "x": _axis(df[x_name]), "y": _axis(df[col]),
            "line": {"color": PALETTE[i % len(PALETTE)], "width": 1.6},
            "marker": {"color": PALETTE[i % len(PALETTE)]},
        }
        for i, col in enumerate(wanted)
    ]
    for trace in traces:
        if trace["mode"] is None:
            trace.pop("mode")
            trace.pop("line")
    return _figure(traces, _layout(title or command_path),
                   {"command": command_path, "rows": len(df)})
