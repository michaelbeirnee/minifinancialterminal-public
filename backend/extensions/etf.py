"""ETF menu: search, profile, holdings, sector/asset-class exposure."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import date_window, norm_symbols, one_symbol, pct_change_table
from ..providers import markets, yahoo

# A broad, liquid cross-section used by /etf/equity_exposure, which has to scan
# holdings ETF-by-ETF because no free source offers a reverse holdings index.
SCAN_UNIVERSE = [
    "SPY", "IVV", "VOO", "VTI", "QQQ", "DIA", "IWM", "IJH", "IJR", "VTV", "VUG", "VIG", "VYM",
    "SCHD", "MTUM", "QUAL", "USMV", "XLK", "XLF", "XLV", "XLE", "XLY", "XLP", "XLI", "XLB",
    "XLU", "XLRE", "XLC", "SMH", "SOXX", "IBB", "XBI", "ARKK", "VNQ", "EFA", "EEM", "VEA", "VWO",
]


@command("/etf/search", providers=("yahoo",), summary="Find ETFs by name or ticker")
def etf_search(query: str, limit: int = 25, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    return Result(yahoo.lookup(query, "etf", limit), provider=src, index_name="symbol")


@command("/etf/info", providers=("yahoo",), summary="ETF profile: strategy, fees, AUM, yield")
def etf_info(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    rows = []
    for sym in norm_symbols(symbol):
        i = yahoo.info(sym)
        rows.append(
            {
                "symbol": sym, "name": i.get("longName"), "family": i.get("fundFamily"),
                "category": i.get("category"), "exchange": i.get("fullExchangeName"),
                "currency": i.get("currency"), "total_assets": i.get("totalAssets"),
                "nav_price": i.get("navPrice"), "net_expense_ratio": i.get("netExpenseRatio"),
                "yield": i.get("yield"), "beta_3y": i.get("beta3Year"),
                "ytd_return": i.get("ytdReturn"), "three_year_return": i.get("threeYearAverageReturn"),
                "five_year_return": i.get("fiveYearAverageReturn"),
                "inception_date": pd.to_datetime(i.get("fundInceptionDate"), unit="s", errors="coerce"),
                "description": i.get("longBusinessSummary"),
            }
        )
    return Result(rows, provider=src)


@command("/etf/historical", providers=("yahoo", "stooq"), summary="ETF price history")
def etf_historical(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
                   interval: str = "1d", provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo", "stooq"))
    start, end = date_window(start_date, end_date)
    sym = one_symbol(symbol)
    df = (markets.stooq_history(sym, str(start), str(end)) if src == "stooq"
          else yahoo.history(sym, str(start), str(end), interval=interval))
    return Result(df, provider=src, index_name="date")


@command("/etf/holdings", providers=("yahoo",), summary="Top holdings and weights")
def etf_holdings(symbol: str, limit: int = 50, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    sym = one_symbol(symbol)
    df = pd.DataFrame(yahoo.fund_data(sym, "top_holdings"))
    df.index.name = "symbol"
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    df.insert(0, "etf", sym)
    return Result(df.head(limit), provider=src, index_name="symbol")


@command("/etf/sectors", providers=("yahoo",), summary="Sector weights")
def etf_sectors(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    weights = yahoo.fund_data(one_symbol(symbol), "sector_weightings")
    rows = [{"sector": k, "weight": v} for k, v in dict(weights).items()]
    rows.sort(key=lambda r: -(r["weight"] or 0))
    return Result(rows, provider=src)


@command("/etf/asset_classes", providers=("yahoo",), summary="Asset-class breakdown")
def etf_asset_classes(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    data = yahoo.fund_data(one_symbol(symbol), "asset_classes")
    return Result([{"asset_class": k, "weight": v} for k, v in dict(data).items()], provider=src)


@command("/etf/equity_holdings", providers=("yahoo",),
         summary="Aggregate valuation & growth stats of the equity sleeve")
def etf_equity_holdings(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    df = pd.DataFrame(yahoo.fund_data(one_symbol(symbol), "equity_holdings"))
    df.index.name = "metric"
    return Result(df, provider=src, index_name="metric")


@command("/etf/bond_holdings", providers=("yahoo",), summary="Fixed-income sleeve statistics")
def etf_bond_holdings(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    df = pd.DataFrame(yahoo.fund_data(one_symbol(symbol), "bond_holdings"))
    df.index.name = "metric"
    return Result(df, provider=src, index_name="metric")


@command("/etf/bond_ratings", providers=("yahoo",), summary="Credit-rating distribution")
def etf_bond_ratings(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    data = yahoo.fund_data(one_symbol(symbol), "bond_ratings")
    return Result([{"rating": k, "weight": v} for k, v in dict(data).items()], provider=src)


@command("/etf/price_performance", providers=("yahoo",), summary="Trailing returns for ETFs")
def etf_performance(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    rows, warnings = [], []
    for sym in norm_symbols(symbol):
        try:
            rows.append(dict(symbol=sym, **pct_change_table(yahoo.history(sym, period="10y")["close"])))
        except Exception as exc:  # noqa: BLE001
            warnings.append("{}: {}".format(sym, exc))
    if not rows:
        raise EmptyDataError("No ETF performance data. {}".format("; ".join(warnings)))
    return Result(rows, provider=src, warnings=warnings)


@command("/etf/equity_exposure", providers=("yahoo",),
         summary="Which ETFs hold a given stock, and at what weight")
def etf_equity_exposure(symbol: str, universe: Optional[str] = None, limit: int = 50,
                        provider: Optional[str] = None) -> Result:
    """Reverse-lookup across a scan universe of large ETFs.

    No free source publishes a reverse holdings index, so this walks each ETF's
    published top holdings. Pass ``universe="SPY,QQQ,..."`` to scan your own list.
    """
    src = resolve_provider(provider, ("yahoo",))
    target = one_symbol(symbol)
    etfs = norm_symbols(universe, limit=200) if universe else SCAN_UNIVERSE
    rows, warnings = [], []
    for etf in etfs:
        try:
            holdings = pd.DataFrame(yahoo.fund_data(etf, "top_holdings"))
        except Exception as exc:  # noqa: BLE001
            warnings.append("{}: {}".format(etf, exc))
            continue
        holdings.index = [str(i).upper() for i in holdings.index]
        if target in holdings.index:
            row = holdings.loc[target]
            rows.append(
                {
                    "etf": etf,
                    "symbol": target,
                    "name": row.get("Name") if hasattr(row, "get") else None,
                    "weight": row.get("Holding Percent") if hasattr(row, "get") else None,
                }
            )
    if not rows:
        raise EmptyDataError(
            "{} is not in the published top holdings of any scanned ETF ({} scanned)".format(
                target, len(etfs)
            )
        )
    rows.sort(key=lambda r: -(r.get("weight") or 0))
    return Result(rows[:limit], provider=src, warnings=warnings,
                  extra={"universe_size": len(etfs)})
