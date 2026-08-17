"""Congressional trading: the other insiders, disclosed under the STOCK Act.

A corporate insider files a Form 4 because they are an officer of the issuer.
A senator files a Periodic Transaction Report because they are a legislator
who trades — a different relationship to the company and a different reason
the disclosure might matter. Both are people with information trading in
public view, which is why this sits beside the Form 4 funnel rather than
inside it: same shape, different population, separately measured.

``/thesis/congress_trades`` is the data. ``/thesis/congress_clusters`` is
the gate — several members, one symbol, one direction, one window — and it
records what it emits into the signal log so the base rates that judge Form 4
clusters end up judging this population too, on the same ruler.

Coverage is the Senate only; :mod:`backend.providers.congress` explains why
(the House publishes PDFs), and every result here says so rather than letting
100 of 535 members read as Congress.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..providers import congress

_COVERAGE = (
    "Senate only: the House publishes its PTRs as PDFs, which this platform "
    "does not parse. 100 of 535 members, so absence of a disclosure is not "
    "evidence of no trade."
)

_DISCLAIMER = (
    "A disclosure is an attention signal, not an alpha signal. Amounts are "
    "brackets, not sizes; the filing may lag the trade by up to 45 days; and "
    "an 'owner' of Spouse or Child, or a managed account, is a trade the "
    "member may never have directed."
)


def _tradeable(frame: pd.DataFrame) -> pd.DataFrame:
    """Rows naming a ticker. Municipal bonds and funds held by name have none."""
    return frame[frame["symbol"].notna() & (frame["symbol"] != "")]


def _owners(frame: pd.DataFrame) -> pd.Series:
    return frame["owner"].fillna("").str.strip().str.lower()


@command("/thesis/congress_trades", providers=("senate",),
         summary="Stock transactions disclosed by members of Congress (STOCK Act)")
def congress_trades(days: int = 90, symbol: Optional[str] = None,
                    side: Optional[str] = None, reports: int = 120,
                    provider: Optional[str] = None) -> Result:
    """Periodic Transaction Reports filed in the last ``days`` days, one row per
    disclosed trade.

    ``filing_date`` is the first date the market could know and is what any
    grading anchors on; ``transaction_date`` can precede it by up to 45 days.
    ``amount_low``/``amount_high`` are the disclosure's bracket — the trade is
    somewhere inside it, and the top bracket is open-ended, so ``amount_high``
    is null there. ``owner`` separates the member's own account from a spouse's
    or a dependent child's; filter on it rather than assuming.

    ``reports`` caps how many filings are opened (one request each); the
    result reports how many were read against how many exist.
    """
    src = resolve_provider(provider, ("senate",))
    days = max(1, min(int(days), 730))

    frame = congress.recent(days=days, symbol=symbol, reports=max(1, min(int(reports), 400)))
    if side:
        wanted = str(side).strip().lower()
        frame = frame[frame["side"] == wanted]
        if frame.empty:
            raise EmptyDataError("No {} disclosures in the last {} days".format(
                wanted, days))

    read, available = frame.attrs.get("reports_read"), frame.attrs.get("reports_available")
    warnings = [_COVERAGE, _DISCLAIMER]
    if read is not None and available is not None and read < available:
        warnings.append(
            "Read the {} newest of {} reports filed in the window — raise "
            "`reports` to widen it.".format(read, available))
    return Result(frame, provider=src, warnings=warnings)


@command("/thesis/congress_clusters", providers=("senate",),
         summary="Symbols several members of Congress traded the same way at once")
def congress_clusters(min_members: int = 2, window_days: int = 45, days: int = 120,
                      self_directed_only: bool = False, reports: int = 200,
                      limit: int = 40, provider: Optional[str] = None) -> Result:
    """Gate the disclosure feed to symbols where several *different* members
    disclosed the same direction inside one window.

    One member trading is a fact about that member. Several, independently,
    inside a window, is the only thing in this data that behaves like a signal
    — and it is still a weak one, which is what the recorded base rates exist
    to measure.

    The window is keyed on FILING date, not transaction date: the cluster is
    what the public could see forming, and disclosures of trades months apart
    can land the same week. ``family`` splits the log four ways — direction
    crossed with whether the trades were the member's own account or a
    household one — because those are different bets and a pooled average
    would hide it.
    """
    src = resolve_provider(provider, ("senate",))
    window_days = max(7, min(int(window_days), 180))
    days = max(window_days, min(int(days), 730))

    frame = _tradeable(congress.recent(days=days, reports=max(1, min(int(reports), 400))))
    if self_directed_only:
        frame = frame[_owners(frame).isin(congress.MEMBER_ACCOUNTS)]
    if frame.empty:
        raise EmptyDataError("No ticker-bearing disclosures in the last {} days".format(days))

    frame = frame.assign(filed=pd.to_datetime(frame["filing_date"], errors="coerce"))
    frame = frame.dropna(subset=["filed"])

    rows: List[Dict[str, Any]] = []
    for (symbol, side), block in frame.groupby(["symbol", "side"], sort=False):
        if side not in ("buy", "sell"):
            continue
        block = block.sort_values("filed")
        filed = block["filed"].values
        best: Optional[Dict[str, Any]] = None
        for i in range(len(block)):
            low = filed[i] - np.timedelta64(window_days, "D")
            window = block[(block["filed"] > low) & (block["filed"] <= filed[i])]
            members = window["member"].nunique()
            if members < int(min_members):
                continue

            directed = int(_owners(window).isin(congress.MEMBER_ACCOUNTS).sum())
            floor = float(window["amount_low"].fillna(0).sum())
            candidate = {
                "symbol": symbol,
                "issuer": (window["asset"].dropna().iloc[0]
                           if window["asset"].notna().any() else symbol),
                # Direction crossed with whose account: four families, each a
                # different bet, each measured on its own.
                "family": "{}_{}".format(
                    side, "self" if directed * 2 >= len(window) else "household"),
                "side": side,
                "last_filing": str(pd.Timestamp(filed[i]).date()),
                "members": int(members),
                "disclosures": int(len(window)),
                "self_directed": directed,
                "member_names": "; ".join(sorted(window["member"].unique())[:6]),
                # The bracket floor, summed. It is a lower bound on the money
                # involved and is labelled as one — the disclosure never says
                # what was actually traded.
                "amount_floor": round(floor, 0),
                "earliest_trade": str(pd.to_datetime(
                    window["transaction_date"]).min().date()),
                # Each disclosure's own lag, then the median: the deadline is
                # 45 days, and a cluster whose typical trade is months old is a
                # filing-calendar artefact rather than a group of people acting
                # at once. Measuring the window's newest filing against its
                # oldest trade would blur several filings into one number.
                "disclosure_lag_days": int(
                    (window["filed"] - pd.to_datetime(window["transaction_date"])
                     ).dt.days.median()),
                "score": round(float(np.log1p(floor / 100_000) * members), 2),
                "action": "investigate",
            }
            if best is None or candidate["members"] > best["members"] or (
                    candidate["members"] == best["members"]
                    and candidate["amount_floor"] > best["amount_floor"]):
                best = candidate
        if best is not None:
            rows.append(best)

    if not rows:
        raise EmptyDataError(
            "No symbol had {} or more members disclose the same way inside "
            "{} days".format(min_members, window_days))
    rows.sort(key=lambda r: (r["last_filing"], r["score"]), reverse=True)
    rows = rows[: max(1, min(int(limit), 100))]

    # Recorded, then studied, then learned from — the same contract every
    # scanner in the engine signs. Anchored on the filing date because that is
    # the first day the cluster was visible to anyone outside the household.
    from ..thesis import memory
    memory.record_events(
        family="congress_cluster",
        rows=[{"symbol": r["symbol"], "known_on": r["last_filing"],
               "score": r["score"], "family": r["family"],
               "payload": {k: r[k] for k in ("family", "side", "members",
                                             "disclosures", "self_directed",
                                             "member_names", "amount_floor",
                                             "disclosure_lag_days")}}
              for r in rows],
        kind="congress_clusters",
        parameters={"min_members": min_members, "window_days": window_days,
                    "days": days, "self_directed_only": self_directed_only},
    )
    return Result(rows, provider=src, warnings=[_COVERAGE, _DISCLAIMER])
