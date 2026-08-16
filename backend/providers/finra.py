"""FINRA and CFTC providers — short-sale volume, ATS/dark-pool volume, COT.

Both regulators publish these as open files/APIs with no key.
"""
from __future__ import annotations

import io
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from ..core.caching import TTL_DAILY, TTL_REFERENCE, cached
from ..core.errors import EmptyDataError, ProviderError
from ..core.http import fetch, get_json

NAME = "finra"

REGSHO = "https://cdn.finra.org/equity/regsho/daily/CNMS{kind}{day}.txt"
FINRA_API = "https://api.finra.org/data/group/otcMarket/name"
CFTC = "https://publicreporting.cftc.gov/resource"

# CFTC Commitments of Traders report variants (Socrata dataset ids).
COT_REPORTS: Dict[str, str] = {
    "legacy": "6dca-aqww",
    "legacy_combined": "jun7-fc8e",
    "disaggregated": "72hh-3qpy",
    "disaggregated_combined": "kh3c-gbw2",
    "financial": "gpe5-46if",
    "financial_combined": "yw9f-hn96",
    "supplemental": "4zgm-a668",
}


# --------------------------------------------------------------------------- #
# Reg SHO daily short-sale volume
# --------------------------------------------------------------------------- #
@cached("finra.regsho", ttl=TTL_REFERENCE)
def _regsho_day(day: date) -> pd.DataFrame:
    body = fetch(REGSHO.format(kind="shvol", day=day.strftime("%Y%m%d")), ttl=TTL_REFERENCE)
    df = pd.read_csv(io.BytesIO(body), sep="|")
    df = df[df["Date"].astype(str).str.isdigit()] if "Date" in df.columns else df
    df.columns = [str(c).strip().lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
    for col in ("shortvolume", "shortexemptvolume", "totalvolume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.rename(
        columns={"shortvolume": "short_volume", "shortexemptvolume": "short_exempt_volume",
                 "totalvolume": "total_volume"}
    )


def short_volume(symbol: str, days: int = 60) -> pd.DataFrame:
    """Consolidated daily short-sale volume for a ticker (FINRA Reg SHO)."""
    symbol = symbol.upper()
    rows: List[pd.DataFrame] = []
    day = date.today()
    checked = 0
    while len(rows) < days and checked < days * 2 + 10:
        checked += 1
        day -= timedelta(days=1)
        if day.weekday() >= 5:
            continue
        try:
            frame = _regsho_day(day)
        except Exception:  # noqa: BLE001 - holidays simply have no file
            continue
        hit = frame[frame["symbol"] == symbol]
        if not hit.empty:
            rows.append(hit)
    if not rows:
        raise EmptyDataError("No FINRA short-volume records for {} in the last {} sessions".format(symbol, days))
    out = pd.concat(rows, ignore_index=True).sort_values("date")
    out["short_volume_percent"] = (out["short_volume"] / out["total_volume"]).round(6)
    return out.reset_index(drop=True)


def short_volume_by_day(day: Optional[str] = None, limit: int = 500) -> pd.DataFrame:
    """The whole consolidated short-volume tape for one session."""
    target = pd.Timestamp(day).date() if day else date.today() - timedelta(days=1)
    for back in range(0, 8):
        candidate = target - timedelta(days=back)
        if candidate.weekday() >= 5:
            continue
        try:
            df = _regsho_day(candidate)
        except Exception:  # noqa: BLE001
            continue
        df["short_volume_percent"] = (df["short_volume"] / df["total_volume"]).round(6)
        return df.sort_values("total_volume", ascending=False).head(limit).reset_index(drop=True)
    raise EmptyDataError("No FINRA short-volume file published near {}".format(target))


# --------------------------------------------------------------------------- #
# OTC / ATS (dark pool) transparency
# --------------------------------------------------------------------------- #
@cached("finra.otc", ttl=TTL_DAILY)
def otc_weekly(
    summary_type: str = "ATS_W_SMBL",
    symbol: Optional[str] = None,
    limit: int = 500,
) -> pd.DataFrame:
    """FINRA OTC transparency weekly volumes.

    ``summary_type``: ``ATS_W_SMBL`` (per-symbol ATS/dark-pool volume),
    ``OTC_W_SMBL`` (per-symbol non-ATS OTC), ``ATS_W_FIRM``/``OTC_W_FIRM``
    (per-venue totals) or ``ATS_W_VOL_STATS`` (market-wide).
    """
    filters: List[Dict[str, Any]] = [
        {"compareType": "EQUAL", "fieldName": "summaryTypeCode", "fieldValue": summary_type}
    ]
    if symbol:
        filters.append(
            {"compareType": "EQUAL", "fieldName": "issueSymbolIdentifier", "fieldValue": symbol.upper()}
        )
    body = fetch(
        "{}/weeklySummary".format(FINRA_API),
        method="POST",
        json_body={"limit": min(limit, 5000), "compareFilters": filters},
        headers={"Content-Type": "application/json"},
        ttl=TTL_DAILY,
    )
    df = pd.read_csv(io.BytesIO(body))
    if df.empty:
        raise EmptyDataError("FINRA OTC transparency returned no rows for {}".format(summary_type))
    df = df.rename(
        columns={
            "issueSymbolIdentifier": "symbol", "issueName": "name", "MPID": "mpid",
            "marketParticipantName": "venue", "tierDescription": "tier",
            "totalWeeklyTradeCount": "trade_count", "totalWeeklyShareQuantity": "share_quantity",
            "totalNotionalSum": "notional", "weekStartDate": "week_start",
            "summaryTypeCode": "summary_type", "lastUpdateDate": "last_updated",
        }
    )
    if "week_start" in df.columns:
        df["week_start"] = pd.to_datetime(df["week_start"], errors="coerce")
        df = df.sort_values("week_start", ascending=False)
    keep = ["week_start", "symbol", "name", "venue", "mpid", "tier", "trade_count",
            "share_quantity", "notional", "summary_type"]
    return df[[c for c in keep if c in df.columns]].head(limit).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# CFTC Commitments of Traders
# --------------------------------------------------------------------------- #
def cot(
    market: Optional[str] = None,
    report: str = "legacy",
    start_date: Optional[str] = None,
    limit: int = 500,
) -> pd.DataFrame:
    """Commitments of Traders positioning for a futures market."""
    dataset = COT_REPORTS.get(report.lower())
    if not dataset:
        raise ValueError("report must be one of {}".format(", ".join(sorted(COT_REPORTS))))
    where: List[str] = []
    if start_date:
        where.append("report_date_as_yyyy_mm_dd >= '{}'".format(start_date))
    if market:
        safe = market.replace("'", "''").upper()
        where.append("upper(market_and_exchange_names) like '%{}%'".format(safe))
    params = {"$limit": min(limit, 50000), "$order": "report_date_as_yyyy_mm_dd DESC"}
    if where:
        params["$where"] = " AND ".join(where)

    rows = get_json("{}/{}.json".format(CFTC, dataset), params=params, ttl=TTL_DAILY)
    if not rows:
        raise EmptyDataError("CFTC returned no {} COT rows for {!r}".format(report, market))
    df = pd.DataFrame(rows)
    df["report_date"] = pd.to_datetime(df["report_date_as_yyyy_mm_dd"], errors="coerce")
    numeric = [c for c in df.columns if any(k in c for k in ("positions", "open_interest", "traders",
                                                             "pct_of_oi", "change_in"))]
    for col in numeric:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    front = ["report_date", "market_and_exchange_names", "commodity_name", "open_interest_all"]
    ordered = [c for c in front if c in df.columns] + [c for c in df.columns if c not in front]
    return df[ordered].drop(columns=["report_date_as_yyyy_mm_dd"], errors="ignore").head(limit)


@cached("cftc.markets", ttl=TTL_REFERENCE)
def cot_markets(report: str = "legacy", limit: int = 5000) -> pd.DataFrame:
    """The distinct market names available in a COT report."""
    dataset = COT_REPORTS.get(report.lower())
    if not dataset:
        raise ValueError("report must be one of {}".format(", ".join(sorted(COT_REPORTS))))
    rows = get_json(
        "{}/{}.json".format(CFTC, dataset),
        params={"$select": "market_and_exchange_names,commodity_name,cftc_contract_market_code",
                "$group": "market_and_exchange_names,commodity_name,cftc_contract_market_code",
                "$limit": limit},
        ttl=TTL_REFERENCE,
    )
    if not rows:
        raise EmptyDataError("CFTC returned no market list for the {} report".format(report))
    return pd.DataFrame(rows).sort_values("market_and_exchange_names").reset_index(drop=True)
