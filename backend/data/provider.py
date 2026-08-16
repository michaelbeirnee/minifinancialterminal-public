"""Historical market-data access.

Data comes exclusively from Yahoo Finance via :mod:`yfinance` (free, open data).
If a download fails or returns nothing, the call raises so callers surface a real
error rather than silently substituting fabricated data.
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from ..cache import cache


def _download_yf(symbol: str, start: str, end: str) -> pd.DataFrame | None:
    try:
        import yfinance as yf

        raw = yf.download(
            symbol,
            start=start,
            end=end,
            progress=False,
            auto_adjust=True,
            threads=False,
        )
    except Exception:
        return None
    if raw is None or raw.empty:
        return None

    # yfinance may return a MultiIndex (column, ticker) for a single symbol.
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw = raw.rename(columns=str.lower)
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in raw.columns]
    df = raw[keep].copy()
    df.index = pd.to_datetime(df.index)
    df.index.name = "date"
    df.attrs["source"] = "yfinance"
    return df


def get_history(symbol: str, start: str, end: str | None = None) -> pd.DataFrame:
    """Return an OHLCV DataFrame indexed by date for ``symbol``.

    Data is pulled from Yahoo Finance and cached (memory + disk) keyed on
    (symbol, start, end). Raises ``ValueError`` if no data is available.
    """
    symbol = symbol.upper().strip()
    if "," in symbol:
        # Multi-symbol downloads return MultiIndex columns that this legacy
        # path cannot represent; the platform endpoint handles lists properly.
        raise ValueError(
            "This endpoint takes one symbol at a time — for several, use "
            "/api/v1/equity/price/historical?symbol={}".format(symbol)
        )
    end = end or date.today().isoformat()
    key = cache.make_key("history", symbol, start, end)

    cached = cache.get(key)
    if cached is not None:
        return cached.copy()

    df = _download_yf(symbol, start, end)
    if df is None or df.empty:
        raise ValueError(f"No data available for {symbol} ({start}..{end})")

    cache.set(key, df)
    return df.copy()


def get_price_panel(symbols: list[str], start: str, end: str | None = None) -> pd.DataFrame:
    """Return a wide DataFrame of adjusted close prices (columns = symbols)."""
    series = {}
    for sym in symbols:
        hist = get_history(sym, start, end)
        series[sym.upper()] = hist["close"]
    panel = pd.DataFrame(series).dropna(how="all").ffill().dropna()
    return panel


def latest_quote(symbol: str) -> dict:
    """Most recent bar plus a 1-day change, suitable for a ticker widget."""
    hist = get_history(symbol, start=(date.today().replace(year=date.today().year - 1)).isoformat())
    last = hist.iloc[-1]
    prev = hist.iloc[-2] if len(hist) > 1 else last
    change = float(last["close"] - prev["close"])
    pct = float(change / prev["close"] * 100) if prev["close"] else 0.0
    return {
        "symbol": symbol.upper(),
        "price": round(float(last["close"]), 4),
        "change": round(change, 4),
        "change_pct": round(pct, 4),
        "volume": int(last["volume"]),
        "as_of": hist.index[-1].date().isoformat()
        if isinstance(hist.index[-1], (pd.Timestamp, datetime))
        else str(hist.index[-1]),
        "source": hist.attrs.get("source", "unknown"),
    }
