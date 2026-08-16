"""Fold the filing artefacts that make one decision look like several.

Form 4 data has three distinct ways of inflating a signal, and all three
produce clusters that look completely convincing:

* **Multi-tranche** — a single 10,000-share order filed as four rows across a
  price band. One buy, four rows.
* **Affiliate** — a fund, its general partner and its managing member each file
  the identical block. One buyer, three reporting owners. Observed live: TKO on
  2025-06-03, where Silver Lake West VoteCo, Endeavor Group Holdings and an
  individual each reported the same 1,579,080 shares at $158.32.
* **Program** — a standing limit order worked over consecutive sessions. MGM's
  10% holder bought nine days running in August 2022 at $4,998,240, $4,996,368,
  $4,998,400 and so on: one decision, nine filings.

Each collapse runs in order, because the later ones depend on the earlier
having already normalised the row into an economic trade.
"""
from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd

#: Two daily notionals this close, on consecutive sessions, are one program.
_PROGRAM_TOLERANCE = 0.02
#: Trading-day gap allowed between legs of the same program.
_PROGRAM_MAX_GAP_DAYS = 4
#: A program needs at least this many legs before we call it one.
_PROGRAM_MIN_LEGS = 3


def multi_tranche(trades: pd.DataFrame) -> pd.DataFrame:
    """One row per (issuer, owner, ownership form, trade date).

    Price becomes the share-weighted average, which is the price the insider
    actually paid. ``shares_after`` takes the max because the tranches report
    a running total as the order fills.
    """
    if trades.empty:
        return trades

    keys = ["issuer_cik", "owner_cik", "ownership_form", "trans_date"]
    grouped = trades.groupby(keys, dropna=False, sort=False)

    out = grouped.agg(
        symbol=("symbol", "first"),
        issuer_name=("issuer_name", "first"),
        owner_name=("owner_name", "first"),
        title=("title", "first"),
        is_officer=("is_officer", "max"),
        is_director=("is_director", "max"),
        is_ten_pct=("is_ten_pct", "max"),
        is_other=("is_other", "max"),
        code=("code", "first"),
        shares=("shares", "sum"),
        value_usd=("value_usd", "sum"),
        shares_after=("shares_after", "max"),
        filing_date=("filing_date", "min"),
        accession=("accession", "first"),
        aff10b5one=("aff10b5one", "first"),
        auto_vehicle=("auto_vehicle", "max"),
        doc_type=("doc_type", "first"),
        tranches=("shares", "size"),
    ).reset_index()

    # Share-weighted, not the mean of the printed prices: a 9,000-share fill at
    # $31.10 and a 1,000-share fill at $31.90 averages to $31.18, not $31.50.
    out["price"] = np.where(out["shares"] > 0, out["value_usd"] / out["shares"], np.nan)
    return out


def affiliates(trades: pd.DataFrame) -> pd.DataFrame:
    """Fold reporting owners who filed the identical block on the same day.

    Matching is on rounded shares and price rather than owner identity, because
    the relationship between the filers is not in the data — only the fact that
    they reported the same trade. The most senior filer is kept; the rest are
    returned in ``affiliate_dupes`` so they can be shown as evidence rather than
    vanishing.
    """
    if trades.empty:
        return trades

    work = trades.copy()
    work["_shares_key"] = work["shares"].round(0)
    work["_price_key"] = work["price"].round(4)
    work["_seniority"] = (
        work["is_officer"].astype(int) * 4
        + work["is_director"].astype(int) * 2
        + work["is_ten_pct"].astype(int)
    )

    keys = ["issuer_cik", "trans_date", "_shares_key", "_price_key"]
    work = work.sort_values(keys + ["_seniority"], ascending=[True] * len(keys) + [False])
    work["affiliate_dupes"] = work.groupby(keys, dropna=False, sort=False)["owner_name"].transform(
        "size"
    ) - 1
    kept = work.drop_duplicates(subset=keys, keep="first")

    return kept.drop(columns=["_shares_key", "_price_key", "_seniority"]).reset_index(drop=True)


def programs(trades: pd.DataFrame) -> pd.DataFrame:
    """Fold a standing order worked across consecutive sessions into one event.

    A run qualifies when the same owner buys the same issuer on near-adjacent
    dates with near-identical daily notional. The collapsed row keeps the whole
    value but reports ``program_legs``, so breadth and "unusual for them" terms
    downstream see one decision instead of nine.
    """
    if trades.empty:
        return trades

    work = trades.sort_values(["issuer_cik", "owner_cik", "trans_date"]).reset_index(drop=True)
    work["program_id"] = np.nan

    program_no = 0
    for (_issuer, _owner), block in work.groupby(["issuer_cik", "owner_cik"], sort=False):
        if len(block) < _PROGRAM_MIN_LEGS:
            continue
        run: List[int] = []
        for idx in block.index:
            if not run:
                run = [idx]
                continue
            prev = work.loc[run[-1]]
            cur = work.loc[idx]
            gap = (cur["trans_date"] - prev["trans_date"]).days
            base = max(abs(prev["value_usd"]), 1.0)
            similar = abs(cur["value_usd"] - prev["value_usd"]) / base <= _PROGRAM_TOLERANCE
            if 0 < gap <= _PROGRAM_MAX_GAP_DAYS and similar:
                run.append(idx)
                continue
            if len(run) >= _PROGRAM_MIN_LEGS:
                program_no += 1
                work.loc[run, "program_id"] = program_no
            run = [idx]
        if len(run) >= _PROGRAM_MIN_LEGS:
            program_no += 1
            work.loc[run, "program_id"] = program_no

    standalone = work[work["program_id"].isna()].copy()
    standalone["program_legs"] = 1

    runs = work[work["program_id"].notna()]
    if runs.empty:
        return standalone.drop(columns=["program_id"]).reset_index(drop=True)

    folded = runs.groupby("program_id", sort=False).agg(
        issuer_cik=("issuer_cik", "first"),
        symbol=("symbol", "first"),
        issuer_name=("issuer_name", "first"),
        owner_cik=("owner_cik", "first"),
        owner_name=("owner_name", "first"),
        title=("title", "first"),
        is_officer=("is_officer", "max"),
        is_director=("is_director", "max"),
        is_ten_pct=("is_ten_pct", "max"),
        is_other=("is_other", "max"),
        code=("code", "first"),
        ownership_form=("ownership_form", "first"),
        shares=("shares", "sum"),
        value_usd=("value_usd", "sum"),
        shares_after=("shares_after", "max"),
        # The decision is made at the start of the program; the fills follow.
        trans_date=("trans_date", "min"),
        filing_date=("filing_date", "max"),
        accession=("accession", "first"),
        aff10b5one=("aff10b5one", "first"),
        auto_vehicle=("auto_vehicle", "max"),
        doc_type=("doc_type", "first"),
        tranches=("tranches", "sum"),
        affiliate_dupes=("affiliate_dupes", "max"),
        program_legs=("value_usd", "size"),
    ).reset_index(drop=True)
    folded["price"] = np.where(folded["shares"] > 0, folded["value_usd"] / folded["shares"], np.nan)

    return pd.concat([standalone.drop(columns=["program_id"]), folded], ignore_index=True)


def economic_trades(raw: pd.DataFrame) -> pd.DataFrame:
    """Run all three collapses in order. Input is raw bulk rows."""
    return programs(affiliates(multi_tranche(raw)))
