"""Evaluate a thesis's executable falsifiers against live data.

Each :class:`~backend.models.ThesisCheck` names a registered command, a field
and a breaking condition. Evaluation re-runs the command through the same
:func:`backend.core.registry.execute` every other interface uses, reads the
field from the *last* result row (the most recent observation in a series),
and compares. The comparator describes failure: ``value < threshold`` with
comparator ``lt`` means the thesis is broken, not confirmed.

:func:`preflight` runs the same evaluation once *before* a check is stored, so
a falsifier that cannot be read, or that is already true, is caught at the door
rather than discovered on the first sweep.

Nothing here calls a model. A thesis registered by hand is graded exactly the
same way as one a generator will eventually produce.
"""
from __future__ import annotations

import operator
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from ..core.registry import execute
from ..models import Thesis, ThesisCheck

_OPS = {
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
    "eq": operator.eq,
    "ne": operator.ne,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _naive(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; compare like with like."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def extract_field(results: Any, field: str) -> Optional[float]:
    """The named field from the last row, coerced to float.

    Rows land newest-last for every time-series command, so "the last row"
    is "the most recent observation". Rows without the field (or with a
    non-numeric value) are skipped walking backwards, so a trailing partial
    row does not mask a real value.
    """
    rows = results if isinstance(results, list) else [results]
    for row in reversed(rows):
        if not isinstance(row, dict) or field not in row:
            continue
        value = row[field]
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def evaluate_check(check: ThesisCheck) -> Tuple[Optional[float], str]:
    """Run one falsifier. Returns ``(value, status)`` and mutates the check.

    Provider failures mark the check ``error`` without breaking the thesis —
    a rate-limited API is not evidence about the claim. An expired check that
    never breached counts as held.
    """
    now = _utcnow()
    check.last_checked_at = now

    if check.status == "broken":
        return check.last_value, check.status  # broken is terminal

    try:
        obj = execute(check.command_path, **dict(check.parameters or {}))
        value = extract_field(obj.results, check.field)
    except Exception as exc:  # noqa: BLE001 - provider errors are the common case
        check.status = "error"
        check.last_error = "{}: {}".format(type(exc).__name__, exc)
        return check.last_value, check.status

    check.last_error = None
    if value is None:
        check.status = "error"
        check.last_error = "field {!r} not found in {} result".format(
            check.field, check.command_path
        )
        return check.last_value, check.status

    check.last_value = value
    if _OPS[check.comparator](value, check.threshold):
        check.status = "broken"
        check.breached_at = now
    elif check.by_date is not None and _naive(check.by_date) < _naive(now):
        check.status = "expired"
    else:
        check.status = "holding"
    return value, check.status


def preflight(check: ThesisCheck) -> Tuple[Optional[float], str, Optional[str]]:
    """Run a check once *before* it is stored. ``(value, status, problem)``.

    Registering a falsifier used to validate only that the command exists and
    the comparator is legal, which let through two checks that are worse than
    no check at all:

    * one naming a field the command does not return, which sits at ``holding``
      until the first sweep quietly turns it to ``error`` — a thesis that looks
      tracked and has nothing watching it;
    * one that is *already* true, which breaks its thesis on the first
      evaluation and files it on the scoreboard as a failure that never had a
      chance to be anything else;
    * one whose ``by_date`` has already passed, which is graded as held from
      the moment it is written and can therefore never fire.

    None of the three is detectable without running the thing, so it is run.
    ``problem`` is ``None`` when the check is worth keeping; the check carries
    its first observed value either way, so an accepted falsifier starts life
    with a reading rather than a null.
    """
    value, status = evaluate_check(check)
    if status == "error":
        return value, status, check.last_error or "the check could not be evaluated"
    if status == "broken":
        return value, status, (
            "already breached at creation: {} is {:g}, and the check breaks on "
            "{} {} {:g}".format(check.field, value, check.field, check.comparator,
                                check.threshold)
        )
    if status == "expired":
        return value, status, (
            "deadline {} has already passed, so this check counts as held from "
            "the moment it is written and can never fire".format(
                _naive(check.by_date).date() if check.by_date else "?")
        )
    return value, status, None


def evaluate_thesis(thesis: Thesis) -> str:
    """Evaluate every check and derive the thesis status.

    One breached falsifier breaks the thesis — that is what a falsifier is.
    A thesis past its review date with nothing breached is ``supported``:
    it survived every way it promised it could fail. Statuses set by hand
    (``closed``) are left alone.
    """
    if thesis.status == "closed":
        return thesis.status

    for check in thesis.checks:
        evaluate_check(check)

    now = _utcnow()
    if any(c.status == "broken" for c in thesis.checks):
        thesis.status = "broken"
        if thesis.closed_at is None:
            thesis.closed_at = now
    elif thesis.review_by is not None and _naive(thesis.review_by) < _naive(now):
        thesis.status = "supported" if thesis.checks else "expired"
        if thesis.closed_at is None:
            thesis.closed_at = now
    else:
        thesis.status = "open"

    # The engine grades its own homework on the same ruler it grades signals:
    # the thesis enters the signal log anchored at its creation date and its
    # row is refreshed here on every evaluation. Recording opens its own
    # session and swallows its errors, so a full disk cannot fail a grading.
    from . import memory

    memory.record_thesis(thesis)
    return thesis.status
