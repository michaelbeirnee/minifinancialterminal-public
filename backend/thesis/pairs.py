"""Pair dislocation along disclosed links: cointegration where a mechanism exists.

Run a cointegration test across every pair in an index and you will find
hundreds of "relationships". At a 5% threshold, five in a hundred unrelated
pairs pass by construction, and with 125,000 pairs in the S&P 500 that is six
thousand spurious matches for the reader to lose money on. The statistics are
fine; the search space is the problem. A pair with no economic reason to move
together has nothing to revert *to* — the spread was never a relationship, so
its widening is not a break.

So this module refuses to test a pair unless something in a filing joins the
two companies first:

* **Supplier / customer** — one of them discloses a quantified concentration
  in the other ("sales to X accounted for 27% of our net sales"), read by
  :mod:`backend.providers.supplychain` from either side's annual report.
* **Shared segment** — the two operate in the same line of business, evidenced
  either by one naming the other as competition in its 10-K, or by the filer's
  own SIC registration and the market classification agreeing. That evidence
  is what :mod:`backend.providers.peers` blends; a comparable named by a single
  filing-cabinet label is not enough on its own.

Only *then* is the price relationship measured, and it is measured
out-of-sample by construction. The history — everything but the most recent
window — fits the hedge ratio, tests for cointegration, and sets the mean and
scale of the spread. The recent window is then projected onto that fitted
relationship, so a spread ``2.5σ`` from its mean is 2.5 historical sigmas away
from a line the recent prices never helped draw. Two states are distinguished:

* **dislocated** — the pair still tests as cointegrated over the whole window,
  and the spread is at an extreme. The relationship looks intact and stretched.
* **broken** — the deviation is large enough that the whole window no longer
  tests as cointegrated. Something in the relationship itself may have changed,
  which is the more interesting and the more dangerous reading: a broken pair
  has no statistical reason to close.

Neither state is a trade. A supplier trading three sigmas cheap to the customer
that is 30% of its revenue may be a lag, or the market having read a filing
that says the concentration ended. The card carries the sentence that joined
the pair, the filing it came from, and the numbers; the join between them is
the reader's.

Three limits, all structural.

* **A disclosed link is as old as its filing.** The pairs come from annual
  reports; a concentration disclosed eighteen months ago may have ended.
* **Engle-Granger is a weak, asymmetric test.** It is run with the anchor as
  the dependent variable and reported with its p-value; a p-value of 0.08 is
  not a relationship, and ``pairs_tested`` is returned so the reader can judge
  how many chances the search had to fool them.
* **Coverage stops where SEC filing does.** A private counterparty, or one
  that never crossed a disclosure threshold, has no edge and no pair.
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ..core.errors import EmptyDataError
from ..providers import peers as peer_source
from ..providers import supplychain, yahoo
from .propagation import exposure_term

RELATIONSHIPS: Tuple[str, ...] = ("supplier", "customer", "shared_segment")

#: How a comparable earns its way into the pair universe. ``filings`` keeps
#: only names somebody wrote down as competition; ``agree`` (the default) also
#: keeps names two independent classifications place in the same industry;
#: ``any`` keeps every comparable the peer group ranked, including ones a
#: single classification produced.
PEER_EVIDENCE: Tuple[str, ...] = ("filings", "agree", "any")

#: A z-score this large is the top of the dislocation scale. Four sigmas from
#: a fitted relationship is not a rounding of a spread.
_FULL_Z = 4.0

#: Strength of a shared-segment link, by how it was evidenced. A disclosed
#: competitor is a statement; two classifications agreeing is a coincidence of
#: filing cabinets that is usually right; one classification is a hint.
_PEER_STRENGTH: Dict[str, float] = {"filings": 1.0, "agree": 0.7, "any": 0.4}


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _safely(fetch: Callable[[], Any]) -> Tuple[Any, Optional[str], bool]:
    """Run one leg; returns ``(value, error, empty)``.

    ``empty`` separates the normal outcome — a filer that names nobody, a
    company nobody names — from a leg that actually failed. The first is an
    answer; only the second is worth a warning.
    """
    try:
        return fetch(), None, False
    except EmptyDataError as exc:
        return None, str(exc), True
    except Exception as exc:  # noqa: BLE001 - one dead leg is not a dead scan
        return None, str(exc), False


# --------------------------------------------------------------------------- #
# The pair universe
# --------------------------------------------------------------------------- #
def peer_evidence_of(sources: Sequence[str]) -> str:
    """The strongest label a comparable's evidence supports: filings, agree, any."""
    if "filings" in sources:
        return "filings"
    if len(set(sources)) >= 2:
        return "agree"
    return "any"


def _admits(evidence: str, required: str) -> bool:
    return PEER_EVIDENCE.index(evidence) <= PEER_EVIDENCE.index(required)


def linked_pairs(anchor: str, years: int = 4, limit: int = 15, peers: int = 8,
                 relationships: Sequence[str] = RELATIONSHIPS,
                 min_exposure_pct: float = 0.0,
                 peer_evidence: str = "agree",
                 ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Every company a filing joins to ``anchor``, one edge each.

    Returns ``(edges, report)``. Three legs are read concurrently — filings that
    name the anchor, the anchor's own annual report, and its peer group — and
    none is allowed to fail the others. ``report`` says what each contributed
    and how many candidates each gate removed, because "no pairs" from a company
    nobody discloses and "no pairs" from a company whose every link was below
    the exposure floor are different answers.

    A company reached by more than one leg keeps its best-evidenced edge: a
    disclosed percentage outranks a shared segment, and a bigger percentage
    outranks a smaller one.
    """
    anchor = anchor.upper().strip()
    wanted = {r for r in relationships if r in RELATIONSHIPS}
    if peer_evidence not in PEER_EVIDENCE:
        raise ValueError("peer_evidence must be one of {}".format(", ".join(PEER_EVIDENCE)))

    legs: Dict[str, Callable[[], Any]] = {}
    if wanted & {"supplier", "customer"}:
        legs["counterparty_filings"] = lambda: supplychain.counterparties(
            anchor, years=years, limit=limit)
        legs["own_filing"] = lambda: supplychain.subject_disclosures(anchor, limit=limit)
    if "shared_segment" in wanted:
        legs["peers"] = lambda: peer_source.peer_group(anchor, limit=peers)
    if not legs:
        raise ValueError("relationships must name at least one of {}".format(
            ", ".join(RELATIONSHIPS)))

    with ThreadPoolExecutor(max_workers=len(legs)) as pool:
        gathered = dict(zip(legs, pool.map(_safely, legs.values())))

    dropped = {"below_floor": 0, "peer_evidence": 0, "unlisted": 0, "relationship": 0}
    report: Dict[str, Any] = {"anchor": anchor, "legs": {}, "dropped": dropped}
    candidates: List[Dict[str, Any]] = []

    for leg in ("counterparty_filings", "own_filing"):
        frame, error, empty = gathered.get(leg, (None, None, True))
        rows = [] if frame is None else frame.to_dict("records")
        report["legs"][leg] = {"rows": len(rows), "error": error, "empty": empty}
        for row in rows:
            side = str(row.get("relationship") or "")
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol or symbol == anchor:
                dropped["unlisted"] += 1
                continue
            if side not in wanted:
                dropped["relationship"] += 1
                continue
            pct = _number(row.get("exposure_pct"))
            if (pct or 0.0) < min_exposure_pct:
                dropped["below_floor"] += 1
                continue
            candidates.append({
                "anchor": anchor,
                "symbol": symbol,
                "issuer": row.get("company") or symbol,
                "relationship": side,
                "exposure_pct": None if pct is None else round(pct, 2),
                "exposure_basis": row.get("exposure_basis"),
                "pct_of": row.get("pct_of"),
                "disclosed_by": row.get("disclosed_by"),
                "evidence": row.get("quote"),
                "form": row.get("form"),
                "filing_date": str(row.get("filing_date") or "")[:10] or None,
                "filing_url": row.get("filing_url"),
                "peer_evidence": None,
                "strength": exposure_term(pct, row.get("exposure_basis")),
            })

    peer_rows, error, empty = gathered.get("peers", (None, None, True))
    if "peers" in legs:
        rows = [] if peer_rows is None else list(peer_rows[0])
        report["legs"]["peers"] = {"rows": len(rows), "error": error, "empty": empty}
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol or symbol == anchor:
                dropped["unlisted"] += 1
                continue
            evidence = peer_evidence_of(row.get("sources") or [])
            if not _admits(evidence, peer_evidence):
                dropped["peer_evidence"] += 1
                continue
            candidates.append({
                "anchor": anchor,
                "symbol": symbol,
                "issuer": row.get("company") or symbol,
                "relationship": "shared_segment",
                "exposure_pct": None,
                "exposure_basis": None,
                "pct_of": None,
                "disclosed_by": ", ".join(row.get("sources") or []),
                "evidence": row.get("why"),
                "form": row.get("form"),
                "filing_date": str(row.get("filed") or "")[:10] or None,
                "filing_url": row.get("filing_url"),
                "peer_evidence": evidence,
                "strength": _PEER_STRENGTH[evidence],
            })

    # One edge per counterparty: a disclosed percentage beats a shared segment,
    # and among percentages the larger one is the better evidence.
    best: Dict[str, Dict[str, Any]] = {}
    for edge in candidates:
        rank = (edge["relationship"] != "shared_segment", edge["exposure_pct"] or 0.0,
                edge["strength"])
        held = best.get(edge["symbol"])
        if held is None or rank > held["_rank"]:
            best[edge["symbol"]] = {**edge, "_rank": rank}
    edges = [{k: v for k, v in edge.items() if k != "_rank"} for edge in best.values()]
    edges.sort(key=lambda e: (-e["strength"], e["symbol"]))
    report["edges"] = len(edges)
    return edges, report


def pair_key(a: str, b: str) -> Tuple[str, str]:
    """The unordered pair, so A→B and B→A are the same test."""
    return (a, b) if a <= b else (b, a)


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #
def close_panel(symbols: Sequence[str], start: str, end: str,
                workers: int = 6) -> Tuple[pd.DataFrame, List[str]]:
    """Adjusted closes for every symbol, concurrently, one column each.

    ``yahoo.close_panel`` is sequential; a pair universe of forty names is
    forty requests, so this fans them out and keeps whatever answered.
    """
    def one(symbol: str) -> Tuple[str, Optional[pd.Series], Optional[str]]:
        try:
            frame = yahoo.history(symbol, start, end, "1d")
            return symbol, frame["close"].astype(float), None
        except Exception as exc:  # noqa: BLE001 - one dead series is not a dead scan
            return symbol, None, str(exc)

    wanted = sorted({str(s).upper() for s in symbols if s})
    if not wanted:
        return pd.DataFrame(), []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(wanted)))) as pool:
        answered = list(pool.map(one, wanted))
    series = {symbol: close for symbol, close, _ in answered if close is not None and len(close)}
    errors = ["{}: {}".format(symbol, err) for symbol, _, err in answered if err]
    if not series:
        return pd.DataFrame(), errors
    return pd.DataFrame(series).sort_index(), errors


# --------------------------------------------------------------------------- #
# The relationship
# --------------------------------------------------------------------------- #
def half_life(spread: pd.Series) -> Optional[float]:
    """Days for a shock to the spread to halve, from an AR(1) fit. ``None`` if it does not revert.

    Regress the day's change on yesterday's level: a negative slope is
    mean-reversion, and ``-ln 2 / ln(1 + slope)`` is how long half of a
    displacement lasts. A slope at or above zero is a spread that wanders — a
    relationship with no pull — and gets no half-life rather than a huge one.
    """
    values = np.asarray(spread, dtype=float)
    if values.size < 20:
        return None
    lagged = values[:-1] - values[:-1].mean()
    change = np.diff(values)
    denominator = float(np.dot(lagged, lagged))
    if denominator <= 0:
        return None
    slope = float(np.dot(lagged, change) / denominator)
    if slope >= 0 or slope <= -1:
        return None
    return round(-math.log(2) / math.log(1 + slope), 1)


def _coint_p(y: np.ndarray, x: np.ndarray) -> Optional[float]:
    from statsmodels.tsa.stattools import coint

    try:
        _stat, p_value, _crit = coint(y, x)
    except Exception:  # noqa: BLE001 - a degenerate series makes the test blow up
        return None
    return _number(p_value)


def relationship(a: pd.Series, b: pd.Series, recent_days: int = 63,
                 min_obs: int = 250, z_threshold: float = 2.0,
                 max_p_value: float = 0.10) -> Optional[Dict[str, Any]]:
    """Fit ``log a`` on ``log b`` over the history and read the recent window against it.

    ``a`` is the dependent leg — the anchor, by convention — and ``b`` the one
    it is hedged with. Returns ``None`` when the two do not overlap for
    ``min_obs`` days; a pair with too little shared history has no historical
    relationship to break, and pretending otherwise is the data mining this
    module exists to avoid.

    Everything the recent window is judged by — hedge ratio, spread mean and
    scale, cointegration — is estimated on the history alone. ``z_now`` is
    therefore an out-of-sample reading, which is what makes it interpretable
    as a dislocation rather than a residual the fit already minimised.
    """
    frame = pd.concat([a.rename("a"), b.rename("b")], axis=1).dropna()
    frame = frame[(frame["a"] > 0) & (frame["b"] > 0)]
    total = len(frame)
    if total < max(min_obs, recent_days + 60):
        return None
    log_a = np.log(frame["a"].to_numpy(dtype=float))
    log_b = np.log(frame["b"].to_numpy(dtype=float))
    split = total - recent_days
    hist_a, hist_b = log_a[:split], log_b[:split]
    recent_a, recent_b = log_a[split:], log_b[split:]

    var_b = float(np.var(hist_b, ddof=1))
    if var_b <= 0 or float(np.var(hist_a, ddof=1)) <= 0:
        return None
    beta = float(np.cov(hist_a, hist_b, ddof=1)[0, 1] / var_b)
    alpha = float(hist_a.mean() - beta * hist_b.mean())
    spread_hist = hist_a - alpha - beta * hist_b
    mu = float(spread_hist.mean())
    sigma = float(spread_hist.std(ddof=1))
    if not sigma > 0:
        return None

    spread_recent = recent_a - alpha - beta * recent_b
    z_recent = (spread_recent - mu) / sigma
    z_now = float(z_recent[-1])
    p_hist = _coint_p(hist_a, hist_b)
    p_full = _coint_p(log_a, log_b)

    returns_a = np.diff(hist_a)
    returns_b = np.diff(hist_b)
    corr = _number(np.corrcoef(returns_a, returns_b)[0, 1]) if returns_a.size > 2 else None

    dislocated = abs(z_now) >= z_threshold
    if not dislocated:
        state = "intact"
    elif p_full is not None and p_full <= max_p_value:
        state = "dislocated"
    else:
        state = "broken"

    def move(values: np.ndarray) -> Optional[float]:
        return _number(math.exp(values[-1] - values[0]) - 1.0)

    return {
        "observations": int(total),
        "history_days": int(split),
        "recent_days": int(recent_days),
        "hedge_ratio": round(beta, 4),
        "intercept": round(alpha, 4),
        "spread_mean": round(mu, 5),
        "spread_sigma": round(sigma, 5),
        "half_life_days": half_life(pd.Series(spread_hist)),
        "return_correlation": None if corr is None else round(corr, 3),
        "p_value_history": None if p_hist is None else round(p_hist, 4),
        "p_value_full": None if p_full is None else round(p_full, 4),
        "z_now": round(z_now, 2),
        "z_recent_mean": round(float(z_recent.mean()), 2),
        "z_recent_extreme": round(float(z_recent[np.argmax(np.abs(z_recent))]), 2),
        "days_outside": int((np.abs(z_recent) >= z_threshold).sum()),
        "recent_move_a": move(np.concatenate([[log_a[split - 1]], recent_a])),
        "recent_move_b": move(np.concatenate([[log_b[split - 1]], recent_b])),
        "state": state,
        "as_of": str(frame.index[-1])[:10],
    }


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def score_row(z: float, p_history: Optional[float], half_life_days: Optional[float],
              strength: float, recent_days: int,
              hedge_ratio: Optional[float] = None) -> float:
    """How far the spread is × how well the pair ever cohered × how real the link is.

    A relationship that never tested as cointegrated over its history
    (``p_history`` near one) contributes almost nothing whatever the z-score,
    because a z-score against a line that never fit is a number and not a
    reading. A half-life longer than the recent window says the spread reverts
    too slowly for the recent window to be a dislocation of it, and is
    discounted rather than dropped; a spread that does not revert at all is
    discounted harder. A hedge ratio at or below zero is a pair the fit found
    moving *against* each other — cointegrated, perhaps, but not the "moves
    with" relationship a supplier or a competitor implies — and is halved.
    """
    z_term = min(abs(z) / _FULL_Z, 1.0)
    sign = 1.0 if hedge_ratio is None or hedge_ratio > 0 else 0.5
    fit = 1.0 - min(max(p_history if p_history is not None else 1.0, 0.0), 1.0)
    if half_life_days is None:
        reversion = 0.5
    elif half_life_days <= recent_days:
        reversion = 1.0
    else:
        reversion = 0.75
    link = 0.5 + 0.5 * min(max(strength, 0.0), 1.0)
    return round(z_term * fit * link * reversion * sign, 4)


def legs(anchor: str, other: str, stats: Mapping[str, Any]) -> Dict[str, Any]:
    """Which leg is rich, which is cheap, and which one moved.

    The spread is ``log anchor − (α + β log other)``, so a positive z means the
    anchor sits above the line the other predicts for it: rich. The leg that
    moved more over the recent window is where the news is; the one that moved
    less is the one that has not repriced against it, and is the candidate.
    """
    z = float(stats.get("z_now") or 0.0)
    move_a = _number(stats.get("recent_move_a"))
    move_b = _number(stats.get("recent_move_b"))
    rich, cheap = (anchor, other) if z > 0 else (other, anchor)
    if move_a is None or move_b is None:
        mover, lagging = anchor, other
    elif abs(move_b) > abs(move_a):
        mover, lagging = other, anchor
    else:
        mover, lagging = anchor, other
    return {"rich_leg": rich, "cheap_leg": cheap, "mover": mover, "lagging": lagging,
            "mover_move": move_b if mover == other else move_a,
            "lagging_move": move_a if mover == other else move_b}
