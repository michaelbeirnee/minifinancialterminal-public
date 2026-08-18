"""Shared-end-market read-through: peers' disclosures as evidence for a laggard.

A company that sells into China, or into data centres, or to carriers, shares
that end market with every other company that discloses the same line — and
US filers disclose them, by geography and by product, in the revenue note of
every 10-Q. When several members of such a cluster report the same inflection
in the shared line — China revenue growth going from +30% to −5% at three
equipment makers in the same fiscal quarter — the read-through to a fourth
member with the same exposure is not a guess. It is their disclosure, and the
question is only whether the fourth's consensus has moved yet.

That is what this module builds:

1. **A cluster.** The hub's peer group (:mod:`backend.providers.peers`) plus
   the hub itself, kept to the members that disclose revenue on the same
   end-market line — the same geography or the same product — read from each
   one's filings by :mod:`backend.providers.segments` and normalised so
   "Greater China", "China" and "PRC" are one line and "Data Center" and
   "Datacenter" are one line.
2. **A common inflection.** For each shared line, each member's change in
   year-over-year growth between its two most recent quarters. Members whose
   most recent quarter falls inside the same fiscal cohort and whose inflection
   points the same way, in enough numbers, are the *confirmers*.
3. **A laggard.** A member with real exposure to the line — a meaningful share
   of its revenue — whose consensus has not moved the way the confirmers' has:
   its next-year EPS estimate is flat or moving the other way over ninety days
   while theirs shifted with the inflection. Whether the laggard has itself
   reported the quarter yet is stated on the row: if it has and its own line
   agrees, its disclosure joins the evidence; if it has not, the peers' are the
   evidence and the laggard's print is the catalyst.

The row is anchored on the day the cluster's pattern completed — the filing
date of the last confirmer needed to make it one — because that is the first
day anyone could have made the read-through from public documents.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from ..core.errors import EmptyDataError
from ..providers import peers as peers_mod
from ..providers import segments as segments_mod
from ..providers import yahoo
from . import READ_THROUGH, row

# --------------------------------------------------------------------------- #
# Thresholds, with the reasoning they encode
# --------------------------------------------------------------------------- #
#: Change in year-over-year growth, in growth-rate points, before a member's
#: line is said to have inflected. Ten points is past what a quarter's
#: shipment timing does to a segment; below it two peers "agreeing" is noise
#: agreeing with noise.
MIN_INFLECTION = 0.10

#: Confirmers needed for a pattern. Two is a coincidence; three peers reading
#: the same end market the same way in the same fiscal quarter is a cohort.
MIN_AGREEING = 3

#: And they must be at least this share of the members that reported the
#: quarter — three of nine is not a common inflection.
MIN_AGREEING_SHARE = 0.5

#: A laggard has to be *in* the end market: the line must be this share of
#: its revenue. Under a tenth the read-through is real and immaterial.
MIN_EXPOSURE = 0.10

#: Members whose latest quarter ended within this many days of the newest
#: quarter in the cluster are the same fiscal cohort. Fiscal year-ends differ
#: by up to two months across a peer group; a quarter later is 91 days.
COHORT_DAYS = 75

#: The consensus is "unmoved" when next-year EPS drifted less than this over
#: ninety days — inside the noise a single desk makes.
FLAT_DRIFT = 0.02

#: And it "lags" the confirmers when its drift is under this fraction of the
#: confirmers' median drift in the inflection's direction.
LAG_FRACTION = 1.0 / 3.0

#: Peers considered around the hub. The peer group is ranked; past a dozen
#: the agreement drops to a single source and the segment reads get expensive.
PEER_LIMIT = 12

#: Quarters of segment history read per member: enough for two year-over-year
#: comparisons with a spare.
QUARTERS = 7

# --------------------------------------------------------------------------- #
# End-market lines, normalised so filers can agree
# --------------------------------------------------------------------------- #
_CANON: Tuple[Tuple[str, str], ...] = (
    ("china", r"^(greater\s+)?china\b|^prc$|people'?s\s+republic|^mainland\s+china"),
    ("taiwan", r"^taiwan\b"),
    ("korea", r"^(south\s+)?korea\b|republic\s+of\s+korea"),
    ("japan", r"^japan\b"),
    ("india", r"^india\b"),
    ("north_america", r"^(united\s+states|u\.?s\.?a?\.?|us|north\s+america|americas?|"
                      r"united\s+states\s+of\s+america)(\s+and\s+canada)?$"),
    ("europe", r"^(europe|emea|eu|europe,?\s+middle\s+east\s+and\s+africa|europe\s+and\s+israel|"
               r"europe\s+middle\s+east\s+africa|europe,?\s+the\s+middle\s+east\s+and\s+africa)\b"),
    ("asia_pacific", r"^(asia|asia[\s\-]*pacific|apac|rest\s+of\s+asia|other\s+asia|"
                     r"southeast\s+asia|south\s+east\s+asia|asia\s+pacific\s+and\s+japan)\b"),
    ("latin_america", r"^(latin\s+america|latam|south\s+america)\b"),
    ("international", r"^(international|foreign|outside\s+(the\s+)?u\.?s|non[\s\-]*u\.?s)\b"),
    ("data_center", r"data\s*cent(er|re)s?|hyperscale|cloud\s+(and\s+)?data"),
    ("gaming", r"^gaming\b|^games?\b"),
    ("automotive", r"^auto(motive)?\b|^vehicles?\b"),
    ("mobile", r"^(mobile|handsets?|smartphones?|wireless|phones?)\b"),
    ("pc", r"^(pcs?|client|personal\s+comput|computing|desktops?\s+and\s+notebooks?)\b"),
    ("industrial", r"^industrial\b"),
    ("consumer", r"^consumer\b"),
    ("enterprise", r"^enterprise\b"),
    ("networking", r"^network(ing)?\b"),
    ("storage", r"^storage\b"),
    ("cloud", r"^cloud\b"),
    ("advertising", r"^advertis"),
    ("subscription", r"^subscriptions?\b"),
    ("services", r"^services?\b"),
    ("hardware", r"^hardware\b|^products?\b|^equipment\b|^systems?\b"),
    ("licensing", r"^licens"),
)
_CANON_RE = [(key, re.compile(pat, re.I)) for key, pat in _CANON]

#: Lines that name a residual, not an end market.
_RESIDUAL = re.compile(r"^(other|others|all\s+other|rest\s+of\s+(the\s+)?world|row|corporate|"
                       r"eliminations?|unallocated|total)\b", re.I)


def canon(label: str) -> Optional[str]:
    """One key per end market however the filer wrote it, or ``None`` for a residual."""
    text = re.sub(r"[^\w\s,.&'\-]", " ", str(label or "")).strip()
    text = re.sub(r"\s+", " ", text)
    if not text or _RESIDUAL.match(text):
        return None
    for key, pattern in _CANON_RE:
        if pattern.search(text):
            return key
    # Anything else keeps its own normalised label — two filers writing the
    # same product line the same way still form a cluster on it.
    return re.sub(r"[\s\-]+", "_", text.lower())


# --------------------------------------------------------------------------- #
# Reading one member
# --------------------------------------------------------------------------- #
def member_lines(symbol: str) -> Dict[str, Any]:
    """Every end-market line ``symbol`` discloses, with its quarterly series.

    Returns ``{"symbol", "filed", "latest", "lines": {key: {...}}}`` where each
    line carries ``dimension``, the label as filed, ``series`` (period end ->
    revenue), ``share`` of the latest quarter's total revenue, and the growth
    figures :func:`inflection` derives. Empty ``lines`` when the filer does not
    disaggregate — a single-segment company is not a cluster member.
    """
    out: Dict[str, Any] = {"symbol": symbol, "filed": None, "latest": None, "lines": {}}
    try:
        rows, meta = segments_mod.revenue_segments(symbol, period="quarter", limit=QUARTERS)
    except Exception as exc:  # noqa: BLE001 - not disaggregating is a state, not a failure
        out["error"] = str(exc)
        return out
    periods = [pd.Timestamp(p) for p in meta.get("periods") or []]
    if not periods:
        return out
    filings = meta.get("filings") or []
    out["filed"] = filings[0]["filed"] if filings else None
    out["latest"] = periods[0].date().isoformat()

    totals: Dict[pd.Timestamp, float] = {}
    for r in rows:
        if r.get("dimension") == "total":
            for p in periods:
                v = r.get(p.date().isoformat())
                if v is not None:
                    totals[p] = float(v)
    for r in rows:
        if r.get("derived") or r.get("dimension") in ("total",):
            continue
        key = canon(r.get("segment"))
        if key is None:
            continue
        series = {p: float(r[p.date().isoformat()]) for p in periods
                  if r.get(p.date().isoformat()) is not None}
        if not series:
            continue
        latest_p = max(series)
        total = totals.get(latest_p)
        share = (series[latest_p] / total) if total else None
        line = {"dimension": r.get("dimension"), "label": r.get("segment"),
                "series": series, "share": None if share is None else round(share, 4)}
        line.update(inflection(series))
        # A filer can put "China" on both the geography and a segment axis;
        # keep the one with the longer history.
        held = out["lines"].get(key)
        if held is None or len(series) > len(held["series"]):
            out["lines"][key] = line
    return out


def _at(series: Dict[pd.Timestamp, float], when: pd.Timestamp,
        tolerance: int = 20) -> Optional[float]:
    for p, v in series.items():
        if abs((p - when).days) <= tolerance:
            return v
    return None


def inflection(series: Dict[pd.Timestamp, float]) -> Dict[str, Any]:
    """Year-over-year growth for the two most recent quarters, and the change.

    Matched on dates rather than positions because a fiscal calendar wobbles
    and a member's series can have a hole where a 10-Q was not disaggregated:
    the comparison quarter is the one about a year before, not the fifth back.
    """
    if len(series) < 2:
        return {"growth": None, "prior_growth": None, "inflection": None, "quarter": None}
    stamps = sorted(series, reverse=True)
    latest = stamps[0]
    prior = next((s for s in stamps[1:] if 60 <= (latest - s).days <= 120), None)
    year_ago = _at(series, latest - pd.Timedelta(365, unit="D"))
    growth = None
    if year_ago:
        growth = series[latest] / year_ago - 1.0
    prior_growth = None
    if prior is not None:
        prior_year_ago = _at(series, prior - pd.Timedelta(365, unit="D"))
        if prior_year_ago:
            prior_growth = series[prior] / prior_year_ago - 1.0
    delta = None if growth is None or prior_growth is None else growth - prior_growth
    return {
        "growth": None if growth is None else round(growth, 4),
        "prior_growth": None if prior_growth is None else round(prior_growth, 4),
        "inflection": None if delta is None else round(delta, 4),
        "quarter": latest.date().isoformat(),
    }


def consensus_move(symbol: str) -> Dict[str, Any]:
    """How the next-fiscal-year EPS consensus has moved: 90-day drift, 30-day net revisions."""
    out: Dict[str, Any] = {"drift_90d": None, "net_revisions_30d": None, "eps_fy1": None}
    try:
        trend = yahoo.estimates(symbol, "eps_trend")
        row = trend.loc["0y"] if "0y" in trend.index else trend.iloc[0]
        cur, ago = float(row.get("current")), float(row.get("90daysAgo"))
        out["eps_fy1"] = round(cur, 4)
        if ago and ago == ago and cur == cur:
            out["drift_90d"] = round(cur / ago - 1.0, 4) if ago > 0 else round((cur - ago) / abs(ago), 4)
    except Exception:  # noqa: BLE001 - no coverage is a state
        pass
    try:
        rev = yahoo.estimates(symbol, "eps_revisions")
        row = rev.loc["0y"] if "0y" in rev.index else rev.iloc[0]
        out["net_revisions_30d"] = int(row.get("upLast30days") or 0) - int(row.get("downLast30days") or 0)
    except Exception:  # noqa: BLE001
        pass
    return out


# --------------------------------------------------------------------------- #
# The cluster
# --------------------------------------------------------------------------- #
def cluster_symbols(hub: str, limit: int = PEER_LIMIT) -> Tuple[List[str], List[Dict[str, Any]]]:
    """The hub and its ranked peers, with the peer evidence carried along."""
    try:
        rows, _meta = peers_mod.peer_group(hub, limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise EmptyDataError("No peer group for {}: {}".format(hub, exc)) from exc
    symbols = [hub] + [str(r["symbol"]).upper() for r in rows if r.get("symbol")]
    seen: List[str] = []
    for s in symbols:
        if s not in seen:
            seen.append(s)
    return seen[: limit + 1], rows


def read_members(symbols: Sequence[str], workers: int = 4) -> List[Dict[str, Any]]:
    """Segment lines and consensus for every member, fetched concurrently."""
    def one(sym: str) -> Dict[str, Any]:
        member = member_lines(sym)
        member["consensus"] = consensus_move(sym)
        return member

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, symbols))


def find(members: List[Dict[str, Any]], *, min_inflection: float = MIN_INFLECTION,
         min_agreeing: int = MIN_AGREEING, min_share: float = MIN_AGREEING_SHARE,
         min_exposure: float = MIN_EXPOSURE, cohort_days: int = COHORT_DAYS,
         hub: Optional[str] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Read-through rows from members already read. Pure; tests feed it directly.

    Returns ``(flags, clusters)`` — the flag rows, and one record per shared
    line describing the cluster whether or not it produced a laggard, so a
    caller can show why a line did not fire.
    """
    with_lines = [m for m in members if m.get("lines")]
    if len(with_lines) < 2:
        return [], []
    keys: Dict[str, List[Dict[str, Any]]] = {}
    for m in with_lines:
        for key in m["lines"]:
            keys.setdefault(key, []).append(m)

    flags: List[Dict[str, Any]] = []
    clusters: List[Dict[str, Any]] = []
    for key, holders in keys.items():
        if len(holders) < 3:
            continue
        # The fiscal cohort: whose latest quarter is current.
        latest = max(pd.Timestamp(m["lines"][key]["quarter"]) for m in holders
                     if m["lines"][key].get("quarter"))
        fresh, stale = [], []
        for m in holders:
            q = m["lines"][key].get("quarter")
            (fresh if q and (latest - pd.Timestamp(q)).days <= cohort_days else stale).append(m)
        measured = [m for m in fresh if m["lines"][key].get("inflection") is not None]
        up = [m for m in measured if m["lines"][key]["inflection"] >= min_inflection]
        down = [m for m in measured if m["lines"][key]["inflection"] <= -min_inflection]
        side, agreeing = ("up", up) if len(up) >= len(down) else ("down", down)
        record = {
            "line": key, "members": [m["symbol"] for m in holders],
            "reported_this_quarter": [m["symbol"] for m in fresh],
            "measured": len(measured), "accelerating": [m["symbol"] for m in up],
            "decelerating": [m["symbol"] for m in down],
            "cohort_quarter_end": latest.date().isoformat(),
        }
        clusters.append(record)
        if len(agreeing) < min_agreeing or (measured and len(agreeing) / len(measured) < min_share):
            record["verdict"] = "no common inflection"
            continue
        sign = 1 if side == "up" else -1
        # When the pattern completed: the filing date of the last confirmer
        # needed to reach min_agreeing, in filing order.
        filed = sorted(m["filed"] for m in agreeing if m.get("filed"))
        known_on = filed[min_agreeing - 1] if len(filed) >= min_agreeing else (filed[-1] if filed else None)
        if not known_on:
            record["verdict"] = "confirmers carry no filing date"
            continue
        drifts = [m["consensus"]["drift_90d"] for m in agreeing
                  if m.get("consensus", {}).get("drift_90d") is not None]
        peer_drift = float(pd.Series(drifts).median()) if drifts else None
        mean_inflection = float(pd.Series([m["lines"][key]["inflection"] for m in agreeing]).mean())
        record.update({"verdict": "common inflection", "direction": side,
                       "confirmers": [m["symbol"] for m in agreeing], "known_on": known_on,
                       "peer_consensus_drift_90d": None if peer_drift is None else round(peer_drift, 4),
                       "mean_inflection": round(mean_inflection, 4)})

        # Laggards: exposed, and unmoved relative to the confirmers.
        for m in holders:
            if m in agreeing:
                continue
            line = m["lines"][key]
            share = line.get("share")
            if share is None or share < min_exposure:
                continue
            drift = m.get("consensus", {}).get("drift_90d")
            if drift is None:
                continue                                    # no coverage: cannot lag
            own = line.get("inflection")
            in_cohort = m in fresh
            if in_cohort and own is not None and own * sign < 0 and abs(own) >= min_inflection:
                continue                                    # reported and diverged: an exception, not a laggard
            moved_with = drift * sign
            peer_moved = (peer_drift or 0.0) * sign
            lagging = (abs(drift) < FLAT_DRIFT) or (moved_with < 0) or \
                      (peer_moved > 0 and moved_with < LAG_FRACTION * peer_moved)
            if not lagging:
                continue
            status = ("reported, own line agrees" if in_cohort and own is not None and own * sign > 0
                      else "reported, own line flat" if in_cohort
                      else "not yet reported")
            evidence = ", ".join("{} {:+.0f}pp".format(c["symbol"], 100 * c["lines"][key]["inflection"])
                                 for c in agreeing)
            summary = (
                "{}: {} of {} reporters show {} growth {} ({}); {} has {:.0f}% of revenue there, "
                "{}, and its FY1 EPS consensus is {:+.1f}% over 90 days against the confirmers' "
                "median {}".format(
                    key.replace("_", " "), len(agreeing), len(measured), key.replace("_", " "),
                    "accelerating" if side == "up" else "decelerating", evidence,
                    m["symbol"], 100 * share, status, 100 * drift,
                    "n/a" if peer_drift is None else "{:+.1f}%".format(100 * peer_drift)))
            score = (min(abs(mean_inflection), 0.5) / 0.5) * min(share, 0.5) / 0.5 * \
                    (len(agreeing) / max(len(measured), 1))
            flags.append(row(
                READ_THROUGH, m["symbol"], known_on, summary, score=score,
                line=key, line_label=line.get("label"), dimension=line.get("dimension"),
                direction=side, hub=hub,
                exposure=round(share, 4), own_inflection=own, own_growth=line.get("growth"),
                own_quarter=line.get("quarter"), own_status=status,
                confirmers=[{"symbol": c["symbol"], "inflection": c["lines"][key]["inflection"],
                             "growth": c["lines"][key]["growth"], "quarter": c["lines"][key]["quarter"],
                             "filed": c.get("filed"), "consensus_drift_90d": c["consensus"].get("drift_90d")}
                            for c in agreeing],
                reporters=len(measured), cluster=[h["symbol"] for h in holders],
                mean_peer_inflection=round(mean_inflection, 4),
                peer_consensus_drift_90d=None if peer_drift is None else round(peer_drift, 4),
                consensus_drift_90d=round(drift, 4),
                net_revisions_30d=m["consensus"].get("net_revisions_30d"),
                eps_fy1=m["consensus"].get("eps_fy1"),
            ))
    flags.sort(key=lambda f: -f["score"])
    return flags, clusters


def read_through(hub: str, peer_limit: int = PEER_LIMIT, extra: Sequence[str] = (),
                 **gates: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """The whole read: cluster the hub, read every member, find the laggards."""
    symbols, peer_rows = cluster_symbols(hub.upper(), peer_limit)
    for s in extra:
        s = str(s).upper()
        if s and s not in symbols:
            symbols.append(s)
    members = read_members(symbols)
    flags, clusters = find(members, hub=hub.upper(), **gates)
    meta = {
        "hub": hub.upper(),
        "cluster": symbols,
        "peers": [{"symbol": r["symbol"], "sources": r.get("sources"), "score": r.get("score")}
                  for r in peer_rows[:peer_limit]],
        "members_disaggregating": [m["symbol"] for m in members if m.get("lines")],
        "members_without_segments": [m["symbol"] for m in members if not m.get("lines")],
        "lines": clusters,
    }
    return flags, meta
