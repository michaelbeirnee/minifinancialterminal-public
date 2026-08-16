"""Screener menu: filter an index's members on size, moves, volatility, beta and alpha.

The existing ``/equity/screener`` proxies Yahoo's own screen fields; this menu
instead builds the metrics itself over a whole universe — Wikipedia for index
membership (or any custom symbol list), the Nasdaq screener table for market
caps, and one batched Yahoo download for a year of closes — so it can rank on
computed volatility, CAPM beta and alpha, trailing moves over any window, and
trend measures (moving-average distance, 52-week range position, RSI). The
heavy build is cached per universe for an hour; filtering and sorting are then
in-memory.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ..core.caching import TTL_DAILY, cached
from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import norm_symbols, pct_change_table
from ..providers import markets

TRADING_DAYS = 252
HISTORY_DAYS = 420  # calendar days — enough bars for a 1-year window

# Universes whose Wikipedia tickers match Yahoo's US symbols, with a liquid
# benchmark ETF for the beta/alpha regression.
UNIVERSES: Dict[str, Dict[str, str]] = {
    "sp500": {"label": "S&P 500", "benchmark": "SPY"},
    "nasdaq100": {"label": "Nasdaq-100", "benchmark": "QQQ"},
    "dowjones": {"label": "Dow Jones Industrial", "benchmark": "DIA"},
    "sp400": {"label": "S&P 400 MidCap", "benchmark": "MDY"},
    "sp600": {"label": "S&P 600 SmallCap", "benchmark": "SPSM"},
    "russell1000": {"label": "Russell 1000", "benchmark": "IWB"},
}

CUSTOM_BENCHMARK = "SPY"
MAX_CUSTOM_SYMBOLS = 300

TIMEFRAMES = ("one_day", "one_week", "one_month", "three_month", "six_month", "ytd", "one_year")

SORT_FIELDS = ("symbol", "name", "sector", "last_price", "market_cap", "volatility",
               "beta", "alpha", "ma50_dist", "ma200_dist", "high52_dist", "low52_dist",
               "rsi14") + TIMEFRAMES


def _norm_sym(symbol: str) -> str:
    """One spelling for BRK.B / BRK-B / BRK/B so the sources can be joined."""
    return re.sub(r"[./]", "-", str(symbol).strip().upper())


def _listed_lookup() -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """symbol -> {market_cap, name, sector} from the Nasdaq listed-company table.

    Caps drive the size filter; name and sector fill gaps in membership tables
    that lack them (the slickcharts fallback and custom symbol lists carry no
    sector column).
    """
    try:
        table = markets.nasdaq_screener(limit=10_000)
    except Exception as exc:  # noqa: BLE001 - enrichment, not the screen itself
        return {}, ["market caps unavailable ({})".format(exc)]
    col = next((c for c in ("market_cap", "marketcap") if c in table.columns), None)
    if col is None:
        return {}, ["Nasdaq screener table carried no market cap column"]
    caps = pd.to_numeric(table[col], errors="coerce")
    out: Dict[str, Dict[str, Any]] = {}
    for i, sym in enumerate(table["symbol"]):
        cap = caps.iloc[i]
        out[_norm_sym(sym)] = {
            # Nasdaq reports unknown caps as 0.00.
            "market_cap": float(cap) if pd.notna(cap) and cap > 0 else None,
            "name": table["name"].iloc[i] if "name" in table.columns else None,
            "sector": table["sector"].iloc[i] if "sector" in table.columns else None,
        }
    return out, []


def _close_panel_bulk(symbols: List[str], start: str) -> pd.DataFrame:
    """Wide close-price frame via one batched (threaded) yfinance download."""
    import yfinance as yf

    data = yf.download(symbols, start=start, interval="1d", auto_adjust=True,
                       progress=False, threads=True, group_by="column")
    if data is None or data.empty:
        raise EmptyDataError("Yahoo returned no prices for the universe")
    closes = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data[["Close"]]
    if closes.shape[1] == 1 and len(symbols) == 1:
        closes.columns = [symbols[0]]
    closes = closes.dropna(axis=1, how="all").sort_index()
    if isinstance(closes.index, pd.DatetimeIndex) and closes.index.tz is not None:
        closes.index = closes.index.tz_localize(None)
    return closes


def _capm(sym_returns: pd.Series, bench_returns: pd.Series) -> Tuple[Optional[float], Optional[float]]:
    """(beta, annualised alpha) vs the benchmark, or (None, None) on thin data."""
    paired = pd.concat([sym_returns, bench_returns], axis=1).dropna().tail(TRADING_DAYS)
    if len(paired) < 60:
        return None, None
    y, x = paired.iloc[:, 0], paired.iloc[:, 1]
    var = float(x.var(ddof=1))
    if not var:
        return None, None
    beta = float(x.cov(y)) / var
    alpha = float(y.mean() - beta * x.mean()) * TRADING_DAYS
    return beta, alpha


def _rsi(prices: pd.Series, length: int = 14) -> Optional[float]:
    """Wilder's RSI on the last bar, or None on thin history."""
    if len(prices) < length + 1:
        return None
    delta = prices.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / length, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / length, adjust=False).mean()
    last_loss = float(loss.iloc[-1])
    if last_loss == 0:
        return 100.0
    rs = float(gain.iloc[-1]) / last_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _build_metrics(members: pd.DataFrame, bench: str) -> Dict[str, Any]:
    """Metrics for every symbol in ``members``: caps, moves, vol, CAPM, trend."""
    symbols = [_norm_sym(s) for s in members["symbol"]]

    warnings: List[str] = []
    listed, cap_warnings = _listed_lookup()
    warnings.extend(cap_warnings)

    start = str(date.today() - timedelta(days=HISTORY_DAYS))
    panel = _close_panel_bulk(symbols + [bench], start)
    returns = panel.pct_change()
    bench_returns = returns[bench] if bench in returns.columns else None
    if bench_returns is None:
        warnings.append("no prices for benchmark {} — beta and alpha unavailable".format(bench))

    missing = 0
    rows: List[Dict[str, Any]] = []
    for member, sym in zip(members.to_dict("records"), symbols):
        prices = panel[sym].dropna() if sym in panel.columns else pd.Series(dtype=float)
        if len(prices) < 2:
            missing += 1
            continue
        last = float(prices.iloc[-1])
        moves = pct_change_table(prices)
        r = returns[sym].dropna().tail(TRADING_DAYS)
        vol = float(r.std(ddof=1) * np.sqrt(TRADING_DAYS)) if len(r) >= 20 else None
        beta, alpha = _capm(returns[sym], bench_returns) if bench_returns is not None else (None, None)
        ma50 = float(prices.tail(50).mean()) if len(prices) >= 50 else None
        ma200 = float(prices.tail(200).mean()) if len(prices) >= 200 else None
        yearly = prices.tail(TRADING_DAYS)
        info = listed.get(sym, {})
        rows.append({
            "symbol": sym,
            "name": member.get("name") or info.get("name"),
            "sector": member.get("sector") or info.get("sector"),
            "last_price": last,
            "market_cap": info.get("market_cap"),
            **{tf: moves.get(tf) for tf in TIMEFRAMES},
            "volatility": vol,
            "beta": beta,
            "alpha": alpha,
            "ma50_dist": last / ma50 - 1 if ma50 else None,
            "ma200_dist": last / ma200 - 1 if ma200 else None,
            "high52_dist": last / float(yearly.max()) - 1 if len(yearly) > 1 else None,
            "low52_dist": last / float(yearly.min()) - 1 if len(yearly) > 1 else None,
            "rsi14": _rsi(prices),
        })
    if not rows:
        raise EmptyDataError("No prices for any symbol in the universe")
    if missing:
        warnings.append("{} symbol(s) returned no price history".format(missing))
    return {"rows": rows, "warnings": warnings, "as_of": str(panel.index[-1].date()),
            "benchmark": bench}


# v2: the row schema grew (trend columns); a new prefix keeps stale cached
# tables from an older build from being served without the new fields.
@cached("screener.metrics.v2", ttl=TTL_DAILY)
def _metrics_table(index: str) -> Dict[str, Any]:
    return _build_metrics(markets.index_constituents(index), UNIVERSES[index]["benchmark"])


@cached("screener.metrics.custom.v2", ttl=TTL_DAILY)
def _custom_metrics(symbols_key: str) -> Dict[str, Any]:
    members = pd.DataFrame({"symbol": symbols_key.split(",")})
    return _build_metrics(members, CUSTOM_BENCHMARK)


def _bounded(value: Optional[float], lo: Optional[float], hi: Optional[float]) -> bool:
    """Range test where an unknown value only passes an unbounded filter."""
    if lo is None and hi is None:
        return True
    if value is None:
        return False
    return (lo is None or value >= lo) and (hi is None or value <= hi)


def _side_of(value: Optional[float], above: Optional[bool]) -> bool:
    """Above/below-zero test where an unknown value only passes 'any'."""
    if above is None:
        return True
    if value is None:
        return False
    return value > 0 if above else value < 0


def _apply_filters(rows: List[Dict[str, Any]], timeframe: str, direction: str,
                   min_move: Optional[float] = None, sector: Optional[str] = None,
                   above_ma50: Optional[bool] = None, above_ma200: Optional[bool] = None,
                   mcap: Tuple[Optional[float], Optional[float]] = (None, None),
                   vol: Tuple[Optional[float], Optional[float]] = (None, None),
                   beta: Tuple[Optional[float], Optional[float]] = (None, None),
                   alpha: Tuple[Optional[float], Optional[float]] = (None, None),
                   rsi: Tuple[Optional[float], Optional[float]] = (None, None),
                   ) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        move = row.get(timeframe)
        if direction != "any" or min_move is not None:
            if move is None:
                continue
            if direction == "up" and move <= 0:
                continue
            if direction == "down" and move >= 0:
                continue
            if min_move is not None and abs(move) * 100 < min_move:
                continue
        if sector and (row.get("sector") or "").lower() != sector.lower():
            continue
        if not _side_of(row.get("ma50_dist"), above_ma50):
            continue
        if not _side_of(row.get("ma200_dist"), above_ma200):
            continue
        cap = row.get("market_cap")
        if not _bounded(cap / 1e9 if cap is not None else None, *mcap):
            continue
        v = row.get("volatility")
        if not _bounded(v * 100 if v is not None else None, *vol):
            continue
        if not _bounded(row.get("beta"), *beta):
            continue
        a = row.get("alpha")
        if not _bounded(a * 100 if a is not None else None, *alpha):
            continue
        if not _bounded(row.get("rsi14"), *rsi):
            continue
        out.append(row)
    return out


def _sorted_rows(rows: List[Dict[str, Any]], sort: str, ascending: bool) -> List[Dict[str, Any]]:
    """Sort with unknown values always last, whichever direction."""
    known = [r for r in rows if r.get(sort) is not None]
    unknown = [r for r in rows if r.get(sort) is None]
    return sorted(known, key=lambda r: r[sort], reverse=not ascending) + unknown


@command("/screener/indexes", providers=("wikipedia",),
         summary="Universes the screener can rank, with their benchmarks")
def screener_indexes() -> Result:
    rows = [{"index": key, "label": u["label"], "benchmark": u["benchmark"]}
            for key, u in UNIVERSES.items()]
    return Result(rows, provider="wikipedia")


@command("/screener/run", providers=("yahoo",),
         summary="Filter an index's members by market cap, moves, volatility, beta and alpha")
def screener_run(index: str = "sp500", symbols: Optional[str] = None,
                 timeframe: str = "one_month", direction: str = "any",
                 min_move: Optional[float] = None, sector: Optional[str] = None,
                 mcap_min: Optional[float] = None, mcap_max: Optional[float] = None,
                 vol_min: Optional[float] = None, vol_max: Optional[float] = None,
                 beta_min: Optional[float] = None, beta_max: Optional[float] = None,
                 alpha_min: Optional[float] = None, alpha_max: Optional[float] = None,
                 above_ma50: Optional[bool] = None, above_ma200: Optional[bool] = None,
                 rsi_min: Optional[float] = None, rsi_max: Optional[float] = None,
                 sort: str = "market_cap", ascending: bool = False, limit: int = 50,
                 provider: Optional[str] = None) -> Result:
    """Pass ``symbols`` (comma-separated) to screen a custom list — a watchlist,
    say — instead of an index; custom lists are benchmarked against SPY. Units:
    market cap bounds in $ billions; min_move, volatility and alpha bounds in
    percent (annualised for volatility and alpha); RSI bounds 0-100. Moves and
    distances in the output are decimals (0.05 = +5%). The first run on a
    universe downloads a year of prices for every member; results are cached
    for an hour."""
    src = resolve_provider(provider, ("yahoo",))
    if timeframe not in TIMEFRAMES:
        raise ValueError("timeframe must be one of {}".format(", ".join(TIMEFRAMES)))
    if direction not in ("any", "up", "down"):
        raise ValueError("direction must be any, up or down")
    if sort == "move":
        sort = timeframe
    if sort not in SORT_FIELDS:
        raise ValueError("sort must be one of {}".format(", ".join(SORT_FIELDS)))

    if symbols:
        universe = norm_symbols(symbols, limit=MAX_CUSTOM_SYMBOLS)
        table = _custom_metrics(",".join(_norm_sym(s) for s in universe))
        index, label = "custom", "Custom list"
    else:
        if index not in UNIVERSES:
            raise ValueError("index must be one of {} (or pass symbols=)".format(
                ", ".join(sorted(UNIVERSES))))
        table = _metrics_table(index)
        label = UNIVERSES[index]["label"]

    matched = _apply_filters(table["rows"], timeframe, direction, min_move, sector,
                             above_ma50, above_ma200,
                             mcap=(mcap_min, mcap_max), vol=(vol_min, vol_max),
                             beta=(beta_min, beta_max), alpha=(alpha_min, alpha_max),
                             rsi=(rsi_min, rsi_max))
    matched = _sorted_rows(matched, sort, ascending)
    limit = max(1, min(int(limit), 1000))
    sectors = sorted({r["sector"] for r in table["rows"] if r.get("sector")})
    return Result(
        matched[:limit],
        provider=src,
        warnings=list(table["warnings"]),
        extra={"index": index, "label": label, "benchmark": table["benchmark"],
               "as_of": table["as_of"], "universe_size": len(table["rows"]),
               "matched": len(matched), "sectors": sectors},
    )
