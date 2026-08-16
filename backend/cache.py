"""Lightweight TTL cache with an in-memory layer backed by SQLite.

Used to avoid re-downloading / re-computing market data on every request.
The interface intentionally mirrors a small subset of what you'd get from
Redis so it can be swapped for a network cache later without touching callers.

The disk layer is a single SQLite file (``<cache_dir>/cache.db``) rather than
one pickle file per key; legacy ``*.pkl`` entries found in the cache dir are
imported into it on startup.
"""
from __future__ import annotations

import pickle
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import hashlib

from .config import settings


class TTLCache:
    def __init__(self, ttl_seconds: int, cache_dir: str) -> None:
        self.ttl = ttl_seconds
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "cache.db"
        self._mem: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        # One shared connection, guarded by the lock (sqlite3 connections are
        # not safe for unsynchronised cross-thread use).
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_entries (
                key        TEXT PRIMARY KEY,
                value      BLOB NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        self._db.commit()
        self._import_legacy_pickles()

    def _import_legacy_pickles(self) -> None:
        """One-time migration of pre-SQLite ``<key>.pkl`` files into the DB."""
        for f in self.dir.glob("*.pkl"):
            try:
                blob = f.read_bytes()
                pickle.loads(blob)  # only migrate entries that still load
            except (OSError, pickle.PickleError, EOFError, AttributeError):
                f.unlink(missing_ok=True)
                continue
            with self._lock:
                self._db.execute(
                    "INSERT OR IGNORE INTO cache_entries (key, value, created_at) VALUES (?, ?, ?)",
                    (f.stem, blob, f.stat().st_mtime),
                )
                self._db.commit()
            f.unlink(missing_ok=True)

    @staticmethod
    def make_key(*parts: Any) -> str:
        raw = "|".join(repr(p) for p in parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def get(self, key: str, ttl: int | None = None) -> Any | None:
        """Return a cached value, or ``None`` if absent/expired.

        ``ttl`` overrides the cache default for this lookup, which lets callers
        keep slow-moving reference data (ticker maps, index membership) far
        longer than intraday quotes without needing a second cache instance.
        """
        ttl = self.ttl if ttl is None else ttl
        now = time.time()
        with self._lock:
            entry = self._mem.get(key)
            if entry and now - entry[0] < ttl:
                self.hits += 1
                return entry[1]

            # Fall through to SQLite (survives process restarts).
            row = self._db.execute(
                "SELECT value, created_at FROM cache_entries WHERE key = ?", (key,)
            ).fetchone()
            if row is not None:
                blob, created_at = row
                if now - created_at < ttl:
                    try:
                        value = pickle.loads(blob)
                    except (pickle.PickleError, EOFError, AttributeError):
                        self._db.execute("DELETE FROM cache_entries WHERE key = ?", (key,))
                        self._db.commit()
                    else:
                        self._mem[key] = (created_at, value)
                        self.hits += 1
                        return value

            self.misses += 1
        return None

    def set(self, key: str, value: Any) -> None:
        now = time.time()
        with self._lock:
            self._mem[key] = (now, value)
            try:
                self._db.execute(
                    "INSERT OR REPLACE INTO cache_entries (key, value, created_at) VALUES (?, ?, ?)",
                    (key, pickle.dumps(value), now),
                )
                self._db.commit()
            except (sqlite3.Error, pickle.PickleError):
                pass  # disk cache is best-effort

    def clear(self) -> int:
        with self._lock:
            self._mem.clear()
            removed = self._db.execute("DELETE FROM cache_entries").rowcount
            self._db.commit()
        # Sweep any stray legacy pickle files too.
        for f in self.dir.glob("*.pkl"):
            f.unlink(missing_ok=True)
            removed += 1
        return removed

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        with self._lock:
            disk_entries = self._db.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]
            memory_entries = len(self._mem)
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
            "memory_entries": memory_entries,
            "disk_entries": disk_entries,
            "database": str(self.db_path),
            "ttl_seconds": self.ttl,
        }


cache = TTLCache(settings.cache_ttl_seconds, settings.cache_dir)
