"""``/flagged/*`` — what changed between a filer's last two filings.

Every other screen here reads a level. These read a delta: the filer's newest
filing against its own previous one, on the dimensions where the difference is
the information — a warning added, a dependency disclosed, an auditor gone, a
share count moving against the money spent to shrink it, an accrual running
ahead of the sales behind it, a concept tagged for the first time, a wave of
sell-side rating changes. The catalogue in :mod:`backend.flagged` states what
each one measures and how it lies; the detectors in
:mod:`backend.flagged.detectors` do the arithmetic; this module wires them to
the registry, records what they emit into the graded signal log, and registers
the whole thing as an idea source so triage can rank it beside every other one.

Five commands:

``/flagged/scan``
    Every flag type for one symbol, from a couple of cached SEC objects and, for
    the document flags, two filing downloads. This is the per-company read.
``/flagged/market``
    The three accrual flags for the entire market from SEC's cross-company
    concept frames, ranked, with each row's percentile in the distribution the
    screen just computed. Four to six requests for several thousand filers.
``/flagged/flows``
    Institutional-flow inflections at small caps: every 13F filer's change in
    position, aggregated per issuer from SEC's structured data set and stated
    in days of the name's own trading volume — so the entry or exit itself is
    the liquidity event, and what the sellers still hold is the forecastable
    part of it.
``/flagged/read_through``
    Shared-end-market read-through: cluster a company with the peers that
    disclose the same geography or product line, find the lines where several
    of them report the same inflection, and name the member whose consensus
    has not moved — the peers' disclosures are the evidence for its claim.
``/flagged/catalogue``
    The flag types themselves — what each compares, where it is read from, and
    the way it characteristically produces a false positive.

Nothing here scores a company. A row is a dated statement that a thing changed,
with both states and the filing they were read from; the graded log decides,
family by family and over time, whether any of it was worth reading.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import norm_symbols
from ..flagged import (CATALOGUE, FROM_DOCUMENT, FROM_ESTIMATES, INSTITUTIONAL_FLOW, NAMESPACE,
                       READ_THROUGH, catalogue as flag_catalogue)
from ..flagged import detectors, documents, facts, flows, market, readthrough
from ..flagged import names as flag_names
from ..providers import sec, yahoo
from ..thesis import sources

_ATTENTION_ONLY = (
    "A flag is a dated statement that something changed between two filings, "
    "not a verdict on the company. Every flag type has a routine explanation "
    "listed in /flagged/catalogue; read the filing the row links to before "
    "treating the change as specific."
)

#: Row keys the signal log keeps its own columns for; everything else is payload.
_STRUCTURAL = frozenset({"symbol", "family", "score", "known_on"})

#: Which detectors run under which ``kinds`` selector, so a caller can ask for
#: the cheap XBRL flags alone without paying for two document downloads.
_DOCUMENT_FLAGS = tuple(f.name for f in CATALOGUE if f.read_from == FROM_DOCUMENT)
_ESTIMATE_FLAGS = tuple(f.name for f in CATALOGUE if f.read_from == FROM_ESTIMATES)
_ALL = "all"
#: Flags that cost a whole cluster to compute — a peer group and a filing read
#: per member — and so are not part of ``all``; ask for them by name.
_ON_DEMAND = frozenset({READ_THROUGH})


def _record(rows: List[Dict[str, Any]], kind: str, parameters: Dict[str, Any]) -> None:
    """Log what a scan emitted, under the flag's own family, so it can be graded.

    The idempotency key is (family, symbol, known_on), which is exactly right
    for a change: the same filing re-scanned tomorrow is the same event, and a
    new filing next year is a new one. It also means one filing is one event
    however many rows of a type it produced — a 10-K that discloses two new
    concentrations is graded once, not twice, because the price can only react
    to the filing once. The rows are merged here rather than in the log: the
    strongest score leads and the others ride in the payload.
    """
    from ..thesis import memory

    merged: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        if not row.get("known_on"):
            continue
        key = (row.get("family"), row["symbol"], row["known_on"])
        payload = {k: v for k, v in row.items()
                   if k not in _STRUCTURAL and v is not None}
        held = merged.get(key)
        if held is None:
            merged[key] = {
                "symbol": row["symbol"], "known_on": row["known_on"],
                "score": row.get("score"), "family": row.get("family"),
                "issuer_cik": row.get("cik"), "payload": payload,
            }
            continue
        held["payload"].setdefault("also", []).append(
            {"summary": payload.get("summary"), "score": row.get("score")})
        if (row.get("score") or 0) > (held["score"] or 0):
            held["payload"] = {**payload, "also": held["payload"].get("also", [])}
            held["score"] = row.get("score")

    memory.record_events(family=NAMESPACE, rows=list(merged.values()),
                         kind=kind, parameters=parameters)


def _wanted(kinds: Optional[str]) -> Sequence[str]:
    """Which flag types the caller asked for. ``all`` or blank is everything cheap."""
    if not kinds or str(kinds).strip().lower() == _ALL:
        return [k for k in flag_names() if k not in _ON_DEMAND]
    asked = [k.strip().lower() for k in str(kinds).split(",") if k.strip()]
    unknown = [k for k in asked if k not in flag_names()]
    if unknown:
        raise ValueError("Unknown flag type(s) {}. Known: {}".format(
            ", ".join(unknown), ", ".join(flag_names())))
    return asked


# --------------------------------------------------------------------------- #
# One symbol
# --------------------------------------------------------------------------- #
def _scan_one(symbol: str, wanted: Sequence[str], period: str,
              rating_window_days: int) -> Dict[str, Any]:
    """Every requested flag for one symbol; failures are reported, not raised.

    Each detector family fails independently. A filer whose annual report will
    not parse still gets its XBRL flags; one with no XBRL at all (some 40-F
    filers) still gets its rating flag. The ``skipped`` list on the result says
    which readers could not run and why, so an absence of flags is never
    mistaken for a clean bill.
    """
    rows: List[Dict[str, Any]] = []
    skipped: List[str] = []
    want = set(wanted)

    # -- the fact table: cheap, cached, feeds five detectors -----------------
    table = None
    if want & {detectors.RECEIVABLES_OUTRUNNING_SALES,
               detectors.DEFERRED_REVENUE_DIVERGENCE,
               detectors.BUYBACK_SHARE_GAP,
               detectors.NEW_ACCOUNTING_CONCEPT}:
        try:
            table = facts.fact_table(symbol)
        except Exception as exc:  # noqa: BLE001
            skipped.append("xbrl: {}".format(exc))
    if table is not None:
        for flag, fn in (
            (detectors.RECEIVABLES_OUTRUNNING_SALES,
             lambda: detectors.receivables_flags(table, symbol, period)),
            (detectors.DEFERRED_REVENUE_DIVERGENCE,
             lambda: detectors.deferred_revenue_flags(table, symbol, period)),
            (detectors.BUYBACK_SHARE_GAP,
             lambda: detectors.buyback_flags(table, symbol, period)),
            (detectors.NEW_ACCOUNTING_CONCEPT,
             lambda: detectors.new_concept_flags(table, symbol)),
        ):
            if flag in want:
                try:
                    rows.extend(fn())
                except Exception as exc:  # noqa: BLE001 - one detector, not the scan
                    skipped.append("{}: {}".format(flag, exc))

    # -- the document pair: two downloads, feeds three detectors -------------
    doc_flags = want & (set(_DOCUMENT_FLAGS) | {detectors.AUDITOR_CHANGE})
    if doc_flags:
        try:
            newer, older = documents.annual_pair(symbol)
            newer["symbol"] = older["symbol"] = symbol
            cik = sec.cik_for(symbol)
            def _year(filing: Dict[str, Any]) -> Optional[int]:
                stamp = filing.get("period_ending") or filing.get("filing_date") or ""
                return int(stamp[:4]) if stamp[:4].isdigit() else None

            with ThreadPoolExecutor(max_workers=2) as pool:
                new_read, old_read = pool.map(
                    lambda f: documents.read(f["url"], f["form"], cik, _year(f)),
                    (newer, older))
        except Exception as exc:  # noqa: BLE001
            skipped.append("documents: {}".format(exc))
            new_read = old_read = None
        if new_read is not None:
            revenue_growth = None
            if table is not None:
                pair = facts.year_over_year(
                    facts.concept_series(table, detectors.REVENUE_TAGS, "annual"))
                if pair is not None:
                    revenue_growth = facts.growth(facts.value(pair[0]),
                                                  facts.value(pair[1]))
            if want & {detectors.RISK_FACTOR_ADDED, detectors.RISK_FACTOR_REMOVED}:
                try:
                    found = detectors.risk_factor_flags(new_read, old_read, newer, older)
                    rows.extend(r for r in found if r["flag"] in want)
                except Exception as exc:  # noqa: BLE001
                    skipped.append("risk_factors: {}".format(exc))
            if want & {detectors.CONCENTRATION_APPEARED,
                       detectors.CONCENTRATION_VANISHED}:
                try:
                    found = detectors.concentration_flags(
                        new_read, old_read, newer, older, revenue_growth)
                    rows.extend(r for r in found if r["flag"] in want)
                except Exception as exc:  # noqa: BLE001
                    skipped.append("concentration: {}".format(exc))
            if detectors.AUDITOR_CHANGE in want:
                try:
                    item = None
                    try:
                        eightk = sec.filings(symbol, form_type="8-K", limit=200)
                        item = eightk[eightk["items"].astype(str)
                                      .str.contains(documents.AUDITOR_ITEM, regex=False)]
                        # Only a 4.01 filed since the prior annual is *this*
                        # change; older ones were already in that report.
                        item = item[item["filing_date"] > pd.Timestamp(older["filing_date"])]
                    except Exception:  # noqa: BLE001 - foreign filers have no 8-Ks
                        item = None
                    rows.extend(detectors.auditor_flags(
                        new_read, old_read, newer, older, item))
                except Exception as exc:  # noqa: BLE001
                    skipped.append("auditor: {}".format(exc))

    # -- ratings: the one vendor-fed flag ------------------------------------
    if detectors.RATING_SHIFT in want:
        actions = mix = None
        problems: List[str] = []
        try:
            actions = yahoo.upgrades_downgrades(symbol)
        except Exception as exc:  # noqa: BLE001 - either read alone still answers
            problems.append("actions: {}".format(exc))
        try:
            mix = yahoo.recommendations(symbol)
        except Exception as exc:  # noqa: BLE001
            problems.append("mix: {}".format(exc))
        if actions is None and mix is None:
            skipped.append("ratings: " + "; ".join(problems))
        else:
            try:
                rows.extend(detectors.rating_flags(actions, mix, symbol,
                                                   window_days=rating_window_days))
            except Exception as exc:  # noqa: BLE001
                skipped.append("ratings: {}".format(exc))

    # -- 13F flow: one cached market-wide table, one tape fetch --------------
    if INSTITUTIONAL_FLOW in want:
        try:
            found, _meta = flows.for_symbols([symbol], min_days=flows.MIN_DAYS_OF_VOLUME)
            rows.extend(found)
        except Exception as exc:  # noqa: BLE001
            skipped.append("flows: {}".format(exc))

    # -- read-through: the symbol as a laggard inside its own cluster ---------
    if READ_THROUGH in want:
        try:
            found, _meta = readthrough.read_through(symbol)
            rows.extend(f for f in found if f["symbol"] == symbol)
        except Exception as exc:  # noqa: BLE001
            skipped.append("read_through: {}".format(exc))

    for row in rows:
        row.setdefault("issuer", None)
    return {"rows": rows, "skipped": skipped}


@command("/flagged/scan", providers=("sec", "yahoo"),
         summary="What changed between a company's last two filings")
def scan(symbol: str, kinds: str = _ALL, period: str = "annual",
         rating_window_days: int = detectors.RATING_WINDOW_DAYS,
         provider: Optional[str] = None) -> Result:
    """Every flag type for one or more symbols — the per-company change read.

    ``kinds`` narrows to a comma-separated subset of the catalogue (see
    ``/flagged/catalogue``). The document flags — risk factors, concentration,
    the auditor cover-page check — cost two filing downloads per symbol on a
    cold cache; everything else is one cached SEC object and one vendor call.
    ``period`` is ``annual`` or ``quarter`` for the XBRL comparisons; the
    document flags always compare annual reports, which is where risk factors
    and concentration disclosures live. ``read_through`` is not part of
    ``all`` — it reads a whole peer cluster — and has to be asked for by name.

    An empty result is the ordinary answer for a company where nothing moved,
    and ``extra.skipped`` says which readers could not run — so no flags never
    silently means no problems.
    """
    src = resolve_provider(provider, ("sec", "yahoo"))
    if period not in ("annual", "quarter"):
        raise ValueError("period must be annual or quarter")
    wanted = _wanted(kinds)
    window = max(7, min(int(rating_window_days), 365))
    symbols = norm_symbols(symbol, limit=25)

    with ThreadPoolExecutor(max_workers=min(4, len(symbols))) as pool:
        results = list(pool.map(
            lambda s: (s, _scan_one(s, wanted, period, window)), symbols))

    rows: List[Dict[str, Any]] = []
    skipped: List[str] = []
    for sym, result in results:
        rows.extend(result["rows"])
        skipped.extend("{}: {}".format(sym, s) for s in result["skipped"])

    # Issuer names for the cards, from the SEC register — one cached table.
    try:
        register = sec.company_map().set_index("symbol")["name"]
        for row in rows:
            if not row.get("issuer") and row["symbol"] in register.index:
                row["issuer"] = str(register[row["symbol"]])
    except Exception:  # noqa: BLE001 - names are decoration
        pass

    rows.sort(key=lambda r: (r["known_on"], r["score"]), reverse=True)
    params = {"symbols": symbols, "kinds": list(wanted), "period": period,
              "rating_window_days": window}
    _record(rows, "flagged_scan", params)
    if not rows:
        raise EmptyDataError(
            "No flags for {} across {} type(s){}".format(
                ", ".join(symbols), len(wanted),
                " (skipped: {})".format("; ".join(skipped)) if skipped else ""))
    return Result(
        rows, provider=src, warnings=[_ATTENTION_ONLY] + skipped,
        extra={"as_of": str(datetime.now(timezone.utc).date()),
               "flags": len(rows), "symbols": symbols, "kinds": list(wanted),
               "skipped": skipped},
    )


# --------------------------------------------------------------------------- #
# The whole market
# --------------------------------------------------------------------------- #
@command("/flagged/market", providers=("sec",),
         summary="Accrual flags computed across every SEC filer at once")
def market_screen(screen: str = "receivables", year: Optional[int] = None,
                  limit: int = 50, provider: Optional[str] = None) -> Result:
    """One accrual flag for the entire market, from SEC's cross-company frames.

    ``screen`` is ``receivables`` (receivables outrunning sales),
    ``deferred_revenue`` (deferred revenue diverging from recognised revenue) or
    ``buybacks`` (share count rising against repurchase spending). ``year`` is
    the calendar year compared against the one before it; it defaults to the
    newest year whose annual frames are complete, which lags the calendar by a
    quarter or so.

    Each row carries ``market_percentile`` — where it sits among every filer
    the screen examined — which no single-company read can know, and
    ``known_on`` is the true filing date recovered from the submissions index,
    never the period end.
    """
    src = resolve_provider(provider, ("sec",))
    key = str(screen).strip().lower()
    if key not in market.SCREENS:
        raise ValueError("screen must be one of {}".format(", ".join(market.SCREENS)))
    limit = max(1, min(int(limit), 200))
    rows, meta = market.SCREENS[key](year=year, limit=limit)
    params = {"screen": key, "year": year or market.default_year(), "limit": limit}
    _record(rows, "flagged_market", params)
    return Result(
        rows, provider=src, warnings=[_ATTENTION_ONLY],
        extra={"as_of": str(datetime.now(timezone.utc).date()), **params, **meta},
    )


@command("/flagged/flows", providers=("sec", "yahoo"),
         summary="Institutional-flow inflections at small caps, in days of volume")
def institutional_flows(symbol: Optional[str] = None,
                        max_market_cap_bn: float = flows.MAX_MARKET_CAP / 1e9,
                        min_market_cap_mn: float = flows.MIN_MARKET_CAP / 1e6,
                        min_days_of_volume: float = flows.MIN_DAYS_OF_VOLUME,
                        min_dollar_volume: float = flows.MIN_DOLLAR_VOLUME,
                        direction: str = "any", include_spacs: bool = False,
                        include_suspect: bool = False, limit: int = 50,
                        provider: Optional[str] = None) -> Result:
    """Quarter-over-quarter 13F position changes, measured against the tape.

    Without ``symbol`` this is the market screen: every small cap where the
    net change in reported institutional holdings between the two most recent
    quarter ends amounts to at least ``min_days_of_volume`` days of the
    quarter's average daily volume, ranked. With ``symbol`` (one or several)
    it is the per-company read, every gate off — the flow whatever its size.

    ``direction`` is ``any``, ``accumulation`` or ``distribution``. SPACs and
    rows the gate labels as probable issuance, a single implausible filer or a
    disagreeing denominator are excluded by default; ``include_spacs`` and
    ``include_suspect`` bring them back, labelled.

    Every row carries who did the buying or selling, what the net sellers
    still hold in days of the same tape (the overhang), how much of the gross
    flow was index managers, and the quarter it describes — which, because a
    13F is due 45 days after quarter end and the data set a fortnight after
    that, is always the quarter before last.
    """
    src = resolve_provider(provider, ("sec", "yahoo"))
    if direction not in ("any", "accumulation", "distribution"):
        raise ValueError("direction must be any, accumulation or distribution")
    limit = max(1, min(int(limit), 200))
    if symbol:
        symbols = norm_symbols(symbol, limit=25)
        rows, meta = flows.for_symbols(symbols)
        if not rows:
            raise EmptyDataError(meta.get("note") or "No 13F flow row for {}".format(symbol))
        params = {"symbols": symbols}
        rows.sort(key=lambda r: -r["days_of_volume"])
    else:
        rows, meta = flows.screen(
            max_market_cap=float(max_market_cap_bn) * 1e9,
            min_market_cap=float(min_market_cap_mn) * 1e6,
            min_days=float(min_days_of_volume), min_dollar_volume=float(min_dollar_volume),
            direction=direction, include_spacs=bool(include_spacs),
            include_suspect=bool(include_suspect), limit=limit)
        params = {"max_market_cap_bn": max_market_cap_bn, "min_market_cap_mn": min_market_cap_mn,
                  "min_days_of_volume": min_days_of_volume, "min_dollar_volume": min_dollar_volume,
                  "direction": direction, "include_spacs": include_spacs,
                  "include_suspect": include_suspect, "limit": limit}
    # Only rows that are flags by the screen's own definition enter the log —
    # a per-symbol read of a mega cap at a tenth of a day is a fact, not a signal.
    _record([r for r in rows if r["days_of_volume"] >= flows.MIN_DAYS_OF_VOLUME],
            "flagged_flows", params)
    return Result(
        rows, provider=src, warnings=[_ATTENTION_ONLY],
        extra={"as_of": str(datetime.now(timezone.utc).date()), **params, **meta,
               "action": "investigate"},
    )


@command("/flagged/read_through", providers=("sec", "yahoo"),
         summary="Shared-end-market read-through: peers' disclosures as a laggard's evidence")
def read_through(symbol: str, peers: Optional[str] = None, peer_limit: int = readthrough.PEER_LIMIT,
                 min_inflection_pct: float = 100 * readthrough.MIN_INFLECTION,
                 min_agreeing: int = readthrough.MIN_AGREEING,
                 min_exposure_pct: float = 100 * readthrough.MIN_EXPOSURE,
                 provider: Optional[str] = None) -> Result:
    """Cluster ``symbol`` with its peers by disclosed end market, then find the laggard.

    The cluster is the hub's peer group (see ``/equity/compare/peers``) plus
    anything in ``peers`` (comma-separated), kept to the members whose filings
    disaggregate revenue. For every geography or product line that at least
    three members disclose, the members whose year-over-year growth in that
    line changed by ``min_inflection_pct`` points between their two most recent
    quarters, in the same direction, in numbers reaching ``min_agreeing`` and
    half the reporters, are the confirmers. A member with at least
    ``min_exposure_pct`` of its revenue on the line whose next-year EPS
    consensus has not moved the way the confirmers' has is a laggard — one row
    per (line, laggard), anchored on the day the last needed confirmer filed.

    ``extra.lines`` describes every shared line the cluster has, fired or not,
    so a line that did not produce a row can still be read.
    """
    src = resolve_provider(provider, ("sec", "yahoo"))
    hub = norm_symbols(symbol, limit=1)[0]
    extra = norm_symbols(peers, limit=25) if peers else []
    flags, meta = readthrough.read_through(
        hub, peer_limit=max(3, min(int(peer_limit), 25)), extra=extra,
        min_inflection=max(1.0, float(min_inflection_pct)) / 100.0,
        min_agreeing=max(2, min(int(min_agreeing), 12)),
        min_exposure=max(0.0, min(float(min_exposure_pct), 100.0)) / 100.0,
    )
    params = {"symbol": hub, "peers": extra, "peer_limit": peer_limit,
              "min_inflection_pct": min_inflection_pct, "min_agreeing": min_agreeing,
              "min_exposure_pct": min_exposure_pct}
    _record(flags, "flagged_read_through", params)
    if not flags:
        fired = [c for c in meta["lines"] if c.get("verdict") == "common inflection"]
        raise EmptyDataError(
            "No laggard in {}'s cluster: {} shared line(s), {} with a common inflection "
            "({}), none with an exposed member whose consensus has not moved".format(
                hub, len(meta["lines"]), len(fired),
                ", ".join(c["line"] for c in fired) or "none"))
    return Result(
        flags, provider=src, warnings=[_ATTENTION_ONLY],
        extra={"as_of": str(datetime.now(timezone.utc).date()), **params, **meta,
               "action": "investigate"},
    )


@command("/flagged/catalogue", providers=("mft",),
         summary="Every change-detection flag type and how it produces false positives")
def flag_types(read_from: Optional[str] = None, provider: Optional[str] = None) -> Result:
    """The flag catalogue: what each compares, where it is read from, how it lies.

    ``read_from`` filters to ``document`` (two filings read as text), ``xbrl``
    (the filer's tagged facts), ``index`` (the filing index alone) or
    ``estimates`` (the vendor-fed rating flag).
    """
    if provider not in (None, "mft"):
        raise ValueError("provider must be mft")
    rows = flag_catalogue(read_from)
    if not rows:
        raise EmptyDataError("No flag types read from {!r}".format(read_from))
    return Result(rows, provider="mft")


# --------------------------------------------------------------------------- #
# As an idea source
# --------------------------------------------------------------------------- #
def _flag_detail(row: Mapping[str, Any]) -> List[str]:
    """The card lines for a flagged row: the change itself, and its provenance."""
    lines = ["  change: {}".format(row.get("summary", "?"))]
    where = row.get("form") or ""
    if row.get("period_end"):
        where += " for period ending {}".format(row["period_end"])
    if row.get("market_percentile") is not None:
        where += " · {}th percentile of {} filers".format(
            row["market_percentile"], row.get("universe", "?"))
    if where.strip():
        lines.append("  read from: {}".format(where.strip(" ·")))
    return lines


def _flow_detail(row: Mapping[str, Any]) -> List[str]:
    """The card lines for a flow row: the event, then who, then what is left."""
    lines = ["  flow: {}".format(row.get("summary", "?"))]
    movers = row.get("top_sellers") if row.get("direction") == "distribution" else row.get("top_buyers")
    if movers:
        lines.append("  movers: " + " · ".join(
            "{} {:+,} (holds {:,})".format(str(m.get("filer"))[:28], int(m.get("change") or 0),
                                          int(m.get("held_now") or 0))
            for m in movers[:3]))
    lines.append("  read from: SEC 13F data set for {} (deadline {}) · US tape only · "
                 "institutions hold {:.0f}% of shares".format(
                     row.get("period_end"), row.get("known_on"),
                     100 * float(row.get("institutional_pct") or 0)))
    return lines


sources.register(sources.Source(
    name="institutional_flows",
    label="Flagged — institutional flow against the tape (small caps)",
    scope=(
        "Small caps where the net change in reported 13F holdings between the "
        "two most recent quarter ends amounted to several days of the name's own "
        "average daily volume — every filer's position aggregated per issuer from "
        "SEC's structured data set, filers present in both quarters only, index "
        "managers' share of the flow measured, and what the sellers still hold "
        "stated in the same units."
    ),
    command="/flagged/flows",
    artifact_rule=(
        "The reported change already happened, during the quarter, and its "
        "price impact is in the past; only the overhang — what the net sellers "
        "still hold, in days of volume — is ahead, so a row with no overhang is "
        "history. A large flow is index arithmetic (passive_share), a change of "
        "CUSIP, a PIPE the company sold directly, or one adviser's filing scaled "
        "by a thousand as often as it is a decision; the row labels each of "
        "those where it can and shows top_buyers/top_sellers where it cannot. "
        "13F is long-only, forty-five days late by statute and a fortnight later "
        "by publication, and the US tape may be a fraction of a dual listing's "
        "volume. Say which of these you have ruled out and how, or decline."
    ),
    detail=_flow_detail,
    namespace=NAMESPACE,
    skip_enrichments=("congress",),
    disclaimer=(
        "13F flow rows describe the quarter before last: the filing deadline is "
        "45 days after quarter end and SEC publishes the data set two weeks "
        "after that. Days of volume is computed on the US line only, and every "
        "row's suspect labels (issuance, single filer, denominators, domicile) "
        "are the row saying it may not be a flow at all."
    ),
    params={
        "max_market_cap_bn": sources.Param("float", 2.0, 0.05, 100.0,
                                           "Largest market value to include, in $ billions."),
        "min_days_of_volume": sources.Param("float", 5.0, 1.0, 100.0,
                                            "Net change as days of the quarter's average volume."),
        "direction": sources.Param("str", "any", None, None,
                                   "any, accumulation or distribution."),
        "limit": sources.Param("int", 20, 1, 40, "Maximum candidate cards to send to triage."),
    },
))


def _read_through_detail(row: Mapping[str, Any]) -> List[str]:
    lines = ["  read-through: {}".format(row.get("summary", "?"))]
    lines.append("  confirmers filed: " + " · ".join(
        "{} {}".format(c.get("symbol"), c.get("filed") or "?") for c in (row.get("confirmers") or [])[:5]))
    return lines


sources.register(sources.Source(
    name="read_through",
    label="Flagged — shared end-market read-through",
    scope=(
        "One company's peer cluster, kept to the members that disclose revenue on "
        "the same geography or product line: where several members report the same "
        "inflection in a shared line in the same fiscal cohort, the member with real "
        "exposure to that line whose next-year EPS consensus has not moved the way "
        "the confirmers' has. Needs a hub symbol."
    ),
    command="/flagged/read_through",
    artifact_rule=(
        "Sharing a line is not sharing an exposure — 'China' means fab capex at "
        "one member and the consumer at another, and the normaliser only makes "
        "them agree on the word. The cluster is the peer group and no better than "
        "it. An inflection is a change in year-over-year growth, which one large "
        "order or last year's easy comparison manufactures. A flat consensus can be "
        "no coverage or a sell side that already treats the line as immaterial. "
        "Say which applies, and name the laggard's own print as the catalyst and "
        "the condition under which the peers' disclosure would not read through."
    ),
    detail=_read_through_detail,
    namespace=NAMESPACE,
    skip_enrichments=("congress",),
    disclaimer=(
        "A read-through row is a claim about the laggard built from other "
        "companies' filings. The exposure share, the confirmers and their filing "
        "dates are on the row so the chain of evidence can be checked link by link."
    ),
    params={
        "symbol": sources.Param("str", "", None, None, "The hub whose peer cluster is read."),
        "min_agreeing": sources.Param("int", 3, 2, 12, "Confirmers required for a common inflection."),
        "min_exposure_pct": sources.Param("float", 10.0, 0.0, 100.0,
                                          "Least share of the laggard's revenue on the line."),
    },
))


sources.register(sources.Source(
    name="flagged_market",
    label="Flagged — market-wide accrual changes",
    scope=(
        "Every SEC filer whose newest annual XBRL facts show one of three "
        "accrual changes against its own prior year — receivables outrunning "
        "sales, deferred revenue diverging from recognised revenue, or the "
        "diluted share count rising despite repurchase spending — computed from "
        "the cross-company concept frames and ranked by market percentile."
    ),
    command="/flagged/market",
    artifact_rule=(
        "These are changes in ratios between two balance-sheet dates, and every "
        "one of them has a routine cause listed in the flag catalogue: a strong "
        "final month, a mix shift, an acquisition consolidated mid-year, a "
        "billing-terms change, stock-based compensation, an ASC 606 tag "
        "migration. The cross-section adds a rank, not a mechanism — the 98th "
        "percentile of DSO drift is still a number about invoice timing until "
        "the filing says otherwise. Say which routine cause you have ruled out "
        "and how, or decline."
    ),
    detail=_flag_detail,
    # The scanner writes under the ``flagged`` namespace with the flag type as
    # the family, so a triage card's base-rate lookup has to ask under the
    # same name — otherwise the market screen's rows and the per-symbol scan's
    # rows would be two populations of the identical event.
    namespace=NAMESPACE,
    disclaimer=(
        "Market-wide accrual flags are computed from SEC XBRL frames, which "
        "assign each filer's fiscal period to a calendar frame; a filer whose "
        "year-end moved may be absent rather than mis-compared. Rows are dated "
        "to the true filing date, and a change in an accrual ratio is an "
        "attention signal with a routine explanation more often than not."
    ),
    params={
        "screen": sources.Param("str", "receivables", None, None,
                                "receivables, deferred_revenue or buybacks."),
        "year": sources.Param("int", None, 2010, 2100,
                              "Calendar year compared against the one before."),
        "limit": sources.Param("int", 20, 1, 40, "Maximum candidate cards to send to triage."),
    },
))
