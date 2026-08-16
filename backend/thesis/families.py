"""Classify insider buyers into signal families — the checkbox is not enough.

The motivating failure: IAC Inc. files Form 4s at MGM ticking only
``isTenPercentOwner``, which reads as a passive whale. But IAC's chief
executive sits on MGM's board and files his own Form 4s there as a Director —
so IAC buys with board representation and full information rights, which is a
different signal family from an index fund crossing a threshold. Nothing on
IAC's *own* filing says so; the fact lives in the relationship graph across
filings.

That graph is already in hand. Every Form 4 names (person, company, role), so
the bulk archive doubles as a market-wide relationship table: entity E buying
issuer X is *board-backed* when someone who files at E as an officer/director
also files at X as a director. A pure join — no new fetches, no NLP on 13D
legal text.

Known limit: the table only sees people who have FILED. A brand-new designee
who has not yet filed any Form 4 at the issuer is invisible until their first
filing; callers can close most of that gap by passing the issuer's fresh rows
(``extra_relations``), which is where a recent directorship shows up first.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import pandas as pd

from ..core.caching import TTL_REFERENCE, cached
from ..core.errors import EmptyDataError
from . import bulk

#: Distinct relationship columns kept from the filing stream.
_REL_COLS = ["owner_cik", "owner_name", "issuer_cik", "is_officer", "is_director"]


def _norm(cik: object) -> str:
    return str(cik).lstrip("0") or "0"


# v2: built from SUBMISSION x REPORTINGOWNER (every filer) rather than the
# transaction join — a director whose filings are all derivative-table RSU
# grants never appears in the non-derivative join, and Joseph Levin's MGM
# directorship is exactly such a case.
@cached("thesis.families.relations.v2", ttl=TTL_REFERENCE)
def relations(max_quarters: int = 12) -> pd.DataFrame:
    """Distinct (person, company, role) tuples from the newest bulk quarters.

    Cached as one object so the join is cheap after the first build. Quarters
    that are not published yet are skipped — the newest one or two never are.
    """
    frames: List[pd.DataFrame] = []
    for year, quarter_no in reversed(bulk.available_quarters(start_year=2020)):
        if len(frames) >= max_quarters:
            break
        try:
            frame = bulk.owner_relations(year, quarter_no)
        except EmptyDataError:
            continue
        frames.append(frame)
    if not frames:
        raise EmptyDataError("No published Form 345 quarters to build relations from")

    rel = pd.concat(frames, ignore_index=True)
    rel["owner_cik"] = rel.owner_cik.map(_norm)
    rel["issuer_cik"] = rel.issuer_cik.map(_norm)
    # A person can appear with different flags over time (officer who joins
    # the board); keep the union of roles per (person, company).
    rel = rel.groupby(["owner_cik", "issuer_cik"], as_index=False).agg(
        owner_name=("owner_name", "first"),
        is_officer=("is_officer", "max"),
        is_director=("is_director", "max"),
    )
    return rel


def board_link(
    entity_cik: str,
    issuer_cik: str,
    extra_relations: Optional[pd.DataFrame] = None,
    max_quarters: int = 12,
) -> Tuple[bool, List[str]]:
    """Is ``entity`` represented on ``issuer``'s board? Returns (yes, names).

    ``extra_relations`` accepts additional filing rows in the bulk/fresh row
    schema — pass the issuer's fresh Form 4 rows so a directorship filed after
    the newest bulk quarter still counts.
    """
    rel = relations(max_quarters=max_quarters)
    entity, issuer = _norm(entity_cik), _norm(issuer_cik)

    entity_people = rel[
        (rel.issuer_cik == entity) & (rel.is_officer | rel.is_director)
    ]
    issuer_directors = rel[(rel.issuer_cik == issuer) & rel.is_director]

    director_ciks = set(issuer_directors.owner_cik)
    if extra_relations is not None and len(extra_relations):
        extra = extra_relations
        director_ciks |= {
            _norm(c)
            for c, d, i in zip(extra.owner_cik, extra.is_director, extra.issuer_cik)
            if d and _norm(i) == issuer
        }

    linked = entity_people[entity_people.owner_cik.isin(director_ciks)]
    names = sorted(linked.owner_name.unique())
    return bool(names), names
