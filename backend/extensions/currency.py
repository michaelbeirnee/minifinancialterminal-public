"""Currency menu: pairs, history, ECB reference rates, snapshots."""
from __future__ import annotations

from typing import Optional

import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import date_window, norm_symbols
from ..providers import intl, yahoo

MAJORS = ["EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY", "SEK", "NOK", "MXN", "BRL", "INR", "KRW"]


@command("/currency/search", providers=("frankfurter", "yahoo"), summary="Available currencies/pairs")
def currency_search(query: Optional[str] = None, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("frankfurter", "yahoo"))
    if src == "yahoo":
        return Result(yahoo.lookup(query or "USD", "currency", 50), provider=src, index_name="symbol")
    df = intl.fx_currencies()
    if query:
        q = query.lower()
        df = df[df["name"].str.lower().str.contains(q, na=False) | df["code"].str.lower().eq(q)]
        if df.empty:
            raise EmptyDataError("No currency matching {!r}".format(query))
    return Result(df, provider=src)


@command("/currency/price/historical", providers=("yahoo", "frankfurter"),
         summary="FX pair price history")
def currency_historical(
    symbol: str = "EURUSD=X",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    interval: str = "1d",
    provider: Optional[str] = None,
) -> Result:
    """Yahoo uses ``EURUSD=X`` style tickers; Frankfurter uses ``base=USD`` plus symbols."""
    src = resolve_provider(provider, ("yahoo", "frankfurter"))
    start, end = date_window(start_date, end_date)
    if src == "frankfurter":
        base, _, quote = symbol.upper().replace("=X", "").partition("/")
        if not quote and len(base) == 6:
            base, quote = base[:3], base[3:]
        df = intl.fx_history(base or "USD", quote or None, str(start), str(end))
        return Result(df, provider=src, index_name="date")
    frames, warnings = [], []
    symbols = norm_symbols(symbol)
    for sym in symbols:
        ticker = sym if ("=" in sym or "-" in sym) else sym + "=X"
        try:
            df = yahoo.history(ticker, str(start), str(end), interval=interval)
        except Exception as exc:  # noqa: BLE001
            warnings.append("{}: {}".format(ticker, exc))
            continue
        if len(symbols) > 1:
            df.insert(0, "symbol", ticker)
        frames.append(df)
    if not frames:
        raise EmptyDataError("No FX history. {}".format("; ".join(warnings)))
    return Result(pd.concat(frames).sort_index(), provider=src, warnings=warnings, index_name="date")


@command("/currency/reference_rates", providers=("ecb", "frankfurter"),
         summary="ECB daily euro reference rates")
def currency_reference_rates(currencies: str = "USD,GBP,JPY,CHF,CAD,AUD,CNY",
                             start_date: Optional[str] = None, end_date: Optional[str] = None,
                             provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("ecb", "frankfurter"))
    if src == "frankfurter":
        return Result(intl.fx_latest("EUR", currencies), provider=src)
    return Result(intl.ecb_reference_rates(currencies, start_date, end_date), provider=src)


@command("/currency/snapshots", providers=("frankfurter", "yahoo"),
         summary="Latest cross rates against one base currency")
def currency_snapshots(base: str = "USD", counter_currencies: Optional[str] = None,
                       provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("frankfurter", "yahoo"))
    if src == "yahoo":
        rows, warnings = [], []
        for cur in norm_symbols(counter_currencies or ",".join(MAJORS)):
            try:
                q = yahoo.quote("{}{}=X".format(base.upper(), cur))
            except Exception as exc:  # noqa: BLE001
                warnings.append("{}: {}".format(cur, exc))
                continue
            rows.append({"base": base.upper(), "currency": cur, "rate": q.get("last_price"),
                         "change_percent": q.get("change_percent")})
        if not rows:
            raise EmptyDataError("No FX snapshots. {}".format("; ".join(warnings)))
        return Result(rows, provider=src, warnings=warnings)
    return Result(intl.fx_latest(base, counter_currencies), provider=src)
