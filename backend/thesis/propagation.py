"""Propagation along disclosed links: a shock at a hub, read at its counterparties.

The supply-chain miner in :mod:`backend.providers.supplychain` recovers
sentences like "sales to Company A accounted for 27% of our net sales", with the
filing behind each one. That edge is not a correlation anybody fitted. It is a
mechanism, written down by the filer under an accounting rule, with a number on
it — so it can be walked.

This module walks it. When something material moves at a hub — the consensus for
its next fiscal year moves, a reportable segment shrinks two quarters running,
the price gaps — every filer that disclosed a quantified dependency on that hub
becomes a candidate, carrying the disclosed percentage as the transmission
channel and its own estimates as the test of whether anyone has done the join:

    B derives 27% of its net sales from A (10-K, 2026-02-19).
    A's Data Center segment fell 6% year on year after falling 2%, and A's
    next-year consensus is down 8% over ninety days.
    B's consensus has not moved, across five covering analysts.

Every clause there is checkable and the last one is the falsifier, stated before
any model sees the row. Which is also why rows whose estimates *have* moved are
emitted rather than filtered out: a scanner that shows only the unpriced half of
its own output can never be measured against the other half.

Four limits, all structural, all restated in the source's artifact rule.

* **Only the counterparty's own disclosure sizes the link.** A hub naming a
  counterparty says what the counterparty is worth to the *hub* — "12% of our
  purchases" tells you nothing about how much of the supplier's revenue the hub
  is. Magnitude only travels in the direction the filer wrote it, so this walks
  counterparty-disclosed edges alone and ignores the hub's own naming.
* **The percentage is of a whole company; the shock is of one segment.** A hub
  segment shrinking is not the same as the demand this particular supplier
  serves shrinking, and no filing makes that join. It is an inference, and it is
  the reader's.
* **The edge is exactly as old as its filing.** A concentration disclosed a year
  ago may already have ended — which would itself be the news, and is not
  something a stale filing can show.
* **Coverage stops where SEC filing does.** A private contract manufacturer, or
  a supplier that never crossed its disclosure threshold, has no edge here. The
  absence of a link is not evidence of independence.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from ..providers import segments, supplychain, yahoo

# --------------------------------------------------------------------------- #
# Consensus movement
# --------------------------------------------------------------------------- #
#: The next full fiscal year — the row the sell side actually moves. The quarter
#: rows mostly re-state the last print; the out-year is where a changed view of
#: the business shows up. (``thesis_candidates.estimate_revisions`` reads the
#: same horizon for its own gate; the two are deliberately independent, because
#: a funnel's measure is part of the funnel.)
_HORIZON = "+1y"

#: A consensus move of this size is the top of the shock scale. Ten percent off
#: next year's earnings is a different company, not a rounding of estimates.
_FULL_DRIFT = 0.10

#: Likewise for price: a 30% three-month move is a full-scale shock.
_FULL_MOVE = 0.30


def _number(value: Any) -> Optional[float]:
    """A provider float, with ``NaN`` treated as the absence it is."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _count(value: Any) -> Optional[int]:
    number = _number(value)
    return None if number is None else int(number)


def _estimate_row(symbol: str, kind: str) -> Mapping[str, Any]:
    """One horizon of one estimates table, or nothing. Never raises.

    An uncovered company is the normal case on this side of the graph — the
    suppliers a concentration disclosure names are frequently small — so a
    missing table is an answer, not a failure.
    """
    try:
        frame = yahoo.estimates(symbol, kind)
    except Exception:  # noqa: BLE001 - no coverage is a fact about the company
        return {}
    if _HORIZON not in getattr(frame, "index", ()):
        return {}
    return dict(frame.loc[_HORIZON])


def consensus_move(symbol: str, with_coverage: bool = True) -> Dict[str, Any]:
    """Where next year's EPS consensus has gone, and how many desks moved it.

    ``with_coverage`` adds the analyst count, which costs a third request and is
    only needed where the answer decides something: a company nobody covers has
    no consensus to move, and "estimates have not moved" means something
    entirely different there.
    """
    counts = _estimate_row(symbol, "eps_revisions")
    trend = _estimate_row(symbol, "eps_trend")
    current = _number(trend.get("current"))

    def drift(ago: str) -> Optional[float]:
        was = _number(trend.get(ago))
        return current / was - 1.0 if current is not None and was else None

    # Yahoo's own casing, which is not consistent across these columns.
    up_30d = _count(counts.get("upLast30days"))
    down_30d = _count(counts.get("downLast30days"))
    out: Dict[str, Any] = {
        "consensus_eps_fy1": current,
        "eps_drift_30d": drift("30daysAgo"),
        "eps_drift_90d": drift("90daysAgo"),
        "up_30d": up_30d,
        "down_30d": down_30d,
        "net_revisions": (up_30d - down_30d)
        if up_30d is not None and down_30d is not None else None,
        "analyst_count": None,
    }
    if with_coverage:
        out["analyst_count"] = _count(
            _estimate_row(symbol, "earnings").get("numberOfAnalysts"))
    return out


# --------------------------------------------------------------------------- #
# Segment trend
# --------------------------------------------------------------------------- #
#: A segment below this share of revenue is not a hub-sized event, whatever it
#: did. Without the floor the "shock" is routinely a 2% line item.
_MIN_SEGMENT_SHARE = 0.10

#: How much year-over-year growth has to change before the change is a fact
#: about demand rather than about the comparison base — five percentage points.
_MATERIAL_SWING = 0.05

#: A twenty-point swing in year-over-year growth is the top of the scale.
_FULL_SWING = 0.20

#: Fiscal quarters do not land 365 days apart. This is how far the year-ago
#: comparison period may sit from that mark and still be the same quarter.
_YOY_WINDOW_DAYS = 45

#: Which breakdown to read first. Reportable segments are the ASC 280 split
#: management runs the company on; product lines are the next best statement of
#: what is actually being bought; geography is the weakest proxy for demand but
#: is sometimes the only axis a filer tags. One axis is chosen and used alone —
#: mixing them would describe the same revenue twice.
_SEGMENT_PREFERENCE: Tuple[str, ...] = ("business", "product", "geographic")

#: Worst first. A segment already shrinking outranks one that is merely growing
#: more slowly, at any share.
_TREND_RANK: Dict[str, int] = {"contracting": 0, "decelerating": 1, "accelerating": 2}


def _period_key(stamp: Any) -> str:
    return str(pd.Timestamp(stamp).date())


def _year_ago(stamp: pd.Timestamp, periods: Sequence[pd.Timestamp]) -> Optional[pd.Timestamp]:
    """The period in ``periods`` that is the same quarter a year earlier.

    The distance between the two stamps is measured rather than a target date
    constructed, so no calendar arithmetic is done on a date at all — fiscal
    periods land where the filer's year does, and 52-week retail years drift
    against the calendar by design.
    """
    best: Optional[Tuple[int, pd.Timestamp]] = None
    for other in periods:
        gap = abs((stamp - other).days - 365)
        if gap <= _YOY_WINDOW_DAYS and (best is None or gap < best[0]):
            best = (gap, other)
    return None if best is None else best[1]


def _two_quarter_trend(row: Mapping[str, Any],
                       periods: Sequence[pd.Timestamp]) -> Optional[Dict[str, Any]]:
    """One segment's last two year-over-year growth rates, classified.

    Both comparisons have to be computable from the same table, against a
    positive base — a segment that was loss-making, newly created or restated
    into existence produces a growth rate that is arithmetic rather than news.
    """
    readings: List[Tuple[pd.Timestamp, float]] = []
    for stamp in periods[:2]:
        base = _year_ago(stamp, periods)
        value = _number(row.get(_period_key(stamp)))
        before = _number(row.get(_period_key(base))) if base is not None else None
        if value is None or before is None or before <= 0:
            return None
        readings.append((stamp, value / before - 1.0))
    (latest_end, latest), (_, prior) = readings

    if latest < 0 and prior < 0:
        trend, magnitude = "contracting", abs(latest) / _FULL_SWING
    elif latest <= prior - _MATERIAL_SWING:
        trend, magnitude = "decelerating", (prior - latest) / _FULL_SWING
    elif latest >= prior + _MATERIAL_SWING and latest > 0:
        trend, magnitude = "accelerating", (latest - prior) / _FULL_SWING
    else:
        return None
    return {
        "trend": trend,
        "direction": "up" if trend == "accelerating" else "down",
        "magnitude": round(min(magnitude, 1.0), 4),
        "yoy_latest": round(latest, 4),
        "yoy_prior": round(prior, 4),
        "period": _period_key(latest_end),
    }


def segment_trend(symbol: str, quarters: int = 8,
                  min_share: float = _MIN_SEGMENT_SHARE) -> Optional[Dict[str, Any]]:
    """The most consequential thing one company's segment revenue has done.

    Reads the filer's own quarterly disaggregation and returns the largest
    material segment whose year-over-year growth has moved one way for two
    quarters running — shrinking, decelerating or accelerating — or ``None``
    when nothing has. Expensive on a cold cache: the first call downloads the
    filings the tagging lives in.
    """
    rows, meta = segments.revenue_segments(symbol, period="quarter", limit=quarters)
    periods = sorted({pd.Timestamp(p) for p in meta.get("periods") or []}, reverse=True)
    # Two year-over-year comparisons need the two newest quarters and their
    # counterparts a year back: six periods at the very least.
    if len(periods) < 6:
        return None

    for dimension in _SEGMENT_PREFERENCE:
        best: Optional[Tuple[Tuple[int, float], Dict[str, Any]]] = None
        for row in rows:
            if row.get("dimension") != dimension or row.get("derived"):
                continue
            share = _number(row.get("revenue_share"))
            if share is None or share < min_share:
                continue
            reading = _two_quarter_trend(row, periods)
            if reading is None:
                continue
            rank = (_TREND_RANK[reading["trend"]], -share)
            if best is None or rank < best[0]:
                best = (rank, {
                    "segment": row.get("segment"),
                    "section": row.get("section"),
                    "dimension": dimension,
                    "share": round(share, 4),
                    **reading,
                })
        if best is not None:
            return best[1]
    return None


# --------------------------------------------------------------------------- #
# The shock at the hub
# --------------------------------------------------------------------------- #
def hub_shock(symbol: str, *, moves: Optional[Mapping[str, Any]] = None,
              drift_floor: float = 0.03, price_floor: float = 0.15,
              read_segments: bool = True, quarters: int = 8) -> Dict[str, Any]:
    """Has something material moved at this company, and which way.

    Three channels, in descending order of how much they are worth and
    ascending order of what they cost:

    ``consensus``
        The next fiscal year's EPS consensus has moved. This is the cheap
        market-wide detector and the timely one — a guidance change shows up
        here within days — but it measures the sell side's view rather than the
        business, and the consensus follows the price at least as often as it
        leads it.
    ``segment``
        A reportable segment shrinking or decelerating two quarters running,
        read out of the filer's own XBRL. This is the channel worth having: it
        is the demand itself, disaggregated by the company, with no forecast in
        it. It is also the expensive one, which is why it runs for a handful of
        selected hubs rather than across a universe.
    ``price``
        A large three-month move. Weakest of the three and included because it
        is free — the caller has already fetched the price table.

    ``magnitude`` is the strongest channel's, on a 0-1 scale, and ``direction``
    is that channel's. ``conflicting`` says the channels disagree, which is a
    finding rather than a defect: a segment shrinking while the consensus rises
    is exactly the configuration where somebody has not done the join yet.
    """
    consensus = consensus_move(symbol, with_coverage=False)
    fired: List[Tuple[str, str, float]] = []

    drift = consensus.get("eps_drift_90d")
    if drift is not None and abs(drift) >= drift_floor:
        fired.append(("consensus", "up" if drift > 0 else "down",
                      min(abs(drift) / _FULL_DRIFT, 1.0)))

    segment: Optional[Dict[str, Any]] = None
    segment_error: Optional[str] = None
    if read_segments:
        try:
            segment = segment_trend(symbol, quarters=quarters)
        except Exception as exc:  # noqa: BLE001 - a filer with no tagging still has a hub shock
            segment_error = str(exc)[:160]
    if segment is not None:
        fired.append(("segment", segment["direction"], segment["magnitude"]))

    move = _number((moves or {}).get("three_month"))
    if move is not None and abs(move) >= price_floor:
        fired.append(("price", "up" if move > 0 else "down",
                      min(abs(move) / _FULL_MOVE, 1.0)))

    fired.sort(key=lambda channel: -channel[2])
    return {
        "symbol": symbol,
        "channels": [name for name, _, _ in fired],
        "direction": fired[0][1] if fired else None,
        "magnitude": round(fired[0][2], 4) if fired else 0.0,
        "conflicting": len({side for _, side, _ in fired}) > 1,
        "consensus": consensus,
        "segment": segment,
        "segment_error": segment_error,
        "three_month": move,
    }


# --------------------------------------------------------------------------- #
# The edges
# --------------------------------------------------------------------------- #
#: What a disclosed percentage is a percentage *of*, and how much of a demand
#: channel that makes it. Revenue is the direct one: the counterparty's sales
#: stop when the hub stops buying. Purchases run the other way — the
#: counterparty depends on the hub for goods, which is a cost and availability
#: exposure rather than a demand one. Receivables are neither: they are a credit
#: exposure to the same name, and they only become a thesis if the hub is in
#: trouble as a payer rather than as a buyer.
BASIS_WEIGHT: Dict[str, float] = {
    "revenue": 1.0,
    "net sales": 1.0,
    "purchases": 0.7,
    "accounts receivable": 0.5,
}

#: A counterparty with half its revenue in one name is at the top of the scale.
#: Above that the disclosure is describing a subsidiary in all but name, and the
#: scanner cannot tell two such rows apart.
_FULL_EXPOSURE = 50.0


def exposure_term(exposure_pct: Optional[float], basis: Optional[str]) -> float:
    """The transmission channel's strength: how much of the counterparty this is."""
    pct = _number(exposure_pct)
    if pct is None:
        return 0.0
    weight = BASIS_WEIGHT.get(str(basis or "").lower(), 0.7)
    return min(pct / _FULL_EXPOSURE, 1.0) * weight


def disclosed_edges(hub: str, years: int = 4, limit: int = 25,
                    min_exposure_pct: float = 10.0) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Filers whose own annual report puts a number on their dependence on ``hub``.

    Returns ``(edges, dropped)``. Only counterparty-disclosed rows are used, and
    only quantified ones: the percentage has to be a share of the *counterparty's*
    books for it to size the transmission at all, and an edge with no number is a
    relationship rather than a channel.

    ``dropped`` counts what the gate removed and why, because a hub with forty
    named counterparties and one that clears the floor are different situations
    and the caller should be able to say which it got.
    """
    frame = supplychain.counterparties(hub, years=years, limit=limit)
    edges: List[Dict[str, Any]] = []
    dropped = {"below_floor": 0, "unlisted": 0}
    for row in frame.to_dict("records"):
        pct = _number(row.get("exposure_pct"))
        if pct is None or pct < min_exposure_pct:
            dropped["below_floor"] += 1
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol or symbol == hub.upper():
            # A filer with no ticker in EDGAR's display name is a real
            # relationship and not a candidate: there is nothing to hold.
            dropped["unlisted"] += 1
            continue
        # ``relationship`` is written from the hub's point of view by the miner:
        # a "supplier" sells *to* the hub, so its revenue is what the hub's
        # demand moves; a "customer" buys *from* the hub, so its costs and
        # supply are.
        sells_to_hub = str(row.get("relationship")) == "supplier"
        edges.append({
            "symbol": symbol,
            "issuer": row.get("company") or symbol,
            "hub": hub.upper(),
            "link": "demand" if sells_to_hub else "supply",
            "exposure_pct": round(float(pct), 2),
            "exposure_basis": row.get("exposure_basis"),
            "quote": row.get("quote"),
            "form": row.get("form"),
            "filing_date": str(row.get("filing_date") or "")[:10] or None,
            "filing_url": row.get("filing_url"),
            "disclosures": row.get("disclosures"),
        })
    return edges, dropped


# --------------------------------------------------------------------------- #
# The counterparty's own state
# --------------------------------------------------------------------------- #
#: Below this absolute 90-day drift, and below this many net revisions, nobody
#: has moved their number. Both are deliberately loose: the claim being made is
#: "the estimates have not moved", and a claim like that should fail easily.
_REFLECTED_DRIFT = 0.02
_REFLECTED_REVISIONS = 3


def reflection(consensus: Mapping[str, Any],
               drift_floor: float = _REFLECTED_DRIFT,
               revision_floor: int = _REFLECTED_REVISIONS) -> str:
    """Has anyone moved this company's numbers yet? ``reflected`` / ``unreflected`` / ``uncovered``.

    ``uncovered`` is not a weaker ``unreflected``, and collapsing the two would
    be the single most misleading thing this module could do. A company with no
    covering analyst has no estimates to fail to move, so the falsifier does not
    exist for it — the row is an exposure with no test attached, and it says so.
    """
    covered = consensus.get("analyst_count")
    drift = consensus.get("eps_drift_90d")
    if not covered or drift is None:
        return "uncovered"
    net = consensus.get("net_revisions")
    moved = abs(drift) >= drift_floor or (net is not None and abs(net) >= revision_floor)
    return "reflected" if moved else "unreflected"


#: How much of the score survives each state. An already-reflected row is kept
#: and heavily discounted rather than dropped: it is the control group the
#: unreflected rows are eventually measured against, and dropping it would leave
#: the graded log unable to tell the two apart.
LATENCY_WEIGHT: Dict[str, float] = {
    "unreflected": 1.0,
    "uncovered": 0.8,
    "reflected": 0.3,
}


def score_row(exposure: float, shock: float, state: str) -> float:
    """Transmission strength × how hard the hub was hit × how little has moved.

    The shock term never falls to zero, so a walk with the shock gate opened all
    the way still orders its rows by exposure rather than collapsing them into a
    tie — that configuration is an exposure map, and the score should say it is
    a weak one rather than say nothing.
    """
    return round(exposure * (0.25 + 0.75 * min(max(shock, 0.0), 1.0))
                 * LATENCY_WEIGHT.get(state, 0.8), 4)
