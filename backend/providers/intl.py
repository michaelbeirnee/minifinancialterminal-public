"""International macro & FX providers: ECB, Frankfurter, World Bank, IMF, OECD.

Every endpoint here is free and key-less. Between them they cover the
cross-country half of the economy menu (GDP, CPI, unemployment, trade,
interest rates) plus euro-area reference rates and the ECB AAA yield curve.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from ..core.caching import TTL_DAILY, TTL_REFERENCE, cached
from ..core.errors import EmptyDataError, ProviderError
from ..core.http import get_csv, get_json

ECB_BASE = "https://data-api.ecb.europa.eu/service/data"
FRANKFURTER = "https://api.frankfurter.dev/v1"
WORLDBANK = "https://api.worldbank.org/v2"
IMF_DM = "https://www.imf.org/external/datamapper/api/v1"
OECD = "https://sdmx.oecd.org/public/rest/data"

# The IMF's CDN 403s anything claiming to be a browser; core.http swaps in the
# plain client string for imf.org, so we only need to ask for JSON here.
_IMF_HEADERS = {"Accept": "application/json"}


# --------------------------------------------------------------------------- #
# ECB (SDMX)
# --------------------------------------------------------------------------- #
@cached("ecb.sdmx", ttl=TTL_DAILY)
def ecb_series(
    dataflow: str,
    key: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    """Any ECB Data Portal series, e.g. ``ecb_series("EXR", "D.USD.EUR.SP00.A")``."""
    df = get_csv(
        "{}/{}/{}".format(ECB_BASE, dataflow.upper(), key),
        params={"format": "csvdata", "startPeriod": start_date, "endPeriod": end_date},
        ttl=TTL_DAILY,
    )
    if df.empty or "TIME_PERIOD" not in df.columns:
        raise EmptyDataError("ECB returned no data for {}/{}".format(dataflow, key))
    df["date"] = pd.to_datetime(df["TIME_PERIOD"], errors="coerce")
    df["value"] = pd.to_numeric(df["OBS_VALUE"], errors="coerce")
    keep = [c for c in ("date", "KEY", "value", "TITLE", "UNIT", "FREQ") if c in df.columns]
    out = df[keep].rename(columns={"KEY": "series", "TITLE": "title", "UNIT": "unit", "FREQ": "frequency"})
    return out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def ecb_reference_rates(
    currencies: str = "USD,GBP,JPY,CHF,CAD,AUD,CNY",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    """ECB daily euro foreign-exchange reference rates (units of X per EUR)."""
    codes = "+".join(c.strip().upper() for c in currencies.split(",") if c.strip())
    df = ecb_series("EXR", "D.{}.EUR.SP00.A".format(codes), start_date, end_date)
    df["currency"] = df["series"].str.split(".").str[1]
    return df[["date", "currency", "value"]].rename(columns={"value": "rate"})


# The euro-area AAA-rated government bond spot curve.
_ECB_CURVE_MATURITIES = [
    ("3M", 0.25), ("6M", 0.5), ("1Y", 1.0), ("2Y", 2.0), ("3Y", 3.0), ("5Y", 5.0),
    ("7Y", 7.0), ("10Y", 10.0), ("15Y", 15.0), ("20Y", 20.0), ("30Y", 30.0),
]


def ecb_yield_curve(as_of: Optional[str] = None) -> pd.DataFrame:
    """Euro-area AAA government bond spot yield curve."""
    key = "B.U2.EUR.4F.G_N_A.SV_C_YM.{}".format(
        "+".join("SR_{}".format(m) for m, _ in _ECB_CURVE_MATURITIES)
    )
    start = as_of or str(pd.Timestamp("today").date() - pd.Timedelta(21, unit="D"))
    df = ecb_series("YC", key, start_date=start, end_date=as_of)
    df["maturity"] = df["series"].str.rsplit(".", n=1).str[-1].str.replace("SR_", "", regex=False)
    years = dict(_ECB_CURVE_MATURITIES)
    df["maturity_years"] = df["maturity"].map(years)
    latest = df[df["date"] == df["date"].max()]
    if latest.empty:
        raise EmptyDataError("ECB published no yield curve on or before {}".format(as_of or "today"))
    return (
        latest[["date", "maturity", "maturity_years", "value"]]
        .rename(columns={"value": "rate"})
        .sort_values("maturity_years")
        .reset_index(drop=True)
    )


# --------------------------------------------------------------------------- #
# Frankfurter (ECB rates, friendlier shape)
# --------------------------------------------------------------------------- #
def fx_latest(base: str = "USD", symbols: Optional[str] = None) -> pd.DataFrame:
    payload = get_json(
        FRANKFURTER + "/latest",
        params={"base": base.upper(), "symbols": symbols.upper() if symbols else None},
        ttl=3600,
    )
    rates = payload.get("rates") or {}
    if not rates:
        raise EmptyDataError("No FX rates returned for base {}".format(base))
    return pd.DataFrame(
        [{"date": payload.get("date"), "base": payload.get("base"), "currency": k, "rate": v}
         for k, v in sorted(rates.items())]
    )


def fx_history(
    base: str = "USD",
    symbols: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    start = start_date or str((pd.Timestamp("today") - pd.DateOffset(years=1)).date())
    path = "{}..{}".format(start, end_date or "")
    payload = get_json(
        "{}/{}".format(FRANKFURTER, path),
        params={"base": base.upper(), "symbols": symbols.upper() if symbols else None},
        ttl=TTL_DAILY,
    )
    rates = payload.get("rates") or {}
    if not rates:
        raise EmptyDataError("No FX history for base {}".format(base))
    rows = [dict(date=d, **vals) for d, vals in sorted(rates.items())]
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()


@cached("fx.currencies", ttl=TTL_REFERENCE)
def fx_currencies() -> pd.DataFrame:
    payload = get_json(FRANKFURTER + "/currencies", ttl=TTL_REFERENCE)
    return pd.DataFrame([{"code": k, "name": v} for k, v in sorted(payload.items())])


# --------------------------------------------------------------------------- #
# World Bank
# --------------------------------------------------------------------------- #
WB_INDICATORS: Dict[str, str] = {
    "gdp_nominal": "NY.GDP.MKTP.CD",
    "gdp_real": "NY.GDP.MKTP.KD",
    "gdp_growth": "NY.GDP.MKTP.KD.ZG",
    "gdp_per_capita": "NY.GDP.PCAP.CD",
    "cpi": "FP.CPI.TOTL",
    "inflation": "FP.CPI.TOTL.ZG",
    "unemployment": "SL.UEM.TOTL.ZS",
    "population": "SP.POP.TOTL",
    "current_account": "BN.CAB.XOKA.GD.ZS",
    "government_debt": "GC.DOD.TOTL.GD.ZS",
    "exports": "NE.EXP.GNFS.CD",
    "imports": "NE.IMP.GNFS.CD",
    "trade_balance": "NE.RSB.GNFS.CD",
    "fdi": "BX.KLT.DINV.CD.WD",
    "interest_rate": "FR.INR.RINR",
    "labour_force": "SL.TLF.TOTL.IN",
    "life_expectancy": "SP.DYN.LE00.IN",
    "reserves": "FI.RES.TOTL.CD",
}


@cached("worldbank.series", ttl=TTL_REFERENCE)
def worldbank(
    indicator: str,
    country: str = "USA",
    start_year: Optional[int] = None,
    end_year: Optional[int] = None,
) -> pd.DataFrame:
    """World Bank indicator series. ``indicator`` may be a friendly alias."""
    code = WB_INDICATORS.get(indicator.lower(), indicator)
    countries = ";".join(c.strip().upper() for c in country.split(",") if c.strip())
    params: Dict[str, Any] = {"format": "json", "per_page": 5000}
    if start_year or end_year:
        params["date"] = "{}:{}".format(start_year or 1960, end_year or pd.Timestamp("today").year)
    payload = get_json(
        "{}/country/{}/indicator/{}".format(WORLDBANK, countries, code), params=params, ttl=TTL_REFERENCE
    )
    if not isinstance(payload, list) or len(payload) < 2 or not payload[1]:
        raise EmptyDataError("World Bank has no {} data for {}".format(indicator, country))
    rows = [
        {
            "date": r["date"],
            "country": (r.get("country") or {}).get("value"),
            "iso3": (r.get("countryiso3code") or ""),
            "indicator": (r.get("indicator") or {}).get("value"),
            "value": r.get("value"),
        }
        for r in payload[1]
    ]
    df = pd.DataFrame(rows).dropna(subset=["value"])
    if df.empty:
        raise EmptyDataError("World Bank returned only nulls for {} / {}".format(indicator, country))
    df["date"] = pd.to_datetime(df["date"], format="%Y", errors="coerce")
    return df.sort_values("date").reset_index(drop=True)


@cached("worldbank.countries", ttl=TTL_REFERENCE)
def worldbank_countries() -> pd.DataFrame:
    payload = get_json(WORLDBANK + "/country", params={"format": "json", "per_page": 400}, ttl=TTL_REFERENCE)
    rows = [
        {
            "iso3": c.get("id"),
            "iso2": c.get("iso2Code"),
            "name": c.get("name"),
            "region": (c.get("region") or {}).get("value"),
            "income_level": (c.get("incomeLevel") or {}).get("value"),
            "capital": c.get("capitalCity"),
        }
        for c in payload[1]
    ]
    return pd.DataFrame(rows)


def country_iso3(name: str) -> str:
    """Resolve ``"united states"`` / ``"US"`` / ``"USA"`` to an ISO-3 code."""
    q = name.strip().lower().replace("_", " ")
    if len(q) == 3 and q.isalpha():
        return q.upper()
    df = worldbank_countries()
    exact = df[df["name"].str.lower() == q]
    if not exact.empty:
        return str(exact.iloc[0]["iso3"])
    if len(q) == 2:
        hit = df[df["iso2"].str.lower() == q]
        if not hit.empty:
            return str(hit.iloc[0]["iso3"])
    partial = df[df["name"].str.lower().str.contains(q, regex=False, na=False)]
    if partial.empty:
        raise EmptyDataError("No country matching {!r}".format(name))
    return str(partial.iloc[0]["iso3"])


# --------------------------------------------------------------------------- #
# IMF DataMapper (World Economic Outlook)
# --------------------------------------------------------------------------- #
IMF_INDICATORS: Dict[str, str] = {
    "gdp_growth": "NGDP_RPCH",
    "gdp_nominal": "NGDPD",
    "gdp_per_capita": "NGDPDPC",
    "gdp_ppp_share": "PPPSH",
    "inflation": "PCPIPCH",
    "unemployment": "LUR",
    "current_account": "BCA_NGDPD",
    "government_debt": "GGXWDG_NGDP",
    "government_balance": "GGXCNL_NGDP",
    "government_revenue": "GGR_NGDP",
    "government_expenditure": "GGX_NGDP",
    "population": "LP",
}


@cached("imf.datamapper", ttl=TTL_REFERENCE)
def imf(indicator: str = "gdp_growth", country: str = "USA") -> pd.DataFrame:
    """IMF World Economic Outlook series, including forward projections."""
    code = IMF_INDICATORS.get(indicator.lower(), indicator.upper())
    countries = "/".join(c.strip().upper() for c in country.split(",") if c.strip())
    payload = get_json(
        "{}/{}/{}".format(IMF_DM, code, countries), headers=_IMF_HEADERS, ttl=TTL_REFERENCE
    )
    values = (payload.get("values") or {}).get(code) or {}
    if not values:
        raise EmptyDataError("IMF has no {} data for {}".format(indicator, country))
    rows = [
        {"date": pd.to_datetime(year, format="%Y", errors="coerce"), "country": iso,
         "indicator": code, "value": val}
        for iso, series in values.items()
        for year, val in series.items()
    ]
    return pd.DataFrame(rows).dropna(subset=["date"]).sort_values(["country", "date"]).reset_index(drop=True)


@cached("imf.indicators", ttl=TTL_REFERENCE)
def imf_indicators() -> pd.DataFrame:
    payload = get_json(IMF_DM + "/indicators", headers=_IMF_HEADERS, ttl=TTL_REFERENCE)
    rows = [
        {"code": k, "label": v.get("label"), "unit": v.get("unit"), "source": v.get("source")}
        for k, v in (payload.get("indicators") or {}).items()
    ]
    if not rows:
        raise EmptyDataError("IMF indicator catalogue was empty")
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# OECD (SDMX) — generic passthrough
# --------------------------------------------------------------------------- #
def oecd_dataset(
    dataflow: str,
    key: str = "all",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    """Any OECD SDMX dataflow, e.g. ``dataflow="OECD.SDD.STES,DSD_STES@DF_CLI,"``.

    OECD's dataflow identifiers are long and versioned; browse them at
    https://data-explorer.oecd.org and paste the agency/dataflow string here.
    """
    df = get_csv(
        "{}/{}/{}".format(OECD, dataflow, key),
        params={"format": "csvfilewithlabels", "startPeriod": start_date, "endPeriod": end_date},
        ttl=TTL_REFERENCE,
    )
    if df.empty:
        raise EmptyDataError("OECD returned no rows for {}/{}".format(dataflow, key))
    df.columns = [str(c).strip() for c in df.columns]
    if "TIME_PERIOD" in df.columns:
        df["date"] = pd.to_datetime(df["TIME_PERIOD"], errors="coerce")
    if "OBS_VALUE" in df.columns:
        df["value"] = pd.to_numeric(df["OBS_VALUE"], errors="coerce")
    return df
