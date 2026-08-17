"""Thesis-engine signal menu: insider clusters as *investigate* candidates.

Framing matters here. The 2023-2025 calibration study found that insider
cluster events are NOT a standalone buy signal: the median event underperforms
its benchmark at every horizon (~40% hit rate) and pooled means are driven by
a tiny tail of huge winners. What the gate does select is *attention* — the
only naturally rare configuration (about 7.5% of cluster windows) is two or
more officers/directors putting over $1M of personal cash in, and that is a
reason to investigate a name, never a reason to buy it. Every result row and
the command's ``extra`` say so explicitly.

Two data paths. ``/thesis/insider_clusters`` scans the SEC quarterly bulk
archive (published ~one quarter in arrears), so it surfaces *recent history*
across the whole market. ``/thesis/insider_activity`` reads one issuer's
Form 4s straight from EDGAR with no lag — the path to use when investigating
a specific name or freezing thesis evidence.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..thesis import bulk, collapse, families, fresh

#: Name-matched passive managers whose filings are mechanical, not a view.
_PASSIVE = (
    "VANGUARD|BLACKROCK|STATE STREET|GEODE|FMR LLC|FIDELITY|"
    "NORTHERN TRUST|CHARLES SCHWAB|DIMENSIONAL FUND"
)

_DISCLAIMER = (
    "Insider clusters are an attention signal, not an alpha signal: in the "
    "2023-2025 calibration the median cluster event underperformed its "
    "benchmark at every horizon. A cluster is a reason to investigate, "
    "never a reason to buy."
)


def _economic_buys(quarters: int) -> pd.DataFrame:
    """Collapsed open-market purchases from the newest published quarters."""
    published: List[pd.DataFrame] = []
    for year, q in reversed(bulk.available_quarters(start_year=2022)):
        if len(published) >= quarters:
            break
        try:
            published.append(bulk.quarter(year, q))
        except EmptyDataError:
            continue  # the newest one or two are normally unpublished
    if not published:
        raise EmptyDataError("No published Form 345 quarters available")

    raw = pd.concat(published, ignore_index=True)
    buys = raw[
        (raw.code == "P")
        & (raw.acq_disp == "A")
        & (raw.shares > 0)
        & (raw.price > 0)
        & (raw.doc_type.isin(["4", "4/A"]))
        & (~raw.auto_vehicle)
    ]
    trades = collapse.economic_trades(buys)
    passive = trades.owner_name.str.upper().str.contains(_PASSIVE, na=False, regex=True)
    # Issuers without a real ticker (private funds, non-listed classes) file
    # Form 4s too; the archive spells the blank as "NONE" or "N/A".
    unlisted = trades.symbol.isin(["", "NONE", "N/A", "NA"])
    return trades[~passive & ~unlisted]


def _norm_cik(cik: Any) -> str:
    return str(cik).lstrip("0") or "0"


@command("/thesis/insider_clusters", providers=("sec",),
         summary="Insider buying clusters worth investigating (attention, not alpha)")
def insider_clusters(min_officers: int = 2, min_officer_value: float = 1_000_000,
                     window_days: int = 90, quarters: int = 2, fresh_days: int = 0,
                     symbol: Optional[str] = None, limit: int = 50,
                     provider: Optional[str] = None) -> Result:
    """Scan for issuers where several officers or directors bought stock with
    their own cash inside one window — or where a 10% holder with board
    representation did the same (the ``family`` column says which fired).

    Defaults are the calibrated gate (>=2 officer/director buyers, >$1M
    combined — about 7.5% of cluster windows in 2023-2025). Windows are keyed
    on FILING date, the day the market could first know. The bulk archive lags
    ~one quarter; pass ``fresh_days`` (business days, up to 30) to also sweep
    the EDGAR daily index for the ~500 largest issuers — the first sweep
    fetches ~100 filings per day swept and is slow, after which every filing
    is cached. ``score`` scales officer value against the calibration's p95
    single buy ($2.7M), purely for sorting."""
    src = resolve_provider(provider, ("sec",))
    quarters = max(1, min(int(quarters), 8))
    window_days = max(7, min(int(window_days), 365))
    fresh_days = max(0, min(int(fresh_days), 30))

    trades = _economic_buys(quarters)
    warnings = [_DISCLAIMER]

    if fresh_days:
        try:
            swept = fresh.sweep(fresh.top_universe_ciks(500), days=fresh_days)
            fresh_buys = swept[
                (swept.code == "P") & (swept.acq_disp == "A")
                & (swept.shares > 0) & (swept.price > 0)
                & (swept.doc_type.isin(["4", "4/A"])) & (~swept.auto_vehicle)
            ]
            if len(fresh_buys):
                fresh_trades = collapse.economic_trades(fresh_buys)
                passive = fresh_trades.owner_name.str.upper().str.contains(
                    _PASSIVE, na=False, regex=True)
                fresh_trades = fresh_trades[~passive]
                # Bulk wins the seam: a filing present in both counts once.
                fresh_trades = fresh_trades[~fresh_trades.accession.isin(set(trades.accession))]
                trades = pd.concat([trades, fresh_trades], ignore_index=True)
        except (EmptyDataError, Exception) as exc:  # noqa: BLE001
            warnings.append("fresh sweep unavailable ({}); bulk only".format(
                str(exc)[:80]))

    # Bulk pads CIKs, the fresh parser strips them — one spelling or the
    # cluster grouping splits an issuer across sources.
    trades = trades.assign(issuer_cik=trades.issuer_cik.map(_norm_cik),
                           owner_cik=trades.owner_cik.map(_norm_cik))

    if symbol:
        trades = trades[trades.symbol == str(symbol).strip().upper()]
        if trades.empty:
            raise EmptyDataError("No open-market insider buys for {}".format(symbol))

    trades = trades.sort_values(["issuer_cik", "filing_date"])
    rows: List[Dict[str, Any]] = []
    for issuer_cik, block in trades.groupby("issuer_cik", sort=False):
        # Board representation is a property of the (entity, issuer) pair —
        # resolve once per issuer, not once per window.
        entity_links: Dict[str, str] = {}
        for entity in block[block.is_ten_pct & ~(block.is_officer | block.is_director)
                            ].owner_cik.unique():
            try:
                linked, names = families.board_link(entity, issuer_cik)
            except (EmptyDataError, Exception):  # noqa: BLE001 - soft miss
                break
            if linked:
                entity_links[entity] = ", ".join(names)

        filed = block.filing_date.values
        best: Optional[Dict[str, Any]] = None
        for i in range(len(block)):
            lo = filed[i] - np.timedelta64(window_days, "D")
            win = block[(block.filing_date > lo) & (block.filing_date <= filed[i])]
            officers = win[win.is_officer | win.is_director]
            n_off = int(officers.owner_cik.nunique())
            off_value = float(officers.value_usd.sum())
            backed = win[win.owner_cik.isin(entity_links)]
            backed_value = float(backed.value_usd.sum())

            officer_fired = n_off >= int(min_officers) and off_value >= float(min_officer_value)
            backed_fired = backed_value >= float(min_officer_value)
            if not officer_fired and not backed_fired:
                continue

            titles = officers.title.str.upper()
            family = ("both" if officer_fired and backed_fired
                      else "officer_conviction" if officer_fired
                      else "board_backed_strategic")
            cand = {
                "symbol": block.symbol.iloc[i],
                "issuer": block.issuer_name.iloc[i],
                "family": family,
                "last_filing": str(pd.Timestamp(filed[i]).date()),
                "officer_buyers": n_off,
                "total_buyers": int(win.owner_cik.nunique()),
                "officer_value": round(off_value, 0),
                "board_backed_value": round(backed_value, 0),
                "board_backed_via": "; ".join(
                    sorted({entity_links[c] for c in backed.owner_cik.unique()})) or None,
                "total_value": round(float(win.value_usd.sum()), 0),
                "has_ceo_cfo": bool(titles.str.contains(
                    "CHIEF EXECUTIVE|CEO|CHIEF FINANCIAL|CFO", na=False).any()),
                "buyers": "; ".join(sorted(
                    officers.owner_name.unique().tolist()
                    + backed.owner_name.unique().tolist())[:6]),
                "score": round(float(
                    np.log1p((off_value + backed_value) / 2_700_000)
                    * max(n_off, 1)), 2),
                "action": "investigate",
            }
            if best is None or (cand["officer_value"] + cand["board_backed_value"]
                                > best["officer_value"] + best["board_backed_value"]):
                best = cand
        if best is not None:
            rows.append(best)

    if not rows:
        raise EmptyDataError(
            "No clusters met the gate (>= {} officers or board-backed holder, "
            ">= ${:,.0f})".format(min_officers, float(min_officer_value))
        )
    rows.sort(key=lambda r: (r["last_filing"], r["score"]), reverse=True)

    # Everything is recorded, then studied, then learned from — every gated
    # cluster lands in the signal log (idempotently) so base rates accumulate.
    # Each row carries its own family: pooling officer_conviction with
    # board_backed_strategic would hand the report a single average and hide
    # the exact distinction the funnel went to the trouble of drawing.
    from ..thesis import memory
    memory.record_events(
        family="insider_cluster",
        rows=[{"symbol": r["symbol"], "known_on": r["last_filing"],
               "score": r["score"], "family": r["family"],
               "payload": {k: r[k] for k in ("family", "officer_buyers",
                                             "officer_value", "board_backed_value",
                                             "board_backed_via", "buyers",
                                             "has_ceo_cfo")}}
              for r in rows],
        kind="insider_clusters",
        parameters={"quarters": quarters, "fresh_days": fresh_days,
                    "min_officers": int(min_officers),
                    "min_officer_value": float(min_officer_value),
                    "window_days": window_days, "symbol": symbol},
    )

    return Result(
        rows[: max(1, min(int(limit), 500))],
        provider=src,
        warnings=warnings,
        extra={
            "gate": {"min_officers": int(min_officers),
                     "min_officer_value": float(min_officer_value),
                     "window_days": window_days, "fresh_days": fresh_days},
            "note": "bulk archive lags ~1 quarter; /thesis/insider_activity gives "
                    "one symbol's fresh view straight from EDGAR",
        },
    )


@command("/thesis/insider_activity", providers=("sec",),
         summary="One issuer's recent insider buys and sells, collapsed to economic trades")
def insider_activity(symbol: str, days: int = 180,
                     provider: Optional[str] = None) -> Result:
    """Fresh Form 4 activity for one company, straight from EDGAR — no bulk
    lag. Rows are *economic trades* (multi-tranche fills, affiliate
    double-filings and multi-day programs already collapsed), open-market
    purchases and sales only (codes P and S). ``role`` distinguishes officers
    and directors from 10% holders — a strategic holder adding is a different
    signal from management conviction, and neither is a buy signal on its own.
    """
    src = resolve_provider(provider, ("sec",))
    days = max(7, min(int(days), 720))
    raw = fresh.issuer_trades(symbol, days=days)

    active = raw[(raw.code.isin(["P", "S"])) & (raw.shares > 0) & (raw.price > 0)
                 & (~raw.auto_vehicle)]
    if active.empty:
        raise EmptyDataError(
            "No open-market insider trades for {} in the last {} days".format(symbol, days)
        )
    trades = collapse.economic_trades(active)

    # A 10% entity with someone on this issuer's board is buying with
    # information rights — a different family from a passive threshold filer.
    # Resolved from the market-wide filing graph; the fresh rows are passed so
    # a directorship filed after the newest bulk quarter still counts.
    issuer_cik = str(raw.issuer_cik.iloc[0])
    board_backed: Dict[str, List[str]] = {}
    for entity_cik in trades[trades.is_ten_pct & ~(trades.is_officer | trades.is_director)
                             ].owner_cik.unique():
        try:
            linked, names = families.board_link(entity_cik, issuer_cik, extra_relations=raw)
        except EmptyDataError:
            break  # no relation table available; label plainly rather than fail
        if linked:
            board_backed[str(entity_cik)] = names

    def role_of(row: Any) -> str:
        if row.is_officer:
            return "officer: {}".format(row.title) if row.title else "officer"
        if row.is_director:
            return "director"
        if row.is_ten_pct:
            names = board_backed.get(str(row.owner_cik))
            if names:
                return "10% holder (board seat: {})".format(", ".join(names))
            return "10% holder"
        return "other"

    rows = [{
        "trade_date": str(pd.Timestamp(r.trans_date).date()),
        "filed": str(pd.Timestamp(r.filing_date).date()) if pd.notna(r.filing_date) else None,
        "owner": r.owner_name,
        "role": role_of(r),
        "side": "buy" if r.code == "P" else "sell",
        "shares": round(float(r.shares), 0),
        "price": round(float(r.price), 4),
        "value": round(float(r.value_usd), 0),
        "on_10b5_1_plan": r.aff10b5one,
        "program_legs": int(getattr(r, "program_legs", 1) or 1),
    } for r in trades.itertuples()]
    rows.sort(key=lambda x: x["trade_date"], reverse=True)

    buys = sum(r["value"] for r in rows if r["side"] == "buy")
    sells = sum(r["value"] for r in rows if r["side"] == "sell")
    officer_buyers = trades[(trades.code == "P") & (trades.is_officer | trades.is_director)]
    board_buys = trades[(trades.code == "P") & trades.owner_cik.astype(str).isin(board_backed)]
    return Result(
        rows,
        provider=src,
        warnings=[_DISCLAIMER],
        extra={
            "symbol": str(symbol).upper(),
            "window_days": days,
            "buy_value": round(buys, 0),
            "sell_value": round(sells, 0),
            "distinct_officer_buyers": int(officer_buyers.owner_cik.nunique()),
            "board_backed_strategic_value": round(float(board_buys.value_usd.sum()), 0),
            "meets_calibrated_gate": bool(
                officer_buyers.owner_cik.nunique() >= 2
                and float(officer_buyers.value_usd.sum()) > 1_000_000
            ),
        },
    )


@command("/thesis/notable_holders", providers=("sec",),
         summary="Which watched sovereign funds and activists hold a symbol (13F, filer-side)")
def notable_holders(symbol: str, funds: Optional[str] = None,
                    provider: Optional[str] = None) -> Result:
    """Scan the newest 13F of every watched fund for a position in ``symbol``.

    This is the filer-side view an issuer's own filings cannot give: 13F-HRs
    are filed under the institution's CIK, so "who holds this" has to be
    answered fund by fund. A row with ``holds: false`` is a real answer, and
    a fund with no 13F filer at all (several sovereigns) says so. Mind the
    lag: a 13F is a quarterly snapshot filed up to 45 days after quarter end,
    and sub-$100M managers do not file. Pass ``funds`` as comma-separated
    slugs to narrow the scan; see the extra for the full watchlist."""
    from ..thesis import holders

    src = resolve_provider(provider, ("sec",))
    wanted = [f.strip().lower() for f in funds.split(",")] if funds else None
    rows = holders.who_holds(symbol, funds=wanted)
    return Result(
        rows,
        provider=src,
        warnings=[
            "13F data is a lagged quarterly snapshot (up to 45 days stale) of long "
            "US-listed positions only. Absence of a filing is not absence of a "
            "position: ADIA, QIA and KIA have no 13F filer at all."
        ],
        extra={"symbol": str(symbol).upper(),
               "watchlist": {k: v["label"] for k, v in holders.WATCHLIST.items()}},
    )


@command("/thesis/fund_holdings", providers=("sec",),
         summary="A watched fund's latest 13F positions, largest first")
def fund_holdings(fund: str, limit: int = 25,
                  provider: Optional[str] = None) -> Result:
    """The newest reported US equity book of one watched fund. ``fund`` is a
    watchlist slug (pif, norges, temasek, mubadala, gic, berkshire, icahn,
    pershing, corvex, elliott, starboard, thirdpoint). Values are as filed —
    whole dollars for current filings."""
    from ..thesis import holders

    src = resolve_provider(provider, ("sec",))
    slug = str(fund).strip().lower()
    if slug not in holders.WATCHLIST:
        raise ValueError("fund must be one of {}".format(", ".join(sorted(holders.WATCHLIST))))
    meta = holders.WATCHLIST[slug]
    resolved = holders.resolve_filer(meta["query"])
    if resolved is None:
        raise EmptyDataError("{} has no 13F filer on EDGAR".format(meta["label"]))
    filing = holders.latest_13f(resolved["cik"])
    positions = sorted(filing["positions"], key=lambda p: -p["value_usd"])
    total = sum(p["value_usd"] for p in filing["positions"])
    rows = [{**p, "pct_of_book": round(100 * p["value_usd"] / total, 2) if total else None}
            for p in positions[: max(1, min(int(limit), 500))]]
    return Result(
        rows,
        provider=src,
        warnings=["A 13F reports long US-listed equities only — non-US listings, "
                  "bonds, privates and shorts are invisible."],
        extra={"fund": meta["label"], "filer": resolved["name"], "cik": filing["cik"],
               "period": filing["period"], "filed": filing["filed"],
               "reported_positions": len(filing["positions"]),
               "reported_value_usd": round(total, 0)},
    )


@command("/thesis/signal_log", providers=("sec",),
         summary="The recorded signal log: every scanner firing, with outcomes once graded")
def signal_log(family: Optional[str] = None, symbol: Optional[str] = None,
               limit: int = 100, provider: Optional[str] = None) -> Result:
    """The engine's memory, newest first. ``fwd_*`` columns are realised
    excess returns vs the event's benchmark, stamped only after each horizon
    has actually elapsed — null means "not yet knowable", never "zero"."""
    from ..database import SessionLocal
    from ..models import SignalEvent

    src = resolve_provider(provider, ("sec",))
    session = SessionLocal()
    try:
        query = session.query(SignalEvent)
        if family:
            query = query.filter(SignalEvent.family == str(family))
        if symbol:
            query = query.filter(SignalEvent.symbol == str(symbol).upper().strip())
        events = (query.order_by(SignalEvent.known_on.desc())
                  .limit(max(1, min(int(limit), 1000))).all())
        if not events:
            raise EmptyDataError("No recorded signals match")
        rows = [{
            "family": e.family, "symbol": e.symbol,
            "known_on": str(e.known_on.date()), "score": e.score,
            "fwd_1m": e.fwd_1m, "fwd_3m": e.fwd_3m,
            "fwd_6m": e.fwd_6m, "fwd_12m": e.fwd_12m,
            "benchmark": e.benchmark,
            "first_recorded": str(e.first_recorded_at.date()) if e.first_recorded_at else None,
            **{k: v for k, v in (e.payload or {}).items()
               if k in ("family", "officer_value", "board_backed_value", "buyers")},
        } for e in events]
        return Result(rows, provider=src)
    finally:
        session.close()


@command("/thesis/signal_report", providers=("sec",),
         summary="Per-family base rates from the graded signal log")
def signal_report(provider: Optional[str] = None) -> Result:
    """What each signal family has actually been worth, measured — mean and
    median excess return vs benchmark and hit rate per horizon, over every
    graded event on record. This is the study that turns recorded signals
    into learned weights; with few graded events it says so rather than
    extrapolating.

    Rows under ``thesis:*`` are the engine's own output and carry ``lift_*``:
    how far the theses it built beat the pooled record of the scanners they
    were built from. A deep dive that cannot show positive lift is an
    expensive way to restate the funnel."""
    from ..thesis import memory

    src = resolve_provider(provider, ("sec",))
    rows = memory.report()
    if not rows:
        raise EmptyDataError("The signal log is empty — run a scan first")
    return Result(
        rows, provider=src,
        warnings=["Base rates need months of graded events before they mean "
                  "anything. Grading runs via POST /api/theses/signals/grade."],
    )
