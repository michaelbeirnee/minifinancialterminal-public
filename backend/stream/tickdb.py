"""DuckDB over the Parquet tick store: SQL analytics without loading pandas first.

``recorder.read_ticks`` concatenates part files through pandas, which is fine
for a day. This module is for when the store has grown: DuckDB scans the
Parquet parts directly — predicate pushdown, no full load — and answers the
two questions recorded ticks exist to answer:

* ``bars(...)``  — resample the tape into OHLC bars of any width, which is
  how a tick-driven strategy gets an honest backtest (``/api/trading/replay``
  with ``source="ticks"`` runs on exactly these), and
* ``day_stats(...)`` — what the store holds, per symbol per day.

``connect()`` hands the Playground a connection with a ``ticks`` view already
registered, for everything else.
"""
from __future__ import annotations

import re
from typing import Any, List, Optional

import pandas as pd

from ..core.errors import EmptyDataError
from .recorder import _DATE_RE, store_dir

_BAR_SECONDS_RE = re.compile(r"^\d+$")


def _glob() -> str:
    return (store_dir() / "date=*" / "*.parquet").as_posix()


def _require_store() -> None:
    if not any(store_dir().glob("date=*/*.parquet")):
        raise EmptyDataError(
            "The tick store is empty — nothing has been recorded yet. Start the recorder "
            "(POST /api/stream/recorder/start?symbols=…, or MFT_RECORD_SYMBOLS at boot) "
            "and ticks will accumulate under {}.".format(store_dir()))


def connect() -> Any:
    """A DuckDB connection with the whole store registered as view ``ticks``.

    ``date`` (the partition) arrives as a column; ``ts`` is the tick's own
    timestamp parsed to TIMESTAMPTZ. The connection is in-memory and cheap —
    open one per use, do not share across threads.
    """
    import duckdb

    _require_store()
    con = duckdb.connect()
    path = _glob().replace("'", "''")
    con.execute(
        "CREATE OR REPLACE VIEW ticks AS "
        "SELECT *, try_cast(\"time\" AS TIMESTAMPTZ) AS ts "
        "FROM read_parquet('{}', hive_partitioning=true, union_by_name=true)".format(path)
    )
    return con


def _dates(start_date: str, end_date: Optional[str]) -> tuple:
    end = end_date or start_date
    if not _DATE_RE.match(start_date) or not _DATE_RE.match(end):
        raise ValueError("Dates must be YYYY-MM-DD")
    return start_date, end


def bars(
    start_date: str,
    end_date: Optional[str] = None,
    symbols: Optional[List[str]] = None,
    bar_seconds: int = 60,
) -> pd.DataFrame:
    """OHLC bars of ``bar_seconds`` built from the recorded tape.

    ``volume`` is the day-cumulative volume's delta within the bar where the
    source carries it (Yahoo does for stocks/crypto), else null. Rows come
    back one per (symbol, bar), oldest first, with ``date`` set to the bar's
    start — the shape ``trading.replay`` eats directly.
    """
    lo, hi = _dates(start_date, end_date)
    bar_seconds = int(bar_seconds)
    if not 1 <= bar_seconds <= 86_400:
        raise ValueError("bar_seconds must be between 1 and 86400")
    con = connect()
    where = "date BETWEEN ? AND ? AND ts IS NOT NULL AND price IS NOT NULL"
    params: List[Any] = [lo, hi]
    if symbols:
        where += " AND symbol IN ({})".format(",".join("?" * len(symbols)))
        params += [s.upper() for s in symbols]
    df = con.execute(
        """
        SELECT symbol,
               to_timestamp(floor(epoch(ts) / {sec}) * {sec}) AS date,
               arg_min(price, ts)                             AS open,
               max(price)                                     AS high,
               min(price)                                     AS low,
               arg_max(price, ts)                             AS close,
               max(volume) - min(volume)                      AS volume,
               count(*)                                       AS ticks
        FROM ticks
        WHERE {where}
        GROUP BY 1, 2
        ORDER BY 2, 1
        """.format(sec=bar_seconds, where=where),
        params,
    ).df()
    con.close()
    if df.empty:
        raise EmptyDataError("No recorded ticks between {} and {}{} — note the store "
                             "partitions by UTC date".format(
                                 lo, hi, " for " + ",".join(symbols) if symbols else ""))
    return df


def day_stats(
    start_date: str,
    end_date: Optional[str] = None,
    symbols: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Per symbol per day: prints, span, first/last/high/low, tick-to-tick vol."""
    lo, hi = _dates(start_date, end_date)
    con = connect()
    where = "date BETWEEN ? AND ? AND ts IS NOT NULL AND price IS NOT NULL"
    params: List[Any] = [lo, hi]
    if symbols:
        where += " AND symbol IN ({})".format(",".join("?" * len(symbols)))
        params += [s.upper() for s in symbols]
    df = con.execute(
        """
        WITH ticks_w AS (
            SELECT symbol, date, ts, price,
                   ln(price / lag(price) OVER (PARTITION BY symbol, date ORDER BY ts)) AS log_ret
            FROM ticks WHERE {where}
        )
        SELECT symbol, date,
               count(*)                        AS prints,
               min(ts)                         AS first_tick,
               max(ts)                         AS last_tick,
               arg_min(price, ts)              AS first_price,
               arg_max(price, ts)              AS last_price,
               max(price)                      AS high,
               min(price)                      AS low,
               round(stddev_samp(log_ret), 8)  AS tick_ret_stddev
        FROM ticks_w
        GROUP BY 1, 2
        ORDER BY 2, 1
        """.format(where=where),
        params,
    ).df()
    con.close()
    if df.empty:
        raise EmptyDataError("No recorded ticks between {} and {} — note the store "
                             "partitions by UTC date".format(lo, hi))
    for col in ("first_tick", "last_tick"):
        df[col] = df[col].astype(str)
    return df
