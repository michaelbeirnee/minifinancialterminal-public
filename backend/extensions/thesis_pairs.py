"""The funnel that tests spreads only where a filing says a relationship exists.

Pairs trading's usual failure is not the statistics but the search: run a
cointegration test over every pair in an index and the 5% that pass by chance
outnumber the ones with a reason to. This funnel inverts the order. First the
universe of pairs is drawn from disclosed links — one company naming the other
as a supplier, a customer, or competition in an SEC filing, or the two sharing
a segment by two independent classifications — and only *then* is the price
relationship measured. A pair with no mechanism is never tested, so a pair that
passes has, at minimum, a reason to have passed.

The scan is three steps.

1. **Choose anchors.** Either the caller names them, or the funnel takes the
   largest US listings — big companies are the ones concentration disclosures
   name, so they are where the edges are.
2. **Draw the pairs.** For each anchor, read the filings that name it, its own
   annual report, and its peer group; keep every counterparty that clears the
   evidence gates. Each pair carries the sentence or classification that
   justified it.
3. **Test the pairs.** Fit the relationship on the history, test it for
   cointegration, and read the most recent window against it out-of-sample.
   Emit the pairs whose spread is at or beyond ``z_threshold`` sigmas — split
   into *dislocated* (still cointegrated over the whole window: stretched) and
   *broken* (no longer: something may have changed) — with the leg that has not
   repriced as the candidate.

The mechanics live in :mod:`backend.thesis.pairs`, which also documents what
this cannot see. The expensive part is EDGAR, not the arithmetic: each anchor
is a full-text search per disclosure phrase plus the filings that answered,
cached hard afterwards, and ``max_anchors`` decides how much of it runs.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import norm_symbols
from ..providers import yahoo
from ..thesis import pairs, sources
from .thesis_propagation import _clamp, _largest_listings

_ATTENTION_ONLY = (
    "A stretched or broken spread is a queue of pairs to explain, not a trade. "
    "The link that justified the pair is a filing as old as its date; the "
    "hedge ratio and the sigma are fitted on history the recent window did not "
    "touch; and 'broken' means the whole window no longer tests as "
    "cointegrated, which is what a relationship that has genuinely ended looks "
    "like as well as what a lag looks like. Read the quoted evidence and the "
    "filing before treating any pair as one that will close."
)

#: Approximate trading days per calendar year, for turning a lookback in years
#: into a start date with margin for holidays.
_CALENDAR_DAYS_PER_YEAR = 365


def _profiles(symbols: Sequence[str]) -> Dict[str, Mapping[str, Any]]:
    """Name, sector and size for the rows that will be emitted. Best-effort."""
    def one(symbol: str) -> Tuple[str, Mapping[str, Any]]:
        try:
            return symbol, yahoo.info(symbol)
        except Exception:  # noqa: BLE001 - one dead profile is not a dead scan
            return symbol, {}

    if not symbols:
        return {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(pool.map(one, sorted(set(symbols))))


def _relationships(raw: Any) -> List[str]:
    """Which link kinds are admitted, in canonical order; empty means all."""
    text = str(raw or "").strip().lower()
    if not text or text == "all":
        return list(pairs.RELATIONSHIPS)
    asked = {piece.strip().replace("-", "_") for piece in text.replace(";", ",").split(",")}
    asked.discard("")
    if "peer" in asked or "peers" in asked or "segment" in asked:
        asked.add("shared_segment")
    kept = [kind for kind in pairs.RELATIONSHIPS if kind in asked]
    if not kept:
        raise ValueError("relationships must name at least one of {}".format(
            ", ".join(pairs.RELATIONSHIPS)))
    return kept


def _record(rows: List[Dict[str, Any]], params: Dict[str, Any], known_on: str) -> None:
    """Log what this scan emitted, so the category can earn a measured base rate."""
    from ..thesis import memory

    structural = {"symbol", "issuer", "family", "score", "action"}
    memory.record_events(
        family=sources.PAIR_DISLOCATION,
        rows=[{
            "symbol": row["symbol"],
            "known_on": known_on,
            "score": row.get("score"),
            "family": row.get("family"),
            "payload": {key: value for key, value in row.items()
                        if key not in structural and value is not None},
        } for row in rows],
        kind=sources.PAIR_DISLOCATION,
        parameters=params,
    )


@command("/thesis/pair_dislocation", providers=("sec", "yahoo"),
         summary="Cointegrated pairs joined by a disclosed link whose spread has broken away")
def pair_dislocation(symbols: Optional[str] = None,
                     anchor_universe: int = 40,
                     max_anchors: int = 4,
                     relationships: str = "supplier,customer,shared_segment",
                     min_exposure_pct: float = 0.0,
                     peer_evidence: str = "agree",
                     peers: int = 8,
                     min_anchor_market_cap_bn: float = 10.0,
                     lookback_years: int = 3,
                     recent_days: int = 63,
                     min_obs: int = 250,
                     z_threshold: float = 2.0,
                     max_p_value: float = 0.10,
                     include_intact: bool = False,
                     years: int = 4,
                     limit: int = 20,
                     provider: Optional[str] = None) -> Result:
    """Emit pairs joined by a disclosed relationship whose spread has left its history.

    Pass ``symbols`` as a comma-separated list to draw pairs around named
    companies; leave it empty and the funnel takes the largest US listings as
    anchors. Every pair is (anchor, counterparty), never (counterparty,
    counterparty): the link is what admits a pair, and only the anchor's links
    were read.

    ``relationships`` restricts the link kinds — ``supplier``, ``customer``,
    ``shared_segment`` — and ``peer_evidence`` how a shared segment has to be
    evidenced: ``filings`` (named as competition in a 10-K), ``agree`` (that,
    or two independent classifications concurring) or ``any``. ``z_threshold``
    is how far, in historical sigmas, the spread must sit from the fitted
    relationship today; ``max_p_value`` is the Engle-Granger ceiling a pair has
    to clear over its *history* to have had a relationship worth breaking, and
    the same ceiling read over the *whole* window is what separates a
    ``dislocated`` pair from a ``broken`` one.

    Direction-neutral by construction. A positive z says the anchor is rich to
    the counterparty; the row names both legs, which one moved and which one
    has not, and stops there.
    """
    src = resolve_provider(provider, ("sec", "yahoo"))
    anchor_universe = int(_clamp(anchor_universe, 5, 200, 40))
    max_anchors = int(_clamp(max_anchors, 1, 10, 4))
    min_exposure_pct = _clamp(min_exposure_pct, 0.0, 100.0, 0.0)
    peers = int(_clamp(peers, 0, 20, 8))
    min_anchor_market_cap_bn = _clamp(min_anchor_market_cap_bn, 0.1, 5000.0, 10.0)
    lookback_years = int(_clamp(lookback_years, 1, 10, 3))
    recent_days = int(_clamp(recent_days, 10, 250, 63))
    min_obs = int(_clamp(min_obs, 120, 2500, 250))
    z_threshold = _clamp(z_threshold, 0.5, 6.0, 2.0)
    max_p_value = _clamp(max_p_value, 0.01, 1.0, 0.10)
    years = int(_clamp(years, 1, 10, 4))
    limit = int(_clamp(limit, 1, 40, 20))
    include_intact = bool(include_intact)
    kinds = _relationships(relationships)
    evidence = str(peer_evidence or "agree").strip().lower()
    if evidence not in pairs.PEER_EVIDENCE:
        raise ValueError("peer_evidence must be one of {}".format(
            ", ".join(pairs.PEER_EVIDENCE)))

    warnings: List[str] = [_ATTENTION_ONLY]

    # -------------------------------------------------------------- anchors
    try:
        named = norm_symbols(symbols, limit=50) if str(symbols or "").strip() else []
    except ValueError:  # punctuation with no ticker in it means "find your own"
        named = []
    if named:
        anchors = named[:max_anchors]
        if len(named) > max_anchors:
            warnings.append(
                "{} symbols named, {} used as anchors (max_anchors={}): {} not "
                "scanned".format(len(named), len(anchors), max_anchors,
                                 ", ".join(named[max_anchors:])))
        scanned = len(named)
    else:
        pool = _largest_listings(anchor_universe, min_anchor_market_cap_bn)
        if not pool:
            raise EmptyDataError(
                "No US listing above ${:,.1f}B to draw pairs around".format(
                    min_anchor_market_cap_bn))
        anchors = [row["symbol"] for row in pool][:max_anchors]
        scanned = len(pool)

    # ------------------------------------------------------------ the pairs
    # Sequential on purpose: each anchor already fans out inside the miner and
    # the peer reader, both against EDGAR, which is rate-limited.
    edges_by_pair: Dict[Tuple[str, str], Dict[str, Any]] = {}
    drawn: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for anchor in anchors:
        try:
            edges, report = pairs.linked_pairs(
                anchor, years=years, limit=25, peers=peers, relationships=kinds,
                min_exposure_pct=min_exposure_pct, peer_evidence=evidence)
        except Exception as exc:  # noqa: BLE001 - an anchor nobody links to is a clean answer
            skipped.append({"anchor": anchor, "reason": str(exc)[:200]})
            continue
        for leg, outcome in report["legs"].items():
            # A leg that came back empty is an answer — most large caps name
            # nobody in their own 10-K — and only a leg that failed is a warning.
            if outcome.get("error") and not outcome.get("empty"):
                warnings.append("{}: {} — {}".format(anchor, leg, str(outcome["error"])[:160]))
        if not edges:
            skipped.append({"anchor": anchor,
                            "reason": "no filing links {} to another listed company under "
                                      "the gates set".format(anchor),
                            "dropped": report["dropped"]})
            continue
        drawn.append({"anchor": anchor, "edges": len(edges), "dropped": report["dropped"],
                      "legs": report["legs"]})
        for edge in edges:
            key = pairs.pair_key(anchor, edge["symbol"])
            held = edges_by_pair.get(key)
            if held is None or edge["strength"] > held["strength"]:
                edges_by_pair[key] = edge

    if not edges_by_pair:
        raise EmptyDataError(
            "No pair to test: {} anchor(s) examined and none is joined to another "
            "listed company by a disclosed link under the gates set.".format(len(anchors))
        )

    # ------------------------------------------------------------- prices
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=lookback_years * _CALENDAR_DAYS_PER_YEAR + 14)
    universe = sorted({s for key in edges_by_pair for s in key})
    panel, price_errors = pairs.close_panel(universe, str(start), str(end))
    for error in price_errors[:10]:
        warnings.append("price history: {}".format(error[:160]))
    if panel.empty:
        raise EmptyDataError("No price history for any of the {} companies in the pair "
                             "universe".format(len(universe)))

    # -------------------------------------------------------------- tests
    tested: List[Dict[str, Any]] = []
    untestable = 0
    never_cointegrated = 0
    intact = 0
    for edge in edges_by_pair.values():
        anchor, other = edge["anchor"], edge["symbol"]
        if anchor not in panel.columns or other not in panel.columns:
            untestable += 1
            continue
        stats = pairs.relationship(
            panel[anchor], panel[other], recent_days=recent_days, min_obs=min_obs,
            z_threshold=z_threshold, max_p_value=max_p_value)
        if stats is None:
            untestable += 1
            continue
        p_hist = stats.get("p_value_history")
        if p_hist is None or p_hist > max_p_value:
            # Never cointegrated over its history: there was no relationship
            # in prices to break, whatever the filing says about the business.
            never_cointegrated += 1
            continue
        if stats["state"] == "intact":
            intact += 1
            if not include_intact:
                continue
        tested.append({**edge, "stats": stats})

    pairs_tested = len(edges_by_pair) - untestable
    if not tested:
        raise EmptyDataError(
            "{} pair(s) tested, none dislocated: {} never cointegrated over their "
            "history (p > {:.2f}), {} within {:.1f} sigma of their fitted "
            "relationship, {} lacked {} overlapping days of prices.".format(
                pairs_tested, never_cointegrated, max_p_value, intact, z_threshold,
                untestable, min_obs)
        )

    # Rows are scored before profiling, so only the rows that will be shown pay
    # for a profile request.
    for entry in tested:
        entry["_score"] = pairs.score_row(
            entry["stats"]["z_now"], entry["stats"]["p_value_history"],
            entry["stats"]["half_life_days"], entry["strength"], recent_days,
            hedge_ratio=entry["stats"]["hedge_ratio"])
    tested.sort(key=lambda entry: -entry["_score"])
    considered = tested[:limit]

    profiles = _profiles([s for entry in considered
                          for s in (entry["anchor"], entry["symbol"])])

    def issuer(symbol: str, fallback: Optional[str] = None) -> str:
        profile = profiles.get(symbol) or {}
        return str(profile.get("longName") or profile.get("shortName") or fallback or symbol)

    rows: List[Dict[str, Any]] = []
    for entry in considered:
        stats = entry["stats"]
        anchor, other = entry["anchor"], entry["symbol"]
        sides = pairs.legs(anchor, other, stats)
        candidate = sides["lagging"]
        counterpart = anchor if candidate == other else other
        profile = profiles.get(candidate) or {}
        market_cap = pairs._number(profile.get("marketCap"))  # noqa: SLF001 - one NaN rule
        rows.append({
            "symbol": candidate,
            "issuer": issuer(candidate, entry["issuer"] if candidate == other else None),
            "family": "{}_pair".format(stats["state"]),
            "pair_with": counterpart,
            "pair_with_issuer": issuer(counterpart, entry["issuer"] if counterpart == other else None),
            "anchor": anchor,
            "relationship": entry["relationship"],
            "exposure_pct": entry["exposure_pct"],
            "exposure_basis": entry["exposure_basis"],
            "pct_of": entry["pct_of"],
            "disclosed_by": entry["disclosed_by"],
            "peer_evidence": entry["peer_evidence"],
            "link_strength": round(float(entry["strength"]), 3),
            "state": stats["state"],
            "z_now": stats["z_now"],
            "z_recent_mean": stats["z_recent_mean"],
            "z_recent_extreme": stats["z_recent_extreme"],
            "days_outside": stats["days_outside"],
            "recent_days": stats["recent_days"],
            "hedge_ratio": stats["hedge_ratio"],
            "half_life_days": stats["half_life_days"],
            "p_value_history": stats["p_value_history"],
            "p_value_full": stats["p_value_full"],
            "return_correlation": stats["return_correlation"],
            "observations": stats["observations"],
            "rich_leg": sides["rich_leg"],
            "cheap_leg": sides["cheap_leg"],
            "mover": sides["mover"],
            "mover_move": sides["mover_move"],
            "recent_move": sides["lagging_move"],
            "market_cap": market_cap,
            "sector": profile.get("sector"),
            "evidence": entry["evidence"],
            "form": entry["form"],
            "filing_date": entry["filing_date"],
            "filing_url": entry["filing_url"],
            "prices_as_of": stats["as_of"],
            "score": entry["_score"],
            "action": "investigate",
        })

    as_of = str(end)
    params = {"symbols": ",".join(anchors), "anchor_universe": anchor_universe,
              "max_anchors": max_anchors, "relationships": ",".join(kinds),
              "min_exposure_pct": min_exposure_pct, "peer_evidence": evidence,
              "peers": peers, "lookback_years": lookback_years,
              "recent_days": recent_days, "min_obs": min_obs,
              "z_threshold": z_threshold, "max_p_value": max_p_value,
              "include_intact": include_intact, "years": years, "limit": limit}
    _record(rows, params, as_of)
    return Result(
        rows,
        provider=src,
        warnings=warnings,
        extra={
            "as_of": as_of,
            "category": sources.PAIR_DISLOCATION,
            "gate": params,
            "anchors_scanned": scanned,
            "anchors_used": anchors,
            "anchors_drawn": drawn,
            "anchors_skipped": skipped,
            "pairs_drawn": len(edges_by_pair),
            "pairs_tested": pairs_tested,
            "pairs_untestable": untestable,
            "pairs_never_cointegrated": never_cointegrated,
            "pairs_intact": intact,
            "pairs_flagged": sum(1 for entry in tested if entry["stats"]["state"] != "intact"),
            # How many chances the search had to fool the reader: at the
            # p-value ceiling, this many unrelated pairs would pass by luck.
            "expected_false_cointegrations": round(pairs_tested * max_p_value, 2),
        },
    )
