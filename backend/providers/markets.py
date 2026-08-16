"""Assorted key-less market providers: Stooq, Cboe, Nasdaq, Wikipedia, multpl.

* **Stooq** — a second free source of daily OHLCV, useful when Yahoo throttles.
* **Cboe** — delayed options chains and the US index definitions file.
* **Nasdaq** — the public calendar endpoints (earnings, dividends, splits, IPO).
* **Wikipedia** — index membership tables (S&P 500, Nasdaq-100, DAX, …).
* **multpl** — long-run S&P 500 valuation series (P/E, dividend yield, CAPE).
"""
from __future__ import annotations

import io
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ..core.caching import TTL_DAILY, TTL_INTRADAY, TTL_REFERENCE, cached
from ..core.errors import EmptyDataError, ProviderError
from ..core.http import get_csv, get_json, get_text
from ..core.utils import tidy_ohlcv

NAME = "markets"

STOOQ = "https://stooq.com/q/d/l/"
CBOE_OPTIONS = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
CBOE_INDICES = "https://cdn.cboe.com/api/global/us_indices/definitions/all_indices.json"
NASDAQ = "https://api.nasdaq.com/api"

_NASDAQ_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}


# --------------------------------------------------------------------------- #
# Stooq
# --------------------------------------------------------------------------- #
def _stooq_symbol(symbol: str) -> str:
    s = symbol.strip().lower()
    if s.startswith("^") or "." in s or s.endswith("=f") or len(s) == 6:
        return s.replace("^", "^")
    return s + ".us"


@cached("stooq.history", ttl=TTL_DAILY)
def stooq_history(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    interval: str = "d",
) -> pd.DataFrame:
    """Daily/weekly/monthly OHLCV from Stooq."""
    if interval not in ("d", "w", "m", "q", "y"):
        raise ValueError("interval must be d, w, m, q or y")
    params: Dict[str, Any] = {"s": _stooq_symbol(symbol), "i": interval}
    if start_date:
        params["d1"] = pd.Timestamp(start_date).strftime("%Y%m%d")
    if end_date:
        params["d2"] = pd.Timestamp(end_date).strftime("%Y%m%d")
    df = get_csv(STOOQ, params=params, ttl=TTL_DAILY)
    if df.empty or "Date" not in df.columns:
        raise EmptyDataError("Stooq has no data for {}".format(symbol))
    df = df.set_index(pd.to_datetime(df["Date"], errors="coerce")).drop(columns=["Date"])
    return tidy_ohlcv(df)


# --------------------------------------------------------------------------- #
# Cboe
# --------------------------------------------------------------------------- #
_CBOE_CONTRACT = re.compile(r"^(?P<root>[A-Z]+)(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})(?P<cp>[CP])(?P<strike>\d{8})$")


@cached("cboe.options", ttl=TTL_INTRADAY)
def cboe_option_chain(symbol: str) -> pd.DataFrame:
    """Delayed full options chain (all expiries) from Cboe's public feed."""
    payload = get_json(CBOE_OPTIONS.format(symbol=symbol.upper()), ttl=TTL_INTRADAY)
    data = payload.get("data") or {}
    options = data.get("options") or []
    if not options:
        raise EmptyDataError("Cboe published no option chain for {}".format(symbol))
    df = pd.DataFrame(options)
    parsed = df["option"].str.extract(_CBOE_CONTRACT)
    df["expiration"] = pd.to_datetime(
        "20" + parsed["y"] + "-" + parsed["m"] + "-" + parsed["d"], errors="coerce"
    )
    df["option_type"] = parsed["cp"].map({"C": "call", "P": "put"})
    df["strike"] = pd.to_numeric(parsed["strike"], errors="coerce") / 1000.0
    df["underlying_symbol"] = symbol.upper()
    df["underlying_price"] = data.get("close")
    df = df.rename(columns={"option": "contract_symbol", "iv": "implied_volatility",
                            "open_interest": "open_interest", "last_trade_price": "last_price",
                            "last_trade_time": "last_trade_time", "percent_change": "change_percent"})
    front = ["underlying_symbol", "expiration", "strike", "option_type", "contract_symbol",
             "bid", "ask", "last_price", "volume", "open_interest", "implied_volatility",
             "delta", "gamma", "theta", "vega", "rho", "theo", "underlying_price"]
    ordered = [c for c in front if c in df.columns] + [c for c in df.columns if c not in front]
    return df[ordered].sort_values(["expiration", "strike", "option_type"]).reset_index(drop=True)


@cached("cboe.indices", ttl=TTL_REFERENCE)
def cboe_index_definitions() -> pd.DataFrame:
    payload = get_json(CBOE_INDICES, ttl=TTL_REFERENCE)
    if not payload:
        raise EmptyDataError("Cboe returned no index definitions")
    df = pd.DataFrame(payload)
    return df.rename(columns={"index_symbol": "symbol", "name": "name"})


# --------------------------------------------------------------------------- #
# Nasdaq calendars & screener
# --------------------------------------------------------------------------- #
def _nasdaq(path: str, params: Optional[Dict[str, Any]] = None, ttl: int = TTL_INTRADAY) -> Any:
    payload = get_json("{}/{}".format(NASDAQ, path.lstrip("/")), params=params,
                       headers=_NASDAQ_HEADERS, ttl=ttl)
    status = (payload.get("status") or {}).get("rCode")
    if status not in (200, None):
        raise ProviderError("Nasdaq API error {}: {}".format(status, payload.get("status")))
    return payload.get("data") or {}


def nasdaq_calendar(kind: str = "earnings", day: Optional[str] = None) -> pd.DataFrame:
    """Nasdaq's public earnings/dividends/splits/IPO calendars."""
    day = day or str(date.today())
    if kind == "ipo":
        data = _nasdaq("calendar/ipo", {"date": day[:7]})
        rows: List[Dict[str, Any]] = []
        for bucket in ("priced", "upcoming", "filed", "withdrawn"):
            node = data.get(bucket) or {}
            table = node.get("rows") if isinstance(node, dict) else None
            if table is None and isinstance(node, dict):
                table = (node.get("upcomingTable") or {}).get("rows")
            for r in table or []:
                r = dict(r)
                r["status"] = bucket
                rows.append(r)
        if not rows:
            raise EmptyDataError("Nasdaq listed no IPOs for {}".format(day[:7]))
        return pd.DataFrame(rows)

    endpoint = {"earnings": "calendar/earnings", "dividends": "calendar/dividends",
                "splits": "calendar/splits"}.get(kind)
    if not endpoint:
        raise ValueError("kind must be earnings, dividends, splits or ipo")
    # Nothing is scheduled on weekends and holidays, so roll forward to the next
    # session that actually has events rather than reporting "no data".
    start = pd.Timestamp(day)
    for offset in range(0, 7):
        target = (start + pd.Timedelta(days=offset)).date()
        data = _nasdaq(endpoint, {"date": str(target)})
        # Earnings puts rows at the top level; dividends and splits nest them
        # one level down under "calendar".
        rows = data.get("rows") or (data.get("calendar") or {}).get("rows") or []
        if rows:
            df = pd.DataFrame(rows)
            df.columns = [_snake(c) for c in df.columns]
            df.insert(0, "calendar_date", str(target))
            return df
    raise EmptyDataError("Nasdaq listed no {} events in the week from {}".format(kind, day))


def nasdaq_screener(exchange: Optional[str] = None, limit: int = 500) -> pd.DataFrame:
    """The full Nasdaq/NYSE/AMEX listed-company table with sector and cap."""
    params = {"tableonly": "true", "limit": min(limit, 10000), "download": "true"}
    if exchange:
        params["exchange"] = exchange.lower()
    data = _nasdaq("screener/stocks", params, ttl=TTL_DAILY)
    rows = data.get("rows") or []
    if not rows:
        raise EmptyDataError("Nasdaq screener returned no rows")
    df = pd.DataFrame(rows)
    df.columns = [_snake(c) for c in df.columns]
    # The raw payload mixes naming styles: lastsale/netchange arrive lowercase
    # but marketCap is camelCase, so it lands as market_cap after _snake.
    for col in ("lastsale", "netchange", "marketcap", "market_cap", "pctchange", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace(r"[$,%]", "", regex=True), errors="coerce"
            )
    return df.head(limit)


def _snake(name: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name))
    return s.replace(" ", "_").replace("%", "pct").lower()


# --------------------------------------------------------------------------- #
# Index membership (Wikipedia)
# --------------------------------------------------------------------------- #
# index key -> (article, table match phrase, symbol column, name column)
INDEX_SOURCES: Dict[str, Tuple[str, str, str, str]] = {
    "sp500": ("List_of_S%26P_500_companies", "Symbol", "Symbol", "Security"),
    "sp400": ("List_of_S%26P_400_companies", "Symbol", "Symbol", "Security"),
    "sp600": ("List_of_S%26P_600_companies", "Symbol", "Symbol", "Company"),
    "nasdaq100": ("Nasdaq-100", "Ticker", "Ticker", "Company"),
    "dowjones": ("Dow_Jones_Industrial_Average", "Symbol", "Symbol", "Company"),
    "ftse100": ("FTSE_100_Index", "Ticker", "Ticker", "Company"),
    "dax": ("DAX", "Ticker", "Ticker", "Company"),
    "cac40": ("CAC_40", "Ticker", "Ticker", "Company"),
    "nikkei225": ("Nikkei_225", "Company", "Code", "Company"),
    "russell1000": ("Russell_1000_Index", "Symbol", "Symbol", "Company"),
    "sptsx60": ("S%26P/TSX_60", "Symbol", "Symbol", "Company"),
    "euro_stoxx_50": ("EURO_STOXX_50", "Ticker", "Ticker", "Name"),
    "ibex35": ("IBEX_35", "Ticker", "Ticker", "Company"),
    "asx200": ("S%26P/ASX_200", "Code", "Code", "Company"),
    "smi": ("Swiss_Market_Index", "Ticker", "Ticker", "Company"),
    "hangseng": ("Hang_Seng_Index", "Ticker", "Ticker", "Company"),
}


# Indexes slickcharts.com also lists — the fallback when a Wikipedia article
# drops its components table (the Nasdaq-100 article did in 2026).
SLICKCHARTS_PAGES: Dict[str, str] = {
    "sp500": "sp500", "nasdaq100": "nasdaq100", "dowjones": "dowjones",
}


@cached("slickcharts.constituents", ttl=TTL_DAILY)
def slickcharts_constituents(index: str) -> pd.DataFrame:
    """Index membership (symbol, name, weight) from slickcharts.com."""
    page = SLICKCHARTS_PAGES.get(index.lower())
    if not page:
        raise ValueError("index must be one of {}".format(", ".join(sorted(SLICKCHARTS_PAGES))))
    html = get_text("https://www.slickcharts.com/" + page, ttl=TTL_DAILY)
    try:
        # Pin the parser: pandas' bs4 fallback needs html5lib, which is not a
        # dependency, and its ImportError would mask the real "no table" error.
        tables = pd.read_html(io.StringIO(html), match="Symbol", flavor="lxml")
    except ValueError as exc:
        raise ProviderError("Could not locate the slickcharts {} table: {}".format(index, exc))
    df = max(tables, key=len).copy()
    df = df.rename(columns={"Symbol": "symbol", "Company": "name", "Weight": "weight"})
    df["symbol"] = df["symbol"].astype(str).str.strip().str.replace(".", "-", regex=False)
    if "weight" in df.columns:
        df["weight"] = pd.to_numeric(
            df["weight"].astype(str).str.rstrip("%"), errors="coerce"
        ) / 100
    keep = [c for c in ("symbol", "name", "weight") if c in df.columns]
    return df[keep].dropna(subset=["symbol"]).reset_index(drop=True)


@cached("wikipedia.constituents", ttl=TTL_REFERENCE)
def index_constituents(index: str = "sp500") -> pd.DataFrame:
    """Current membership of a major equity index, scraped from Wikipedia."""
    key = index.lower().replace("^", "").replace("-", "").replace(" ", "")
    source = INDEX_SOURCES.get(key)
    if not source:
        raise ValueError("index must be one of {}".format(", ".join(sorted(INDEX_SOURCES))))
    try:
        return _wikipedia_constituents(key)
    except (ProviderError, EmptyDataError):
        if key in SLICKCHARTS_PAGES:
            return slickcharts_constituents(key)
        raise


def _wikipedia_constituents(key: str) -> pd.DataFrame:
    article, match, sym_col, name_col = INDEX_SOURCES[key]
    html = get_text("https://en.wikipedia.org/wiki/" + article, ttl=TTL_REFERENCE)
    try:
        tables = pd.read_html(io.StringIO(html), match=match, flavor="lxml")
    except ValueError as exc:
        raise ProviderError("Could not locate the {} membership table: {}".format(key, exc))
    df = max(tables, key=len).copy()
    # Flatten any MultiIndex header and squash internal whitespace, but keep the
    # full label — "GICS Sector" must not be truncated to "GICS".
    df.columns = [
        " ".join(str(c if not isinstance(c, tuple) else " ".join(map(str, c))).split())
        for c in df.columns
    ]
    rename: Dict[str, str] = {}
    for col in df.columns:
        low = col.lower()
        if "sub-industry" in low or "sub industry" in low or low.endswith("industry"):
            rename[col] = "industry"
        elif "sector" in low:
            rename[col] = "sector"
        elif low.startswith(sym_col.lower()):
            rename[col] = "symbol"
        elif low.startswith(name_col.lower()):
            rename[col] = "name"
        elif "headquarters" in low or "location" in low:
            rename[col] = "headquarters"
        elif "date" in low and "added" in low:
            rename[col] = "date_added"
        elif low == "cik":
            rename[col] = "cik"
    df = df.rename(columns=rename)
    df = df.loc[:, ~df.columns.duplicated()]
    if "symbol" not in df.columns:
        raise ProviderError("Membership table for {} has no symbol column".format(key))
    df["symbol"] = df["symbol"].astype(str).str.strip().str.replace(".", "-", regex=False)
    keep = [c for c in ("symbol", "name", "sector", "industry", "headquarters", "date_added", "cik")
            if c in df.columns]
    return df[keep].dropna(subset=["symbol"]).reset_index(drop=True)


def available_indices() -> pd.DataFrame:
    return pd.DataFrame(
        [{"index": k, "source": "wikipedia:" + v[0]} for k, v in sorted(INDEX_SOURCES.items())]
    )


# --------------------------------------------------------------------------- #
# multpl — long-run S&P 500 valuation series
# --------------------------------------------------------------------------- #
MULTPL_SERIES: Dict[str, str] = {
    "pe_ratio": "s-p-500-pe-ratio",
    "shiller_pe": "shiller-pe",
    "dividend_yield": "s-p-500-dividend-yield",
    "dividend": "s-p-500-dividend",
    "earnings": "s-p-500-earnings",
    "earnings_yield": "s-p-500-earnings-yield",
    "price_to_book": "s-p-500-price-to-book",
    "price_to_sales": "s-p-500-price-to-sales",
    "book_value": "s-p-500-book-value",
    "sales": "s-p-500-sales",
    "real_price": "inflation-adjusted-s-p-500",
}


@cached("multpl.series", ttl=TTL_REFERENCE)
def multpl(series: str = "shiller_pe", frequency: str = "month") -> pd.DataFrame:
    """S&P 500 valuation history (CAPE, P/E, dividend yield, …) from multpl.com."""
    slug = MULTPL_SERIES.get(series.lower())
    if not slug:
        raise ValueError("series must be one of {}".format(", ".join(sorted(MULTPL_SERIES))))
    if frequency not in ("month", "year"):
        raise ValueError("frequency must be month or year")
    url = "https://www.multpl.com/{}/table/by-{}".format(slug, frequency)
    tables = pd.read_html(io.StringIO(get_text(url, ttl=TTL_REFERENCE)))
    if not tables:
        raise EmptyDataError("multpl returned no table for {}".format(series))
    df = tables[0]
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["value"] = pd.to_numeric(
        df["value"].astype(str).str.replace(r"[^0-9.\-]", "", regex=True), errors="coerce"
    )
    df["series"] = series
    return df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
