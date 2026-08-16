"""CoinGecko provider — free crypto market data, no key (rate-limited).

Complements Yahoo's crypto pairs with market caps, dominance, categories and
the full multi-thousand coin universe.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from ..core.caching import TTL_DAILY, TTL_INTRADAY, TTL_QUOTE, TTL_REFERENCE, cached
from ..core.errors import EmptyDataError
from ..core.http import get_json

NAME = "coingecko"
BASE = "https://api.coingecko.com/api/v3"


@cached("coingecko.markets", ttl=TTL_QUOTE)
def markets(vs_currency: str = "usd", limit: int = 100, category: Optional[str] = None) -> pd.DataFrame:
    """Ranked coin table with price, market cap, volume and trailing changes."""
    rows = get_json(
        BASE + "/coins/markets",
        params={
            "vs_currency": vs_currency.lower(), "order": "market_cap_desc",
            "per_page": min(limit, 250), "page": 1, "sparkline": "false", "category": category,
            "price_change_percentage": "1h,24h,7d,30d,1y",
        },
        ttl=TTL_QUOTE,
    )
    if not rows:
        raise EmptyDataError("CoinGecko returned no market rows")
    df = pd.DataFrame(rows)
    keep = [
        "market_cap_rank", "id", "symbol", "name", "current_price", "market_cap",
        "fully_diluted_valuation", "total_volume", "high_24h", "low_24h",
        "price_change_percentage_1h_in_currency", "price_change_percentage_24h_in_currency",
        "price_change_percentage_7d_in_currency", "price_change_percentage_30d_in_currency",
        "price_change_percentage_1y_in_currency", "circulating_supply", "total_supply",
        "max_supply", "ath", "ath_change_percentage", "atl", "last_updated",
    ]
    df = df[[c for c in keep if c in df.columns]]
    df["symbol"] = df["symbol"].str.upper()
    return df.rename(columns=lambda c: c.replace("_in_currency", ""))


@cached("coingecko.history", ttl=TTL_DAILY)
def market_chart(
    coin_id: str = "bitcoin",
    vs_currency: str = "usd",
    days: int = 365,
    interval: Optional[str] = None,
) -> pd.DataFrame:
    """Price / market-cap / volume history for one coin."""
    payload = get_json(
        "{}/coins/{}/market_chart".format(BASE, coin_id.lower()),
        params={"vs_currency": vs_currency.lower(), "days": days, "interval": interval},
        ttl=TTL_DAILY,
    )
    prices = payload.get("prices") or []
    if not prices:
        raise EmptyDataError("CoinGecko has no chart data for {}".format(coin_id))
    df = pd.DataFrame(prices, columns=["ts", "close"])
    for field, key in (("market_cap", "market_caps"), ("volume", "total_volumes")):
        series = payload.get(key) or []
        if series:
            df[field] = pd.DataFrame(series, columns=["ts", field])[field]
    df["date"] = pd.to_datetime(df["ts"], unit="ms")
    df["coin_id"] = coin_id.lower()
    return df.drop(columns=["ts"]).set_index("date").sort_index()


@cached("coingecko.ohlc", ttl=TTL_DAILY)
def ohlc(coin_id: str = "bitcoin", vs_currency: str = "usd", days: int = 365) -> pd.DataFrame:
    """OHLC candles (CoinGecko supports 1/7/14/30/90/180/365/max days)."""
    rows = get_json(
        "{}/coins/{}/ohlc".format(BASE, coin_id.lower()),
        params={"vs_currency": vs_currency.lower(), "days": days},
        ttl=TTL_DAILY,
    )
    if not rows:
        raise EmptyDataError("CoinGecko has no OHLC data for {}".format(coin_id))
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])
    df["date"] = pd.to_datetime(df["ts"], unit="ms")
    return df.drop(columns=["ts"]).set_index("date").sort_index()


@cached("coingecko.search", ttl=TTL_DAILY)
def search(query: str, limit: int = 25) -> pd.DataFrame:
    payload = get_json(BASE + "/search", params={"query": query}, ttl=TTL_DAILY)
    coins = payload.get("coins") or []
    if not coins:
        raise EmptyDataError("CoinGecko found no coin matching {!r}".format(query))
    df = pd.DataFrame(coins)
    keep = ["id", "symbol", "name", "market_cap_rank", "api_symbol"]
    return df[[c for c in keep if c in df.columns]].head(limit)


@cached("coingecko.list", ttl=TTL_REFERENCE)
def coin_list() -> pd.DataFrame:
    rows = get_json(BASE + "/coins/list", ttl=TTL_REFERENCE)
    if not rows:
        raise EmptyDataError("CoinGecko coin list was empty")
    return pd.DataFrame(rows)


@cached("coingecko.global", ttl=TTL_INTRADAY)
def global_stats() -> Dict[str, Any]:
    payload = get_json(BASE + "/global", ttl=TTL_INTRADAY).get("data") or {}
    if not payload:
        raise EmptyDataError("CoinGecko global stats unavailable")
    return {
        "active_cryptocurrencies": payload.get("active_cryptocurrencies"),
        "markets": payload.get("markets"),
        "total_market_cap_usd": (payload.get("total_market_cap") or {}).get("usd"),
        "total_volume_usd": (payload.get("total_volume") or {}).get("usd"),
        "btc_dominance": (payload.get("market_cap_percentage") or {}).get("btc"),
        "eth_dominance": (payload.get("market_cap_percentage") or {}).get("eth"),
        "market_cap_change_24h": payload.get("market_cap_change_percentage_24h_usd"),
        "updated_at": pd.to_datetime(payload.get("updated_at"), unit="s", errors="coerce"),
    }


@cached("coingecko.categories", ttl=TTL_INTRADAY)
def categories(limit: int = 100) -> pd.DataFrame:
    rows = get_json(BASE + "/coins/categories", ttl=TTL_INTRADAY)
    if not rows:
        raise EmptyDataError("CoinGecko returned no categories")
    df = pd.DataFrame(rows)
    keep = ["id", "name", "market_cap", "market_cap_change_24h", "volume_24h", "top_3_coins_id"]
    return df[[c for c in keep if c in df.columns]].head(limit)
