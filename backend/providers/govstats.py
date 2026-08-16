"""US statistical-agency providers: EIA (energy) and BLS (labour & prices).

BLS v1 works with no registration at all; a free key raises the daily limit and
unlocks v2 extras. EIA requires a free key — the commands that use it say so
and point at the key-less FRED equivalents where one exists.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import pandas as pd

from ..config import settings
from ..core.caching import TTL_DAILY, TTL_REFERENCE, cached
from ..core.errors import EmptyDataError, MissingCredentialError, ProviderError
from ..core.http import get_json

EIA_BASE = "https://api.eia.gov/v2"
BLS_V1 = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
BLS_V2 = "https://api.bls.gov/publicAPI/v2/timeseries/data/"


# --------------------------------------------------------------------------- #
# EIA
# --------------------------------------------------------------------------- #
def _eia_key() -> str:
    if not settings.eia_api_key:
        raise MissingCredentialError(
            "EIA data needs a free API key: register at "
            "https://www.eia.gov/opendata/register.php and set MFT_EIA_API_KEY. "
            "Key-free crude/gas/nat-gas prices are available via the FRED provider."
        )
    return settings.eia_api_key


@cached("eia.series", ttl=TTL_DAILY)
def eia_series(
    route: str,
    facets: Optional[Dict[str, str]] = None,
    frequency: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 5000,
) -> pd.DataFrame:
    """Any EIA v2 data route, e.g. ``petroleum/sum/sndw``."""
    params: Dict[str, Any] = {
        "api_key": _eia_key(),
        "data[0]": "value",
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": min(limit, 5000),
        "frequency": frequency,
        "start": start_date,
        "end": end_date,
    }
    for k, v in (facets or {}).items():
        params["facets[{}][]".format(k)] = v
    payload = get_json("{}/{}/data/".format(EIA_BASE, route.strip("/")), params=params, ttl=TTL_DAILY)
    rows = (payload.get("response") or {}).get("data") or []
    if not rows:
        raise EmptyDataError("EIA returned no data for route {}".format(route))
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["period"], errors="coerce", format="mixed")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.sort_values("date").reset_index(drop=True)


def petroleum_status(limit: int = 500) -> pd.DataFrame:
    """Weekly Petroleum Status Report: US crude & product stocks."""
    return eia_series("petroleum/stoc/wstk", frequency="weekly", limit=limit)


def short_term_energy_outlook(limit: int = 2000) -> pd.DataFrame:
    """Short-Term Energy Outlook (STEO) monthly projections."""
    return eia_series("steo", frequency="monthly", limit=limit)


def natural_gas_storage(limit: int = 500) -> pd.DataFrame:
    """Weekly working gas in underground storage."""
    return eia_series("natural-gas/stor/wkly", frequency="weekly", limit=limit)


# --------------------------------------------------------------------------- #
# BLS
# --------------------------------------------------------------------------- #
BLS_SERIES: Dict[str, str] = {
    "cpi_all_urban": "CUUR0000SA0",
    "cpi_core": "CUUR0000SA0L1E",
    "ppi_final_demand": "WPUFD4",
    "unemployment_rate": "LNS14000000",
    "nonfarm_payrolls": "CES0000000001",
    "labour_force_participation": "LNS11300000",
    "average_hourly_earnings": "CES0500000003",
    "employment_cost_index": "CIU1010000000000A",
    "job_openings": "JTS000000000000000JOL",
    "productivity": "PRS85006092",
    "import_price_index": "EIUIR",
    "export_price_index": "EIUIQ",
}


@cached("bls.series", ttl=TTL_DAILY)
def bls_series(
    series_id: str,
    start_year: Optional[int] = None,
    end_year: Optional[int] = None,
) -> pd.DataFrame:
    """BLS time series (comma-separated ids or friendly aliases)."""
    ids = [BLS_SERIES.get(s.strip().lower(), s.strip().upper())
           for s in series_id.split(",") if s.strip()]
    end = end_year or date.today().year
    start = start_year or (end - 10)
    body: Dict[str, Any] = {"seriesid": ids, "startyear": str(start), "endyear": str(end)}
    url = BLS_V1
    if settings.bls_api_key:
        body["registrationkey"] = settings.bls_api_key
        body["calculations"] = True
        url = BLS_V2

    payload = get_json(url, method="POST", json_body=body,
                       headers={"Content-Type": "application/json"}, ttl=TTL_DAILY)
    if payload.get("status") != "REQUEST_SUCCEEDED":
        raise ProviderError("BLS request failed: {}".format("; ".join(payload.get("message") or [])))
    rows: List[Dict[str, Any]] = []
    for series in (payload.get("Results") or {}).get("series") or []:
        sid = series.get("seriesID")
        for item in series.get("data") or []:
            period = item.get("period", "")
            if period.startswith("M") and period != "M13":
                stamp = "{}-{}-01".format(item["year"], period[1:])
            elif period.startswith("Q"):
                stamp = "{}-{:02d}-01".format(item["year"], (int(period[1:]) - 1) * 3 + 1)
            else:
                stamp = "{}-01-01".format(item["year"])
            rows.append(
                {
                    "date": pd.to_datetime(stamp, errors="coerce"),
                    "series_id": sid,
                    "value": pd.to_numeric(item.get("value"), errors="coerce"),
                    "period_name": item.get("periodName"),
                }
            )
    if not rows:
        raise EmptyDataError("BLS returned no observations for {}".format(series_id))
    return pd.DataFrame(rows).dropna(subset=["date"]).sort_values(["series_id", "date"]).reset_index(drop=True)


def bls_catalogue() -> pd.DataFrame:
    return pd.DataFrame([{"alias": k, "series_id": v} for k, v in sorted(BLS_SERIES.items())])
