"""The filer's own XBRL facts, reshaped into something two periods can be read out of.

``sec.company_facts`` hands back a four-level nested object — taxonomy, then
concept, then unit, then a list of every value ever reported for it. That shape
is right for looking up one number and wrong for every question this package
asks, all of which are of the form "what did the newest filing say, what did the
one before it say, and when did each become public".

So the whole object is flattened once into a table with one row per reported
fact, and the three things the statement builders in
:mod:`backend.providers.sec` do not keep — ``filed``, ``accn`` and ``form`` —
are kept here, because they are what makes a change *dated*. A flag anchored on
the period end would claim knowledge two months before the filing existed.

The span windows are imported from the SEC provider rather than restated. What
counts as an annual period is one fact about fiscal calendars, and two copies of
it would eventually disagree.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from ..core.caching import TTL_FUNDAMENTAL, cached
from ..core.errors import EmptyDataError
from ..providers import sec

#: Fiscal years and quarters wobble around 364 and 91 days; these are the SEC
#: provider's own windows, shared so the two cannot drift.
ANNUAL_DAYS = sec._ANNUAL_DAYS
QUARTER_DAYS = sec._QUARTER_DAYS

#: Forms that carry a periodic financial report. 10-K and 10-Q for domestic
#: filers, 20-F and 40-F for the foreign private issuers that trade here as ADRs.
ANNUAL_FORMS: Tuple[str, ...] = ("10-K", "20-F", "40-F")
PERIODIC_FORMS: Tuple[str, ...] = ("10-K", "10-Q", "20-F", "40-F")

#: How far apart two period ends may be and still count as the same period one
#: year on. Wide enough for a 52/53-week calendar and a fiscal year-end that
#: moved by a few days; narrow enough to never pair a year with two years back.
YEAR_GAP: Tuple[int, int] = (300, 430)

#: Columns every fact row carries, whether or not the filer's payload had them.
_COLUMNS: Tuple[str, ...] = (
    "taxonomy", "concept", "label", "unit", "start", "end", "val",
    "accn", "fy", "fp", "form", "filed", "frame",
)


@cached("flagged.facts.v1", ttl=TTL_FUNDAMENTAL)
def fact_table(symbol: str) -> pd.DataFrame:
    """Every fact ``symbol`` has ever tagged, one row each.

    Cached as one object because the flattening is the expensive part and every
    detector in this package wants the same table. A large filer produces tens
    of thousands of rows, which is a few megabytes on disk and the reason this
    is not rebuilt per detector.
    """
    payload = sec.company_facts(sec.cik_for(symbol))
    # One list of records, one frame. Concatenating a frame per concept is
    # both slower and noisier — instantaneous concepts have no ``start``
    # column, and pandas warns about every all-NA column it has to invent.
    records: List[Dict[str, Any]] = []
    for taxonomy, concepts in (payload.get("facts") or {}).items():
        for concept, node in (concepts or {}).items():
            label = node.get("label") or concept
            for unit, values in (node.get("units") or {}).items():
                for fact in values or ():
                    records.append({
                        "taxonomy": taxonomy, "concept": concept, "label": label,
                        "unit": unit, **fact,
                    })
    if not records:
        raise EmptyDataError("{} has no XBRL facts on file with SEC".format(symbol))

    df = pd.DataFrame(records)
    for column in _COLUMNS:
        if column not in df.columns:
            df[column] = None
    for column in ("start", "end", "filed"):
        df[column] = pd.to_datetime(df[column], errors="coerce")
    df["val"] = pd.to_numeric(df["val"], errors="coerce")
    df["form"] = df["form"].astype("string").fillna("")
    # Duration facts carry a start; instantaneous ones (balance-sheet items) do
    # not, and NaN days is what later tells the two apart.
    df["days"] = (df["end"] - df["start"]).dt.days
    df = df.dropna(subset=["end", "val", "filed"])
    if df.empty:
        raise EmptyDataError("{} tags no dated XBRL values".format(symbol))
    return df[list(_COLUMNS) + ["days"]].reset_index(drop=True)


def _spans(frame: pd.DataFrame, period: str) -> pd.DataFrame:
    """Keep only the rows whose reporting span is the one that was asked for.

    An instantaneous fact has no span at all and is returned untouched: a
    balance-sheet line is the same number whether the reader wanted the year or
    the quarter, and filtering it on a duration would discard every row.
    """
    if frame["days"].isna().all():
        return frame
    window = ANNUAL_DAYS if period == "annual" else QUARTER_DAYS
    return frame[frame["days"].between(*window)]


def concept_series(
    facts: pd.DataFrame,
    tags: Sequence[str],
    period: str = "annual",
    forms: Optional[Sequence[str]] = None,
    taxonomy: str = "us-gaap",
) -> pd.DataFrame:
    """One row per period end for the best of several synonymous tags.

    ``tags`` is a preference list, not a priority queue, and it is resolved the
    same way :func:`backend.providers.sec._fact_series` resolves one: every tag
    is evaluated and the one reaching the most recent period wins, with more
    history breaking a tie and declaration order settling an exact one. Filers
    migrate between synonyms and abandon the old tag with a few stale years
    still on it, so taking the first tag carrying *any* data is how a 2017 value
    ends up presented as this year's.

    Restatements are resolved to the newest filing that stated the period, which
    is what makes the two ends of a comparison like-for-like: a prior year
    restated by the current filing is the version the current filing is
    comparable to. It also means ``filed`` on the prior row can equal ``filed``
    on the current one, which is correct — both numbers became public together.

    Returns an empty frame rather than raising: a filer that does not tag a
    concept is the ordinary case, and a detector wants to skip it, not fail.
    """
    wanted = tuple(forms or (ANNUAL_FORMS if period == "annual" else PERIODIC_FORMS))
    pool = facts[(facts["taxonomy"] == taxonomy) & (facts["form"].isin(wanted))]

    best = pd.DataFrame()
    best_rank: Optional[Tuple[Any, int]] = None
    for tag in tags:
        rows = _spans(pool[pool["concept"] == tag], period)
        if rows.empty:
            continue
        for unit, part in rows.groupby("unit", sort=False):
            part = (part.sort_values("filed")
                        .drop_duplicates("end", keep="last")
                        .sort_values("end"))
            rank = (part["end"].max(), len(part))
            if best_rank is None or rank > best_rank:
                best, best_rank = part.assign(unit=unit), rank
    if best.empty:
        return best
    keep = ["end", "start", "val", "filed", "accn", "form", "fy", "fp", "concept", "unit"]
    return best[keep].reset_index(drop=True)


def year_over_year(series: pd.DataFrame) -> Optional[Tuple[pd.Series, pd.Series]]:
    """``(newest period, the period about a year before it)``, or ``None``.

    Pairing on the gap rather than on position is what keeps a filer that
    skipped a year, changed its fiscal year-end, or has a stub period in the
    series from being compared against something two years old without saying so.
    """
    if series is None or len(series) < 2:
        return None
    current = series.iloc[-1]
    gap = (current["end"] - series["end"]).dt.days
    prior = series[gap.between(*YEAR_GAP)]
    if prior.empty:
        return None
    return current, prior.iloc[-1]


def latest_filings(facts: pd.DataFrame, forms: Optional[Sequence[str]] = None,
                   limit: int = 2) -> List[Dict[str, Any]]:
    """The newest periodic filings that carried facts, newest first.

    Grouped by accession number because that is what a filing *is*; the period
    end is the newest one the filing tagged, since an annual report also
    restates the two years before it.
    """
    wanted = tuple(forms or PERIODIC_FORMS)
    rows = facts[facts["form"].isin(wanted)]
    if rows.empty:
        return []
    grouped = rows.groupby("accn", as_index=False).agg(
        filed=("filed", "max"), form=("form", "first"),
        period_end=("end", "max"), fy=("fy", "first"), fp=("fp", "first"),
    )
    grouped = grouped.sort_values(["filed", "period_end"], ascending=False)
    return [
        {
            "accession_number": str(r.accn),
            "filed": str(pd.Timestamp(r.filed).date()),
            "form": str(r.form),
            "period_end": str(pd.Timestamp(r.period_end).date()),
            "fiscal_year": None if pd.isna(r.fy) else int(r.fy),
            "fiscal_period": None if r.fp is None or pd.isna(r.fp) else str(r.fp),
        }
        for r in grouped.head(limit).itertuples()
    ]


def first_appearances(facts: pd.DataFrame,
                      taxonomies: Iterable[str] = ("us-gaap",)) -> pd.DataFrame:
    """Per concept, the filing that reported it for the first time.

    Ordered on ``filed`` and not on the period end: a filer tagging a concept
    for the first time in an annual report frequently backfills the two
    comparative years with it, so the earliest *period* carrying a concept is
    routinely years before anyone could see it.
    """
    pool = facts[facts["taxonomy"].isin(tuple(taxonomies))]
    if pool.empty:
        return pool
    ordered = pool.sort_values(["filed", "end"])
    first = ordered.groupby(["taxonomy", "concept"], as_index=False).agg(
        label=("label", "first"), first_filed=("filed", "first"),
        first_accn=("accn", "first"), first_form=("form", "first"),
        first_end=("end", "first"), unit=("unit", "first"),
        first_val=("val", "first"), observations=("val", "size"),
    )
    return first.sort_values("first_filed").reset_index(drop=True)


def silenced_in(facts: pd.DataFrame, accession: str,
                taxonomies: Iterable[str] = ("us-gaap",)) -> set:
    """Concepts the previous filing *of the same form* tagged and this one did not.

    The counterweight to a first appearance. A concept arriving while another
    goes quiet in the same filing is a tag migration — the same fact renamed —
    far more often than it is a new economic event, and this is what lets a
    detector say so instead of reporting both halves as news.

    Same form, deliberately. A 10-Q tags a fraction of what a 10-K does, so
    measured against the annual report every quarterly filing "silences" a
    hundred concepts and every quarter reads as a migration. Against the
    previous 10-Q it silences a handful, which is the real number.
    """
    pool = facts[facts["taxonomy"].isin(tuple(taxonomies))]
    if pool.empty:
        return set()
    this = pool[pool["accn"] == accession]
    if this.empty:
        return set()
    here = set(this["concept"])
    filed = this["filed"].max()
    form = str(this["form"].iloc[0])
    earlier = pool[(pool["filed"] < filed) & (pool["form"] == form)]
    if earlier.empty:
        return set()
    previous_accn = earlier.sort_values("filed")["accn"].iloc[-1]
    before = set(earlier.loc[earlier["accn"] == previous_accn, "concept"])
    return before - here


def value(row: Optional[pd.Series], field: str = "val") -> Optional[float]:
    """A float out of a fact row, or ``None`` — the guard every detector needs."""
    if row is None:
        return None
    raw = row.get(field)
    if raw is None or pd.isna(raw):
        return None
    return float(raw)


def growth(current: Optional[float], prior: Optional[float]) -> Optional[float]:
    """Fractional change, or ``None`` where the base cannot carry one.

    A zero or negative base does not produce a percentage anybody can read: a
    deferred balance going from nothing to something is not "infinite growth",
    it is a fact that has to be stated in units. Detectors say so rather than
    printing a number the sign of which is meaningless.
    """
    if current is None or prior is None or prior <= 0:
        return None
    return current / prior - 1.0
