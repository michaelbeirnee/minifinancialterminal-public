"""The tick recorder: what the live hub sees, written down as Parquet.

Free real-time data is ephemeral — no free source sells you yesterday's tape
after the fact. The recorder closes that asymmetry: it subscribes to the
stream hub like any other reader and appends every tick to a date-partitioned
Parquet store, so history accumulates from the day it is switched on.

Layout::

    tick_store/
      date=2026-08-19/
        yahoo-143022-514301.parquet     one "part" file per flush
        yahoo-144108-021557.parquet

Parquet files are immutable, so the recorder buffers in memory and flushes a
new part file every ``FLUSH_ROWS`` ticks or ``FLUSH_SECONDS`` seconds,
whichever comes first — and once more on stop, so a clean shutdown loses
nothing. Readers glob the parts; ``/equity/price/ticks`` is the command form.

One recorder per process. It holds a hub subscription, so the upstream socket
stays open while recording even with no browser attached — that is the point.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from ..config import settings

log = logging.getLogger("mft.stream")

FLUSH_ROWS = 5_000
FLUSH_SECONDS = 60.0

#: One fixed column order so every part file carries the same schema.
COLUMNS = ["symbol", "price", "change", "change_percent", "bid", "ask", "bid_size",
           "ask_size", "size", "volume", "day_high", "day_low", "prev_close",
           "time", "exchange", "market_hours", "kind", "provider", "recorded_at"]
_FLOAT_COLS = ["price", "change", "change_percent", "bid", "ask", "bid_size",
               "ask_size", "size", "volume", "day_high", "day_low", "prev_close"]
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def store_dir() -> Path:
    return Path(settings.tick_store_dir)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TickRecorder:
    """Buffers hub ticks and flushes them as Parquet part files."""

    def __init__(self, symbols: List[str], provider: Optional[str] = None) -> None:
        self.symbols = symbols
        self.provider = provider
        self._buffer: List[Dict[str, Any]] = []
        self._task: Optional[asyncio.Task] = None
        self._sub = None
        self._last_flush = time.monotonic()
        self.rows_written = 0
        self.files_written = 0
        self.started_at: Optional[str] = None
        self.last_error: Optional[str] = None

    # ---- lifecycle -------------------------------------------------------- #
    async def start(self) -> None:
        from .hub import get_hub

        hub = get_hub(self.provider)
        self.provider = hub.provider
        self._sub = await hub.subscribe(self.symbols)
        self.symbols = sorted(self._sub.symbols)
        self.started_at = _utcnow().isoformat()
        self._task = asyncio.get_running_loop().create_task(self._run(), name="mft-tick-recorder")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._sub is not None:
            # Sweep anything still sitting in the subscription — ticks that
            # arrived after the last drain would otherwise vanish on stop.
            self._append(list(self._sub.pending.values()))
            self._sub.pending.clear()
            await self._sub.close()
            self._sub = None
        self.flush()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ---- the loop --------------------------------------------------------- #
    async def _run(self) -> None:
        while True:
            ticks = await self._sub.drain(timeout=5.0)
            self._append(ticks)
            if self._buffer and (
                len(self._buffer) >= FLUSH_ROWS
                or time.monotonic() - self._last_flush >= FLUSH_SECONDS
            ):
                # Flushing is file I/O; off the event loop so ticks keep draining.
                await asyncio.get_running_loop().run_in_executor(None, self.flush)

    def _append(self, ticks) -> None:
        now_iso = _utcnow().isoformat()
        for t in ticks:
            row = {k: t.get(k) for k in COLUMNS[:-1]}
            row["recorded_at"] = now_iso
            self._buffer.append(row)

    def flush(self) -> int:
        """Write the buffer as one part file. Returns rows written."""
        rows, self._buffer = self._buffer, []
        self._last_flush = time.monotonic()
        if not rows:
            return 0
        try:
            df = pd.DataFrame(rows, columns=COLUMNS)
            for col in _FLOAT_COLS:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            for col in ("time", "exchange", "market_hours", "kind", "provider", "recorded_at", "symbol"):
                df[col] = df[col].astype("string")
            now = _utcnow()
            part_dir = store_dir() / "date={}".format(now.date())
            part_dir.mkdir(parents=True, exist_ok=True)
            path = part_dir / "{}-{}.parquet".format(
                self.provider or "yahoo", now.strftime("%H%M%S-%f"))
            df.to_parquet(path, index=False)
            self.rows_written += len(df)
            self.files_written += 1
            self.last_error = None
            return len(df)
        except Exception as exc:  # noqa: BLE001 - recording must not kill the stream
            self.last_error = "{}: {}".format(type(exc).__name__, exc)
            log.warning("tick recorder flush failed: %s", self.last_error)
            return 0

    def status(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "provider": self.provider,
            "symbols": self.symbols,
            "buffered": len(self._buffer),
            "rows_written": self.rows_written,
            "files_written": self.files_written,
            "started_at": self.started_at,
            "last_error": self.last_error,
            "store_dir": str(store_dir()),
        }


# --------------------------------------------------------------------------- #
# The process-wide recorder
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_recorder: Optional[TickRecorder] = None


async def start_recording(symbols: List[str], provider: Optional[str] = None) -> TickRecorder:
    """Start (or replace) the process recorder for ``symbols``."""
    global _recorder
    old = _recorder
    if old is not None and old.running:
        await old.stop()
    rec = TickRecorder(symbols, provider)
    await rec.start()
    with _lock:
        _recorder = rec
    return rec


async def stop_recording() -> Optional[Dict[str, Any]]:
    global _recorder
    with _lock:
        rec, _recorder = _recorder, None
    if rec is None:
        return None
    await rec.stop()
    return rec.status()


def recorder_status() -> Optional[Dict[str, Any]]:
    rec = _recorder
    return rec.status() if rec else None


def store_overview() -> Dict[str, Any]:
    """What the store holds on disk: dates, files, sizes."""
    root = store_dir()
    dates = []
    total_bytes = 0
    total_files = 0
    if root.exists():
        for d in sorted(root.glob("date=*")):
            parts = list(d.glob("*.parquet"))
            size = sum(p.stat().st_size for p in parts)
            total_bytes += size
            total_files += len(parts)
            dates.append({"date": d.name.split("=", 1)[1], "files": len(parts),
                          "bytes": size})
    return {"store_dir": str(root), "dates": dates,
            "total_files": total_files, "total_bytes": total_bytes}


def read_ticks(
    start_date: str,
    end_date: Optional[str] = None,
    symbols: Optional[List[str]] = None,
    limit: int = 100_000,
) -> pd.DataFrame:
    """Read recorded ticks back, filtered by date range and symbols."""
    if not _DATE_RE.match(start_date) or (end_date and not _DATE_RE.match(end_date)):
        raise ValueError("Dates must be YYYY-MM-DD")
    root = store_dir()
    lo, hi = start_date, end_date or start_date
    frames: List[pd.DataFrame] = []
    if root.exists():
        for d in sorted(root.glob("date=*")):
            day = d.name.split("=", 1)[1]
            if not (lo <= day <= hi):
                continue
            for part in sorted(d.glob("*.parquet")):
                try:
                    df = pd.read_parquet(part)
                except Exception as exc:  # noqa: BLE001 - one bad part file
                    log.warning("unreadable tick part %s: %s", part, exc)
                    continue
                if symbols:
                    df = df[df["symbol"].isin(symbols)]
                if not df.empty:
                    frames.append(df)
    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values("time", kind="stable").reset_index(drop=True)
    if len(out) > limit:
        out = out.tail(limit).reset_index(drop=True)
    return out
