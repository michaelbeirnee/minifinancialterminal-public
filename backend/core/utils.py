"""Shared helpers for turning provider payloads into JSON-safe records."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

import numpy as np
import pandas as pd

from .errors import EmptyDataError

Records = List[Dict[str, Any]]


# --------------------------------------------------------------------------- #
# JSON coercion
# --------------------------------------------------------------------------- #
def jsonable(value: Any) -> Any:
    """Recursively convert numpy/pandas/datetime values into JSON primitives."""
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return value
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating, Decimal)):
        f = float(value)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(value, (pd.Timestamp, datetime)):
        # Drop a midnight timestamp down to a plain date, which is what almost
        # every daily-frequency series actually means.
        if value.tzinfo is not None:
            value = value.tz_convert("UTC") if hasattr(value, "tz_convert") else value
        if getattr(value, "hour", 0) == 0 and getattr(value, "minute", 0) == 0 and getattr(value, "second", 0) == 0:
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, np.ndarray):
        return [jsonable(v) for v in value.tolist()]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, pd.Series):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, pd.DataFrame):
        return to_records(value)
    return str(value)


def _unique(names: Iterable[str]) -> List[str]:
    """Suffix repeated column names.

    ``DataFrame.to_dict("records")`` silently drops all but one column when
    names collide — which a few provider payloads do after transposing — so
    disambiguate rather than lose the data.
    """
    seen: Dict[str, int] = {}
    out: List[str] = []
    for name in names:
        if name in seen:
            seen[name] += 1
            out.append("{}_{}".format(name, seen[name]))
        else:
            seen[name] = 0
            out.append(name)
    return out


def to_records(data: Any, index_name: Optional[str] = None) -> Any:
    """Normalise a provider payload into JSON-safe records.

    DataFrames become a list of row dicts (the index is promoted to a column
    when it carries information); Series become one row per entry; dicts and
    lists are passed through with their values coerced.
    """
    if data is None:
        return []
    if isinstance(data, pd.DataFrame):
        df = data.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [" ".join(str(p) for p in c if p not in (None, "")).strip() for c in df.columns]
        df.columns = [str(c) for c in df.columns]
        if not isinstance(df.index, pd.RangeIndex):
            # Insert rather than reset_index(): the latter raises outright when
            # the index label already exists as a column, which several provider
            # payloads do (a "symbol"-indexed frame that also has a symbol field).
            name = index_name or df.index.name or "index"
            values = df.index.to_list()
            df = df.reset_index(drop=True)
            df.insert(0, name, values, allow_duplicates=True)
        # Dedupe last: promoting the index can itself collide with a column.
        df.columns = _unique(df.columns)
        return [{k: jsonable(v) for k, v in row.items()} for row in df.to_dict("records")]
    if isinstance(data, pd.Series):
        name = index_name or data.index.name or "index"
        value_name = data.name or "value"
        return [{name: jsonable(k), str(value_name): jsonable(v)} for k, v in data.items()]
    return jsonable(data)


def require_rows(data: Any, message: str) -> Any:
    """Raise :class:`EmptyDataError` when a provider came back with nothing."""
    empty = (
        data is None
        or (isinstance(data, (pd.DataFrame, pd.Series)) and data.empty)
        or (isinstance(data, (list, tuple, dict)) and len(data) == 0)
    )
    if empty:
        raise EmptyDataError(message)
    return data


# --------------------------------------------------------------------------- #
# Dates & symbols
# --------------------------------------------------------------------------- #
def parse_date(value: Union[str, date, datetime, None]) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(str(value)).date()


def date_window(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    default_days: int = 365 * 3,
) -> "tuple[date, date]":
    """Resolve an optional (start, end) pair into concrete dates."""
    end = parse_date(end_date) or date.today()
    start = parse_date(start_date) or (end - timedelta(days=default_days))
    if start > end:
        start, end = end, start
    return start, end


def norm_symbols(symbol: Union[str, Sequence[str]], limit: int = 50) -> List[str]:
    """Accept ``"AAPL,MSFT"`` or ``["aapl", "msft"]`` and return clean tickers."""
    if isinstance(symbol, str):
        raw: Iterable[str] = symbol.replace(";", ",").split(",")
    else:
        raw = symbol
    out: List[str] = []
    for s in raw:
        s = str(s).strip().upper()
        if s and s not in out:
            out.append(s)
    if not out:
        raise ValueError("No symbols supplied")
    return out[:limit]


def one_symbol(symbol: str) -> str:
    return norm_symbols(symbol, limit=1)[0]


# --------------------------------------------------------------------------- #
# Frame shaping
# --------------------------------------------------------------------------- #
OHLCV = ["open", "high", "low", "close", "volume"]


def tidy_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Lower-case column names, keep OHLCV(+extras), sort by a date index."""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=lambda c: str(c).strip().lower().replace(" ", "_"))
    df = df.loc[:, ~df.columns.duplicated()]
    ordered = [c for c in OHLCV if c in df.columns]
    extras = [c for c in df.columns if c not in ordered]
    df = df[ordered + extras]
    df.index = pd.to_datetime(df.index)
    df.index.name = "date"
    return df.sort_index()


def clip_window(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """Slice a date-indexed frame to [start, end], tz-safely."""
    if df.empty:
        return df
    idx = df.index
    if isinstance(idx, pd.DatetimeIndex) and idx.tz is not None:
        idx = idx.tz_convert(None)
        df = df.copy()
        df.index = idx
    return df.loc[str(start) : str(end)]  # noqa: E203


def pct_change_table(prices: pd.Series) -> Dict[str, Optional[float]]:
    """Trailing return table (1D/1W/1M/…) used by the *performance* commands."""
    if prices.empty:
        return {}
    prices = prices.dropna()
    last = float(prices.iloc[-1])
    windows = {"one_day": 1, "one_week": 5, "one_month": 21, "three_month": 63,
               "six_month": 126, "one_year": 252, "three_year": 756, "five_year": 1260}
    out: Dict[str, Optional[float]] = {}
    for label, bars in windows.items():
        out[label] = round(last / float(prices.iloc[-bars - 1]) - 1, 6) if len(prices) > bars else None
    ytd = prices[prices.index >= pd.Timestamp(date(prices.index[-1].year, 1, 1))]
    out["ytd"] = round(last / float(ytd.iloc[0]) - 1, 6) if len(ytd) > 1 else None
    out["max"] = round(last / float(prices.iloc[0]) - 1, 6) if len(prices) > 1 else None
    return out
