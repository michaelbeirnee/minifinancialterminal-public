"""Federal Reserve policy: the hikes and cuts, the cycles, and where it stands.

The fed funds target is the one number every other rate in the platform prices
off, and it moves in a shape no level series shows on its own: a run of hikes,
a hold at the peak, a run of cuts. FRED publishes the *level* daily; this module
turns it into the *decisions* — one row per meeting that moved rates, grouped
into the tightening and easing cycles they add up to.

**How the target series is spliced.** Before 16 December 2008 the FOMC set a
single target (``DFEDTAR``); since then it sets a range (``DFEDTARL`` /
``DFEDTARU``). Neither series alone spans the record, so both are read and the
upper bound carries the history — the single target is its own upper bound.
Changes are measured on that upper bound rather than the midpoint, because it
is the number the announcements are written in: the December 2008 move from
1.00% to "0 to 0.25%" reads as the 75bp cut it was called, not the 87.5bp the
midpoints imply. The lower bound and midpoint come back alongside it.

**A cycle is a run of moves in one direction.** It starts at the first move,
ends at the last one before the direction reverses, and the long holds inside
it stay inside it: the 2015-2018 tightening had twelve months between its first
and second hike and is still one cycle, because that is how it was lived and
how it is written about. The wait at the peak is reported separately as
``hold_days`` — from the last hike to the first cut is the number most of the
questions about a hiking cycle are actually about.

**What this cannot show.** Where the market thinks the next move goes comes
from fed funds futures, and CME's FedWatch probabilities have no free feed. So
nothing here is dressed up as an implied probability. What is free is the
market's own rate: ``stance`` reports the 2-year Treasury yield against the
target midpoint, which is the same directional read — the 2-year trading well
below the target is the market pricing cuts — without inventing a percentage.

Meeting dates come from the Fed's published calendar
(:mod:`backend.providers.fomc`), which covers the recent past and the year
ahead; the decision history from FRED runs back to 1982.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import pandas as pd

from ..core.errors import EmptyDataError, ProviderError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..providers import fomc, fred, yahoo

#: The target range (post-2008), the single target (pre-2008) and the rate that
#: actually printed. Read as one frame so the splice is a column operation.
TARGET_SERIES = "DFEDTARU,DFEDTARL,DFEDTAR,DFF"

#: A rate change is effective the morning after the decision, so a meeting on
#: the 18th shows up in the target series on the 19th. A few days of slack cover
#: a Friday decision and the odd holiday, while staying well short of the six
#: weeks to the next meeting.
DECISION_LAG_DAYS = 4


# --------------------------------------------------------------------------- #
# The series, spliced
# --------------------------------------------------------------------------- #
def _column(df: pd.DataFrame, name: str) -> pd.Series:
    """One FRED column, or an empty one — FRED omits a series with no rows in
    the window rather than returning it full of NaN."""
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce")
    return pd.Series(index=df.index, dtype="float64")


def _splice(df: pd.DataFrame) -> pd.DataFrame:
    """FRED's three target series as one continuous policy path."""
    single = _column(df, "DFEDTAR")
    upper = _column(df, "DFEDTARU").combine_first(single)
    lower = _column(df, "DFEDTARL").combine_first(single)
    out = pd.DataFrame(
        {
            "target_lower": lower.round(4),
            "target_upper": upper.round(4),
            "target_midpoint": ((upper + lower) / 2).round(4),
            "effective_rate": _column(df, "DFF").round(4),
        },
        index=df.index,
    )
    out.index.name = "date"
    return out.dropna(how="all")


def _policy_path(start_date: Optional[str] = None, end_date: Optional[str] = None) -> pd.DataFrame:
    """The spliced path, windowed here rather than upstream.

    Every command in this module needs the whole series — a change is only
    visible against the observation before it — so one full download is fetched
    and cached, and each window is a slice of it. Asking FRED per window would
    make each new date range its own cold request for data already in hand.
    """
    path = _splice(fred.series(TARGET_SERIES))
    if start_date:
        path = path[path.index >= pd.Timestamp(start_date)]
    if end_date:
        path = path[path.index <= pd.Timestamp(end_date)]
    if path.empty:
        raise EmptyDataError("FRED returned no fed funds target observations for that window")
    return path


#: FRED ignores its own resample argument on a multi-series download, so a
#: coarser frequency is applied here instead of asked for upstream.
_PERIODS = {"d": None, "w": "W", "m": "M", "q": "Q", "a": "Y"}


def _resample(path: pd.DataFrame, frequency: Optional[str]) -> pd.DataFrame:
    """Thin a daily path to period-end observations.

    The target is a step function, so the last observation in a period *is* the
    period's rate — but a move and a reversal inside one period collapse to the
    later of the two. Decisions therefore always come from the daily series;
    this is for drawing forty years of it without shipping forty years of rows.
    """
    if not frequency:
        return path
    key = frequency.strip().lower()
    if key not in _PERIODS:
        raise ValueError("frequency must be one of {}".format(", ".join(_PERIODS)))
    code = _PERIODS[key]
    if code is None:      # daily: already the underlying frequency
        return path
    out = path.groupby(path.index.to_period(code)).last()
    ends = out.index.to_timestamp(how="end").normalize()
    # The period in progress ends in the future; date it by the last real
    # observation instead of dropping the most recent rate off the series.
    out.index = ends.where(ends <= path.index[-1], path.index[-1])
    out.index.name = "date"
    return out


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #
def _moves(path: pd.DataFrame) -> pd.DataFrame:
    """One row per day the target moved, oldest first.

    The first observation can never be a change — there is nothing before it to
    compare against — so a path that opens on a decision day loses that one
    move. Every command therefore reads the whole path and filters the moves
    afterwards, rather than reading a windowed path.
    """
    upper = path["target_upper"].dropna()
    if upper.empty:
        return pd.DataFrame(columns=["date", "direction", "change_bps"])
    step = upper.diff()
    moved = step.ne(0) & step.notna()
    if not moved.any():
        return pd.DataFrame(columns=["date", "direction", "change_bps"])

    rows: List[Dict[str, Any]] = []
    previous = upper.shift(1)
    for stamp in upper.index[moved]:
        change_bps = round(float(step.loc[stamp]) * 100, 1)
        rows.append(
            {
                "date": str(stamp.date()),
                "direction": "hike" if change_bps > 0 else "cut",
                "change_bps": change_bps,
                "previous_upper": round(float(previous.loc[stamp]), 4),
                "target_upper": round(float(upper.loc[stamp]), 4),
                "target_lower": _value(path, stamp, "target_lower"),
                "target_midpoint": _value(path, stamp, "target_midpoint"),
                "effective_rate": _value(path, stamp, "effective_rate"),
            }
        )
    return pd.DataFrame(rows)


def _value(path: pd.DataFrame, stamp: Any, column: str) -> Optional[float]:
    value = path.loc[stamp, column]
    return None if pd.isna(value) else round(float(value), 4)


def _label(kind: str, start: str, end: str) -> str:
    """``2022-2023 tightening`` — how a cycle is referred to in writing."""
    years = start[:4] if start[:4] == end[:4] else "{}-{}".format(start[:4], end[:4])
    return "{} {}".format(years, kind)


def _cycles(moves: pd.DataFrame, as_of: Optional[str] = None) -> List[Dict[str, Any]]:
    """Consecutive moves in one direction, grouped into cycles."""
    if moves.empty:
        return []
    today = as_of or str(date.today())
    runs: List[Dict[str, Any]] = []
    for row in moves.itertuples():
        kind = "tightening" if row.direction == "hike" else "easing"
        if runs and runs[-1]["kind"] == kind:
            current = runs[-1]
            current["end_date"] = row.date
            current["moves"] += 1
            current["total_bps"] = round(current["total_bps"] + row.change_bps, 1)
            current["largest_move_bps"] = max(current["largest_move_bps"], abs(row.change_bps))
            current["to_rate"] = row.target_upper
            continue
        runs.append(
            {
                "kind": kind,
                "start_date": row.date,
                "end_date": row.date,
                "moves": 1,
                "total_bps": row.change_bps,
                "largest_move_bps": abs(row.change_bps),
                "from_rate": row.previous_upper,
                "to_rate": row.target_upper,
            }
        )

    for i, cycle in enumerate(runs):
        following = runs[i + 1]["start_date"] if i + 1 < len(runs) else today
        cycle["cycle"] = _label(cycle["kind"], cycle["start_date"], cycle["end_date"])
        cycle["months"] = _months(cycle["start_date"], cycle["end_date"])
        # From the last move to the reversal — "how long did they hold at the
        # peak", which for the current cycle is still running.
        cycle["hold_days"] = (pd.Timestamp(following) - pd.Timestamp(cycle["end_date"])).days
        cycle["status"] = "complete" if i + 1 < len(runs) else "current"
    return runs


def _months(start: str, end: str) -> float:
    return round((pd.Timestamp(end) - pd.Timestamp(start)).days / 30.44, 1)


def _in_window(rows: List[Dict[str, Any]], start_date: Optional[str], end_date: Optional[str],
               key: str = "date") -> List[Dict[str, Any]]:
    first = str(pd.Timestamp(start_date).date()) if start_date else None
    last = str(pd.Timestamp(end_date).date()) if end_date else None
    return [r for r in rows
            if (first is None or r[key] >= first) and (last is None or r[key] <= last)]


def _cycle_for(cycles: List[Dict[str, Any]], when: str) -> Optional[Dict[str, Any]]:
    for cycle in cycles:
        if cycle["start_date"] <= when <= cycle["end_date"]:
            return cycle
    return None


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@command("/economy/fed/policy_rate", providers=("fred",),
         summary="Fed funds target range and the effective rate")
def policy_rate(start_date: Optional[str] = None, end_date: Optional[str] = None,
                frequency: Optional[str] = None, provider: Optional[str] = None) -> Result:
    """The daily policy path: ``target_lower``, ``target_upper``,
    ``target_midpoint`` and the ``effective_rate`` that printed inside it.

    The single pre-2008 target and the post-2008 range are spliced into one
    series, so a window spanning December 2008 is continuous. Before 1982 FRED
    publishes no target at all and only the effective rate comes back.

    ``frequency`` thins the daily series to period-end observations (``w``,
    ``m``, ``q``, ``a``) for drawing long histories. Use the daily default, or
    ``/economy/fed/rate_changes``, for the decisions themselves.
    """
    resolve_provider(provider, ("fred",))
    path = _resample(_policy_path(start_date, end_date), frequency)
    return Result(path, provider="fred", index_name="date",
                  extra={"series_id": TARGET_SERIES, "frequency": frequency or "d",
                         "source": "FRED"})


@command("/economy/fed/rate_changes", providers=("fred",),
         summary="Every hike and cut, newest first")
def rate_changes(start_date: Optional[str] = None, end_date: Optional[str] = None,
                 move: str = "all", min_bps: float = 0, limit: int = 100,
                 provider: Optional[str] = None) -> Result:
    """One row per decision that moved the target, with the cycle it belongs to.

    ``move``: ``all``, ``hike`` or ``cut``. ``min_bps`` filters on the absolute
    size of the move, so ``min_bps=50`` finds the outsized ones. Rows come back
    newest first — a rate history is read from the present backwards.

    Dates are *effective* dates: a decision announced on the afternoon of the
    18th moves the target series on the 19th. ``/economy/fed/meetings`` pairs
    each move with the meeting that took it.
    """
    resolve_provider(provider, ("fred",))
    wanted = move.strip().lower()
    if wanted not in ("all", "hike", "cut", "hikes", "cuts"):
        raise ValueError("move must be all, hike or cut")
    wanted = wanted.rstrip("s")

    path = _policy_path()
    moves = _moves(path)
    if moves.empty:
        raise EmptyDataError("FRED's target series shows no changes")
    cycles = _cycles(moves)

    rows = moves.to_dict("records")
    for row in rows:
        cycle = _cycle_for(cycles, row["date"])
        row["cycle"] = cycle["cycle"] if cycle else None
    rows = _in_window(rows, start_date, end_date)
    if wanted != "all":
        rows = [r for r in rows if r["direction"] == wanted]
    if min_bps:
        rows = [r for r in rows if abs(r["change_bps"]) >= float(min_bps)]
    if not rows:
        raise EmptyDataError("No {} between {} and {}".format(
            "moves" if wanted == "all" else wanted + "s", start_date or "1982", end_date or "today"))

    rows.reverse()
    capped = rows[:max(1, int(limit))]
    hikes = sum(1 for r in rows if r["direction"] == "hike")
    return Result(capped, provider="fred", extra={
        "total": len(rows), "hikes": hikes, "cuts": len(rows) - hikes,
        "net_bps": round(sum(r["change_bps"] for r in rows), 1),
        "truncated": max(0, len(rows) - len(capped)), "source": "FRED"})


@command("/economy/fed/cycles", providers=("fred",),
         summary="Tightening and easing cycles the moves add up to")
def cycles(kind: str = "all", start_date: Optional[str] = None,
           provider: Optional[str] = None) -> Result:
    """Every run of moves in one direction since 1982, newest first.

    ``kind``: ``all``, ``tightening`` or ``easing``. Each row carries how far
    rates travelled (``total_bps``, ``from_rate`` to ``to_rate``), how long the
    run took (``months``), and ``hold_days`` — the wait between its last move
    and the first move of the next cycle, which for the current cycle is the
    time since the Fed last did anything.
    """
    resolve_provider(provider, ("fred",))
    wanted = kind.strip().lower()
    if wanted not in ("all", "tightening", "easing"):
        raise ValueError("kind must be all, tightening or easing")

    rows = _cycles(_moves(_policy_path()))
    if not rows:
        raise EmptyDataError("FRED's target series shows no changes")
    if wanted != "all":
        rows = [r for r in rows if r["kind"] == wanted]
    if start_date:
        rows = [r for r in rows if r["end_date"] >= str(pd.Timestamp(start_date).date())]
    if not rows:
        raise EmptyDataError("No {} cycles after {}".format(wanted, start_date))

    rows.reverse()
    return Result(rows, provider="fred", extra={
        "total": len(rows),
        "tightening": sum(1 for r in rows if r["kind"] == "tightening"),
        "easing": sum(1 for r in rows if r["kind"] == "easing"), "source": "FRED"})


@command("/economy/fed/stance", providers=("fred",),
         summary="Where policy stands right now, and what it took to get here")
def stance(provider: Optional[str] = None) -> Result:
    """The current target range, the last move, the cycle it belongs to, and
    the two comparisons that say whether policy is tight.

    ``real_policy_rate`` is the target midpoint less core PCE inflation
    year-over-year — the rate that matters to a borrower, and the one the
    committee talks in. ``two_year_minus_midpoint`` is the market's answer
    instead of a forecast: the 2-year Treasury trading below the target is the
    market pricing cuts, above it pricing hikes. There is no implied
    probability here because the futures that produce one are not free.
    """
    resolve_provider(provider, ("fred",))
    path = _policy_path()
    moves = _moves(path)
    if moves.empty:
        raise EmptyDataError("FRED's target series shows no changes")

    warnings: List[str] = []
    # The effective rate lags the target by a day or two, so "now" is the last
    # day with a target on it rather than the last row of the frame.
    targeted = path.dropna(subset=["target_upper"])
    latest = targeted.iloc[-1]
    as_of = str(targeted.index[-1].date())
    last = moves.iloc[-1].to_dict()
    all_cycles = _cycles(moves, as_of=as_of)
    current = all_cycles[-1]

    effective = path["effective_rate"].dropna()
    snapshot: Dict[str, Any] = {
        "as_of": as_of,
        "target_lower": round(float(latest["target_lower"]), 4),
        "target_upper": round(float(latest["target_upper"]), 4),
        "target_midpoint": round(float(latest["target_midpoint"]), 4),
        "target_range": "{:.2f}-{:.2f}%".format(latest["target_lower"], latest["target_upper"]),
        "effective_rate": round(float(effective.iloc[-1]), 4) if not effective.empty else None,
        "last_move": last["direction"],
        "last_move_date": last["date"],
        "last_move_bps": last["change_bps"],
        "days_since_last_move": (pd.Timestamp(as_of) - pd.Timestamp(last["date"])).days,
        "cycle": current["cycle"],
        "cycle_kind": current["kind"],
        "cycle_start_date": current["start_date"],
        "cycle_moves": current["moves"],
        "cycle_total_bps": current["total_bps"],
        "cycle_from_rate": current["from_rate"],
    }

    # Both of these are extras rather than the answer, so a provider having a
    # bad afternoon costs a field and a warning, not the command.
    try:
        core_pce = fred.series("PCEPILFE", transform="pc1").dropna().iloc[-1, 0]
        snapshot["core_pce_yoy"] = round(float(core_pce), 2)
        snapshot["real_policy_rate"] = round(snapshot["target_midpoint"] - float(core_pce), 2)
    except (EmptyDataError, ProviderError) as exc:
        warnings.append("Core PCE unavailable, so no real policy rate: {}".format(exc))
    try:
        two_year = fred.series("DGS2").dropna().iloc[-1, 0]
        snapshot["two_year_yield"] = round(float(two_year), 4)
        snapshot["two_year_minus_midpoint"] = round(
            float(two_year) - snapshot["target_midpoint"], 4)
    except (EmptyDataError, ProviderError) as exc:
        warnings.append("2-year Treasury yield unavailable: {}".format(exc))

    try:
        upcoming = [m for m in fomc.meetings().to_dict("records") if m["date"] >= as_of]
        if upcoming:
            snapshot["next_meeting"] = upcoming[0]["date"]
            snapshot["next_meeting_projections"] = bool(upcoming[0]["projections"])
            snapshot["days_to_next_meeting"] = (
                pd.Timestamp(upcoming[0]["date"]) - pd.Timestamp(as_of)).days
    except (EmptyDataError, ProviderError) as exc:
        warnings.append("The Fed's meeting calendar did not load: {}".format(exc))

    return Result(snapshot, provider="fred", warnings=warnings,
                  extra={"cycles": len(all_cycles), "source": "FRED"})


@command("/economy/fed/meetings", providers=("federalreserve",),
         summary="FOMC meeting calendar, with what each meeting did to rates")
def meetings(year: Optional[int] = None, upcoming_only: bool = False, limit: int = 100,
             provider: Optional[str] = None) -> Result:
    """Scheduled meeting dates, oldest first, joined to the decision each took.

    ``decision`` reads ``hiked``, ``cut`` or ``held`` for a meeting that has
    happened and is empty for one that has not; the move itself is matched by
    effective date, which lands the day after the statement. ``projections``
    marks the four meetings a year that publish the Summary of Economic
    Projections — the dot plot — and ``press_conference`` the ones the chair
    takes questions after.

    The Fed's calendar page covers the last few years and the year ahead. For
    the decisions before that, ``/economy/fed/rate_changes`` runs back to 1982.
    """
    resolve_provider(provider, ("federalreserve",))
    rows = fomc.meetings().to_dict("records")
    if year:
        rows = [r for r in rows if r["year"] == int(year)]
    today = str(date.today())
    if upcoming_only:
        rows = [r for r in rows if r["date"] >= today]
    if not rows:
        raise EmptyDataError("No FOMC meetings on the published calendar for that filter")

    warnings: List[str] = []
    by_date: Dict[str, Dict[str, Any]] = {}
    path = pd.DataFrame()
    try:
        path = _policy_path()
        by_date = {row["date"]: row for row in _moves(path).to_dict("records")}
    except (EmptyDataError, ProviderError) as exc:
        warnings.append("Rate changes unavailable, so no decision column: {}".format(exc))

    for row in rows:
        row["status"] = "past" if row["date"] < today else "upcoming"
        move = _decision_for(by_date, row["date"]) if by_date else None
        if move:
            row.update({
                "decision": "hiked" if move["direction"] == "hike" else "cut",
                "change_bps": move["change_bps"],
                "target_upper": move["target_upper"],
                "target_lower": move["target_lower"],
                "effective_date": move["date"],
            })
        else:
            held = row["status"] == "past" and bool(by_date)
            # A meeting that held still sets a rate — the one already in force —
            # so the column reads as a decision rather than a blank.
            level = _level_at(path, row["date"]) if held else {}
            row.update({
                "decision": "held" if held else None,
                "change_bps": 0 if held else None,
                "target_upper": level.get("target_upper"),
                "target_lower": level.get("target_lower"),
                "effective_date": None,
            })

    capped = rows[:max(1, int(limit))]
    moved = sum(1 for r in rows if r.get("change_bps"))
    return Result(capped, provider="federalreserve", warnings=warnings, extra={
        "total": len(rows), "moved_rates": moved,
        "next_meeting": next((r["date"] for r in rows if r["status"] == "upcoming"), None),
        "source": "federalreserve.gov"})


def _level_at(path: pd.DataFrame, day: str) -> Dict[str, Optional[float]]:
    """The target in force on a date — the last observation at or before it."""
    if path.empty:
        return {}
    row = path[["target_lower", "target_upper"]].dropna().asof(pd.Timestamp(day))
    if row is None or row.isna().all():
        return {}
    return {k: None if pd.isna(v) else round(float(v), 4) for k, v in row.items()}


def _decision_for(by_date: Dict[str, Dict[str, Any]], meeting_date: str) -> Optional[Dict[str, Any]]:
    """The move a meeting produced, if it produced one.

    A decision is effective the next business morning, so the search runs
    forward from the meeting rather than expecting a same-day change.
    """
    start = pd.Timestamp(meeting_date)
    for offset in range(DECISION_LAG_DAYS + 1):
        hit = by_date.get(str((start + pd.Timedelta(offset, unit="D")).date()))
        if hit:
            return hit
    return None


@command("/economy/fed/cycle_performance", providers=("yahoo",),
         summary="What assets did during each hiking and cutting cycle")
def cycle_performance(symbols: str = "SPY,TLT,GLD", kind: str = "all",
                      start_date: Optional[str] = None, provider: Optional[str] = None) -> Result:
    """Return of each symbol from a cycle's first move to its last, in percent.

    The question behind a hiking cycle — what happened to stocks, to long
    bonds, to gold — measured over the run itself rather than a calendar year.
    Closes are split- and dividend-adjusted, so these are total returns from
    the first session on or after the opening move to the last on or before the
    closing one. A cycle that predates a fund comes back empty for that fund
    rather than quietly measured from its first day.

    ``kind``: ``all``, ``tightening`` or ``easing``. Note that the cycle end is
    the last *move*, not the reversal — the hold at the peak is in the next
    cycle's run-up, and ``/economy/fed/cycles`` reports it as ``hold_days``.
    """
    resolve_provider(provider, ("yahoo",))
    wanted = kind.strip().lower()
    if wanted not in ("all", "tightening", "easing"):
        raise ValueError("kind must be all, tightening or easing")
    tickers = [s.strip().upper() for s in symbols.replace(" ", ",").split(",") if s.strip()]
    if not tickers:
        raise ValueError("symbols must name at least one ticker")

    runs = _cycles(_moves(_policy_path()))
    if wanted != "all":
        runs = [r for r in runs if r["kind"] == wanted]
    if start_date:
        runs = [r for r in runs if r["end_date"] >= str(pd.Timestamp(start_date).date())]
    if not runs:
        raise EmptyDataError("No {} cycles to measure".format(wanted))

    warnings: List[str] = []
    closes: Dict[str, pd.Series] = {}
    for ticker in tickers:
        try:
            history = yahoo.history(ticker, start=runs[0]["start_date"])
            closes[ticker] = history["close"].dropna()
        except (EmptyDataError, ProviderError, ValueError) as exc:
            warnings.append("{}: {}".format(ticker, exc))

    if not closes:
        raise EmptyDataError("No price history for {}".format(", ".join(tickers)))

    rows: List[Dict[str, Any]] = []
    for cycle in runs:
        row = {k: cycle[k] for k in ("cycle", "kind", "start_date", "end_date", "moves",
                                     "total_bps", "months", "from_rate", "to_rate")}
        for ticker, series in closes.items():
            row[ticker] = _window_return(series, cycle["start_date"], cycle["end_date"])
        rows.append(row)

    rows.reverse()
    return Result(rows, provider="yahoo", warnings=warnings, extra={
        "symbols": list(closes), "cycles": len(rows),
        "note": "Total return over the cycle, in percent, on adjusted closes."})


def _window_return(closes: pd.Series, start: str, end: str) -> Optional[float]:
    """Percent return between two dates, or ``None`` before the data starts."""
    inside = closes.loc[(closes.index >= pd.Timestamp(start)) & (closes.index <= pd.Timestamp(end))]
    if len(inside) < 2 or closes.index.min() > pd.Timestamp(start):
        return None
    return round(float(inside.iloc[-1] / inside.iloc[0] - 1) * 100, 2)
