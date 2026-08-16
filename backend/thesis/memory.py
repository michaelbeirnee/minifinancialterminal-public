"""The engine's memory: record everything, then study it, then learn from it.

Scanners call :func:`record_events` as they emit; the spine calls
:func:`record_thesis` whenever a thesis is graded, so the engine's own output
is measured on the same ruler as its inputs; the model endpoints call
:func:`record_triage` / :func:`record_deepdive` with every verdict including
declines. :func:`grade_pending` stamps realised excess returns onto events
once each horizon has actually elapsed — outcomes are measured, never
predicted — and :func:`report` turns the graded log into per-family base
rates, which is how the gate weights eventually stop being guesses.

That last step only closes the loop if something reads it, so
:func:`base_rate_index` and :func:`describe_base_rate` put the measured
history of a family in front of triage before it ranks anything.

Recording opens its own database session (scanners run from all four
interfaces, most of which carry no request session) and is deliberately
non-fatal: a full disk or a locked database must never break a scan, so every
writer swallows its errors after logging them. The read side is exposed as
registry commands; the write side lives here and in the router only.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..database import SessionLocal
from ..models import DeepDiveRecord, SignalEvent, SignalRun, TriageRecord

log = logging.getLogger(__name__)

#: Trading-day horizons graded, and the calendar-day cushion that must have
#: passed before each is considered gradeable (sessions ~= days * 5/7).
HORIZONS: Dict[str, Tuple[int, int]] = {
    "fwd_1m": (21, 35),
    "fwd_3m": (63, 100),
    "fwd_6m": (126, 195),
    "fwd_12m": (252, 380),
}

#: Separates a scanner's namespace from the specific family it emitted.
#: ``insider_cluster:board_backed_strategic`` keeps the provenance while still
#: splitting the discriminator the base-rate report exists to measure — an
#: index fund crossing 10% and a board-backed strategic buyer are not the same
#: bet, and a report that pools them can never say so.
FAMILY_SEP = ":"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def qualify(namespace: str, family: Optional[str]) -> str:
    """``insider_cluster`` + ``both`` -> ``insider_cluster:both``.

    A blank family, or one already carrying the namespace, is returned
    unchanged so requalifying is a no-op.
    """
    family = str(family or "").strip()
    if not family or family == namespace:
        return namespace
    if family.startswith(namespace + FAMILY_SEP):
        return family
    return "{}{}{}".format(namespace, FAMILY_SEP, family)


def event_key(family: str, symbol: str, anchor: str) -> str:
    raw = "{}|{}|{}".format(family, symbol, anchor)
    return hashlib.sha1(raw.encode()).hexdigest()[:32]


def record_events(family: str, rows: List[Dict[str, Any]], kind: str,
                  parameters: Optional[Dict[str, Any]] = None,
                  duration_ms: Optional[int] = None) -> Optional[int]:
    """Upsert one scan's emissions and log the run. Returns new-event count.

    ``rows`` need ``symbol``, ``known_on`` (ISO date), and optionally
    ``issuer_cik``, ``score``, a ``payload`` dict, and ``family`` — the
    specific family *this* row belongs to, qualified under the ``family``
    argument's namespace. The idempotency key is (qualified family, symbol,
    known_on), so re-running a scan refreshes ``last_seen_at`` and the score
    instead of duplicating the event.
    """
    new = 0
    session = None
    try:
        session = SessionLocal()
        for row in rows:
            symbol = str(row["symbol"]).upper()
            anchor = str(row["known_on"])[:10]
            row_family = qualify(family, row.get("family"))
            key = event_key(row_family, symbol, anchor)
            existing = (
                session.query(SignalEvent).filter(SignalEvent.event_key == key).first()
            )
            if existing is None:
                session.add(SignalEvent(
                    event_key=key, family=row_family, symbol=symbol,
                    issuer_cik=str(row.get("issuer_cik") or "") or None,
                    known_on=datetime.fromisoformat(anchor),
                    score=row.get("score"),
                    payload=dict(row.get("payload") or {}),
                ))
                new += 1
            else:
                existing.last_seen_at = _utcnow()
                if row.get("score") is not None:
                    existing.score = row["score"]
                if row.get("payload"):
                    existing.payload = dict(row["payload"])
        session.add(SignalRun(
            kind=kind, parameters=dict(parameters or {}),
            events_seen=len(rows), events_new=new, duration_ms=duration_ms,
        ))
        session.commit()
        return new
    except Exception as exc:  # noqa: BLE001 - memory must never break a scan
        if session is not None:
            session.rollback()
        log.warning("signal memory write failed: %s", exc)
        return None
    finally:
        if session is not None:
            session.close()


def record_triage(user_id: Optional[int], model: Optional[str],
                  parameters: Dict[str, Any], cards: List[str],
                  verdict: Dict[str, Any]) -> None:
    session = None
    try:
        session = SessionLocal()
        promoted = sum(1 for c in verdict.get("candidates", []) if c.get("promote"))
        session.add(TriageRecord(user_id=user_id, model=model,
                                 parameters=parameters, cards=cards,
                                 verdict=verdict, promoted=promoted))
        session.commit()
    except Exception as exc:  # noqa: BLE001
        if session is not None:
            session.rollback()
        log.warning("triage memory write failed: %s", exc)
    finally:
        if session is not None:
            session.close()


def record_deepdive(user_id: Optional[int], model: Optional[str], symbol: str,
                    candidate: Dict[str, Any], dossier: Dict[str, Any],
                    draft_thesis_id: Optional[int]) -> None:
    session = None
    try:
        session = SessionLocal()
        session.add(DeepDiveRecord(
            user_id=user_id, model=model, symbol=symbol, candidate=candidate,
            dossier=dossier, proceeded=bool(dossier.get("proceed")),
            draft_thesis_id=draft_thesis_id,
        ))
        session.commit()
    except Exception as exc:  # noqa: BLE001
        if session is not None:
            session.rollback()
        log.warning("deepdive memory write failed: %s", exc)
    finally:
        if session is not None:
            session.close()


def record_thesis(thesis: Any) -> Optional[int]:
    """Log a thesis into the signal log so its own outcomes get graded too.

    Without this the engine learns about *signals* but never about the theses
    it builds from them — and the deep-dive drafts, the most expensive thing it
    produces, are never scored against what the price actually did.

    The event is anchored at the thesis's creation date, not at the verdict:
    the question worth calibrating is "was this idea any good", measured from
    the moment it was actionable. Because the key is (family, symbol, creation
    date), re-evaluating a thesis refreshes its payload with the current status
    rather than duplicating it — so a thesis enters the log on its first
    evaluation and its row stays current through to the terminal verdict.

    One event per symbol: a two-name pair trade is two things to grade.
    """
    symbols = [s.strip().upper() for s in str(thesis.symbols or "").split(",") if s.strip()]
    if not symbols:
        return None  # nothing to price, so nothing to learn from

    created = getattr(thesis, "created_at", None) or _utcnow()
    payload = {
        "status": thesis.status,
        "source": thesis.source,
        "title": str(thesis.title or "")[:200],
        "direction": thesis.direction,
        "checks": len(thesis.checks or []),
        "checks_broken": sum(1 for c in (thesis.checks or []) if c.status == "broken"),
        "review_by": str(thesis.review_by.date()) if thesis.review_by else None,
        "thesis_id": thesis.id,
    }
    return record_events(
        family="thesis",
        rows=[{"symbol": s, "known_on": str(created.date()),
               "family": thesis.source, "score": thesis.prior, "payload": payload}
              for s in symbols],
        kind="thesis_evaluate",
        parameters={"thesis_id": thesis.id, "status": thesis.status},
    )


# --------------------------------------------------------------------------- #
# Study: stamp outcomes once they exist
# --------------------------------------------------------------------------- #
def _price_panel(symbols: List[str], start: str):
    """Wide adjusted-close frame. Split out so tests can monkeypatch it."""
    import yfinance as yf

    data = yf.download(symbols, start=start, interval="1d", auto_adjust=True,
                       progress=False, threads=True, group_by="column")
    if data is None or data.empty:
        return None
    import pandas as pd

    closes = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data[["Close"]]
    if closes.shape[1] == 1 and len(symbols) == 1:
        closes.columns = [symbols[0]]
    if isinstance(closes.index, pd.DatetimeIndex) and closes.index.tz is not None:
        closes.index = closes.index.tz_localize(None)
    return closes.sort_index()


def grade_pending(limit: int = 500) -> Dict[str, Any]:
    """Fill outcome columns for events whose horizons have elapsed.

    Entry is the first close *after* ``known_on`` — the first session the
    signal was public. Each horizon column is excess return vs the event's
    benchmark over the identical window, written only when enough calendar
    time has passed; an event keeps being revisited until its longest horizon
    is stamped.
    """
    session = SessionLocal()
    try:
        now = _utcnow().replace(tzinfo=None)
        cutoff = now - timedelta(days=HORIZONS["fwd_1m"][1])
        pending = (
            session.query(SignalEvent)
            .filter(SignalEvent.fwd_12m.is_(None), SignalEvent.known_on <= cutoff)
            .order_by(SignalEvent.known_on)
            .limit(limit)
            .all()
        )
        if not pending:
            return {"graded": 0, "note": "nothing gradeable yet"}

        symbols = sorted({e.symbol for e in pending})
        benchmarks = sorted({e.benchmark for e in pending})
        start = (min(e.known_on for e in pending) - timedelta(days=7)).date().isoformat()
        panel = _price_panel(symbols + benchmarks, start)
        if panel is None:
            return {"graded": 0, "note": "price download failed"}

        graded = 0
        for event in pending:
            if event.symbol not in panel.columns or event.benchmark not in panel.columns:
                continue
            series = panel[event.symbol].dropna()
            bench = panel[event.benchmark].dropna()
            pos = series.index.searchsorted(event.known_on, side="right")
            bpos = bench.index.searchsorted(event.known_on, side="right")
            if pos >= len(series) or bpos >= len(bench):
                continue
            touched = False
            for column, (sessions_n, cushion_days) in HORIZONS.items():
                if getattr(event, column) is not None:
                    continue
                if event.known_on + timedelta(days=cushion_days) > now:
                    continue
                if pos + sessions_n >= len(series) or bpos + sessions_n >= len(bench):
                    continue
                entry, exit_ = float(series.iloc[pos]), float(series.iloc[pos + sessions_n])
                bentry, bexit = float(bench.iloc[bpos]), float(bench.iloc[bpos + sessions_n])
                if entry <= 0 or bentry <= 0:
                    continue
                setattr(event, column, (exit_ / entry - 1) - (bexit / bentry - 1))
                touched = True
            if touched:
                event.graded_at = _utcnow()
                graded += 1
        session.commit()
        return {"graded": graded, "pending_examined": len(pending)}
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        return {"graded": 0, "error": str(exc)}
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Learn: base rates per family, straight from the log
# --------------------------------------------------------------------------- #
def report() -> List[Dict[str, Any]]:
    """Per-family outcome summary of every graded event on record."""
    import numpy as np

    session = SessionLocal()
    try:
        events = session.query(SignalEvent).all()
        by_family: Dict[str, List[SignalEvent]] = {}
        for event in events:
            by_family.setdefault(event.family, []).append(event)

        rows: List[Dict[str, Any]] = []
        for family, members in sorted(by_family.items()):
            row: Dict[str, Any] = {
                "family": family,
                "events": len(members),
                "first": str(min(m.known_on for m in members).date()),
                "last": str(max(m.known_on for m in members).date()),
            }
            for column in HORIZONS:
                values = [getattr(m, column) for m in members
                          if getattr(m, column) is not None]
                label = column.replace("fwd_", "")
                if not values:
                    row["graded_" + label] = 0
                    continue
                arr = np.array(values, dtype=float)
                row["graded_" + label] = len(arr)
                row["mean_excess_" + label] = round(float(arr.mean()), 4)
                row["median_excess_" + label] = round(float(np.median(arr)), 4)
                row["hit_rate_" + label] = round(float((arr > 0).mean()), 3)
            rows.append(row)
        return rows
    finally:
        session.close()


def base_rate_index() -> Dict[str, Dict[str, Any]]:
    """:func:`report` keyed by family, for callers that need one lookup.

    Never raises: a caller decorating a scan with base rates would rather show
    no base rate than fail the scan.
    """
    try:
        return {row["family"]: row for row in report()}
    except Exception as exc:  # noqa: BLE001
        log.warning("base rate lookup failed: %s", exc)
        return {}


def describe_base_rate(row: Optional[Dict[str, Any]], horizon: str = "3m") -> Optional[str]:
    """One line of measured history for a family, or ``None`` if too thin.

    Below ten graded events a hit rate is noise dressed as evidence, so it is
    withheld rather than shown with a caveat nobody reads.
    """
    if not row:
        return None
    graded = row.get("graded_" + horizon) or 0
    if graded < 10:
        return None
    return "{} graded {} events: {:.0f}% beat benchmark, mean excess {:+.1f}%".format(
        row["family"], graded,
        100 * row["hit_rate_" + horizon], 100 * row["mean_excess_" + horizon],
    )


# --------------------------------------------------------------------------- #
# Maintenance
# --------------------------------------------------------------------------- #
def backfill_families() -> Dict[str, Any]:
    """Re-key events written before families were recorded separately.

    The original scanner wrote every cluster under its namespace with the real
    family buried in ``payload["family"]``, which made :func:`report` collapse
    all three families into one bucket. This moves each such event onto its
    qualified family and re-derives ``event_key``.

    Idempotent, and safe to run on every startup: rows already qualified are
    skipped. Where a re-keyed event collides with one the fixed scanner already
    wrote, the two are merged — grades earned under the old key are copied onto
    the survivor before the duplicate is dropped, because a stamped horizon is
    the one thing here that cannot be recomputed on demand.
    """
    session = None
    moved, merged = 0, 0
    try:
        session = SessionLocal()
        # Filtered in SQL: on an already-fixed database this boots on an empty
        # result rather than dragging every event through Python.
        stale = [
            e for e in session.query(SignalEvent)
            .filter(~SignalEvent.family.like("%{}%".format(FAMILY_SEP))).all()
            if (e.payload or {}).get("family")
        ]
        for event in stale:
            new_family = qualify(event.family, (event.payload or {}).get("family"))
            if new_family == event.family:
                continue
            anchor = event.known_on.date().isoformat()
            new_key = event_key(new_family, event.symbol, anchor)
            twin = (
                session.query(SignalEvent)
                .filter(SignalEvent.event_key == new_key, SignalEvent.id != event.id)
                .first()
            )
            if twin is None:
                event.family, event.event_key = new_family, new_key
                moved += 1
                continue
            for column in HORIZONS:
                if getattr(twin, column) is None and getattr(event, column) is not None:
                    setattr(twin, column, getattr(event, column))
                    twin.graded_at = twin.graded_at or event.graded_at
            session.delete(event)
            merged += 1
        session.commit()
        return {"moved": moved, "merged": merged}
    except Exception as exc:  # noqa: BLE001 - a failed backfill must not stop boot
        if session is not None:
            session.rollback()
        log.warning("signal family backfill failed: %s", exc)
        return {"moved": 0, "merged": 0, "error": str(exc)}
    finally:
        if session is not None:
            session.close()
