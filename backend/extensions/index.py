"""Index menu: membership, index prices, snapshots and long-run multiples."""
from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import date_window, norm_symbols, pct_change_table
from ..providers import markets, yahoo

# Friendly name -> Yahoo ticker.
INDEX_TICKERS: Dict[str, str] = {
    "sp500": "^GSPC", "sp400": "^SP400", "sp600": "^SP600", "nasdaq100": "^NDX",
    "nasdaq_composite": "^IXIC", "dowjones": "^DJI", "russell2000": "^RUT", "russell1000": "^RUI",
    "wilshire5000": "^W5000", "vix": "^VIX", "vxn": "^VXN", "move": "^MOVE",
    "ftse100": "^FTSE", "dax": "^GDAXI", "cac40": "^FCHI", "euro_stoxx_50": "^STOXX50E",
    "ibex35": "^IBEX", "smi": "^SSMI", "aex": "^AEX", "nikkei225": "^N225",
    "hangseng": "^HSI", "shanghai": "000001.SS", "kospi": "^KS11", "sensex": "^BSESN",
    "nifty50": "^NSEI", "asx200": "^AXJO", "tsx": "^GSPTSE", "bovespa": "^BVSP",
    "dollar_index": "DX-Y.NYB",
}


@command("/index/available", providers=("yahoo", "wikipedia", "cboe"),
         summary="Indices this terminal can quote or list")
def index_available(provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo", "wikipedia", "cboe"))
    if src == "cboe":
        return Result(markets.cboe_index_definitions(), provider=src)
    if src == "wikipedia":
        return Result(markets.available_indices(), provider=src)
    constituent_keys = set(markets.INDEX_SOURCES)
    rows = [
        {"index": name, "symbol": ticker, "has_constituents": name in constituent_keys}
        for name, ticker in sorted(INDEX_TICKERS.items())
    ]
    return Result(rows, provider=src)


@command("/index/constituents", providers=("wikipedia",), summary="Current index membership")
def index_constituents(index: str = "sp500", provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("wikipedia",))
    return Result(markets.index_constituents(index), provider=src)


@command("/index/price/historical", providers=("yahoo", "stooq"), summary="Index price history")
def index_historical(index: str = "sp500", start_date: Optional[str] = None,
                     end_date: Optional[str] = None, interval: str = "1d",
                     provider: Optional[str] = None) -> Result:
    """``index`` accepts a friendly name (``sp500``) or a raw ticker (``^GSPC``)."""
    src = resolve_provider(provider, ("yahoo", "stooq"))
    start, end = date_window(start_date, end_date)
    frames, warnings = [], []
    names = [n.strip() for n in index.split(",") if n.strip()]
    for name in names:
        ticker = INDEX_TICKERS.get(name.lower(), name)
        try:
            df = (markets.stooq_history(ticker, str(start), str(end)) if src == "stooq"
                  else yahoo.history(ticker, str(start), str(end), interval=interval))
        except Exception as exc:  # noqa: BLE001
            warnings.append("{}: {}".format(ticker, exc))
            continue
        if len(names) > 1:
            df.insert(0, "index", name)
        frames.append(df)
    if not frames:
        raise EmptyDataError("No index history. {}".format("; ".join(warnings)))
    return Result(pd.concat(frames).sort_index(), provider=src, warnings=warnings, index_name="date")


@command("/index/snapshots", providers=("yahoo",), summary="Level and trailing returns per index")
def index_snapshots(region: str = "global", provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    groups = {
        "us": ["sp500", "nasdaq100", "nasdaq_composite", "dowjones", "russell2000", "vix"],
        "europe": ["ftse100", "dax", "cac40", "euro_stoxx_50", "ibex35", "smi", "aex"],
        "asia": ["nikkei225", "hangseng", "shanghai", "kospi", "sensex", "nifty50", "asx200"],
        "americas": ["sp500", "tsx", "bovespa"],
    }
    groups["global"] = groups["us"] + groups["europe"] + groups["asia"]
    names = groups.get(region.lower())
    if not names:
        raise ValueError("region must be one of {}".format(", ".join(sorted(groups))))
    rows, warnings = [], []
    for name in names:
        ticker = INDEX_TICKERS[name]
        try:
            closes = yahoo.history(ticker, period="5y")["close"]
            quote = yahoo.quote(ticker)
        except Exception as exc:  # noqa: BLE001
            warnings.append("{}: {}".format(ticker, exc))
            continue
        rows.append(dict(index=name, symbol=ticker, last=quote.get("last_price"),
                         change_percent=quote.get("change_percent"), **pct_change_table(closes)))
    if not rows:
        raise EmptyDataError("No index snapshots. {}".format("; ".join(warnings)))
    return Result(rows, provider=src, warnings=warnings)


@command("/index/sectors", providers=("wikipedia",), summary="Index membership grouped by sector")
def index_sectors(index: str = "sp500", provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("wikipedia",))
    df = markets.index_constituents(index)
    if "sector" not in df.columns:
        raise EmptyDataError("The {} membership table carries no sector column".format(index))
    grouped = (
        df.groupby("sector")
        .agg(constituents=("symbol", "count"), examples=("symbol", lambda s: ", ".join(s.head(5))))
        .sort_values("constituents", ascending=False)
    )
    grouped["weight_by_count"] = (grouped["constituents"] / grouped["constituents"].sum()).round(4)
    return Result(grouped, provider=src, index_name="sector")


@command("/index/multiples", providers=("multpl",),
         summary="Long-run S&P 500 valuation history (CAPE, P/E, yield)")
def index_multiples(series: str = "shiller_pe", frequency: str = "month",
                    provider: Optional[str] = None) -> Result:
    """Series: shiller_pe, pe_ratio, dividend_yield, earnings_yield, price_to_book, price_to_sales…"""
    src = resolve_provider(provider, ("multpl",))
    return Result(markets.multpl(series, frequency), provider=src)


@command("/index/multiples_available", providers=("multpl",), summary="Valuation series available")
def index_multiples_available() -> Result:
    return Result([{"series": k, "slug": v} for k, v in sorted(markets.MULTPL_SERIES.items())],
                  provider="multpl")
