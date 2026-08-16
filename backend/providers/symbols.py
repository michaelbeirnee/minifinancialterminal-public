"""Local ticker directory powering the type-ahead suggestions in the UI.

Autocomplete fires on every keystroke, so it cannot afford a network hop per
call. Instead the directory is assembled once — every SEC-registered ticker
(equities and ETFs, refreshed weekly) plus a curated set of indices, futures,
FX pairs and crypto that SEC does not cover — and matched in-process.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..core.caching import TTL_REFERENCE, cached

# (symbol, name, type, extra search keywords) — things the SEC register lacks.
CURATED: List[Tuple[str, str, str, str]] = [
    # Indices
    ("^GSPC", "S&P 500 Index", "index", "SP500 SPX S AND P"),
    ("^IXIC", "Nasdaq Composite Index", "index", "NASDAQ"),
    ("^NDX", "Nasdaq 100 Index", "index", "NASDAQ100"),
    ("^DJI", "Dow Jones Industrial Average", "index", "DOW DJIA"),
    ("^RUT", "Russell 2000 Index", "index", "RUSSELL"),
    ("^RUI", "Russell 1000 Index", "index", "RUSSELL"),
    ("^VIX", "CBOE Volatility Index", "index", "VOLATILITY FEAR"),
    ("^TNX", "10-Year Treasury Yield Index", "index", "TREASURY 10Y"),
    ("^FTSE", "FTSE 100 Index", "index", "UK LONDON"),
    ("^GDAXI", "DAX Index", "index", "GERMANY"),
    ("^FCHI", "CAC 40 Index", "index", "FRANCE"),
    ("^STOXX50E", "EURO STOXX 50 Index", "index", "EUROPE"),
    ("^N225", "Nikkei 225 Index", "index", "JAPAN"),
    ("^HSI", "Hang Seng Index", "index", "HONG KONG"),
    ("^GSPTSE", "S&P/TSX Composite Index", "index", "CANADA TSX"),
    ("^BVSP", "Bovespa Index", "index", "BRAZIL"),
    ("^AXJO", "S&P/ASX 200 Index", "index", "AUSTRALIA"),
    ("DX-Y.NYB", "US Dollar Index", "index", "DXY DOLLAR"),
    # Crypto (Yahoo pairs)
    ("BTC-USD", "Bitcoin USD", "crypto", "CRYPTO"),
    ("ETH-USD", "Ethereum USD", "crypto", "CRYPTO"),
    ("SOL-USD", "Solana USD", "crypto", "CRYPTO"),
    ("XRP-USD", "XRP USD", "crypto", "CRYPTO"),
    ("DOGE-USD", "Dogecoin USD", "crypto", "CRYPTO"),
    ("ADA-USD", "Cardano USD", "crypto", "CRYPTO"),
    ("LTC-USD", "Litecoin USD", "crypto", "CRYPTO"),
    # FX majors
    ("EURUSD=X", "Euro / US Dollar", "currency", "FX FOREX"),
    ("GBPUSD=X", "British Pound / US Dollar", "currency", "FX FOREX CABLE"),
    ("USDJPY=X", "US Dollar / Japanese Yen", "currency", "FX FOREX"),
    ("USDCHF=X", "US Dollar / Swiss Franc", "currency", "FX FOREX"),
    ("USDCAD=X", "US Dollar / Canadian Dollar", "currency", "FX FOREX"),
    ("AUDUSD=X", "Australian Dollar / US Dollar", "currency", "FX FOREX"),
    ("USDCNY=X", "US Dollar / Chinese Yuan", "currency", "FX FOREX"),
]

# Liquid names that should outrank alphabetical neighbours on short queries
# ("SP" should surface SPY before SPB).
POPULAR = {
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "TLT", "GLD", "SLV", "HYG", "AGG", "XLE", "XLK",
    "XLF", "XLV", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK-B", "JPM",
    "V", "UNH", "XOM", "WMT", "JNJ", "PG", "KO", "AMD", "NFLX", "INTC", "BA", "DIS", "GE",
    "BTC-USD", "ETH-USD", "^GSPC", "^IXIC", "^DJI", "^VIX", "EURUSD=X", "CL=F", "GC=F", "ES=F",
}


def _futures() -> List[Tuple[str, str, str, str]]:
    """Continuous contracts from the derivatives extension's root table."""
    try:
        from ..extensions.derivatives import FUTURES_ROOTS
    except Exception:  # noqa: BLE001 - directory must build even if that moves
        return []
    return [
        (root + "=F", "{} Futures (continuous)".format(desc), "future", "FUTURES " + exchange)
        for root, (exchange, desc) in FUTURES_ROOTS.items()
    ]


@cached("symbols.directory", ttl=TTL_REFERENCE)
def directory() -> List[Dict[str, str]]:
    """The merged, deduplicated symbol table with precomputed match text."""
    rows: List[Dict[str, str]] = []
    seen = set()

    def add(symbol: str, name: str, kind: str, exchange: str = "", keywords: str = "") -> None:
        symbol = symbol.strip().upper()
        if not symbol or symbol in seen:
            return
        seen.add(symbol)
        rows.append(
            {
                "symbol": symbol,
                "name": name.strip(),
                "type": kind,
                "exchange": exchange,
                "_name_u": name.strip().upper(),
                "_kw_u": keywords.upper(),
            }
        )

    for symbol, name, kind, keywords in CURATED:
        add(symbol, name, kind, keywords=keywords)
    for symbol, name, kind, keywords in _futures():
        add(symbol, name, kind, keywords=keywords)

    # The SEC register: every US-listed ticker with a filer behind it, which
    # includes the big ETF trusts. Network failure just means curated-only.
    try:
        from . import sec

        frame = sec.company_map()
        for symbol, name, exchange in zip(frame["symbol"], frame["name"], frame["exchange"]):
            kind = "etf" if "ETF" in str(name).upper() or "TRUST" in str(name).upper() else "equity"
            add(str(symbol), str(name), kind, exchange=str(exchange or ""))
    except Exception:  # noqa: BLE001 - type-ahead should degrade, not 500
        pass
    return rows


def suggest(query: str, limit: int = 10) -> List[Dict[str, str]]:
    """Rank the directory against a partial ticker or company name."""
    q = query.strip().upper()
    if not q:
        return []
    scored: List[Tuple[float, int, str, Dict[str, str]]] = []
    for row in directory():
        symbol = row["symbol"]
        name_u = row["_name_u"]
        score: Optional[float] = None
        if symbol == q:
            score = 0.0
        elif symbol.startswith(q):
            score = 1.0
        elif row["_kw_u"] and q in row["_kw_u"]:
            score = 2.0
        elif q in symbol:
            score = 3.0
        elif name_u.startswith(q):
            score = 4.0
        elif any(word.startswith(q) for word in name_u.split()):
            score = 5.0
        elif len(q) >= 3 and q in name_u:
            score = 6.0
        if score is None:
            continue
        if symbol in POPULAR:
            score -= 0.5
        scored.append((score, len(symbol), symbol, row))

    scored.sort(key=lambda item: item[:3])
    return [
        {"symbol": r["symbol"], "name": r["name"], "type": r["type"], "exchange": r["exchange"]}
        for _, _, _, r in scored[:limit]
    ]
