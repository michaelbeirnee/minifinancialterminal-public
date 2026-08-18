"""The ten detectors. Each one answers "did this change" for one filer.

Every detector has the same contract: take what the shared readers already
fetched (the fact table, the parsed document pair, the filing index), return a
list of flag rows in the shape :func:`backend.flagged.row` builds — possibly
empty, which is the ordinary answer — and never raise for "the filer does not
report this". A company that tags no deferred revenue has no deferred-revenue
flag; that is an answer, not an error.

Detectors do no I/O. The readers in :mod:`.facts` and :mod:`.documents` fetch
and cache; the command layer in :mod:`backend.extensions.flagged` decides what
to fetch for whom. Keeping the detectors pure is what makes them testable
against hand-built tables, which matters more here than anywhere else in the
platform: a change detector that is itself wrong produces the most convincing
false positives a screen can produce, because every row arrives dressed as a
dated fact with a filing behind it.

Thresholds are module constants with their reasoning attached, not tunables
buried in signatures. A caller can override them through the commands, but the
defaults are the documented opinion of this module about what "material" means,
and changing one should mean editing a sentence too.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from . import (
    AUDITOR_CHANGE,
    BUYBACK_SHARE_GAP,
    CONCENTRATION_APPEARED,
    CONCENTRATION_VANISHED,
    DEFERRED_REVENUE_DIVERGENCE,
    NEW_ACCOUNTING_CONCEPT,
    RATING_SHIFT,
    RECEIVABLES_OUTRUNNING_SALES,
    RISK_FACTOR_ADDED,
    RISK_FACTOR_REMOVED,
    row,
)
from . import documents as docs
from . import facts as fx

# --------------------------------------------------------------------------- #
# Thresholds, with the reasoning they encode
# --------------------------------------------------------------------------- #
#: DSO must move by this many days before receivables are "outrunning" sales.
#: Five days is about a week of float: below it the change is indistinguishable
#: from the timing of a single large invoice against the balance date.
DSO_MATERIAL_DAYS = 5.0

#: And receivables growth must exceed revenue growth by this margin. The DSO
#: test alone fires on a shrinking company whose receivables shrink slower —
#: real, but a different (and already-visible) story than sales running ahead
#: of collections.
RECEIVABLES_GROWTH_MARGIN = 0.10

#: Deferred revenue and recognised revenue must diverge by this much, as a
#: difference of growth rates. Billing-term noise routinely moves the balance
#: ±10% with demand unchanged; 15 points of divergence is past what invoice
#: timing alone produces in a normal year.
DEFERRED_DIVERGENCE = 0.15

#: The diluted count must have *risen* by at least this fraction, against an
#: active repurchase programme, before the gap is a flag. Below half a percent
#: the movement is within what weighted averaging and option timing produce on
#: their own; above it, shares are genuinely being issued faster than the
#: programme retires them.
BUYBACK_COUNT_RISE = 0.005

#: Annual periods the cumulative buyback view runs over. One year of gap is
#: weak evidence (the catalogue says why: late-year purchases barely dent a
#: weighted average); three years of payments with no net shrink is a pattern.
BUYBACK_LOOKBACK_PERIODS = 3

#: Rating actions inside the window must net this one-sided, as a share of
#: covering desks, before a cluster is a shift rather than a desk. A fifth of
#: coverage moving one way in ninety days is roughly the widest reading of
#: "several firms" that does not fire on every earnings week.
RATING_NET_SHARE = 0.20

#: And at least this many desks must have acted, whatever the share — two
#: actions at a thinly covered name is one conversation, not a cluster.
RATING_MIN_ACTIONS = 3

#: Days of rating actions the shift is measured over.
RATING_WINDOW_DAYS = 90

#: The second way ratings shift, and in practice the more common one to see:
#: the vendor's consensus mix (how many desks at Strong Buy / Buy / Hold / Sell
#: / Strong Sell) moving between this month and three months ago. Scored 5 to
#: 1 and averaged; a quarter of a point is roughly one desk in four moving one
#: notch, which is what "several firms changed their minds" looks like on the
#: mix when the action feed missed the individual actions.
RATING_MIX_DRIFT = 0.25

#: First appearances below this many, with no watched concept among them, are
#: not a row. Every quarter some filer tags one new thing; a family whose base
#: rate is one-concept 10-Qs measures nothing.
MIN_NEW_CONCEPTS = 3

#: First-appearance concepts worth naming individually. Curated for meaning,
#: not completeness: each of these appearing for the first time states that a
#: specific thing happened. Everything else still appears in the count.
WATCHED_CONCEPTS: Dict[str, str] = {
    "GoodwillImpairmentLoss": "first goodwill impairment",
    "ImpairmentOfIntangibleAssetsExcludingGoodwill": "first intangible impairment",
    "ImpairmentOfLongLivedAssetsHeldAndUsed": "first long-lived asset impairment",
    "AssetImpairmentCharges": "first asset impairment charge",
    "RestructuringCharges": "first restructuring charge",
    "RestructuringAndRelatedCostIncurredCost": "first restructuring cost",
    "SeveranceCosts1": "first severance charge",
    "LitigationSettlementExpense": "first litigation settlement expense",
    "LossContingencyAccrualAtCarryingValue": "first litigation accrual",
    "SubstantialDoubtAboutGoingConcern": "going-concern doubt raised",
    "AllowanceForDoubtfulAccountsReceivableWriteOffs": "first receivables write-off",
    "InventoryWriteDown": "first inventory write-down",
    "GainLossOnDispositionOfBusiness": "first business disposition",
    "BusinessCombinationBargainPurchaseGainRecognizedAmount": "bargain purchase gain",
    "DerivativeInstrumentsNotDesignatedAsHedgingInstrumentsGainLossNet": "first undesignated derivative gain/loss",
}

#: Revenue tag preference, shared by three detectors. Same order as the SEC
#: provider's income map — the resolution rule in :func:`facts.concept_series`
#: keeps a stale synonym from winning regardless of order.
REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
)

DEFERRED_TAGS = ("ContractWithCustomerLiabilityCurrent", "DeferredRevenueCurrent")
RECEIVABLE_TAGS = ("AccountsReceivableNetCurrent",)
BUYBACK_TAGS = ("PaymentsForRepurchaseOfCommonStock",)
DILUTED_TAGS = ("WeightedAverageNumberOfDilutedSharesOutstanding",)
BASIC_TAGS = ("WeightedAverageNumberOfSharesOutstandingBasic",)


def _known_on(current: pd.Series) -> str:
    """The filing date of the newer fact — never the period end."""
    return str(pd.Timestamp(current["filed"]).date())


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else "{:+.1f}%".format(100 * value)


# --------------------------------------------------------------------------- #
# Document flags: risk factors
# --------------------------------------------------------------------------- #
def risk_factor_flags(new_read: Dict[str, Any], old_read: Dict[str, Any],
                      newer: Dict[str, Any], older: Dict[str, Any],
                      limit: int = 10) -> List[Dict[str, Any]]:
    """Added and removed risk factors between two annual reports.

    One row per flag type carrying the individual paragraphs, rather than one
    row per paragraph: a filing that adds six risk factors is one event on one
    date, and grading it as six would let a single rewrite outvote every other
    signal in the family's base rate.

    ``rewrite_suspected`` is the honesty bit. When additions and removals arrive
    in similar numbers, the likeliest cause is an edit — a reorganisation, a
    merge, a split — and the flag says so on the row instead of leaving the
    reader to discover it in the quotes.
    """
    new_paras = new_read.get("risk_factors") or []
    old_paras = old_read.get("risk_factors") or []
    if not new_paras or not old_paras:
        return []  # one unreadable section means no diff, never a fake one
    diff = docs.match_paragraphs(new_paras, old_paras)
    added, removed = diff["added"], diff["removed"]
    if not added and not removed:
        return []

    both = min(len(added), len(removed))
    rewrite = both >= 3 and both >= 0.5 * max(len(added), len(removed))
    common = {
        "form": newer["form"],
        "filing_url": newer["url"],
        "prior_filing_url": older["url"],
        "prior_filing_date": older["filing_date"],
        "paragraphs_compared": diff["compared"],
        "rewrite_suspected": rewrite,
    }

    def _clip(paras: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [{"heading": p["heading"], "text": p["text"][:500],
                 "best_match": p["best_match"]} for p in paras[:limit]]

    out: List[Dict[str, Any]] = []
    if added:
        out.append(row(
            RISK_FACTOR_ADDED, newer.get("symbol", ""), newer["filing_date"],
            "{} risk factor paragraph(s) with no counterpart in the prior {}"
            "{}".format(len(added), older["form"],
                        " — additions and removals balance, likely a rewrite"
                        if rewrite else ""),
            score=min(len(added), 20) / 20.0 * (0.3 if rewrite else 1.0),
            paragraphs=_clip(added), count=len(added), **common,
        ))
    if removed:
        out.append(row(
            RISK_FACTOR_REMOVED, newer.get("symbol", ""), newer["filing_date"],
            "{} prior risk factor paragraph(s) no longer present"
            "{}".format(len(removed),
                        " — additions and removals balance, likely a rewrite"
                        if rewrite else ""),
            score=min(len(removed), 20) / 20.0 * (0.3 if rewrite else 1.0),
            paragraphs=_clip(removed), count=len(removed), **common,
        ))
    return out


# --------------------------------------------------------------------------- #
# Document flags: concentration
# --------------------------------------------------------------------------- #
def concentration_flags(new_read: Dict[str, Any], old_read: Dict[str, Any],
                        newer: Dict[str, Any], older: Dict[str, Any],
                        revenue_growth: Optional[float] = None) -> List[Dict[str, Any]]:
    """Concentration disclosures that appeared or vanished between two annuals.

    ``revenue_growth`` over the same two periods rides on every row because it
    is the disambiguator the artifact rule promises: a vanished customer
    concentration at a company growing 30% is the business outgrowing the
    customer, and at one shrinking 20% it very probably is the customer.
    """
    new_stmts = new_read.get("concentration") or []
    old_stmts = old_read.get("concentration") or []
    if not new_stmts and not old_stmts:
        return []
    diff = docs.diff_concentration(new_stmts, old_stmts)

    common = {
        "form": newer["form"],
        "filing_url": newer["url"],
        "prior_filing_url": older["url"],
        "prior_filing_date": older["filing_date"],
        "revenue_growth": revenue_growth,
    }
    out: List[Dict[str, Any]] = []
    for stmt in diff["appeared"]:
        if stmt["negated"]:
            continue  # "no customer exceeded 10%" appearing is reassurance, not a flag
        who = stmt["counterparty"] or "an unnamed {}".format(stmt["role"])
        out.append(row(
            CONCENTRATION_APPEARED, newer.get("symbol", ""), newer["filing_date"],
            "now discloses {} at {:.0f}% of {} — the prior {} disclosed no such "
            "concentration".format(who, stmt["exposure_pct"],
                                   stmt["exposure_basis"], older["form"]),
            score=min(float(stmt["exposure_pct"]), 50.0) / 50.0,
            **stmt, **common,
        ))
    for stmt in diff["vanished"]:
        if stmt["negated"]:
            continue
        who = stmt["counterparty"] or "an unnamed {}".format(stmt["role"])
        out.append(row(
            CONCENTRATION_VANISHED, newer.get("symbol", ""), newer["filing_date"],
            "no longer discloses {} (was {:.0f}% of {}) — revenue over the same "
            "two periods: {}".format(who, stmt["exposure_pct"],
                                     stmt["exposure_basis"], _pct(revenue_growth)),
            score=min(float(stmt["exposure_pct"]), 50.0) / 50.0,
            **stmt, **common,
        ))
    return out


# --------------------------------------------------------------------------- #
# Auditor
# --------------------------------------------------------------------------- #
def auditor_flags(new_read: Dict[str, Any], old_read: Dict[str, Any],
                  newer: Dict[str, Any], older: Dict[str, Any],
                  item_401: Optional[pd.DataFrame] = None) -> List[Dict[str, Any]]:
    """A change of certifying accountant, from either of its two traces.

    The 8-K Item 4.01 is the stronger evidence and the earlier date — the
    registrant is required to announce within four business days, while the
    cover-page change waits for the next annual report. When both exist the 8-K
    wins the ``known_on``; when only the cover pages disagree the flag still
    fires, dated to the annual filing, because foreign private issuers file no
    8-Ks at all.
    """
    new_key = docs.auditor_key(new_read.get("auditor"))
    old_key = docs.auditor_key(old_read.get("auditor"))

    eightk = None
    if item_401 is not None and not item_401.empty:
        eightk = item_401.iloc[0]

    if eightk is None and not (new_key and old_key and new_key != old_key):
        return []

    new_aud = new_read.get("auditor") or {}
    old_aud = old_read.get("auditor") or {}
    if eightk is not None:
        known_on = str(eightk["filing_date"])[:10]
        url = str(eightk["url"])
        via = "8-K Item 4.01 filed {}".format(known_on)
    else:
        known_on = newer["filing_date"]
        url = newer["url"]
        via = "cover pages of the two most recent annual reports"

    return [row(
        AUDITOR_CHANGE, newer.get("symbol", ""), known_on,
        "certifying accountant changed{}{} — via {}".format(
            ": {} -> {}".format(old_aud.get("auditor"), new_aud.get("auditor"))
            if old_aud.get("auditor") and new_aud.get("auditor")
            and new_key != old_key else "",
            "" if eightk is None else " (reported on Form 8-K)", via),
        score=1.0 if eightk is not None else 0.7,
        auditor=new_aud.get("auditor"),
        auditor_firm_id=new_aud.get("auditor_firm_id"),
        prior_auditor=old_aud.get("auditor"),
        prior_auditor_firm_id=old_aud.get("auditor_firm_id"),
        auditor_source=new_aud.get("auditor_source"),
        filing_url=url,
        prior_filing_url=older["url"],
    )]


# --------------------------------------------------------------------------- #
# XBRL flags
# --------------------------------------------------------------------------- #
def receivables_flags(facts: pd.DataFrame, symbol: str,
                      period: str = "annual",
                      min_dso_change: float = DSO_MATERIAL_DAYS) -> List[Dict[str, Any]]:
    """Receivables growing faster than the sales that produced them.

    Stated as the change in days sales outstanding, because DSO is the unit in
    which the benign explanations are also argued — a reader can weigh "eight
    more days of sales in the receivable" against a mix shift or a late-quarter
    ramp, where a raw percentage invites no such argument.
    """
    revenue = fx.concept_series(facts, REVENUE_TAGS, period)
    receivables = fx.concept_series(facts, RECEIVABLE_TAGS, period)
    rev_pair = fx.year_over_year(revenue)
    rec_pair = fx.year_over_year(receivables)
    if rev_pair is None or rec_pair is None:
        return []
    rev_now, rev_then = rev_pair
    rec_now, rec_then = rec_pair
    # The two series must describe the same pair of periods, or the ratio is
    # comparing this year's receivable to last year's unrelated quarter.
    if abs((rev_now["end"] - rec_now["end"]).days) > 10:
        return []

    rev_growth = fx.growth(fx.value(rev_now), fx.value(rev_then))
    rec_growth = fx.growth(fx.value(rec_now), fx.value(rec_then))
    if rev_growth is None or rec_growth is None:
        return []

    days = 365.0 if period == "annual" else 91.0
    dso_now = fx.value(rec_now) / fx.value(rev_now) * days
    dso_then = fx.value(rec_then) / fx.value(rev_then) * days
    dso_change = dso_now - dso_then
    if dso_change < min_dso_change:
        return []
    if rec_growth - rev_growth < RECEIVABLES_GROWTH_MARGIN:
        return []

    return [row(
        RECEIVABLES_OUTRUNNING_SALES, symbol, _known_on(rec_now),
        "receivables grew {} against revenue {} — DSO {:.0f} -> {:.0f} days "
        "({:+.0f})".format(_pct(rec_growth), _pct(rev_growth),
                           dso_then, dso_now, dso_change),
        score=min(dso_change, 30.0) / 30.0,
        period_end=str(pd.Timestamp(rec_now["end"]).date()),
        prior_period_end=str(pd.Timestamp(rec_then["end"]).date()),
        receivables=fx.value(rec_now), prior_receivables=fx.value(rec_then),
        revenue=fx.value(rev_now), prior_revenue=fx.value(rev_then),
        receivables_growth=round(rec_growth, 4), revenue_growth=round(rev_growth, 4),
        dso=round(dso_now, 1), prior_dso=round(dso_then, 1),
        dso_change_days=round(dso_change, 1),
        form=str(rec_now["form"]), accession_number=str(rec_now["accn"]),
    )]


def deferred_revenue_flags(facts: pd.DataFrame, symbol: str,
                           period: str = "annual",
                           min_divergence: float = DEFERRED_DIVERGENCE) -> List[Dict[str, Any]]:
    """Deferred revenue and recognised revenue growing apart.

    Fires in both directions and says which. The balance falling behind revenue
    is the classic "eating the backlog" read; the balance running ahead is
    bookings outpacing recognition, which is the same arithmetic with the
    opposite conventional sign — and both inherit every billing-term caveat in
    the catalogue entry.
    """
    revenue = fx.concept_series(facts, REVENUE_TAGS, period)
    deferred = fx.concept_series(facts, DEFERRED_TAGS, period)
    rev_pair = fx.year_over_year(revenue)
    def_pair = fx.year_over_year(deferred)
    if rev_pair is None or def_pair is None:
        return []
    rev_now, rev_then = rev_pair
    def_now, def_then = def_pair
    if abs((rev_now["end"] - def_now["end"]).days) > 10:
        return []
    # A tag migration mid-pair (DeferredRevenueCurrent one year, the contract
    # tag the next) would compare two different definitions of the balance.
    if str(def_now["concept"]) != str(def_then["concept"]):
        return []

    rev_growth = fx.growth(fx.value(rev_now), fx.value(rev_then))
    def_growth = fx.growth(fx.value(def_now), fx.value(def_then))
    if rev_growth is None or def_growth is None:
        return []
    divergence = def_growth - rev_growth
    if abs(divergence) < min_divergence:
        return []

    direction = ("deferred balance outrunning recognised revenue"
                 if divergence > 0 else
                 "recognised revenue outrunning the deferred balance that funds it")
    return [row(
        DEFERRED_REVENUE_DIVERGENCE, symbol, _known_on(def_now),
        "{}: deferred revenue {} vs revenue {}".format(
            direction, _pct(def_growth), _pct(rev_growth)),
        score=min(abs(divergence), 0.6) / 0.6,
        period_end=str(pd.Timestamp(def_now["end"]).date()),
        prior_period_end=str(pd.Timestamp(def_then["end"]).date()),
        deferred_revenue=fx.value(def_now), prior_deferred_revenue=fx.value(def_then),
        revenue=fx.value(rev_now), prior_revenue=fx.value(rev_then),
        deferred_growth=round(def_growth, 4), revenue_growth=round(rev_growth, 4),
        divergence=round(divergence, 4),
        concept=str(def_now["concept"]),
        form=str(def_now["form"]), accession_number=str(def_now["accn"]),
    )]


def buyback_flags(facts: pd.DataFrame, symbol: str,
                  period: str = "annual",
                  min_count_rise: float = BUYBACK_COUNT_RISE) -> List[Dict[str, Any]]:
    """Cash out for repurchases while the diluted share count rose anyway.

    The test needs no price and no vendor: the cash-flow statement says money
    left for buybacks, and the income statement's weighted-average diluted
    count says whether the share base actually shrank. When the count *rose*
    despite the spending, issuance — stock compensation, an offering, a deal
    paid in shares — consumed the whole programme and more, and the per-share
    accretion the spending is conventionally credited with never happened.

    Diluted count is the deliberate choice over basic: options moving into the
    money raise it with no certificate printed, which is exactly the dilution a
    repurchase is usually claimed to be offsetting.

    The row also carries the cumulative view over up to
    :data:`BUYBACK_LOOKBACK_PERIODS` periods — total dollars in against the
    share count's total move — because the catalogue's own artifact note is
    right: one year of gap can be the mechanics of weighted averaging, while
    three years of payments with nothing to show for them is a pattern.
    """
    payments = fx.concept_series(facts, BUYBACK_TAGS, period)
    diluted = fx.concept_series(facts, DILUTED_TAGS, period)
    dil_pair = fx.year_over_year(diluted)
    if payments.empty or dil_pair is None:
        return []
    dil_now, dil_then = dil_pair

    paid = payments[abs((payments["end"] - dil_now["end"]).dt.days) <= 10]
    if paid.empty:
        return []
    spent = fx.value(paid.iloc[-1])
    shares_now, shares_then = fx.value(dil_now), fx.value(dil_then)
    if not spent or spent <= 0 or not shares_now or not shares_then:
        return []

    rise = shares_now / shares_then - 1.0
    if rise < min_count_rise:
        return []

    # The pattern check: everything spent over the lookback, against where the
    # count went over the same span.
    window = payments.tail(BUYBACK_LOOKBACK_PERIODS)
    cumulative_spent = float(window["val"].sum())
    span_start = window["end"].min()
    earlier = diluted[diluted["end"] <= pd.Timestamp(span_start) + pd.Timedelta(10, unit="D")]
    base = fx.value(earlier.iloc[-1]) if not earlier.empty else None
    cumulative_change = (shares_now / base - 1.0) if base else None

    return [row(
        BUYBACK_SHARE_GAP, symbol, _known_on(dil_now),
        "spent ${:,.0f} on repurchases while diluted shares still rose "
        "{:+.1f}% ({:,.0f} -> {:,.0f}); {} period cumulative: ${:,.0f} spent, "
        "count {}".format(
            spent, 100 * rise, shares_then, shares_now, len(window),
            cumulative_spent, _pct(cumulative_change)),
        score=round(min(rise, 0.05) / 0.05, 4),
        period_end=str(pd.Timestamp(dil_now["end"]).date()),
        prior_period_end=str(pd.Timestamp(dil_then["end"]).date()),
        repurchase_payments=spent,
        diluted_shares=shares_now, prior_diluted_shares=shares_then,
        share_count_change_pct=round(rise, 4),
        cumulative_periods=int(len(window)),
        cumulative_repurchase_payments=cumulative_spent,
        cumulative_share_count_change_pct=(
            None if cumulative_change is None else round(cumulative_change, 4)),
        form=str(dil_now["form"]), accession_number=str(dil_now["accn"]),
    )]


def new_concept_flags(facts: pd.DataFrame, symbol: str,
                      recent_filings: int = 1) -> List[Dict[str, Any]]:
    """Concepts the filer tagged for the first time in its newest filing(s).

    One row per filing, watched concepts named individually, the rest counted.
    ``paired_with_silence`` marks first appearances that arrived in the same
    filing an old concept went quiet in — the signature of a tag migration, per
    the catalogue's artifact note.

    A filer's *first ever* filing tags everything for the first time; a history
    of at least two filings is required before "new" means anything.
    """
    filings = fx.latest_filings(facts, limit=recent_filings + 1)
    if len(filings) < 2:
        return []
    first = fx.first_appearances(facts)
    if first.empty:
        return []

    out: List[Dict[str, Any]] = []
    for filing in filings[:recent_filings]:
        accn = filing["accession_number"]
        arrived = first[first["first_accn"] == accn]
        if arrived.empty:
            continue
        silenced = fx.silenced_in(facts, accn)
        watched_rows: List[Dict[str, Any]] = []
        other = 0
        for r in arrived.itertuples():
            entry = {
                "concept": str(r.concept),
                "label": str(r.label),
                "value": None if pd.isna(r.first_val) else float(r.first_val),
                "unit": str(r.unit),
                "watched": str(r.concept) in WATCHED_CONCEPTS,
                "means": WATCHED_CONCEPTS.get(str(r.concept)),
            }
            if entry["watched"]:
                watched_rows.append(entry)
            else:
                other += 1
        if not watched_rows and len(arrived) < MIN_NEW_CONCEPTS:
            continue
        migration = len(silenced) >= max(3, len(arrived) // 2)
        score = (min(len(watched_rows), 3) / 3.0 if watched_rows
                 else min(len(arrived), 20) / 60.0)
        if migration:
            score *= 0.3
        watched_names = ", ".join(w["means"] for w in watched_rows)
        out.append(row(
            NEW_ACCOUNTING_CONCEPT, symbol, filing["filed"],
            "{} concept(s) tagged for the first time{}{}".format(
                len(arrived),
                " — including {}".format(watched_names) if watched_rows else "",
                " (old concepts went quiet in the same filing — likely a tag "
                "migration)" if migration else ""),
            score=score,
            form=filing["form"], accession_number=accn,
            period_end=filing["period_end"],
            first_appearances=len(arrived),
            watched=watched_rows[:10],
            unwatched_count=other,
            concepts_silenced=len(silenced),
            paired_with_silence=migration,
        ))
    return out


# --------------------------------------------------------------------------- #
# Ratings
# --------------------------------------------------------------------------- #
#: Consensus buckets, best to worst, and the score each carries.
_MIX_BUCKETS: Tuple[Tuple[str, int], ...] = (
    ("strongBuy", 5), ("buy", 4), ("hold", 3), ("sell", 2), ("strongSell", 1),
)


def _mix_score(counts: Mapping[str, Any]) -> Optional[Tuple[float, int]]:
    """``(mean rating 1-5, desks)`` for one month's distribution, or ``None``."""
    total, weighted = 0, 0.0
    for bucket, weight in _MIX_BUCKETS:
        n = counts.get(bucket)
        try:
            n = int(n or 0)
        except (TypeError, ValueError):
            n = 0
        total += n
        weighted += weight * n
    if total <= 0:
        return None
    return weighted / total, total


def rating_flags(actions: Optional[pd.DataFrame], mix: Optional[pd.DataFrame],
                 symbol: str,
                 window_days: int = RATING_WINDOW_DAYS,
                 min_actions: int = RATING_MIN_ACTIONS,
                 min_net_share: float = RATING_NET_SHARE,
                 min_mix_drift: float = RATING_MIX_DRIFT,
                 today: Optional[pd.Timestamp] = None) -> List[Dict[str, Any]]:
    """Sell-side ratings moving one way, read two ways and reported once.

    ``actions`` is the vendor's dated action table (Yahoo's shape: a GradeDate
    index with Firm / Action columns, ``up`` / ``down`` / ``init`` / ``main`` /
    ``reit``). ``mix`` is the vendor's consensus distribution by month (rows
    ``0m``, ``-1m``, ``-2m``, ``-3m`` with a count per bucket). The two are
    read together because each misses what the other sees: the action feed
    is where individual firms and dates live, and it is sparse — most entries
    are "maintains" with a price-target move — while the mix carries every
    covering desk but neither names nor dates.

    A cluster of dated actions is the stronger reading and wins the anchor date
    and the summary. A drift in the mix alone still fires, anchored to the
    first of the current month — the vendor's ``0m`` bucket is "this month",
    and one event per month while the drift persists is what keeps a scan run
    daily from filing the same shift under thirty dates.

    Firms are deduplicated to their latest action inside the window: a desk
    that cut twice changed its mind once. Coverage comes from the mix when it
    is there, so three downgrades at a three-desk small cap and at a forty-desk
    mega cap read as differently as they are.
    """
    now = today if today is not None else pd.Timestamp.now()

    # -- reading one: the dated actions ---------------------------------------
    ups = downs = inits = 0
    detail: List[Dict[str, Any]] = []
    action_anchor: Optional[str] = None
    if actions is not None and not actions.empty:
        frame = actions.reset_index()
        date_col = next((c for c in frame.columns
                         if str(c).lower() in ("gradedate", "date", "index")), None)
        if date_col is not None:
            frame["when"] = pd.to_datetime(frame[date_col], errors="coerce")
            frame = frame.dropna(subset=["when"])
            if not frame.empty and getattr(frame["when"].dt, "tz", None) is not None:
                frame["when"] = frame["when"].dt.tz_localize(None)
            recent = frame[frame["when"] >= pd.Timestamp(now) - pd.Timedelta(int(window_days), unit="D")]
            if not recent.empty and "Firm" in recent.columns and "Action" in recent.columns:
                recent = (recent.sort_values("when")
                                .drop_duplicates(subset=["Firm"], keep="last"))
                acts = recent["Action"].astype(str).str.lower()
                ups = int((acts == "up").sum())
                downs = int((acts == "down").sum())
                inits = int((acts == "init").sum())
                moved = recent[acts.isin(("up", "down"))]
                detail = [
                    {"date": str(r.when.date()), "firm": str(r.Firm),
                     "action": str(r.Action).lower(),
                     "to_grade": str(getattr(r, "ToGrade", "") or ""),
                     "from_grade": str(getattr(r, "FromGrade", "") or "")}
                    for r in moved.itertuples()
                ]
                if not moved.empty:
                    action_anchor = str(moved["when"].max().date())

    # -- reading two: the consensus mix ---------------------------------------
    now_score = then_score = None
    desks: Optional[int] = None
    if mix is not None and not mix.empty and "period" in mix.columns:
        by_period = {str(r["period"]): r for _, r in mix.iterrows()}
        now_score = _mix_score(by_period.get("0m", {}))
        then_score = _mix_score(by_period.get("-3m", by_period.get("-2m", {})))
        if now_score:
            desks = now_score[1]
    mix_drift = (now_score[0] - then_score[0]) if now_score and then_score else None

    # -- decide ---------------------------------------------------------------
    net = ups - downs
    moved_n = ups + downs
    denominator = desks or max(moved_n + inits, 1)
    net_share = abs(net) / denominator if denominator else 0.0
    cluster = moved_n >= min_actions and net != 0 and net_share >= min_net_share
    drift = mix_drift is not None and abs(mix_drift) >= min_mix_drift
    if not cluster and not drift:
        return []
    # The two readings must agree on direction to be reported as one shift; a
    # cluster of upgrades against a mix drifting down is not a shift, it is a
    # disagreement, and disagreements are not flags.
    if cluster and drift and (net > 0) != (mix_drift > 0):
        return []

    direction = "up" if (net > 0 if cluster else mix_drift > 0) else "down"
    side = "upgrades" if direction == "up" else "downgrades"
    if cluster:
        known_on = action_anchor or str(now.date())
        summary = ("net {} {} in {} days ({} up, {} down, {} initiations) across "
                   "~{} covering desks".format(abs(net), side, window_days, ups,
                                               downs, inits, denominator))
        if drift:
            summary += "; consensus mix moved {:+.2f} on a 1-5 scale over three months".format(mix_drift)
        score = min(net_share, 0.6) / 0.6
        via = "actions" + ("+mix" if drift else "")
    else:
        known_on = str(now.replace(day=1).date())
        summary = ("consensus mix moved {:+.2f} on a 1-5 scale over three months "
                   "({:.2f} -> {:.2f}) across {} covering desks; the action feed "
                   "shows {} up, {} down".format(mix_drift, then_score[0],
                                                  now_score[0], denominator, ups, downs))
        score = min(abs(mix_drift), 1.0)
        via = "mix"

    return [row(
        RATING_SHIFT, symbol, known_on, summary,
        score=round(score, 4),
        direction=direction, read_via=via,
        window_days=window_days,
        upgrades=ups, downgrades=downs, initiations=inits,
        net_actions=net, covering_desks=denominator,
        net_share_of_coverage=round(net_share, 4),
        mix_now=None if not now_score else round(now_score[0], 3),
        mix_three_months_ago=None if not then_score else round(then_score[0], 3),
        mix_drift=None if mix_drift is None else round(mix_drift, 3),
        actions=detail[-12:],
    )]
