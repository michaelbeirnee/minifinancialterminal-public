"""US Treasury, TreasuryDirect and New York Fed providers.

All three publish open data with no key: the daily par yield curves, the Fiscal
Data API (debt, average interest rates, receipts), auction results, and the NY
Fed's reference rates (SOFR, EFFR, OBFR, BGCR, TGCR).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import pandas as pd

from ..core.caching import TTL_DAILY, TTL_REFERENCE, cached
from ..core.errors import EmptyDataError, ProviderError
from ..core.http import get_csv, get_json

NAME = "treasury"

RATES_CSV = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all"
FISCAL = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
DIRECT = "https://www.treasurydirect.gov/TA_WS"
NYFED = "https://markets.newyorkfed.org/api"

CURVE_TYPES = {
    "nominal": "daily_treasury_yield_curve",
    "bill": "daily_treasury_bill_rates",
    "real": "daily_treasury_real_yield_curve",
    "long_term": "daily_treasury_long_term_rate",
    "real_long_term": "daily_treasury_real_long_term",
}

# Column header -> maturity in years, used to plot/sort the curve.
_MATURITY_YEARS = {
    "1 mo": 1 / 12, "1.5 month": 0.125, "2 mo": 2 / 12, "3 mo": 0.25, "4 mo": 4 / 12,
    "6 mo": 0.5, "1 yr": 1.0, "2 yr": 2.0, "3 yr": 3.0, "5 yr": 5.0, "7 yr": 7.0,
    "10 yr": 10.0, "20 yr": 20.0, "30 yr": 30.0,
    "5 yr": 5.0, "5.0 yr": 5.0, "7.0 yr": 7.0, "10.0 yr": 10.0, "20.0 yr": 20.0, "30.0 yr": 30.0,
}


@cached("treasury.rates_year", ttl=TTL_DAILY)
def _rates_for_year(year: int, curve: str) -> pd.DataFrame:
    df = get_csv(
        RATES_CSV.format(year=year),
        params={"type": CURVE_TYPES[curve], "field_tdr_date_value": year, "_format": "csv"},
        ttl=TTL_DAILY,
    )
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.rename(columns={"date": "date"})
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df.dropna(subset=["date"]).set_index("date").sort_index()


def rates(start_date: Optional[str] = None, end_date: Optional[str] = None,
          curve: str = "nominal") -> pd.DataFrame:
    """Daily Treasury par yield curve rates, one column per maturity."""
    if curve not in CURVE_TYPES:
        raise ValueError("curve must be one of {}".format(", ".join(CURVE_TYPES)))
    end = pd.Timestamp(end_date) if end_date else pd.Timestamp(date.today())
    start = pd.Timestamp(start_date) if start_date else end - pd.DateOffset(years=1)
    frames: List[pd.DataFrame] = []
    errors: List[str] = []
    for year in range(start.year, end.year + 1):
        try:
            frames.append(_rates_for_year(year, curve))
        except Exception as exc:  # noqa: BLE001 - a missing year should not kill the range
            errors.append("{}: {}".format(year, exc))
    if not frames:
        raise ProviderError("Treasury rates unavailable. {}".format("; ".join(errors)))
    df = pd.concat(frames).sort_index()
    df = df.loc[str(start.date()) : str(end.date())]  # noqa: E203
    if df.empty:
        raise EmptyDataError("No Treasury rates published between {} and {}".format(start.date(), end.date()))
    return df


def yield_curve(as_of: Optional[str] = None, curve: str = "nominal") -> pd.DataFrame:
    """The curve on one date, as (maturity, rate) rows ready to plot."""
    end = pd.Timestamp(as_of) if as_of else pd.Timestamp(date.today())
    df = rates(start_date=str((end - pd.DateOffset(days=14)).date()), end_date=str(end.date()), curve=curve)
    row = df.iloc[-1]
    out = []
    for label, value in row.items():
        years = _MATURITY_YEARS.get(str(label).strip().lower())
        if years is None or pd.isna(value):
            continue
        out.append({"date": df.index[-1], "maturity": label, "maturity_years": years, "rate": float(value)})
    if not out:
        raise EmptyDataError("No Treasury curve published on or before {}".format(end.date()))
    return pd.DataFrame(out).sort_values("maturity_years").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Fiscal Data API
# --------------------------------------------------------------------------- #
def fiscal_dataset(
    endpoint: str,
    fields: Optional[str] = None,
    filters: Optional[str] = None,
    sort: str = "-record_date",
    limit: int = 500,
) -> pd.DataFrame:
    """Generic reader for any Fiscal Data endpoint (no key required)."""
    params = {"format": "json", "page[size]": min(limit, 10000), "sort": sort}
    if fields:
        params["fields"] = fields
    if filters:
        params["filter"] = filters
    payload = get_json("{}/{}".format(FISCAL, endpoint.strip("/")), params=params, ttl=TTL_DAILY)
    rows = payload.get("data") or []
    if not rows:
        raise EmptyDataError("Fiscal Data returned no rows for {}".format(endpoint))
    df = pd.DataFrame(rows)
    for col in df.columns:
        if col.endswith("_date"):
            df[col] = pd.to_datetime(df[col], errors="coerce")
        elif col not in ("security_type_desc", "security_desc", "classification_desc"):
            converted = pd.to_numeric(df[col], errors="coerce")
            if converted.notna().any():
                df[col] = converted
    return df


def debt_to_penny(start_date: Optional[str] = None, limit: int = 500) -> pd.DataFrame:
    """Total US public debt outstanding, daily."""
    filters = "record_date:gte:{}".format(start_date) if start_date else None
    df = fiscal_dataset("v2/accounting/od/debt_to_penny", filters=filters, limit=limit)
    return df.set_index("record_date").sort_index()


def average_interest_rates(start_date: Optional[str] = None, limit: int = 1000) -> pd.DataFrame:
    """Average interest rate the Treasury pays by security type."""
    filters = "record_date:gte:{}".format(start_date) if start_date else None
    return fiscal_dataset("v2/accounting/od/avg_interest_rates", filters=filters, limit=limit)


def treasury_auctions(security_type: Optional[str] = None, limit: int = 100) -> pd.DataFrame:
    """Recently auctioned Treasury securities from TreasuryDirect."""
    params: Dict[str, Any] = {"format": "json", "pagesize": min(limit, 250)}
    if security_type:
        params["type"] = security_type.title()
    payload = get_json(DIRECT + "/securities/auctioned", params=params, ttl=TTL_DAILY)
    if not payload:
        raise EmptyDataError("TreasuryDirect returned no auction results")
    df = pd.DataFrame(payload)
    keep = ["cusip", "securityType", "securityTerm", "auctionDate", "issueDate", "maturityDate",
            "highYield", "interestRate", "pricePer100", "offeringAmount", "totalTendered",
            "totalAccepted", "bidToCoverRatio"]
    df = df[[c for c in keep if c in df.columns]]
    for col in ("auctionDate", "issueDate", "maturityDate"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    df.columns = [_snake(c) for c in df.columns]
    return df.head(limit)


def treasury_prices(as_of: Optional[str] = None, limit: int = 500) -> pd.DataFrame:
    """End-of-day prices for outstanding marketable Treasury securities."""
    day = as_of or str(date.today())
    df = fiscal_dataset(
        "v1/accounting/od/securities_sales",
        filters="record_date:lte:{}".format(day),
        limit=limit,
    )
    return df


def _snake(name: str) -> str:
    out = []
    for i, ch in enumerate(str(name)):
        if ch.isupper() and i:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


# --------------------------------------------------------------------------- #
# New York Fed reference rates
# --------------------------------------------------------------------------- #
SECURED = {"sofr", "sofrai", "bgcr", "tgcr"}
UNSECURED = {"effr", "obfr"}


def reference_rate(
    rate: str = "sofr",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 250,
) -> pd.DataFrame:
    """NY Fed published reference rate history."""
    rate = rate.lower().strip()
    if rate in SECURED:
        group = "secured"
    elif rate in UNSECURED:
        group = "unsecured"
    else:
        raise ValueError("rate must be one of {}".format(", ".join(sorted(SECURED | UNSECURED))))

    if start_date or end_date:
        url = "{}/rates/{}/{}/search.json".format(NYFED, group, rate)
        params = {"startDate": start_date, "endDate": end_date}
    else:
        url = "{}/rates/{}/{}/last/{}.json".format(NYFED, group, rate, min(limit, 5000))
        params = {}
    payload = get_json(url, params=params, ttl=TTL_DAILY)
    rows = payload.get("refRates") or []
    if not rows:
        raise EmptyDataError("NY Fed published no {} observations for that window".format(rate.upper()))
    df = pd.DataFrame(rows)
    df["effectiveDate"] = pd.to_datetime(df["effectiveDate"], errors="coerce")
    df.columns = [_snake(c) for c in df.columns]
    return df.rename(columns={"effective_date": "date"}).set_index("date").sort_index().tail(limit)


def all_reference_rates() -> pd.DataFrame:
    """Latest print for every NY Fed reference rate."""
    payload = get_json(NYFED + "/rates/all/latest.json", ttl=3600)
    rows = payload.get("refRates") or []
    if not rows:
        raise EmptyDataError("NY Fed returned no reference rates")
    df = pd.DataFrame(rows)
    df["effectiveDate"] = pd.to_datetime(df["effectiveDate"], errors="coerce")
    df.columns = [_snake(c) for c in df.columns]
    return df
