"""Equity menu: prices, search, screening, discovery, calendars, shorts."""
from __future__ import annotations

from datetime import date
from typing import Any, List, Optional

import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import date_window, norm_symbols, one_symbol, pct_change_table
from ..providers import finra, markets, sec, yahoo

# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #
@command("/equity/price/historical", providers=("yahoo", "stooq"),
         summary="Historical OHLCV bars for one or more equities")
def price_historical(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    interval: str = "1d",
    provider: Optional[str] = None,
) -> Result:
    """Daily or intraday bars.

    ``symbol`` accepts a comma-separated list; multi-symbol responses carry a
    ``symbol`` column. Yahoo supports intraday intervals (``1m``…``1h``) for
    recent windows only; Stooq is daily-and-slower but rarely rate-limits.
    """
    src = resolve_provider(provider, ("yahoo", "stooq"))
    symbols = norm_symbols(symbol)
    start, end = date_window(start_date, end_date)
    frames: List[pd.DataFrame] = []
    warnings: List[str] = []
    for sym in symbols:
        try:
            if src == "stooq":
                df = markets.stooq_history(sym, str(start), str(end))
            else:
                df = yahoo.history(sym, str(start), str(end), interval=interval)
        except Exception as exc:  # noqa: BLE001 - report per-symbol, keep the rest
            warnings.append("{}: {}".format(sym, exc))
            continue
        df = df.copy()
        if len(symbols) > 1:
            df.insert(0, "symbol", sym)
        frames.append(df)
    if not frames:
        raise EmptyDataError("No price history returned. {}".format("; ".join(warnings)))
    out = pd.concat(frames).sort_index()
    return Result(out, provider=src, warnings=warnings, index_name="date")


@command("/equity/price/quote", providers=("yahoo",), summary="Real-time-ish snapshot quote")
def price_quote(symbol: str, provider: Optional[str] = None) -> Result:
    """Last price, change, day range, 52-week range, volume and key multiples."""
    src = resolve_provider(provider, ("yahoo",))
    rows, warnings = [], []
    for sym in norm_symbols(symbol):
        try:
            rows.append(yahoo.quote(sym))
        except Exception as exc:  # noqa: BLE001
            warnings.append("{}: {}".format(sym, exc))
    if not rows:
        raise EmptyDataError("No quotes returned. {}".format("; ".join(warnings)))
    return Result(rows, provider=src, warnings=warnings)


@command("/equity/price/performance", providers=("yahoo",),
         summary="Trailing return table (1D through max) per symbol")
def price_performance(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    rows, warnings = [], []
    for sym in norm_symbols(symbol):
        try:
            closes = yahoo.history(sym, period="10y")["close"]
        except Exception as exc:  # noqa: BLE001
            warnings.append("{}: {}".format(sym, exc))
            continue
        rows.append(dict(symbol=sym, **pct_change_table(closes)))
    if not rows:
        raise EmptyDataError("No performance data. {}".format("; ".join(warnings)))
    return Result(rows, provider=src, warnings=warnings)


@command("/equity/profile", providers=("yahoo", "sec"), summary="Company profile and key statistics")
def profile(symbol: str, provider: Optional[str] = None) -> Result:
    """Sector, industry, headquarters, employee count, description and multiples."""
    src = resolve_provider(provider, ("yahoo", "sec"))
    sym = one_symbol(symbol)
    if src == "sec":
        payload = sec.submissions(sec.cik_for(sym))
        addr = (payload.get("addresses") or {}).get("business") or {}
        return Result(
            {
                "symbol": sym,
                "name": payload.get("name"),
                "cik": payload.get("cik"),
                "sic": payload.get("sic"),
                "sic_description": payload.get("sicDescription"),
                "exchange": ", ".join(payload.get("exchanges") or []),
                "state_of_incorporation": payload.get("stateOfIncorporation"),
                "fiscal_year_end": payload.get("fiscalYearEnd"),
                "phone": payload.get("phone"),
                "website": payload.get("website"),
                "city": addr.get("city"),
                "state": addr.get("stateOrCountry"),
                "former_names": [f.get("name") for f in payload.get("formerNames") or []],
            },
            provider=src,
        )
    info = yahoo.info(sym)
    keep = {
        "symbol": sym, "name": info.get("longName"), "exchange": info.get("fullExchangeName"),
        "currency": info.get("currency"), "sector": info.get("sector"), "industry": info.get("industry"),
        "country": info.get("country"), "city": info.get("city"), "website": info.get("website"),
        "employees": info.get("fullTimeEmployees"), "description": info.get("longBusinessSummary"),
        "market_cap": info.get("marketCap"), "enterprise_value": info.get("enterpriseValue"),
        "shares_outstanding": info.get("sharesOutstanding"), "float_shares": info.get("floatShares"),
        "beta": info.get("beta"), "trailing_pe": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"), "price_to_book": info.get("priceToBook"),
        "dividend_yield": info.get("dividendYield"), "payout_ratio": info.get("payoutRatio"),
        "profit_margin": info.get("profitMargins"), "return_on_equity": info.get("returnOnEquity"),
        "revenue_ttm": info.get("totalRevenue"), "ebitda": info.get("ebitda"),
        "total_debt": info.get("totalDebt"), "total_cash": info.get("totalCash"),
        "ir_website": info.get("irWebsite"),
    }
    return Result(keep, provider=src)


# --------------------------------------------------------------------------- #
# Search & screening
# --------------------------------------------------------------------------- #
@command("/equity/search", providers=("yahoo", "sec", "nasdaq"), summary="Find a ticker by name")
def search(query: str, limit: int = 25, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo", "sec", "nasdaq"))
    if src == "sec":
        return Result(sec.search_companies(query, limit), provider=src)
    if src == "nasdaq":
        df = markets.nasdaq_screener(limit=10000)
        q = query.lower()
        hit = df[df["name"].str.lower().str.contains(q, na=False, regex=False)
                 | df["symbol"].str.lower().eq(q)]
        if hit.empty:
            raise EmptyDataError("Nasdaq listings contain nothing matching {!r}".format(query))
        return Result(hit.head(limit), provider=src)
    return Result(yahoo.search(query, limit), provider=src)


@command("/equity/screener", providers=("yahoo", "nasdaq"), summary="Screen equities on fundamentals")
def screener(
    preset: Optional[str] = None,
    filters: Optional[str] = None,
    exchange: Optional[str] = None,
    sort_field: Optional[str] = None,
    limit: int = 50,
    provider: Optional[str] = None,
) -> Result:
    """Run a screen.

    ``preset`` uses one of Yahoo's saved screens (see
    ``/equity/screener_presets``). ``filters`` is a semicolon-separated list of
    ``operator,field,value`` clauses, e.g.
    ``"gt,intradaymarketcap,10000000000;lt,peratio,15"``.
    """
    src = resolve_provider(provider, ("yahoo", "nasdaq"))
    if src == "nasdaq":
        return Result(markets.nasdaq_screener(exchange=exchange, limit=limit), provider=src)
    if filters:
        parsed: List[List[Any]] = []
        for clause in filters.split(";"):
            bits = [b.strip() for b in clause.split(",") if b.strip()]
            if len(bits) < 3:
                raise ValueError("Each filter needs operator,field,value — got {!r}".format(clause))
            values: List[Any] = []
            for b in bits[2:]:
                try:
                    values.append(float(b))
                except ValueError:
                    values.append(b)
            parsed.append([bits[0], bits[1]] + values)
        return Result(yahoo.equity_screen(parsed, limit=limit, sort_field=sort_field), provider=src)
    return Result(yahoo.predefined_screen(preset or "most_actives", limit), provider=src)


@command("/equity/screener_presets", providers=("yahoo",), summary="Saved screener names")
def screener_presets() -> Result:
    return Result([{"preset": n} for n in yahoo.screener_names()], provider="yahoo")


@command("/equity/market_snapshots", providers=("yahoo",), summary="Broad market summary tiles")
def market_snapshots(market: str = "US", provider: Optional[str] = None) -> Result:
    """``market``: US, GB, ASIA, EUROPE, RATES, COMMODITIES, CURRENCIES or CRYPTOCURRENCIES."""
    src = resolve_provider(provider, ("yahoo",))
    return Result(yahoo.market_summary(market), provider=src, index_name="symbol")


@command("/equity/market_status", providers=("yahoo",), summary="Exchange open/closed status")
def market_status(market: str = "US", provider: Optional[str] = None) -> Result:
    """``market``: US, GB, ASIA, EUROPE, RATES, COMMODITIES, CURRENCIES or CRYPTOCURRENCIES."""
    src = resolve_provider(provider, ("yahoo",))
    return Result(yahoo.market_status(market), provider=src)


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
@command("/equity/compare/peers", providers=("yahoo",), summary="Peer companies in the same industry")
def compare_peers(symbol: str, limit: int = 25, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    sym = one_symbol(symbol)
    info = yahoo.info(sym)
    industry_key = info.get("industryKey")
    sector_key = info.get("sectorKey")
    if industry_key:
        df = yahoo.industry(industry_key, "top_companies")
    elif sector_key:
        df = yahoo.sector(sector_key, "top_companies")
    else:
        raise EmptyDataError("Yahoo does not classify {} into an industry".format(sym))
    df = pd.DataFrame(df)
    df.insert(0, "peer_of", sym)
    return Result(df.head(limit), provider=src, index_name="symbol",
                  extra={"industry": info.get("industry"), "sector": info.get("sector")})


@command("/equity/compare/groups", providers=("yahoo",),
         summary="Performance by sector, industry or asset class")
def compare_groups(group: str = "sector", provider: Optional[str] = None) -> Result:
    """Trailing performance for the 11 GICS sectors (via SPDR sector ETFs),
    the major style boxes, or the main asset classes."""
    src = resolve_provider(provider, ("yahoo",))
    universes = {
        "sector": {
            "Technology": "XLK", "Health Care": "XLV", "Financials": "XLF", "Energy": "XLE",
            "Consumer Discretionary": "XLY", "Consumer Staples": "XLP", "Industrials": "XLI",
            "Materials": "XLB", "Utilities": "XLU", "Real Estate": "XLRE",
            "Communication Services": "XLC",
        },
        "style": {
            "Large Growth": "IWF", "Large Value": "IWD", "Mid Cap": "IJH", "Small Growth": "IWO",
            "Small Value": "IWN", "Momentum": "MTUM", "Quality": "QUAL", "Low Volatility": "USMV",
        },
        "asset_class": {
            "US Equities": "SPY", "Intl Developed": "EFA", "Emerging Markets": "EEM",
            "US Aggregate Bonds": "AGG", "Long Treasuries": "TLT", "High Yield": "HYG",
            "TIPS": "TIP", "Gold": "GLD", "Commodities": "DBC", "REITs": "VNQ", "US Dollar": "UUP",
        },
        "country": {
            "United States": "SPY", "Japan": "EWJ", "United Kingdom": "EWU", "Germany": "EWG",
            "France": "EWQ", "China": "MCHI", "India": "INDA", "Brazil": "EWZ", "Canada": "EWC",
            "Australia": "EWA", "South Korea": "EWY", "Mexico": "EWW",
        },
    }
    if group not in universes:
        raise ValueError("group must be one of {}".format(", ".join(universes)))
    rows, warnings = [], []
    for label, etf in universes[group].items():
        try:
            closes = yahoo.history(etf, period="10y")["close"]
        except Exception as exc:  # noqa: BLE001
            warnings.append("{}: {}".format(etf, exc))
            continue
        rows.append(dict(group=label, symbol=etf, **pct_change_table(closes)))
    if not rows:
        raise EmptyDataError("No group performance data. {}".format("; ".join(warnings)))
    rows.sort(key=lambda r: (r.get("ytd") is None, -(r.get("ytd") or 0)))
    return Result(rows, provider=src, warnings=warnings)


@command("/equity/compare/sector_overview", providers=("yahoo",), summary="Sector fundamentals overview")
def sector_overview(sector: str = "technology", provider: Optional[str] = None) -> Result:
    """Yahoo's sector page: market weight, industry list and top holdings."""
    src = resolve_provider(provider, ("yahoo",))
    overview = yahoo.sector(sector.lower().replace(" ", "-"), "overview")
    return Result(dict(sector=sector, **dict(overview)), provider=src)


@command("/equity/compare/sector_companies", providers=("yahoo",),
         summary="Largest companies in a sector")
def sector_companies(sector: str = "technology", limit: int = 25,
                     provider: Optional[str] = None) -> Result:
    """``sector`` uses Yahoo's sector keys: technology, healthcare,
    financial-services, energy, consumer-cyclical, consumer-defensive,
    industrials, basic-materials, utilities, real-estate, communication-services."""
    src = resolve_provider(provider, ("yahoo",))
    df = pd.DataFrame(yahoo.sector(sector.lower().replace(" ", "-"), "top_companies"))
    df.index.name = "symbol"
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    return Result(df.head(limit), provider=src, index_name="symbol")


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
_DISCOVERY = {
    "gainers": "day_gainers",
    "losers": "day_losers",
    "active": "most_actives",
    "growth_tech": "growth_technology_stocks",
    "undervalued_growth": "undervalued_growth_stocks",
    "undervalued_large_caps": "undervalued_large_caps",
    "aggressive_small_caps": "aggressive_small_caps",
    "small_cap_gainers": "small_cap_gainers",
    "most_shorted": "most_shorted_stocks",
}


def _discovery(kind: str, limit: int) -> Result:
    df = yahoo.predefined_screen(_DISCOVERY[kind], limit)
    keep = ["symbol", "shortName", "regularMarketPrice", "regularMarketChange",
            "regularMarketChangePercent", "regularMarketVolume", "averageDailyVolume3Month",
            "marketCap", "trailingPE", "fiftyTwoWeekRange", "fullExchangeName"]
    cols = [c for c in keep if c in df.columns]
    out = df[cols] if cols else df
    return Result(out, provider="yahoo")


for _name in _DISCOVERY:
    def _make(kind: str):
        def fn(limit: int = 25, provider: Optional[str] = None) -> Result:
            resolve_provider(provider, ("yahoo",))
            return _discovery(kind, limit)

        fn.__name__ = "discovery_" + kind
        fn.__doc__ = "Yahoo's {} screen.".format(kind.replace("_", " "))
        return fn

    command("/equity/discovery/" + _name, providers=("yahoo",),
            summary="Discovery screen: {}".format(_name.replace("_", " ")))(_make(_name))


@command("/equity/discovery/filings", providers=("sec",), summary="Latest filings hitting EDGAR")
def discovery_filings(form_type: Optional[str] = None, limit: int = 40,
                      provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("sec",))
    return Result(sec.latest_filings(form_type, limit), provider=src)


# --------------------------------------------------------------------------- #
# Calendars
# --------------------------------------------------------------------------- #
@command("/equity/calendar/earnings", providers=("yahoo", "nasdaq"), summary="Upcoming earnings dates")
def calendar_earnings(start_date: Optional[str] = None, end_date: Optional[str] = None,
                      limit: int = 200, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo", "nasdaq"))
    if src == "nasdaq":
        return Result(markets.nasdaq_calendar("earnings", start_date), provider=src)
    return Result(yahoo.market_calendar("earnings", start_date, end_date).head(limit), provider=src)


@command("/equity/calendar/dividends", providers=("nasdaq", "yahoo"), summary="Dividend calendar")
def calendar_dividends(start_date: Optional[str] = None, limit: int = 200,
                       provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("nasdaq", "yahoo"))
    return Result(markets.nasdaq_calendar("dividends", start_date).head(limit), provider="nasdaq")


@command("/equity/calendar/splits", providers=("yahoo", "nasdaq"), summary="Stock split calendar")
def calendar_splits(start_date: Optional[str] = None, end_date: Optional[str] = None,
                    limit: int = 200, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo", "nasdaq"))
    if src == "nasdaq":
        return Result(markets.nasdaq_calendar("splits", start_date).head(limit), provider=src)
    return Result(yahoo.market_calendar("splits", start_date, end_date).head(limit), provider=src)


@command("/equity/calendar/ipo", providers=("yahoo", "nasdaq"), summary="IPO calendar")
def calendar_ipo(start_date: Optional[str] = None, end_date: Optional[str] = None,
                 limit: int = 200, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo", "nasdaq"))
    if src == "nasdaq":
        return Result(markets.nasdaq_calendar("ipo", start_date).head(limit), provider=src)
    return Result(yahoo.market_calendar("ipo", start_date, end_date).head(limit), provider=src)


@command("/equity/calendar/economic", providers=("yahoo",), summary="Macro release calendar")
def calendar_economic(start_date: Optional[str] = None, end_date: Optional[str] = None,
                      limit: int = 200, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    return Result(yahoo.market_calendar("economic", start_date, end_date).head(limit), provider=src)


# --------------------------------------------------------------------------- #
# Shorts & dark pool
# --------------------------------------------------------------------------- #
@command("/equity/shorts/short_volume", providers=("finra",),
         summary="Daily consolidated short-sale volume")
def shorts_volume(symbol: str, days: int = 30, provider: Optional[str] = None) -> Result:
    """FINRA publishes one file per session, so ``days`` is also the number of
    files fetched on a cold cache."""
    src = resolve_provider(provider, ("finra",))
    return Result(finra.short_volume(one_symbol(symbol), days), provider=src)


@command("/equity/shorts/short_interest", providers=("yahoo",),
         summary="Reported short interest, days-to-cover and short float")
def shorts_interest(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    rows = []
    for sym in norm_symbols(symbol):
        info = yahoo.info(sym)
        rows.append(
            {
                "symbol": sym,
                "shares_short": info.get("sharesShort"),
                "shares_short_prior_month": info.get("sharesShortPriorMonth"),
                "short_ratio": info.get("shortRatio"),
                "short_percent_of_float": info.get("shortPercentOfFloat"),
                "short_percent_of_shares_out": info.get("sharesPercentSharesOut"),
                "float_shares": info.get("floatShares"),
                "shares_outstanding": info.get("sharesOutstanding"),
                "date_short_interest": pd.to_datetime(info.get("dateShortInterest"), unit="s",
                                                      errors="coerce"),
            }
        )
    return Result(rows, provider=src)


@command("/equity/shorts/fails_to_deliver", providers=("sec",), summary="SEC fails-to-deliver history")
def shorts_ftd(symbol: str, months: int = 3, provider: Optional[str] = None) -> Result:
    """SEC publishes fails twice a month as national files; each extra month
    downloads another archive, so the first call for a window is slow."""
    src = resolve_provider(provider, ("sec",))
    return Result(sec.fails_to_deliver(one_symbol(symbol), months), provider=src)


@command("/equity/shorts/market_short_volume", providers=("finra",),
         summary="Whole-market short volume for one session")
def shorts_market(day: Optional[str] = None, limit: int = 250,
                  provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("finra",))
    return Result(finra.short_volume_by_day(day, limit), provider=src)


@command("/equity/darkpool/otc", providers=("finra",),
         summary="FINRA ATS (dark pool) and OTC weekly volume")
def darkpool_otc(symbol: Optional[str] = None, summary_type: str = "ATS_W_SMBL",
                 limit: int = 200, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("finra",))
    return Result(finra.otc_weekly(summary_type, symbol, limit), provider=src)
