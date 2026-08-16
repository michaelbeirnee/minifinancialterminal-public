"""Yahoo Finance provider (via the open-source ``yfinance`` client).

Free, key-less, and by far the widest single source: prices for every asset
class, fundamentals, holders, estimates, options chains, fund holdings,
screeners and calendars. Yahoo's endpoints are unofficial and occasionally
return nothing for a given symbol, so each helper raises a ``ProviderError``
rather than handing back an empty frame that would read as "no such data".
"""
from __future__ import annotations

import warnings
from datetime import date
from typing import Any, Dict, List, Optional

import pandas as pd

from ..core.caching import (
    TTL_DAILY,
    TTL_FUNDAMENTAL,
    TTL_INTRADAY,
    TTL_QUOTE,
    TTL_REFERENCE,
    cached,
)
from ..core.errors import EmptyDataError, ProviderError
from ..core.utils import tidy_ohlcv

warnings.filterwarnings("ignore", module="yfinance")

NAME = "yahoo"

# Yahoo's interval vocabulary; anything else is rejected up front.
INTERVALS = ("1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo", "3mo")
INTRADAY = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"}


def _yf():
    import yfinance as yf

    return yf


def _ticker(symbol: str):
    return _yf().Ticker(symbol.upper())


def _guard(value: Any, what: str) -> Any:
    """Raise a useful error instead of returning an empty payload."""
    empty = (
        value is None
        or (isinstance(value, (pd.DataFrame, pd.Series)) and value.empty)
        or (isinstance(value, (list, dict, tuple)) and len(value) == 0)
    )
    if empty:
        raise EmptyDataError("Yahoo Finance returned no {}".format(what))
    return value


def _call(fn, what: str, *args: Any, **kwargs: Any) -> Any:
    try:
        return _guard(fn(*args, **kwargs), what)
    except EmptyDataError:
        raise
    except Exception as exc:  # noqa: BLE001 - yfinance raises many shapes
        raise ProviderError("Yahoo Finance failed fetching {}: {}".format(what, exc))


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #
@cached("yahoo.history", ttl=TTL_DAILY)
def history(
    symbol: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    interval: str = "1d",
    period: Optional[str] = None,
    prepost: bool = False,
    adjusted: bool = True,
) -> pd.DataFrame:
    """OHLCV bars for one symbol."""
    if interval not in INTERVALS:
        raise ValueError("interval must be one of {}".format(", ".join(INTERVALS)))
    kwargs: Dict[str, Any] = {
        "interval": interval,
        "auto_adjust": adjusted,
        "prepost": prepost,
        "actions": True,
        "raise_errors": False,
    }
    if period:
        kwargs["period"] = period
    else:
        kwargs["start"] = start
        kwargs["end"] = end
    raw = _call(_ticker(symbol).history, "price history for {}".format(symbol), **kwargs)
    df = tidy_ohlcv(raw)
    if interval not in INTRADAY and isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


@cached("yahoo.quote", ttl=TTL_QUOTE)
def quote(symbol: str) -> Dict[str, Any]:
    """Snapshot quote merged from Yahoo's fast-quote and profile payloads."""
    t = _ticker(symbol)
    out: Dict[str, Any] = {"symbol": symbol.upper()}
    try:
        fast = dict(t.fast_info)
    except Exception:  # noqa: BLE001
        fast = {}
    try:
        info = dict(t.info or {})
    except Exception:  # noqa: BLE001
        info = {}
    if not fast and not info:
        raise ProviderError("Yahoo Finance returned no quote for {}".format(symbol))

    last = info.get("regularMarketPrice") or fast.get("last_price")
    prev = info.get("regularMarketPreviousClose") or fast.get("previous_close")
    change = (last - prev) if (last is not None and prev not in (None, 0)) else None
    out.update(
        {
            "name": info.get("longName") or info.get("shortName"),
            "exchange": info.get("fullExchangeName") or fast.get("exchange"),
            "currency": info.get("currency") or fast.get("currency"),
            "last_price": last,
            "open": info.get("regularMarketOpen") or fast.get("open"),
            "high": info.get("regularMarketDayHigh") or fast.get("day_high"),
            "low": info.get("regularMarketDayLow") or fast.get("day_low"),
            "prev_close": prev,
            "change": change,
            "change_percent": (change / prev) if (change is not None and prev) else None,
            "volume": info.get("regularMarketVolume") or fast.get("last_volume"),
            "avg_volume": info.get("averageVolume") or fast.get("three_month_average_volume"),
            "market_cap": info.get("marketCap") or fast.get("market_cap"),
            "shares_outstanding": info.get("sharesOutstanding") or fast.get("shares"),
            "year_high": info.get("fiftyTwoWeekHigh") or fast.get("year_high"),
            "year_low": info.get("fiftyTwoWeekLow") or fast.get("year_low"),
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "eps": info.get("trailingEps"),
            "dividend_yield": info.get("dividendYield"),
            "beta": info.get("beta"),
            "bid": info.get("bid"),
            "ask": info.get("ask"),
            "bid_size": info.get("bidSize"),
            "ask_size": info.get("askSize"),
        }
    )
    return out


@cached("yahoo.info", ttl=TTL_FUNDAMENTAL)
def info(symbol: str) -> Dict[str, Any]:
    return dict(_call(lambda: _ticker(symbol).info, "profile for {}".format(symbol)))


# --------------------------------------------------------------------------- #
# Fundamentals
# --------------------------------------------------------------------------- #
_STATEMENTS = {
    "income": ("income_stmt", "quarterly_income_stmt", "ttm_income_stmt"),
    "balance": ("balance_sheet", "quarterly_balance_sheet", None),
    "cash": ("cashflow", "quarterly_cashflow", "ttm_cashflow"),
}


@cached("yahoo.statement", ttl=TTL_FUNDAMENTAL)
def statement(symbol: str, kind: str = "income", period: str = "annual") -> pd.DataFrame:
    """Financial statement as one row per reporting period."""
    if kind not in _STATEMENTS:
        raise ValueError("kind must be one of {}".format(", ".join(_STATEMENTS)))
    annual, quarterly, ttm = _STATEMENTS[kind]
    attr = {"annual": annual, "quarter": quarterly, "quarterly": quarterly, "ttm": ttm}.get(period)
    if not attr:
        raise ValueError("period must be annual, quarter or ttm")
    raw = _call(lambda: getattr(_ticker(symbol), attr), "{} statement for {}".format(kind, symbol))
    df = raw.T  # Yahoo returns line items as rows; we want periods as rows.
    df.index = pd.to_datetime(df.index, errors="coerce")
    df.index.name = "period_ending"
    df.columns = [str(c).strip().lower().replace(" ", "_").replace("&", "and") for c in df.columns]
    return df.sort_index()


@cached("yahoo.actions", ttl=TTL_FUNDAMENTAL)
def dividends(symbol: str) -> pd.Series:
    return _call(lambda: _ticker(symbol).dividends, "dividends for {}".format(symbol)).rename("dividend")


@cached("yahoo.splits", ttl=TTL_FUNDAMENTAL)
def splits(symbol: str) -> pd.Series:
    return _call(lambda: _ticker(symbol).splits, "splits for {}".format(symbol)).rename("split_ratio")


@cached("yahoo.earnings_dates", ttl=TTL_FUNDAMENTAL)
def earnings_dates(symbol: str, limit: int = 24) -> pd.DataFrame:
    return _call(_ticker(symbol).get_earnings_dates, "earnings dates for {}".format(symbol), limit=limit)


@cached("yahoo.shares", ttl=TTL_FUNDAMENTAL)
def shares_full(symbol: str, start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    return _call(
        _ticker(symbol).get_shares_full, "share count for {}".format(symbol), start=start, end=end
    ).rename("shares_outstanding")


@cached("yahoo.calendar", ttl=TTL_FUNDAMENTAL)
def calendar(symbol: str) -> Dict[str, Any]:
    return dict(_call(lambda: _ticker(symbol).calendar, "calendar for {}".format(symbol)))


@cached("yahoo.filings", ttl=TTL_FUNDAMENTAL)
def sec_filings(symbol: str) -> pd.DataFrame:
    raw = _call(lambda: _ticker(symbol).sec_filings, "filings for {}".format(symbol))
    return pd.DataFrame(raw)


# --------------------------------------------------------------------------- #
# Ownership & sentiment
# --------------------------------------------------------------------------- #
_HOLDER_ATTRS = {
    "institutional": "institutional_holders",
    "mutualfund": "mutualfund_holders",
    "major": "major_holders",
    "insider_roster": "insider_roster_holders",
    "insider_purchases": "insider_purchases",
}


@cached("yahoo.holders", ttl=TTL_FUNDAMENTAL)
def holders(symbol: str, kind: str = "institutional") -> pd.DataFrame:
    attr = _HOLDER_ATTRS.get(kind)
    if not attr:
        raise ValueError("kind must be one of {}".format(", ".join(_HOLDER_ATTRS)))
    raw = _call(lambda: getattr(_ticker(symbol), attr), "{} holders for {}".format(kind, symbol))
    return pd.DataFrame(raw)


@cached("yahoo.insider", ttl=TTL_FUNDAMENTAL)
def insider_transactions(symbol: str) -> pd.DataFrame:
    return pd.DataFrame(
        _call(lambda: _ticker(symbol).insider_transactions, "insider trades for {}".format(symbol))
    )


@cached("yahoo.recommendations", ttl=TTL_FUNDAMENTAL)
def recommendations(symbol: str) -> pd.DataFrame:
    return pd.DataFrame(
        _call(lambda: _ticker(symbol).recommendations, "recommendations for {}".format(symbol))
    )


@cached("yahoo.upgrades", ttl=TTL_FUNDAMENTAL)
def upgrades_downgrades(symbol: str) -> pd.DataFrame:
    return pd.DataFrame(
        _call(lambda: _ticker(symbol).upgrades_downgrades, "analyst actions for {}".format(symbol))
    )


@cached("yahoo.price_targets", ttl=TTL_FUNDAMENTAL)
def price_targets(symbol: str) -> Dict[str, Any]:
    return dict(_call(lambda: _ticker(symbol).analyst_price_targets, "price targets for {}".format(symbol)))


_ESTIMATE_ATTRS = {
    "earnings": "earnings_estimate",
    "revenue": "revenue_estimate",
    "eps_trend": "eps_trend",
    "eps_revisions": "eps_revisions",
    "growth": "growth_estimates",
}


@cached("yahoo.estimates", ttl=TTL_FUNDAMENTAL)
def estimates(symbol: str, kind: str = "earnings") -> pd.DataFrame:
    attr = _ESTIMATE_ATTRS.get(kind)
    if not attr:
        raise ValueError("kind must be one of {}".format(", ".join(_ESTIMATE_ATTRS)))
    return pd.DataFrame(
        _call(lambda: getattr(_ticker(symbol), attr), "{} estimates for {}".format(kind, symbol))
    )


@cached("yahoo.news", ttl=TTL_INTRADAY)
def news(symbol: str, limit: int = 25) -> List[Dict[str, Any]]:
    return _call(_ticker(symbol).get_news, "news for {}".format(symbol), count=limit)


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #
@cached("yahoo.expirations", ttl=TTL_INTRADAY)
def option_expirations(symbol: str) -> List[str]:
    return list(_call(lambda: _ticker(symbol).options, "option expirations for {}".format(symbol)))


@cached("yahoo.chain", ttl=TTL_INTRADAY)
def option_chain(symbol: str, expiration: Optional[str] = None) -> pd.DataFrame:
    """Full calls+puts chain for one expiry (or the nearest one)."""
    t = _ticker(symbol)
    expirations = option_expirations(symbol)
    expiry = expiration or expirations[0]
    if expiry not in expirations:
        raise ValueError(
            "No {} expiry for {}. Available: {}".format(expiry, symbol, ", ".join(expirations[:12]))
        )
    chain = _call(t.option_chain, "option chain for {}".format(symbol), expiry)
    frames = []
    for side, frame in (("call", chain.calls), ("put", chain.puts)):
        if frame is None or frame.empty:
            continue
        f = frame.copy()
        f["option_type"] = side
        f["expiration"] = expiry
        f["underlying_symbol"] = symbol.upper()
        frames.append(f)
    if not frames:
        raise EmptyDataError("No option contracts for {} {}".format(symbol, expiry))
    df = pd.concat(frames, ignore_index=True)
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df.rename(columns={"contractsymbol": "contract_symbol", "lasttradedate": "last_trade_date",
                              "impliedvolatility": "implied_volatility", "openinterest": "open_interest",
                              "percentchange": "change_percent", "lastprice": "last_price",
                              "inthemoney": "in_the_money", "contractsize": "contract_size"})


# --------------------------------------------------------------------------- #
# Funds / ETFs
# --------------------------------------------------------------------------- #
@cached("yahoo.fund", ttl=TTL_FUNDAMENTAL)
def fund_data(symbol: str, section: str = "top_holdings") -> Any:
    """ETF/mutual-fund details: holdings, sector & asset-class weights, profile."""
    valid = (
        "description", "fund_overview", "fund_operations", "asset_classes",
        "top_holdings", "equity_holdings", "bond_holdings", "bond_ratings",
        "sector_weightings", "quarterly_returns",
    )
    if section not in valid:
        raise ValueError("section must be one of {}".format(", ".join(valid)))
    return _call(
        lambda: getattr(_ticker(symbol).funds_data, section), "{} for {}".format(section, symbol)
    )


# --------------------------------------------------------------------------- #
# Search, screening, sectors, calendars
# --------------------------------------------------------------------------- #
@cached("yahoo.search", ttl=TTL_DAILY)
def search(query: str, limit: int = 25) -> pd.DataFrame:
    res = _call(lambda: _yf().Search(query, max_results=limit, news_count=0).quotes, "search hits")
    return pd.DataFrame(res)


@cached("yahoo.lookup", ttl=TTL_DAILY)
def lookup(query: str, kind: str = "all", limit: int = 25) -> pd.DataFrame:
    """Symbol lookup restricted to an instrument type (equity, etf, future…)."""
    lk = _yf().Lookup(query)
    getter = {
        "all": lk.get_all, "equity": lk.get_stock, "etf": lk.get_etf, "future": lk.get_future,
        "index": lk.get_index, "mutualfund": lk.get_mutualfund, "currency": lk.get_currency,
        "cryptocurrency": lk.get_cryptocurrency,
    }.get(kind)
    if getter is None:
        raise ValueError("kind must be all, equity, etf, future, index, mutualfund, currency or cryptocurrency")
    return pd.DataFrame(_call(getter, "lookup results", count=limit))


@cached("yahoo.screen", ttl=TTL_INTRADAY)
def predefined_screen(name: str, limit: int = 50) -> pd.DataFrame:
    yf = _yf()
    if name not in yf.PREDEFINED_SCREENER_QUERIES:
        raise ValueError(
            "Unknown screener {!r}. Available: {}".format(
                name, ", ".join(sorted(yf.PREDEFINED_SCREENER_QUERIES))
            )
        )
    body = _call(yf.screen, "screener {}".format(name), name, count=min(limit, 250))
    quotes = body.get("quotes") if isinstance(body, dict) else body
    return pd.DataFrame(_guard(quotes, "screener rows"))


def screener_names() -> List[str]:
    return sorted(_yf().PREDEFINED_SCREENER_QUERIES)


@cached("yahoo.equity_screen", ttl=TTL_INTRADAY)
def equity_screen(filters: List[List[Any]], limit: int = 50, sort_field: Optional[str] = None,
                  sort_asc: bool = False) -> pd.DataFrame:
    """Custom screen. ``filters`` is a list of ``[operator, field, value...]``."""
    yf = _yf()
    clauses = []
    for f in filters:
        op = str(f[0]).lower()
        clauses.append(yf.EquityQuery(op, list(f[1:])))
    query = clauses[0] if len(clauses) == 1 else yf.EquityQuery("and", clauses)
    body = _call(yf.screen, "custom screen", query, count=min(limit, 250),
                 sortField=sort_field, sortAsc=sort_asc)
    quotes = body.get("quotes") if isinstance(body, dict) else body
    return pd.DataFrame(_guard(quotes, "screener rows"))


@cached("yahoo.sector", ttl=TTL_DAILY)
def sector(key: str, section: str = "overview") -> Any:
    s = _yf().Sector(key)
    valid = ("overview", "top_companies", "top_etfs", "top_mutual_funds", "industries", "research_reports")
    if section not in valid:
        raise ValueError("section must be one of {}".format(", ".join(valid)))
    return _call(lambda: getattr(s, section), "sector {} {}".format(key, section))


@cached("yahoo.industry", ttl=TTL_DAILY)
def industry(key: str, section: str = "overview") -> Any:
    i = _yf().Industry(key)
    valid = ("overview", "top_companies", "top_performing", "top_growth_companies", "research_reports")
    if section not in valid:
        raise ValueError("section must be one of {}".format(", ".join(valid)))
    return _call(lambda: getattr(i, section), "industry {} {}".format(key, section))


# Yahoo matches these case-sensitively, so normalise before handing them over.
MARKETS = ("US", "GB", "ASIA", "EUROPE", "RATES", "COMMODITIES", "CURRENCIES", "CRYPTOCURRENCIES")


def _market(market: str) -> str:
    key = str(market).strip().upper()
    if key not in MARKETS:
        raise ValueError("market must be one of {}".format(", ".join(MARKETS)))
    return key


@cached("yahoo.market", ttl=TTL_QUOTE)
def market_summary(market: str = "US") -> pd.DataFrame:
    key = _market(market)
    summary = _call(lambda: _yf().Market(key).summary, "market summary for {}".format(key))
    return pd.DataFrame(summary).T if isinstance(summary, dict) else pd.DataFrame(summary)


@cached("yahoo.market_status", ttl=TTL_QUOTE)
def market_status(market: str = "US") -> Dict[str, Any]:
    key = _market(market)
    return dict(_call(lambda: _yf().Market(key).status, "market status for {}".format(key)))


_CALENDAR_ATTRS = {
    "earnings": "get_earnings_calendar",
    "splits": "get_splits_calendar",
    "ipo": "get_ipo_info_calendar",
    "economic": "get_economic_events_calendar",
}


@cached("yahoo.calendars", ttl=TTL_INTRADAY)
def market_calendar(kind: str = "earnings", start: Optional[str] = None,
                    end: Optional[str] = None) -> pd.DataFrame:
    attr = _CALENDAR_ATTRS.get(kind)
    if not attr:
        raise ValueError("kind must be one of {}".format(", ".join(_CALENDAR_ATTRS)))
    cal = _yf().Calendars(start=start, end=end)
    return pd.DataFrame(_call(getattr(cal, attr), "{} calendar".format(kind)))


# --------------------------------------------------------------------------- #
# Multi-symbol convenience
# --------------------------------------------------------------------------- #
def close_panel(symbols: List[str], start: Optional[str] = None, end: Optional[str] = None,
                interval: str = "1d") -> pd.DataFrame:
    """Wide close-price frame (columns = symbols), inner-joined on dates."""
    series: Dict[str, pd.Series] = {}
    errors: List[str] = []
    for sym in symbols:
        try:
            series[sym.upper()] = history(sym, start, end, interval)["close"]
        except Exception as exc:  # noqa: BLE001 - keep the rest of the panel
            errors.append("{}: {}".format(sym, exc))
    if not series:
        raise ProviderError("No price data for any of {}. {}".format(", ".join(symbols), "; ".join(errors)))
    panel = pd.DataFrame(series).sort_index()
    panel.attrs["errors"] = errors
    return panel
