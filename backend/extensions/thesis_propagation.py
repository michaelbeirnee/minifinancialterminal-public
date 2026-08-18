"""The funnel that walks disclosed links: a hub is hit, its counterparties are ranked.

Every other funnel on the thesis menu selects on a property of one company —
what it is worth, what it earns, what its price did. This one selects on a
*relationship between two companies*, and the relationship is not inferred from
returns: it was written down by one of them in a filing, with a percentage
attached, because an accounting rule required it.

The scan is three steps.

1. **Find a hub that was hit.** Either the caller names one, or the funnel reads
   the next-fiscal-year consensus across the largest US listings and keeps the
   ones whose number has moved. That is the cheap, timely detector — a guidance
   change lands in the consensus within days.
2. **Confirm it in the accounts.** For the handful of hubs selected, read the
   filer's own quarterly segment revenue and look for a material segment that
   has shrunk or decelerated two quarters running. This is the channel worth
   having and the expensive one, which is why it runs on a shortlist rather than
   a universe. ``read_segments=false`` turns it off.
3. **Walk the edges.** Every filer whose own annual report puts a percentage on
   its dependence on that hub becomes a candidate, ranked by how big the
   disclosed dependence is, how hard the hub was hit, and whether anybody has
   moved that candidate's estimates yet.

The mechanics live in :mod:`backend.thesis.propagation`, which also documents
what this cannot see — most importantly that a hub naming a counterparty does
*not* size the link, so only counterparty-disclosed edges are walked.

The expensive part is EDGAR, not the model: one walk reads a full-text search
per disclosure phrase and then opens the filings that answered. It is cached
hard afterwards, and ``max_hubs`` is the knob that decides how much of it runs.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import norm_symbols
from ..providers import yahoo
from ..thesis import propagation, sources

_ATTENTION_ONLY = (
    "A disclosed link is a mechanism, not a forecast. The percentage is a share "
    "of the counterparty's revenue in the year of its filing, which may have "
    "changed or ended since; it is a share of the whole company, not of the "
    "hub segment that moved; and a hub's consensus moving is a change in the "
    "sell side's view rather than in orders. Read the quoted sentence and the "
    "filing behind it before treating any row as an exposure that still exists."
)

#: Ordinary US listings — the pool a hub is looked for in. A hub that files
#: nowhere, or trades nowhere, cannot be read from here at all.
_US_LISTED = ["is-in", "exchange", "NMS", "NYQ"]

#: How many counterparties per hub the miner is asked for, per side of the
#: relationship. The exposure floor then removes most of them.
_EDGES_PER_HUB = 25


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        return max(low, min(float(value), high))
    except (TypeError, ValueError):
        return default


def _as_pct(value: Any) -> str:
    return "{:+.1f}%".format(100 * value) if isinstance(value, (int, float)) else "unreadable"


def _money(value: Any) -> Optional[float]:
    """A provider figure as a float, or nothing. ``NaN`` counts as nothing."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # noqa: PLR0124 - NaN check


def _largest_listings(universe: int, min_market_cap_bn: float) -> List[Dict[str, str]]:
    """The biggest US listings, one row per issuer.

    A hub is large by construction — a company that is a quarter of somebody's
    revenue is rarely small — so size is the right prior for where to look. One
    row per issuer, because Yahoo screens listings rather than companies and
    both of Alphabet's share classes would otherwise be walked as two hubs.
    """
    frame = yahoo.equity_screen(
        [["gte", "intradaymarketcap", min_market_cap_bn * 1_000_000_000], _US_LISTED],
        limit=universe, sort_field="intradaymarketcap",
    )
    out: List[Dict[str, str]] = []
    seen: set = set()
    for raw in frame.to_dict("records"):
        symbol = str(raw.get("symbol") or "").strip().upper()
        issuer = str(raw.get("longName") or raw.get("shortName")
                     or raw.get("displayName") or symbol).strip()
        key = issuer.casefold() or symbol
        if not symbol or key in seen:
            continue
        seen.add(key)
        out.append({"symbol": symbol, "issuer": issuer})
    return out


def _moves(symbols: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Trailing return tables, batched. Absence is survivable everywhere it is used."""
    from .equity import price_performance

    if not symbols:
        return {}
    try:
        rows = price_performance(symbol=",".join(list(symbols)[:50])).data
    except Exception:  # noqa: BLE001 - price context, never the point of the scan
        return {}
    return {str(row.get("symbol")): row for row in rows or []}


def _profiles(symbols: Sequence[str]) -> Dict[str, Mapping[str, Any]]:
    """Size and sector for the counterparties that survived the exposure gate."""
    def one(symbol: str) -> Tuple[str, Mapping[str, Any]]:
        try:
            return symbol, yahoo.info(symbol)
        except Exception:  # noqa: BLE001 - one dead profile is not a dead scan
            return symbol, {}

    if not symbols:
        return {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(pool.map(one, sorted(set(symbols))))


def _record(rows: List[Dict[str, Any]], params: Dict[str, Any], known_on: str) -> None:
    """Log what this scan emitted, so the category can earn a measured base rate."""
    from ..thesis import memory

    structural = {"symbol", "issuer", "family", "score", "action"}
    memory.record_events(
        family=sources.LINK_PROPAGATION,
        rows=[{
            "symbol": row["symbol"],
            "known_on": known_on,
            "score": row.get("score"),
            "family": row.get("family"),
            "payload": {key: value for key, value in row.items()
                        if key not in structural and value is not None},
        } for row in rows],
        kind=sources.LINK_PROPAGATION,
        parameters=params,
    )


@command("/thesis/link_propagation", providers=("sec", "yahoo"),
         summary="Counterparties of a hub where something material just moved")
def link_propagation(hubs: Optional[str] = None,
                     hub_universe: int = 60,
                     max_hubs: int = 3,
                     min_hub_drift_pct: float = 3.0,
                     min_hub_move_pct: float = 15.0,
                     min_hub_market_cap_bn: float = 10.0,
                     min_exposure_pct: float = 10.0,
                     min_market_cap_bn: float = 0.3,
                     read_segments: bool = True,
                     years: int = 4,
                     limit: int = 20,
                     provider: Optional[str] = None) -> Result:
    """Emit candidates at the counterparties of hubs where something material moved.

    Pass ``hubs`` as a comma-separated list to walk named companies; leave it
    empty and the funnel finds its own, by reading the next-year consensus
    across the largest US listings and keeping the ones that moved most.

    ``min_hub_drift_pct`` is the gate a hub has to clear on that consensus.
    Setting it to zero walks any named hub whose consensus is readable at all,
    which turns the command into an exposure map with the shock term near zero —
    still ranked, visibly weak.

    Direction-neutral by construction: a hub accelerating propagates upside to
    the same counterparties a hub contracting propagates downside to, and the
    row says which via ``hub_direction``. What it does not say is whether the
    counterparty is a long or a short, because a supplier losing its largest
    customer and a supplier whose customer is booming are both stories about the
    same disclosed sentence.
    """
    src = resolve_provider(provider, ("sec", "yahoo"))
    hub_universe = int(_clamp(hub_universe, 10, 200, 60))
    max_hubs = int(_clamp(max_hubs, 1, 8, 3))
    drift_floor = _clamp(min_hub_drift_pct, 0.0, 50.0, 3.0) / 100.0
    price_floor = _clamp(min_hub_move_pct, 1.0, 100.0, 15.0) / 100.0
    min_exposure_pct = _clamp(min_exposure_pct, 1.0, 100.0, 10.0)
    min_market_cap_bn = _clamp(min_market_cap_bn, 0.0, 1000.0, 0.3)
    min_hub_market_cap_bn = _clamp(min_hub_market_cap_bn, 0.1, 5000.0, 10.0)
    years = int(_clamp(years, 1, 10, 4))
    limit = int(_clamp(limit, 1, 40, 20))
    read_segments = bool(read_segments)

    warnings: List[str] = [_ATTENTION_ONLY]

    # ----------------------------------------------------------------- hubs
    try:
        named = norm_symbols(hubs, limit=50) if str(hubs or "").strip() else []
    except ValueError:  # punctuation with no ticker in it means "find your own"
        named = []
    if named:
        selected = named[:max_hubs]
        if len(named) > max_hubs:
            # Never silently. A caller who named six hubs and got three walked
            # would read the result as "these are the only exposures there are".
            warnings.append(
                "{} hubs named, {} walked (max_hubs={}): {} not scanned".format(
                    len(named), len(selected), max_hubs,
                    ", ".join(named[max_hubs:])))
        issuers = {symbol: symbol for symbol in selected}
        scanned = len(named)
    else:
        pool = _largest_listings(hub_universe, min_hub_market_cap_bn)
        if not pool:
            raise EmptyDataError(
                "No US listing above ${:,.1f}B to look for a hub in".format(
                    min_hub_market_cap_bn))
        symbols = [row["symbol"] for row in pool]
        issuers = {row["symbol"]: row["issuer"] for row in pool}
        scanned = len(symbols)
        with ThreadPoolExecutor(max_workers=8) as executor:
            readings = list(executor.map(
                lambda s: propagation.consensus_move(s, with_coverage=False), symbols))
        moved: List[Tuple[float, str]] = []
        for symbol, consensus in zip(symbols, readings):
            drift = consensus.get("eps_drift_90d")
            if drift is not None and abs(drift) >= drift_floor:
                moved.append((abs(drift), symbol))
        if not moved:
            raise EmptyDataError(
                "No hub found: none of the {} largest US listings has moved its "
                "next-year consensus by {:.1f}% over ninety days".format(
                    scanned, drift_floor * 100))
        moved.sort(reverse=True)
        selected = [symbol for _, symbol in moved[:max_hubs]]

    hub_moves = _moves(selected)
    for hub, profile in _profiles(selected).items():
        name = profile.get("longName") or profile.get("shortName")
        if name:
            issuers[hub] = str(name)

    # -------------------------------------------------------------- the walk
    # Sequential on purpose. Each hub already fans out inside the miner and the
    # segment reader, both against EDGAR, which is rate-limited and asks to be
    # treated politely.
    edges: List[Dict[str, Any]] = []
    walked: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for hub in selected:
        shock = propagation.hub_shock(
            hub, moves=hub_moves.get(hub), drift_floor=drift_floor,
            price_floor=price_floor, read_segments=read_segments)
        if shock.get("segment_error"):
            warnings.append("{}: segment revenue unreadable ({})".format(
                hub, shock["segment_error"]))
        if not shock["channels"]:
            # What was looked at, not just that it came back empty: a hub with
            # no estimates at all and one whose estimates simply held still are
            # different answers, and only the second is about the company.
            segment = shock["segment"] or {}
            skipped.append({"hub": hub, "reason": (
                "nothing material moved: consensus {}, segment {}, 3m price {}".format(
                    _as_pct(shock["consensus"].get("eps_drift_90d")),
                    segment.get("trend") or ("unreadable" if shock["segment_error"]
                                             else "steady"),
                    _as_pct(shock["three_month"])))})
            continue
        try:
            found, dropped = propagation.disclosed_edges(
                hub, years=years, limit=_EDGES_PER_HUB,
                min_exposure_pct=min_exposure_pct)
        except Exception as exc:  # noqa: BLE001 - a hub nobody discloses is a clean answer
            skipped.append({"hub": hub, "reason": str(exc)[:200]})
            continue
        if not found:
            skipped.append({
                "hub": hub,
                "reason": "no filer discloses {:.0f}% or more of its own revenue "
                          "in {}".format(min_exposure_pct, hub),
                "dropped": dropped,
            })
            continue
        walked.append({
            "hub": hub, "issuer": issuers.get(hub, hub),
            "channels": shock["channels"], "direction": shock["direction"],
            "magnitude": shock["magnitude"], "conflicting": shock["conflicting"],
            "eps_drift_90d": shock["consensus"].get("eps_drift_90d"),
            "three_month": shock["three_month"],
            "segment": shock["segment"], "edges": len(found), "dropped": dropped,
        })
        for edge in found:
            edges.append({**edge, "shock": shock})

    if not edges:
        raise EmptyDataError(
            "Nothing propagated: {} hub(s) examined, none of them both moved and "
            "has a counterparty disclosing {:.0f}% or more of its own revenue in "
            "them.".format(len(selected), min_exposure_pct)
        )

    # One row per counterparty. A company exposed to two shocked hubs is a
    # stronger candidate, not two candidates — the strongest link keeps the slot
    # and the others are named on it.
    by_symbol: Dict[str, Dict[str, Any]] = {}
    for edge in edges:
        strength = (propagation.exposure_term(edge["exposure_pct"], edge["exposure_basis"])
                    * edge["shock"]["magnitude"])
        held = by_symbol.get(edge["symbol"])
        if held is None:
            by_symbol[edge["symbol"]] = {**edge, "_strength": strength, "_others": []}
        elif strength > held["_strength"]:
            by_symbol[edge["symbol"]] = {**edge, "_strength": strength,
                                         "_others": held["_others"] + [held["hub"]]}
        else:
            held["_others"].append(edge["hub"])

    # Grading a counterparty costs three estimate reads and a profile, so the
    # pool is bounded — and the bound is reported rather than applied silently.
    ranked = sorted(by_symbol.values(), key=lambda row: -row["_strength"])
    considered = ranked[:limit * 2]

    symbols = [row["symbol"] for row in considered]
    with ThreadPoolExecutor(max_workers=6) as executor:
        consensus_by_symbol = dict(zip(
            symbols, executor.map(propagation.consensus_move, symbols)))
    profiles = _profiles(symbols)
    counterparty_moves = _moves(symbols)

    rows: List[Dict[str, Any]] = []
    below_cap = 0
    for edge in considered:
        symbol = edge["symbol"]
        profile = profiles.get(symbol) or {}
        market_cap = _money(profile.get("marketCap"))
        if market_cap is not None and market_cap < min_market_cap_bn * 1_000_000_000:
            below_cap += 1
            continue

        consensus = consensus_by_symbol.get(symbol) or {}
        state = propagation.reflection(consensus)
        shock = edge["shock"]
        segment = shock.get("segment") or {}
        moves = counterparty_moves.get(symbol) or {}
        exposure = propagation.exposure_term(edge["exposure_pct"], edge["exposure_basis"])
        drift = consensus.get("eps_drift_90d")
        rows.append({
            "symbol": symbol,
            "issuer": edge["issuer"],
            "family": "{}_exposure".format(state),
            "hub": edge["hub"],
            "also_exposed_to": ", ".join(sorted(set(edge["_others"]))) or None,
            "link": edge["link"],
            "exposure_pct": edge["exposure_pct"],
            "exposure_basis": edge["exposure_basis"],
            "hub_direction": shock["direction"],
            "hub_channels": ", ".join(shock["channels"]),
            "hub_conflicting": shock["conflicting"],
            "hub_eps_drift_90d": shock["consensus"].get("eps_drift_90d"),
            "hub_three_month": shock["three_month"],
            "hub_segment": segment.get("segment"),
            "hub_segment_share": segment.get("share"),
            "hub_segment_trend": segment.get("trend"),
            "hub_segment_yoy": segment.get("yoy_latest"),
            "hub_segment_yoy_prior": segment.get("yoy_prior"),
            "eps_drift_90d": drift,
            "net_revisions": consensus.get("net_revisions"),
            "analyst_count": consensus.get("analyst_count"),
            # Whether the counterparty's own consensus moved the way the hub's
            # did. A move the other way is not confirmation with a sign error:
            # it is two sets of desks disagreeing about the same link.
            "moved_with_hub": (None if drift is None or not shock["direction"]
                               else (drift > 0) == (shock["direction"] == "up")),
            "three_month": moves.get("three_month"),
            "one_year_change": moves.get("one_year"),
            "market_cap": market_cap,
            "sector": profile.get("sector"),
            "quote": edge["quote"],
            "form": edge["form"],
            "filing_date": edge["filing_date"],
            "filing_url": edge["filing_url"],
            "score": propagation.score_row(exposure, shock["magnitude"], state),
            "action": "investigate",
        })

    if not rows:
        raise EmptyDataError(
            "Every counterparty of the {} hub(s) walked is below the ${:,.1f}B "
            "size floor".format(len(walked), min_market_cap_bn)
        )
    rows.sort(key=lambda row: row["score"], reverse=True)
    rows = rows[:limit]

    as_of = str(datetime.now(timezone.utc).date())
    params = {"hubs": ",".join(selected), "hub_universe": hub_universe,
              "max_hubs": max_hubs, "min_hub_drift_pct": drift_floor * 100,
              "min_exposure_pct": min_exposure_pct,
              "min_market_cap_bn": min_market_cap_bn,
              "read_segments": read_segments, "years": years, "limit": limit}
    _record(rows, params, as_of)
    return Result(
        rows,
        provider=src,
        warnings=warnings,
        extra={
            "as_of": as_of,
            "category": sources.LINK_PROPAGATION,
            "gate": params,
            "hubs_scanned": scanned,
            "hubs_walked": walked,
            "hubs_skipped": skipped,
            "edges_found": len(by_symbol),
            "edges_considered": len(considered),
            "dropped_below_market_cap": below_cap,
        },
    )
