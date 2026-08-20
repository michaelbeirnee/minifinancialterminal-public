"""Free / open-source market-data providers.

Every provider here is either public-domain government data or a public
endpoint that needs no paid subscription. Three of them (FRED, EIA, BLS) can
optionally use a *free* API key for higher limits or extra endpoints; each has
a documented key-free path so the platform is fully functional unconfigured.

+-----------------+-------------------------------------------+-----------+
| Module          | Covers                                    | Key       |
+=================+===========================================+===========+
| ``yahoo``       | prices, fundamentals, options, screeners  | no        |
| ``sec``         | XBRL fundamentals, filings, FTD, insiders | no        |
| ``fred``        | US macro, rates, credit spreads           | optional  |
| ``treasury``    | yield curves, auctions, debt, NY Fed rates| no        |
| ``intl``        | ECB, World Bank, IMF, OECD, FX            | no        |
| ``finra``       | short volume, dark pool, CFTC COT         | no        |
| ``markets``     | Stooq, Cboe, Nasdaq, index membership     | no        |
| ``coingecko``   | crypto market data                        | no        |
| ``govstats``    | EIA energy, BLS labour & prices           | optional  |
| ``newsfeeds``   | RSS newswires, Google News                | no        |
| ``congress``    | Senate STOCK Act transaction reports      | no        |
| ``spdr``        | full daily holdings for SPDR ETF baskets   | no        |
| ``thirteenf``   | SEC 13F data sets — every filer's holdings| no        |
| ``fomc``        | the Fed's own FOMC meeting calendar       | no        |
| ``alpaca``      | live trades and bid/ask, IEX feed (stream)| optional  |
+-----------------+-------------------------------------------+-----------+
"""
from __future__ import annotations

from typing import Dict, List

PROVIDERS: Dict[str, Dict[str, object]] = {
    "yahoo": {"module": "yahoo", "requires_key": False,
              "description": "Yahoo Finance via yfinance — the broadest single free source"},
    "sec": {"module": "sec", "requires_key": False,
            "description": "SEC EDGAR — XBRL fundamentals, filings, insider and FTD data"},
    "fred": {"module": "fred", "requires_key": False,
             "description": "St. Louis Fed FRED — macro, rates and credit series"},
    "treasury": {"module": "treasury", "requires_key": False,
                 "description": "US Treasury, TreasuryDirect and NY Fed reference rates"},
    "ecb": {"module": "intl", "requires_key": False,
            "description": "European Central Bank SDMX data portal"},
    "worldbank": {"module": "intl", "requires_key": False,
                  "description": "World Bank open data indicators"},
    "imf": {"module": "intl", "requires_key": False,
            "description": "IMF World Economic Outlook DataMapper"},
    "oecd": {"module": "intl", "requires_key": False, "description": "OECD SDMX data explorer"},
    "frankfurter": {"module": "intl", "requires_key": False,
                    "description": "ECB FX reference rates, simplified"},
    "finra": {"module": "finra", "requires_key": False,
              "description": "FINRA short-sale volume and OTC/ATS transparency"},
    "cftc": {"module": "finra", "requires_key": False,
             "description": "CFTC Commitments of Traders"},
    "stooq": {"module": "markets", "requires_key": False,
              "description": "Stooq end-of-day price history"},
    "cboe": {"module": "markets", "requires_key": False,
             "description": "Cboe delayed options chains and index definitions"},
    "nasdaq": {"module": "markets", "requires_key": False,
               "description": "Nasdaq public calendars and listed-company screener"},
    "wikipedia": {"module": "markets", "requires_key": False,
                  "description": "Index constituent tables"},
    "multpl": {"module": "markets", "requires_key": False,
               "description": "Long-run S&P 500 valuation history"},
    "coingecko": {"module": "coingecko", "requires_key": False,
                  "description": "Crypto prices, market caps and dominance"},
    "eia": {"module": "govstats", "requires_key": True,
            "description": "US Energy Information Administration (free key)"},
    "bls": {"module": "govstats", "requires_key": False,
            "description": "Bureau of Labor Statistics (free key optional)"},
    "rss": {"module": "newsfeeds", "requires_key": False,
            "description": "Public financial newswire RSS feeds"},
    "senate": {"module": "congress", "requires_key": False,
               "description": "Senate EFD — STOCK Act periodic transaction reports"},
    "ssga": {"module": "spdr", "requires_key": False,
             "description": "State Street — full daily holdings for every SPDR fund"},
    "federalreserve": {"module": "fomc", "requires_key": False,
                       "description": "Federal Reserve Board — the FOMC meeting calendar"},
    # Streaming only (backend.stream): Yahoo's streamer rides the ``yahoo``
    # entry above; Alpaca is the one source that wants a key.
    "alpaca": {"module": "stream", "requires_key": True,
               "description": "Alpaca Markets — licensed live trades and bid/ask on the free IEX feed"},
    "local": {"module": "stream", "requires_key": False,
              "description": "The local Parquet tick store — history the recorder wrote down"},
}


def provider_table() -> List[Dict[str, object]]:
    return [dict(name=k, **v) for k, v in sorted(PROVIDERS.items())]
