"""Commodity menu: spot prices, energy reports and futures positioning."""
from __future__ import annotations

from typing import Optional

import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import date_window, norm_symbols, pct_change_table
from ..providers import finra, fred, govstats, yahoo

# Free spot/benchmark series available without any key.
SPOT_SERIES = {
    "wti": "DCOILWTICO", "brent": "DCOILBRENTEU", "natural_gas": "DHHNGSP",
    "gasoline": "GASREGW", "all_commodities": "PALLFNFINDEXQ",
}

FUTURES_TICKERS = {
    "crude_oil": "CL=F", "brent": "BZ=F", "natural_gas": "NG=F", "gasoline": "RB=F",
    "heating_oil": "HO=F", "gold": "GC=F", "silver": "SI=F", "copper": "HG=F",
    "platinum": "PL=F", "palladium": "PA=F", "corn": "ZC=F", "soybeans": "ZS=F",
    "wheat": "ZW=F", "sugar": "SB=F", "coffee": "KC=F", "cocoa": "CC=F", "cotton": "CT=F",
    "live_cattle": "LE=F", "lean_hogs": "HE=F", "lumber": "LBS=F",
}


@command("/commodity/price/spot", providers=("fred",), summary="Benchmark spot prices")
def spot(commodity: str = "wti", start_date: Optional[str] = None, end_date: Optional[str] = None,
         provider: Optional[str] = None) -> Result:
    """``commodity``: wti, brent, natural_gas, gasoline or all_commodities."""
    resolve_provider(provider, ("fred",))
    ids = []
    for name in commodity.split(","):
        key = name.strip().lower()
        if key not in SPOT_SERIES:
            raise ValueError("commodity must be one of {}".format(", ".join(sorted(SPOT_SERIES))))
        ids.append(SPOT_SERIES[key])
    return Result(fred.series(",".join(ids), start_date, end_date), provider="fred", index_name="date")


@command("/commodity/price/futures", providers=("yahoo",), summary="Front-month futures history")
def futures_price(commodity: str = "gold", start_date: Optional[str] = None,
                  end_date: Optional[str] = None, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    start, end = date_window(start_date, end_date)
    frames, warnings = [], []
    names = [c.strip().lower() for c in commodity.split(",") if c.strip()]
    for name in names:
        ticker = FUTURES_TICKERS.get(name, name.upper())
        try:
            df = yahoo.history(ticker, str(start), str(end))
        except Exception as exc:  # noqa: BLE001
            warnings.append("{}: {}".format(ticker, exc))
            continue
        if len(names) > 1:
            df.insert(0, "commodity", name)
        frames.append(df)
    if not frames:
        raise EmptyDataError("No futures history. {}".format("; ".join(warnings)))
    return Result(pd.concat(frames).sort_index(), provider=src, warnings=warnings, index_name="date")


@command("/commodity/performance", providers=("yahoo",),
         summary="Trailing returns across the commodity complex")
def performance(provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    rows, warnings = [], []
    for name, ticker in FUTURES_TICKERS.items():
        try:
            closes = yahoo.history(ticker, period="5y")["close"]
        except Exception as exc:  # noqa: BLE001
            warnings.append("{}: {}".format(ticker, exc))
            continue
        rows.append(dict(commodity=name, symbol=ticker, **pct_change_table(closes)))
    if not rows:
        raise EmptyDataError("No commodity performance data. {}".format("; ".join(warnings)))
    rows.sort(key=lambda r: (r.get("ytd") is None, -(r.get("ytd") or 0)))
    return Result(rows, provider=src, warnings=warnings)


@command("/commodity/available", providers=("yahoo", "fred"), summary="Commodities this menu covers")
def available() -> Result:
    rows = [{"commodity": k, "futures_symbol": v, "spot_series": SPOT_SERIES.get(k)}
            for k, v in sorted(FUTURES_TICKERS.items())]
    return Result(rows, provider="yahoo")


@command("/commodity/petroleum_status_report", providers=("eia",),
         summary="EIA Weekly Petroleum Status Report")
def petroleum_status_report(limit: int = 500, provider: Optional[str] = None) -> Result:
    """Needs a free EIA key (``MFT_EIA_API_KEY``); crude/gas *prices* are
    available key-free via ``/commodity/price/spot``."""
    resolve_provider(provider, ("eia",))
    return Result(govstats.petroleum_status(limit), provider="eia")


@command("/commodity/short_term_energy_outlook", providers=("eia",),
         summary="EIA Short-Term Energy Outlook projections")
def short_term_energy_outlook(limit: int = 2000, provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("eia",))
    return Result(govstats.short_term_energy_outlook(limit), provider="eia")


@command("/commodity/natural_gas_storage", providers=("eia",),
         summary="Weekly working gas in underground storage")
def natural_gas_storage(limit: int = 500, provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("eia",))
    return Result(govstats.natural_gas_storage(limit), provider="eia")


@command("/commodity/cot", providers=("cftc",),
         summary="Commitments of Traders positioning for a commodity")
def commodity_cot(market: str = "GOLD", report: str = "legacy", start_date: Optional[str] = None,
                  limit: int = 200, provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("cftc",))
    return Result(finra.cot(market, report, start_date, limit), provider="cftc")
