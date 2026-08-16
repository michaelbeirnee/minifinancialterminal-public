"""FRED / ALFRED provider (Federal Reserve Bank of St. Louis).

FRED's ``fredgraph.csv`` download works without an API key, so every series
observation in this file is available on a bare install. A free API key
(``MFT_FRED_API_KEY``) additionally unlocks server-side series search and
metadata; without one we fall back to searching the curated catalogue below.
"""
from __future__ import annotations

import io
import zipfile
from typing import Any, Dict, List, Optional

import pandas as pd

from ..config import settings
from ..core.caching import TTL_DAILY, TTL_REFERENCE, cached
from ..core.errors import EmptyDataError, MissingCredentialError, ProviderError
from ..core.http import fetch, get_json

NAME = "fred"

GRAPH = "https://fred.stlouisfed.org/graph/fredgraph.csv"
API = "https://api.stlouisfed.org/fred"

# Curated map of the series the terminal itself references, so that search and
# the macro commands work with no key configured.
CATALOGUE: Dict[str, str] = {
    # Rates & curve
    "DFF": "Federal Funds Effective Rate (daily)",
    "EFFR": "Effective Federal Funds Rate",
    "IORB": "Interest Rate on Reserve Balances",
    "SOFR": "Secured Overnight Financing Rate",
    "OBFR": "Overnight Bank Funding Rate",
    "BGCR": "Broad General Collateral Rate",
    "TGCR": "Tri-Party General Collateral Rate",
    "DPCREDIT": "Discount Window Primary Credit Rate",
    "AMERIBOR": "American Interbank Offered Rate",
    "SONIA": "SONIA Interest Rate Benchmark (UK)",
    "ECBESTRVOLWGTTRMDMNRT": "Euro Short-Term Rate (€STR)",
    "DGS1MO": "1-Month Treasury Constant Maturity Rate",
    "DGS3MO": "3-Month Treasury Constant Maturity Rate",
    "DGS6MO": "6-Month Treasury Constant Maturity Rate",
    "DGS1": "1-Year Treasury Constant Maturity Rate",
    "DGS2": "2-Year Treasury Constant Maturity Rate",
    "DGS3": "3-Year Treasury Constant Maturity Rate",
    "DGS5": "5-Year Treasury Constant Maturity Rate",
    "DGS7": "7-Year Treasury Constant Maturity Rate",
    "DGS10": "10-Year Treasury Constant Maturity Rate",
    "DGS20": "20-Year Treasury Constant Maturity Rate",
    "DGS30": "30-Year Treasury Constant Maturity Rate",
    "T10Y2Y": "10-Year minus 2-Year Treasury Spread",
    "T10Y3M": "10-Year minus 3-Month Treasury Spread",
    "T10YIE": "10-Year Breakeven Inflation Rate",
    "T5YIFR": "5-Year 5-Year Forward Inflation Expectation",
    "DFII10": "10-Year TIPS Real Yield",
    "MORTGAGE30US": "30-Year Fixed Rate Mortgage Average",
    "MORTGAGE15US": "15-Year Fixed Rate Mortgage Average",
    "DPRIME": "Bank Prime Loan Rate",
    # Corporate credit
    "BAMLC0A0CM": "ICE BofA US Corporate Index OAS",
    "BAMLH0A0HYM2": "ICE BofA US High Yield Index OAS",
    "BAMLC0A1CAAAEY": "ICE BofA AAA US Corporate Effective Yield",
    "BAMLC0A4CBBBEY": "ICE BofA BBB US Corporate Effective Yield",
    "BAMLH0A3HYCEY": "ICE BofA CCC & Lower US High Yield Effective Yield",
    "AAA": "Moody's Seasoned Aaa Corporate Bond Yield",
    "BAA": "Moody's Seasoned Baa Corporate Bond Yield",
    "BAA10Y": "Moody's Baa Corporate Bond minus 10-Year Treasury",
    "DCPN30": "30-Day AA Nonfinancial Commercial Paper Rate",
    "DCPF3M": "3-Month AA Financial Commercial Paper Rate",
    "HQMCB10YR": "High Quality Market Corporate Bond Spot Rate, 10-Year",
    "HQMCB30YR": "High Quality Market Corporate Bond Spot Rate, 30-Year",
    # Inflation & prices
    "CPIAUCSL": "CPI for All Urban Consumers: All Items (SA)",
    "CPIAUCNS": "CPI for All Urban Consumers: All Items (NSA)",
    "CPILFESL": "CPI: All Items Less Food and Energy (Core CPI)",
    "PCEPI": "PCE Price Index",
    "PCEPILFE": "Core PCE Price Index",
    "PPIACO": "Producer Price Index: All Commodities",
    "MICH": "University of Michigan: Inflation Expectation",
    # Growth & activity
    "GDP": "Gross Domestic Product (nominal)",
    "GDPC1": "Real Gross Domestic Product",
    "A191RL1Q225SBEA": "Real GDP Percent Change from Preceding Period",
    "INDPRO": "Industrial Production Index",
    "TCU": "Capacity Utilization: Total Industry",
    "USSLIND": "Leading Index for the United States",
    "RSAFS": "Advance Retail Sales: Retail and Food Services",
    "DGORDER": "Manufacturers' New Orders: Durable Goods",
    "HOUST": "Housing Starts: Total New Privately Owned",
    "CSUSHPINSA": "S&P/Case-Shiller US National Home Price Index",
    "PERMIT": "New Private Housing Units Authorized by Building Permits",
    # Labour
    "UNRATE": "Unemployment Rate",
    "PAYEMS": "All Employees: Total Nonfarm Payrolls",
    "ICSA": "Initial Jobless Claims",
    "CCSA": "Continued Claims (Insured Unemployment)",
    "CIVPART": "Labor Force Participation Rate",
    "AHETPI": "Average Hourly Earnings: Production and Nonsupervisory",
    "JTSJOL": "Job Openings: Total Nonfarm (JOLTS)",
    # Money & credit
    "M1SL": "M1 Money Stock",
    "M2SL": "M2 Money Stock",
    "M2V": "Velocity of M2 Money Stock",
    "BOGMBASE": "Monetary Base: Total",
    "WALCL": "Fed Total Assets (balance sheet)",
    "TOTRESNS": "Reserves of Depository Institutions",
    "DRTSCILM": "SLOOS: Net % Tightening Standards for C&I Loans",
    "TOTALSL": "Total Consumer Credit Outstanding",
    # Markets & sentiment
    "VIXCLS": "CBOE Volatility Index (VIX)",
    "SP500": "S&P 500 Index",
    "NASDAQCOM": "NASDAQ Composite Index",
    "DJIA": "Dow Jones Industrial Average",
    "WILL5000PR": "Wilshire 5000 Total Market Index",
    "UMCSENT": "University of Michigan: Consumer Sentiment",
    "STLFSI4": "St. Louis Fed Financial Stress Index",
    "NFCI": "Chicago Fed National Financial Conditions Index",
    "CFNAI": "Chicago Fed National Activity Index",
    "RECPROUSM156N": "Smoothed US Recession Probabilities",
    # FX & commodities
    "DTWEXBGS": "Nominal Broad US Dollar Index",
    "DEXUSEU": "USD/EUR Spot Exchange Rate",
    "DEXJPUS": "JPY/USD Spot Exchange Rate",
    "DEXCHUS": "CNY/USD Spot Exchange Rate",
    "DEXUSUK": "USD/GBP Spot Exchange Rate",
    "DCOILWTICO": "Crude Oil Prices: WTI",
    "DCOILBRENTEU": "Crude Oil Prices: Brent",
    "DHHNGSP": "Henry Hub Natural Gas Spot Price",
    "GASREGW": "US Regular All Formulations Gas Price",
    "PALLFNFINDEXQ": "Global Price Index of All Commodities",
    # Fiscal
    "GFDEBTN": "Federal Debt: Total Public Debt",
    "GFDEGDQ188S": "Federal Debt as Percent of GDP",
    "FYFSD": "Federal Surplus or Deficit",
    "MTSDS133FMS": "Federal Surplus or Deficit (monthly)",
}


def _key() -> str:
    if not settings.fred_api_key:
        raise MissingCredentialError(
            "This command needs a free FRED API key. Get one at "
            "https://fred.stlouisfed.org/docs/api/api_key.html and set MFT_FRED_API_KEY. "
            "Series observations still work without it."
        )
    return settings.fred_api_key


@cached("fred.series", ttl=TTL_DAILY)
def series(
    series_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    frequency: Optional[str] = None,
    transform: Optional[str] = None,
) -> pd.DataFrame:
    """One or more FRED series (comma-separated ids) as a date-indexed frame.

    ``frequency`` re-samples server-side (``d``, ``w``, ``m``, ``q``, ``a``);
    ``transform`` applies a FRED unit transform (``lin``, ``chg``, ``ch1``,
    ``pch``, ``pc1``, ``pca``, ``cch``, ``cca``, ``log``).
    """
    ids = [s.strip().upper() for s in series_id.replace(" ", ",").split(",") if s.strip()]
    params: Dict[str, Any] = {"id": ",".join(ids)}
    if start_date:
        params["cosd"] = start_date
    if end_date:
        params["coed"] = end_date
    if frequency:
        params["fq"] = frequency
    if transform:
        params["transformation"] = transform

    body = fetch(GRAPH, params=params, ttl=TTL_DAILY)
    df = _parse_zip(body) if body[:2] == b"PK" else _parse_csv(body)

    # FRED ignores cosd/coed on the multi-series (zip) download, so enforce the
    # window here — otherwise a "2 year" request ships four decades of rows.
    if start_date:
        df = df[df.index >= pd.Timestamp(start_date)]
    if end_date:
        df = df[df.index <= pd.Timestamp(end_date)]

    missing = [s for s in ids if s not in df.columns]
    df = df.dropna(how="all")
    if df.empty:
        raise EmptyDataError("FRED returned no observations for {}".format(series_id))
    if missing:
        # FRED silently omits a series it cannot align with the others.
        df.attrs["missing_series"] = missing
    return df.sort_index()


def _clean_values(frame: pd.DataFrame) -> pd.DataFrame:
    """Date-index a fredgraph CSV and coerce values ('.' means missing)."""
    frame = frame.rename(columns={frame.columns[0]: "date"})
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for col in frame.columns[1:]:
        frame[col] = pd.to_numeric(frame[col].replace(".", pd.NA), errors="coerce")
    return frame.dropna(subset=["date"]).set_index("date")


def _parse_csv(body: bytes) -> pd.DataFrame:
    return _clean_values(pd.read_csv(io.BytesIO(body)))


def _parse_zip(body: bytes) -> pd.DataFrame:
    """FRED ships a zip of one CSV per series when frequencies differ."""
    frames = []
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        for name in archive.namelist():
            if not name.lower().endswith(".csv"):
                continue
            with archive.open(name) as handle:
                frames.append(_clean_values(pd.read_csv(handle)))
    if not frames:
        raise ProviderError("FRED returned an archive with no series in it")
    return pd.concat(frames, axis=1).sort_index()


def search(query: str, limit: int = 25) -> pd.DataFrame:
    """Series search — FRED's API when a key is set, the catalogue otherwise."""
    if settings.fred_api_key:
        payload = get_json(
            API + "/series/search",
            params={"search_text": query, "api_key": settings.fred_api_key,
                    "file_type": "json", "limit": limit},
            ttl=TTL_DAILY,
        )
        rows = payload.get("seriess") or []
        if not rows:
            raise EmptyDataError("FRED search found nothing for {!r}".format(query))
        df = pd.DataFrame(rows)
        keep = ["id", "title", "frequency", "units", "seasonal_adjustment",
                "observation_start", "observation_end", "popularity", "notes"]
        return df[[c for c in keep if c in df.columns]].head(limit)

    q = query.lower().strip()
    hits = [
        {"id": sid, "title": title, "source": "built-in catalogue"}
        for sid, title in CATALOGUE.items()
        if q in title.lower() or q in sid.lower()
    ]
    if not hits:
        raise EmptyDataError(
            "No catalogue series matches {!r}. Set MFT_FRED_API_KEY (free) to search "
            "all ~800k FRED series.".format(query)
        )
    return pd.DataFrame(hits).head(limit)


def series_info(series_id: str) -> pd.DataFrame:
    """Metadata (units, frequency, vintage dates) for a series — needs a key."""
    payload = get_json(
        API + "/series",
        params={"series_id": series_id.upper(), "api_key": _key(), "file_type": "json"},
        ttl=TTL_REFERENCE,
    )
    rows = payload.get("seriess") or []
    if not rows:
        raise EmptyDataError("No FRED series called {!r}".format(series_id))
    return pd.DataFrame(rows)


def releases(limit: int = 100) -> pd.DataFrame:
    """Economic-release catalogue — needs a key."""
    payload = get_json(
        API + "/releases",
        params={"api_key": _key(), "file_type": "json", "limit": limit},
        ttl=TTL_REFERENCE,
    )
    return pd.DataFrame(payload.get("releases") or [])


def release_dates(start_date: Optional[str] = None, end_date: Optional[str] = None,
                  limit: int = 200) -> pd.DataFrame:
    """Upcoming/most recent release dates — the economic calendar. Needs a key."""
    payload = get_json(
        API + "/releases/dates",
        params={"api_key": _key(), "file_type": "json", "limit": limit,
                "realtime_start": start_date, "realtime_end": end_date,
                "include_release_dates_with_no_data": "true"},
        ttl=TTL_DAILY,
    )
    rows = payload.get("release_dates") or []
    if not rows:
        raise EmptyDataError("FRED returned no release dates for that window")
    return pd.DataFrame(rows)


def catalogue() -> pd.DataFrame:
    return pd.DataFrame(
        [{"id": k, "title": v} for k, v in sorted(CATALOGUE.items(), key=lambda kv: kv[1])]
    )
