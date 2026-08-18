"""The accrual flags, computed for the entire market at once.

The per-symbol detectors in :mod:`.detectors` read one filer's facts. Three of
them — receivables against sales, deferred revenue against recognised revenue,
share count against buybacks — are pure arithmetic on tagged concepts, and
SEC's frames endpoint serves one concept **across every filer** in a single
request. So the whole market's worth of each flag costs four to six requests,
not four thousand, and the numbers come from the filings themselves rather than
from a vendor's quarterly-refreshed fields.

The trade the frames endpoint makes, and what this module does about it:

* **Calendar frames, fiscal filers.** A frame is a calendar period; SEC assigns
  each filer's fact to the frame its own fiscal period most closely fits. The
  join between the current and prior frame is on CIK, and a filer that changed
  its year-end can land in one frame and miss the other — it drops out of the
  screen rather than being compared across unequal spans.
* **No filing date on the row.** A frame row carries the accession number but
  not the day it was filed, and a flag without a date cannot be graded. Rather
  than anchor on the period end — a date months before anyone could have read
  the number — the screen recovers the true filing date from the (cached)
  submissions index, for the rows that survived the gates only. That is one
  cheap lookup per *hit*, not per filer, and it is what lets these rows enter
  the same graded log as everything else with an honest ``known_on``.
* **Tag synonyms.** Revenue lives under several tags; each is fetched as its
  own frame and the union is taken per CIK with the contract-revenue tag
  preferred, mirroring the per-symbol resolution order.

What the cross-section adds that the single-symbol read cannot: a *rank*. Five
days of DSO drift is noise at one company and the 96th percentile of the whole
market in a bad year — each row carries where it sits in the distribution the
screen just computed, which no per-symbol scan can know.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from ..core.caching import TTL_FUNDAMENTAL, cached
from ..core.errors import EmptyDataError
from ..providers import sec
from . import (
    BUYBACK_SHARE_GAP,
    DEFERRED_REVENUE_DIVERGENCE,
    RECEIVABLES_OUTRUNNING_SALES,
)
from .detectors import (
    BUYBACK_COUNT_RISE,
    DEFERRED_DIVERGENCE,
    DEFERRED_TAGS,
    DSO_MATERIAL_DAYS,
    RECEIVABLES_GROWTH_MARGIN,
    REVENUE_TAGS,
)

#: Below this much annual revenue — in *either* year — a DSO is an artifact of
#: the denominator: a pre-revenue biotech billing its first milestone shows
#: infinite receivable growth without any information in it, and gating the
#: current year alone lets last year's near-zero base through.
MIN_REVENUE = 50_000_000

#: A balance smaller than this share of revenue is not a feature of the
#: business model, and its growth is noise: a deferred balance going from
#: $10,000 to $2 million is a filer that started invoicing in advance, not a
#: change in bookings.
MIN_BALANCE_SHARE = 0.01

#: Below this repurchase spend the buyback screen has nothing to measure
#: against — the count moves more from option timing than from the programme.
MIN_BUYBACK = 10_000_000

#: Ranking is on the *capped* measure, then on size. Ranked raw, every list is
#: led by whatever filer has the most pathological denominator — a crypto
#: treasury that issued a hundred times its float, a shell whose revenue went
#: from nothing to something — and the reader learns about small denominators
#: rather than about accruals. Past the cap the screen cannot tell two filers
#: apart on the measure, so it stops pretending to and orders them by how much
#: business is behind the number; the raw figure still reaches the row in full.
DSO_CAP_DAYS = 30.0
DIVERGENCE_CAP = 0.60
COUNT_RISE_CAP = 0.05


def _frame(tag: str, period: str, unit: str = "USD") -> pd.DataFrame:
    """One concept across the market, empty rather than raising.

    A missing frame is a real state — the newest year's instant frame appears
    before its duration frame, and older years predate some tags entirely.
    """
    try:
        return sec.frames(tag, period, unit=unit)
    except Exception:  # noqa: BLE001 - an absent frame is an empty cross-section
        return pd.DataFrame(columns=["accn", "cik", "entityName", "end", "val"])


def _revenue_frame(period: str) -> pd.DataFrame:
    """Revenue across the market, unioned over the synonym tags.

    Preference is declaration order — the contract-revenue tag first, matching
    the per-symbol resolution — applied per CIK, so a filer tagging both is
    counted once under the more specific concept.
    """
    frames: List[pd.DataFrame] = []
    for rank, tag in enumerate(REVENUE_TAGS):
        frame = _frame(tag, period)
        if not frame.empty:
            frames.append(frame.assign(_rank=rank))
    if not frames:
        return pd.DataFrame(columns=["accn", "cik", "entityName", "end", "val"])
    merged = pd.concat(frames, ignore_index=True)
    return (merged.sort_values("_rank")
                  .drop_duplicates("cik", keep="first")
                  .drop(columns="_rank"))


#: How far apart two period ends may sit and still be the same balance date.
#: The frames endpoint maps each filer's *fiscal* period onto a calendar frame,
#: and for a June year-end the instant frame nearest December is a 10-Q balance
#: while the annual duration frame is the 10-K's — Super Micro's December
#: receivable against its June revenue reads as DSO tripling when, on a
#: like-for-like year, it fell. A balance and a flow are only comparable when
#: they end together, so the join insists on it and reports what it dropped.
SAME_PERIOD_DAYS = 15


def _pair(current: pd.DataFrame, prior: pd.DataFrame,
          value: str) -> pd.DataFrame:
    """Join two years of one concept on CIK: ``{value}`` and ``prior_{value}``."""
    if current.empty or prior.empty:
        return pd.DataFrame()
    left = current[["cik", "entityName", "accn", "end", "val"]].rename(
        columns={"val": value, "end": "period_end"})
    right = prior[["cik", "val", "end"]].rename(
        columns={"val": "prior_" + value, "end": "prior_period_end"})
    return left.merge(right, on="cik", how="inner")


def _aligned(flow: pd.DataFrame, balance: pd.DataFrame,
             value: str) -> Tuple[pd.DataFrame, int]:
    """Join a flow pair to a balance pair on CIK, keeping only aligned periods.

    Returns the joined frame and the number of filers dropped for having a
    balance date that does not sit at the end of the flow's period.
    """
    if flow.empty or balance.empty:
        return pd.DataFrame(), 0
    right = balance.drop(columns=["entityName", "accn"]).rename(
        columns={"period_end": value + "_end", "prior_period_end": "prior_" + value + "_end"})
    joined = flow.merge(right, on="cik", how="inner")
    if joined.empty:
        return joined, 0
    gap = (pd.to_datetime(joined["period_end"]) - pd.to_datetime(joined[value + "_end"])).dt.days.abs()
    prior_gap = (pd.to_datetime(joined["prior_period_end"])
                 - pd.to_datetime(joined["prior_" + value + "_end"])).dt.days.abs()
    keep = (gap <= SAME_PERIOD_DAYS) & (prior_gap <= SAME_PERIOD_DAYS)
    return joined[keep].drop(columns=[value + "_end", "prior_" + value + "_end"]), int((~keep).sum())


@cached("flagged.market.symbols", ttl=TTL_FUNDAMENTAL)
def _symbol_map() -> Dict[str, str]:
    """CIK -> primary ticker, for every registrant that has one."""
    table = sec.company_map()
    # The map lists one row per listing; keep the first (primary) per CIK.
    return dict(table.drop_duplicates("cik")[["cik", "symbol"]].itertuples(index=False))


def _filed_dates(rows: List[Dict[str, Any]], workers: int = 6) -> None:
    """Stamp each hit with the day its accession was actually filed.

    In place, for the hits only. The submissions index is cached for a day per
    CIK, so a repeated screen costs nothing; a row whose accession cannot be
    found (an old amendment aged out of the recent index) keeps ``known_on``
    empty and is dropped by the caller rather than mis-dated.
    """
    def one(row: Dict[str, Any]) -> None:
        try:
            recent = sec.submissions(row["cik"]).get("filings", {}).get("recent", {})
            accessions = recent.get("accessionNumber") or []
            at = accessions.index(row["accession_number"])
            row["known_on"] = str(recent["filingDate"][at])[:10]
            row["form"] = str(recent["form"][at])
        except Exception:  # noqa: BLE001 - an undatable row must not be guessed at
            row["known_on"] = None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, rows))


def _finish(rows: List[Dict[str, Any]], flag: str, limit: int,
            rank_on: str, cap: float, size_on: str,
            universe: int, misaligned: int = 0) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Rank, percentile, name, date and trim one screen's hits.

    Ranking is on ``rank_on`` capped at ``cap`` — see :data:`DSO_CAP_DAYS` for
    why — with ``size_on`` breaking the tie past the cap, larger first.

    Percentiles are computed over the *hits*, against the full universe count —
    "worse than 99.2% of filers" is the sentence the rank exists to support, and
    it needs the population size the screen actually examined, not the length
    of the trimmed list.
    """
    if not rows:
        raise EmptyDataError("No filer crossed the {} gates this period".format(flag))
    symbols = _symbol_map()
    named = []
    for row in rows:
        ticker = symbols.get(row["cik"])
        if ticker:
            row["symbol"] = ticker
            named.append(row)
    named.sort(key=lambda r: (-min(abs(r[rank_on]), cap), -abs(r.get(size_on) or 0)))
    for position, row in enumerate(named):
        row["market_percentile"] = round(100.0 * (1 - position / universe), 1)
    kept = named[:limit]
    _filed_dates(kept)
    dated = [r for r in kept if r.get("known_on")]
    for row in dated:
        row["flag"] = flag
        row["family"] = flag
    meta = {
        "universe": universe,
        "crossed_gates": len(rows),
        "listed": len(named),
        "returned": len(dated),
        "undatable_dropped": len(kept) - len(dated),
        # Filers whose balance and flow frames came from different fiscal
        # periods — mostly off-calendar year-ends. They are not compared
        # rather than compared wrongly; the per-symbol scan reads them.
        "misaligned_periods_dropped": misaligned,
    }
    return dated, meta


# --------------------------------------------------------------------------- #
# The three screens
# --------------------------------------------------------------------------- #
def default_year() -> int:
    """The newest calendar year whose annual frames can exist.

    A CY annual frame needs the fiscal years that map to it to be filed, which
    for December filers means roughly March of the following year. Before
    April, the year before last is the one that is actually complete.
    """
    today = date.today()
    return today.year - 1 if today.month >= 4 else today.year - 2


def receivables_screen(year: Optional[int] = None,
                       min_dso_change: float = DSO_MATERIAL_DAYS,
                       min_revenue: float = MIN_REVENUE,
                       limit: int = 50) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Every filer whose receivables outran its sales, ranked by DSO drift."""
    y = int(year or default_year())
    revenue = _pair(_revenue_frame("CY{}".format(y)),
                    _revenue_frame("CY{}".format(y - 1)), "revenue")
    receivable = _pair(_frame("AccountsReceivableNetCurrent", "CY{}Q4I".format(y)),
                       _frame("AccountsReceivableNetCurrent", "CY{}Q4I".format(y - 1)),
                       "receivables")
    if revenue.empty or receivable.empty:
        raise EmptyDataError(
            "SEC has no complete CY{} frames yet — try year={}".format(y, y - 1))
    joined, misaligned = _aligned(revenue, receivable, "receivables")
    joined = joined[(joined["revenue"] >= min_revenue)
                    & (joined["prior_revenue"] >= min_revenue)
                    & (joined["receivables"] > 0)
                    & (joined["prior_receivables"] >= MIN_BALANCE_SHARE * joined["prior_revenue"])]

    rows: List[Dict[str, Any]] = []
    for r in joined.itertuples():
        rev_growth = r.revenue / r.prior_revenue - 1.0
        rec_growth = r.receivables / r.prior_receivables - 1.0
        dso = r.receivables / r.revenue * 365.0
        prior_dso = r.prior_receivables / r.prior_revenue * 365.0
        drift = dso - prior_dso
        if drift < min_dso_change:
            continue
        if rec_growth - rev_growth < RECEIVABLES_GROWTH_MARGIN:
            continue
        rows.append({
            "cik": r.cik, "issuer": r.entityName,
            "accession_number": r.accn,
            "period_end": str(r.period_end)[:10],
            "revenue": float(r.revenue), "prior_revenue": float(r.prior_revenue),
            "receivables": float(r.receivables),
            "revenue_growth": round(rev_growth, 4),
            "receivables_growth": round(rec_growth, 4),
            "dso": round(dso, 1), "prior_dso": round(prior_dso, 1),
            "dso_change_days": round(drift, 1),
            "score": round(min(drift, DSO_CAP_DAYS) / DSO_CAP_DAYS, 4),
            "summary": "DSO {:.0f} -> {:.0f} days; receivables {:+.0f}% vs "
                       "revenue {:+.0f}%".format(prior_dso, dso,
                                                 100 * rec_growth, 100 * rev_growth),
        })
    return _finish(rows, RECEIVABLES_OUTRUNNING_SALES, limit,
                   "dso_change_days", DSO_CAP_DAYS, "revenue",
                   universe=len(joined), misaligned=misaligned)


def deferred_screen(year: Optional[int] = None,
                    min_divergence: float = DEFERRED_DIVERGENCE,
                    min_revenue: float = MIN_REVENUE,
                    limit: int = 50) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Every filer whose deferred revenue diverged from recognised revenue.

    Both tags are screened but never mixed within a filer: the current and
    prior balance must come from the same concept, or a mid-migration filer
    shows a collapse that never happened (the catalogue's own warning).
    """
    y = int(year or default_year())
    revenue = _pair(_revenue_frame("CY{}".format(y)),
                    _revenue_frame("CY{}".format(y - 1)), "revenue")
    parts = []
    for tag in DEFERRED_TAGS:
        part = _pair(_frame(tag, "CY{}Q4I".format(y)),
                     _frame(tag, "CY{}Q4I".format(y - 1)), "deferred")
        if not part.empty:
            parts.append(part.assign(concept=tag))
    if revenue.empty or not parts:
        raise EmptyDataError(
            "SEC has no complete CY{} frames yet — try year={}".format(y, y - 1))
    deferred = (pd.concat(parts, ignore_index=True)
                  .drop_duplicates("cik", keep="first"))
    joined, misaligned = _aligned(revenue, deferred, "deferred")
    joined = joined[(joined["revenue"] >= min_revenue)
                    & (joined["prior_revenue"] >= min_revenue)
                    & (joined["prior_deferred"] >= MIN_BALANCE_SHARE * joined["prior_revenue"])]

    rows: List[Dict[str, Any]] = []
    for r in joined.itertuples():
        rev_growth = r.revenue / r.prior_revenue - 1.0
        def_growth = r.deferred / r.prior_deferred - 1.0
        divergence = def_growth - rev_growth
        if abs(divergence) < min_divergence:
            continue
        rows.append({
            "cik": r.cik, "issuer": r.entityName,
            "accession_number": r.accn,
            "period_end": str(r.period_end)[:10],
            "revenue": float(r.revenue), "deferred_revenue": float(r.deferred),
            "prior_deferred_revenue": float(r.prior_deferred),
            "revenue_growth": round(rev_growth, 4),
            "deferred_growth": round(def_growth, 4),
            "divergence": round(divergence, 4),
            "concept": r.concept,
            "score": round(min(abs(divergence), DIVERGENCE_CAP) / DIVERGENCE_CAP, 4),
            "summary": "deferred revenue {:+.0f}% vs revenue {:+.0f}%".format(
                100 * def_growth, 100 * rev_growth),
        })
    return _finish(rows, DEFERRED_REVENUE_DIVERGENCE, limit,
                   "divergence", DIVERGENCE_CAP, "revenue",
                   universe=len(joined), misaligned=misaligned)


def buyback_screen(year: Optional[int] = None,
                   min_count_rise: float = BUYBACK_COUNT_RISE,
                   min_buyback: float = MIN_BUYBACK,
                   limit: int = 50) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Every filer that paid for buybacks while its diluted count still rose."""
    y = int(year or default_year())
    paid = _frame("PaymentsForRepurchaseOfCommonStock", "CY{}".format(y))
    shares = _pair(
        _frame("WeightedAverageNumberOfDilutedSharesOutstanding",
               "CY{}".format(y), unit="shares"),
        _frame("WeightedAverageNumberOfDilutedSharesOutstanding",
               "CY{}".format(y - 1), unit="shares"),
        "diluted_shares")
    if paid.empty or shares.empty:
        raise EmptyDataError(
            "SEC has no complete CY{} frames yet — try year={}".format(y, y - 1))
    joined = shares.merge(
        paid[["cik", "val", "end"]].rename(
            columns={"val": "repurchase_payments", "end": "paid_end"}),
        on="cik", how="inner")
    gap = (pd.to_datetime(joined["period_end"]) - pd.to_datetime(joined["paid_end"])).dt.days.abs()
    misaligned = int((gap > SAME_PERIOD_DAYS).sum())
    joined = joined[gap <= SAME_PERIOD_DAYS].drop(columns="paid_end")
    joined = joined[(joined["repurchase_payments"] >= min_buyback)
                    & (joined["prior_diluted_shares"] > 0)]

    rows: List[Dict[str, Any]] = []
    for r in joined.itertuples():
        rise = r.diluted_shares / r.prior_diluted_shares - 1.0
        if rise < min_count_rise:
            continue
        rows.append({
            "cik": r.cik, "issuer": r.entityName,
            "accession_number": r.accn,
            "period_end": str(r.period_end)[:10],
            "repurchase_payments": float(r.repurchase_payments),
            "diluted_shares": float(r.diluted_shares),
            "prior_diluted_shares": float(r.prior_diluted_shares),
            "share_count_change_pct": round(rise, 4),
            "score": round(min(rise, COUNT_RISE_CAP) / COUNT_RISE_CAP, 4),
            "summary": "${:,.0f} of repurchases; diluted count still "
                       "{:+.1f}%".format(r.repurchase_payments, 100 * rise),
        })
    return _finish(rows, BUYBACK_SHARE_GAP, limit,
                   "share_count_change_pct", COUNT_RISE_CAP, "repurchase_payments",
                   universe=len(joined), misaligned=misaligned)


SCREENS = {
    "receivables": receivables_screen,
    "deferred_revenue": deferred_screen,
    "buybacks": buyback_screen,
}
