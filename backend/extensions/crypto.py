"""Crypto menu: prices, market table, dominance and categories."""
from __future__ import annotations

from typing import Optional

import pandas as pd

from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import date_window, norm_symbols
from ..providers import coingecko, yahoo


@command("/crypto/search", providers=("coingecko", "yahoo"), summary="Find a coin or trading pair")
def crypto_search(query: str, limit: int = 25, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("coingecko", "yahoo"))
    if src == "yahoo":
        return Result(yahoo.lookup(query, "cryptocurrency", limit), provider=src, index_name="symbol")
    return Result(coingecko.search(query, limit), provider=src)


@command("/crypto/price/historical", providers=("yahoo", "coingecko"),
         summary="Historical crypto prices")
def crypto_historical(
    symbol: str = "BTC-USD",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    interval: str = "1d",
    provider: Optional[str] = None,
) -> Result:
    """Yahoo takes pair tickers (``BTC-USD``); CoinGecko takes coin ids (``bitcoin``)."""
    src = resolve_provider(provider, ("yahoo", "coingecko"))
    start, end = date_window(start_date, end_date)
    if src == "coingecko":
        days = max((end - start).days, 1)
        return Result(coingecko.ohlc(symbol.lower(), days=days), provider=src, index_name="date")
    frames = []
    warnings = []
    symbols = norm_symbols(symbol)
    for sym in symbols:
        try:
            df = yahoo.history(sym, str(start), str(end), interval=interval)
        except Exception as exc:  # noqa: BLE001
            warnings.append("{}: {}".format(sym, exc))
            continue
        if len(symbols) > 1:
            df.insert(0, "symbol", sym)
        frames.append(df)
    if not frames:
        raise ValueError("No crypto price data. {}".format("; ".join(warnings)))
    return Result(pd.concat(frames).sort_index(), provider=src, warnings=warnings, index_name="date")


@command("/crypto/market", providers=("coingecko",),
         summary="Ranked coin table with market cap and trailing changes")
def crypto_market(vs_currency: str = "usd", category: Optional[str] = None, limit: int = 100,
                  provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("coingecko",))
    return Result(coingecko.markets(vs_currency, limit, category), provider=src)


@command("/crypto/global", providers=("coingecko",),
         summary="Total market cap, volume and BTC/ETH dominance")
def crypto_global(provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("coingecko",))
    return Result(coingecko.global_stats(), provider=src)


@command("/crypto/categories", providers=("coingecko",), summary="Sector categories by market cap")
def crypto_categories(limit: int = 100, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("coingecko",))
    return Result(coingecko.categories(limit), provider=src)


@command("/crypto/coin_list", providers=("coingecko",), summary="Every coin id CoinGecko tracks")
def crypto_coin_list(limit: int = 5000, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("coingecko",))
    return Result(coingecko.coin_list().head(limit), provider=src)
