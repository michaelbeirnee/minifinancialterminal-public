"""Economy menu: growth, prices, labour, money, trade and survey data.

US series come from FRED (key-free CSV download) and BLS; cross-country series
from the World Bank and the IMF World Economic Outlook.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..providers import fred, govstats, intl, markets, yahoo

# Aliases used by the country-aware commands below. FRED is US-only; anything
# else falls through to the World Bank / IMF.
_US_SERIES = {
    "cpi": {"headline": "CPIAUCSL", "core": "CPILFESL", "nsa": "CPIAUCNS"},
    "pce": {"headline": "PCEPI", "core": "PCEPILFE"},
    "gdp_nominal": "GDP",
    "gdp_real": "GDPC1",
    "gdp_growth": "A191RL1Q225SBEA",
    "unemployment": "UNRATE",
    "payrolls": "PAYEMS",
    "claims": "ICSA",
    "participation": "CIVPART",
    "retail_sales": "RSAFS",
    "industrial_production": "INDPRO",
    "housing_starts": "HOUST",
    "house_price_index": "CSUSHPINSA",
    "cli": "USSLIND",
    "consumer_sentiment": "UMCSENT",
    "inflation_expectation": "MICH",
    "financial_conditions": "NFCI",
    "recession_probability": "RECPROUSM156N",
}


def _is_us(country: str) -> bool:
    return country.strip().lower().replace("_", " ") in ("us", "usa", "united states", "united states of america")


def _fred_result(series_id: str, start_date: Optional[str], end_date: Optional[str],
                 transform: Optional[str] = None, label: Optional[str] = None) -> Result:
    df = fred.series(series_id, start_date, end_date, transform=transform)
    if label and len(df.columns) == 1:
        df.columns = [label]
    missing = df.attrs.get("missing_series") or []
    return Result(df, provider="fred", index_name="date",
                  warnings=["FRED returned no data for: " + ", ".join(missing)] if missing else [],
                  extra={"series_id": series_id, "source": "FRED"})


# --------------------------------------------------------------------------- #
# Generic access
# --------------------------------------------------------------------------- #
@command("/economy/fred_series", providers=("fred",), summary="Any FRED series by id")
def fred_series(series_id: str = "GDP", start_date: Optional[str] = None,
                end_date: Optional[str] = None, frequency: Optional[str] = None,
                transform: Optional[str] = None, provider: Optional[str] = None) -> Result:
    """Comma-separate ids to pull several series into one frame."""
    resolve_provider(provider, ("fred",))
    return Result(fred.series(series_id, start_date, end_date, frequency, transform),
                  provider="fred", index_name="date")


@command("/economy/fred_search", providers=("fred",), summary="Search FRED for a series")
def fred_search(query: str, limit: int = 25, provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return Result(fred.search(query, limit), provider="fred")


@command("/economy/fred_catalogue", providers=("fred",),
         summary="Curated FRED series this terminal knows about")
def fred_catalogue() -> Result:
    return Result(fred.catalogue(), provider="fred")


@command("/economy/indicators", providers=("worldbank", "imf"),
         summary="Cross-country macro indicator series")
def indicators(indicator: str = "gdp_growth", country: str = "USA",
               start_year: Optional[int] = None, end_year: Optional[int] = None,
               provider: Optional[str] = None) -> Result:
    """Aliases: gdp_nominal, gdp_real, gdp_growth, gdp_per_capita, cpi, inflation,
    unemployment, population, current_account, government_debt, exports, imports…
    Any raw World Bank / IMF indicator code also works."""
    src = resolve_provider(provider, ("worldbank", "imf"))
    if src == "imf":
        return Result(intl.imf(indicator, country), provider=src)
    iso = ",".join(intl.country_iso3(c) for c in country.split(","))
    return Result(intl.worldbank(indicator, iso, start_year, end_year), provider=src)


@command("/economy/available_indicators", providers=("worldbank", "imf"),
         summary="Indicator aliases and codes")
def available_indicators(provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("worldbank", "imf"))
    mapping = intl.IMF_INDICATORS if src == "imf" else intl.WB_INDICATORS
    return Result([{"alias": k, "code": v} for k, v in sorted(mapping.items())], provider=src)


@command("/economy/countries", providers=("worldbank",), summary="Country reference list")
def countries(provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("worldbank",))
    return Result(intl.worldbank_countries(), provider="worldbank")


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #
@command("/economy/cpi", providers=("fred", "worldbank", "bls"), summary="Consumer price inflation")
def cpi(country: str = "united_states", measure: str = "headline",
        start_date: Optional[str] = None, end_date: Optional[str] = None,
        transform: Optional[str] = None, provider: Optional[str] = None) -> Result:
    """``measure``: headline, core or nsa (US only). ``transform="pc1"`` gives
    year-over-year percent change."""
    src = resolve_provider(provider, ("fred", "worldbank", "bls"),
                           default="fred" if _is_us(country) else "worldbank")
    if src == "bls":
        alias = "cpi_core" if measure == "core" else "cpi_all_urban"
        return Result(govstats.bls_series(alias), provider=src)
    if src == "worldbank" or not _is_us(country):
        return Result(intl.worldbank("inflation", intl.country_iso3(country)), provider="worldbank")
    series_id = _US_SERIES["cpi"].get(measure)
    if not series_id:
        raise ValueError("measure must be headline, core or nsa")
    return _fred_result(series_id, start_date, end_date, transform, label="cpi_" + measure)


@command("/economy/pce", providers=("fred",), summary="PCE price index (the Fed's target measure)")
def pce(measure: str = "core", start_date: Optional[str] = None, end_date: Optional[str] = None,
        transform: Optional[str] = None, provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    series_id = _US_SERIES["pce"].get(measure)
    if not series_id:
        raise ValueError("measure must be headline or core")
    return _fred_result(series_id, start_date, end_date, transform, label="pce_" + measure)


@command("/economy/retail_prices", providers=("fred",), summary="Producer and retail price indices")
def retail_prices(start_date: Optional[str] = None, end_date: Optional[str] = None,
                  provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return _fred_result("PPIACO,RSAFS,GASREGW", start_date, end_date)


@command("/economy/inflation_expectations", providers=("fred",),
         summary="Breakeven and survey-based inflation expectations")
def inflation_expectations(start_date: Optional[str] = None, end_date: Optional[str] = None,
                           provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return _fred_result("T10YIE,T5YIFR,MICH", start_date, end_date)


# --------------------------------------------------------------------------- #
# Growth & activity
# --------------------------------------------------------------------------- #
@command("/economy/gdp", providers=("fred", "worldbank", "imf"), summary="Gross domestic product")
def gdp(country: str = "united_states", measure: str = "real", start_date: Optional[str] = None,
        end_date: Optional[str] = None, provider: Optional[str] = None) -> Result:
    """``measure``: real, nominal or growth."""
    src = resolve_provider(provider, ("fred", "worldbank", "imf"),
                           default="fred" if _is_us(country) else "worldbank")
    key = {"real": "gdp_real", "nominal": "gdp_nominal", "growth": "gdp_growth"}.get(measure)
    if not key:
        raise ValueError("measure must be real, nominal or growth")
    if src == "imf":
        return Result(intl.imf(key, intl.country_iso3(country)), provider=src)
    if src == "worldbank" or not _is_us(country):
        return Result(intl.worldbank(key, intl.country_iso3(country)), provider="worldbank")
    return _fred_result(_US_SERIES[key], start_date, end_date, label=key)


@command("/economy/gdp_forecast", providers=("imf",), summary="IMF WEO GDP projections")
def gdp_forecast(country: str = "USA,CHN,DEU,JPN,GBR", measure: str = "growth",
                 provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("imf",))
    key = {"growth": "gdp_growth", "nominal": "gdp_nominal", "per_capita": "gdp_per_capita"}.get(measure)
    if not key:
        raise ValueError("measure must be growth, nominal or per_capita")
    return Result(intl.imf(key, country), provider="imf")


@command("/economy/industrial_production", providers=("fred",),
         summary="Industrial production and capacity utilisation")
def industrial_production(start_date: Optional[str] = None, end_date: Optional[str] = None,
                          provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return _fred_result("INDPRO,TCU", start_date, end_date)


@command("/economy/composite_leading_indicator", providers=("fred",),
         summary="Leading and coincident activity indices")
def composite_leading_indicator(start_date: Optional[str] = None, end_date: Optional[str] = None,
                                provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return _fred_result("USSLIND,CFNAI,NFCI", start_date, end_date)


@command("/economy/house_price_index", providers=("fred",), summary="National house price indices")
def house_price_index(start_date: Optional[str] = None, end_date: Optional[str] = None,
                      provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return _fred_result("CSUSHPINSA,HOUST,PERMIT", start_date, end_date)


@command("/economy/share_price_index", providers=("fred",), summary="Equity index levels as macro series")
def share_price_index(start_date: Optional[str] = None, end_date: Optional[str] = None,
                      provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return _fred_result("SP500,NASDAQCOM,DJIA,WILL5000PR", start_date, end_date)


# --------------------------------------------------------------------------- #
# Labour
# --------------------------------------------------------------------------- #
@command("/economy/unemployment", providers=("fred", "worldbank", "bls"), summary="Unemployment rate")
def unemployment(country: str = "united_states", start_date: Optional[str] = None,
                 end_date: Optional[str] = None, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("fred", "worldbank", "bls"),
                           default="fred" if _is_us(country) else "worldbank")
    if src == "bls":
        return Result(govstats.bls_series("unemployment_rate"), provider=src)
    if src == "worldbank" or not _is_us(country):
        return Result(intl.worldbank("unemployment", intl.country_iso3(country)), provider="worldbank")
    return _fred_result(_US_SERIES["unemployment"], start_date, end_date, label="unemployment_rate")


@command("/economy/nonfarm_payrolls", providers=("fred", "bls"), summary="Nonfarm payroll employment")
def nonfarm_payrolls(start_date: Optional[str] = None, end_date: Optional[str] = None,
                     provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("fred", "bls"))
    if src == "bls":
        return Result(govstats.bls_series("nonfarm_payrolls"), provider=src)
    return _fred_result("PAYEMS,CES0500000003", start_date, end_date)


@command("/economy/jobless_claims", providers=("fred",), summary="Initial and continuing claims")
def jobless_claims(start_date: Optional[str] = None, end_date: Optional[str] = None,
                   provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return _fred_result("ICSA,CCSA", start_date, end_date)


@command("/economy/labour_market", providers=("fred",),
         summary="Participation, openings and wage growth")
def labour_market(start_date: Optional[str] = None, end_date: Optional[str] = None,
                  provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return _fred_result("CIVPART,JTSJOL,AHETPI,UNRATE", start_date, end_date)


@command("/economy/bls_series", providers=("bls",), summary="Any BLS series by id or alias")
def bls_series(series_id: str = "cpi_all_urban", start_year: Optional[int] = None,
               end_year: Optional[int] = None, provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("bls",))
    return Result(govstats.bls_series(series_id, start_year, end_year), provider="bls")


@command("/economy/bls_catalogue", providers=("bls",), summary="BLS series aliases")
def bls_catalogue() -> Result:
    return Result(govstats.bls_catalogue(), provider="bls")


# --------------------------------------------------------------------------- #
# Money, credit & the Fed
# --------------------------------------------------------------------------- #
@command("/economy/money_measures", providers=("fred",), summary="M1, M2, velocity and the base")
def money_measures(start_date: Optional[str] = None, end_date: Optional[str] = None,
                   provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return _fred_result("M1SL,M2SL,M2V,BOGMBASE", start_date, end_date)


@command("/economy/central_bank_holdings", providers=("fred",),
         summary="Federal Reserve balance sheet and reserves")
def central_bank_holdings(start_date: Optional[str] = None, end_date: Optional[str] = None,
                          provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return _fred_result("WALCL,TOTRESNS", start_date, end_date)


@command("/economy/sloos", providers=("fred",),
         summary="Senior Loan Officer Survey — lending standards")
def sloos(start_date: Optional[str] = None, end_date: Optional[str] = None,
          provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return _fred_result("DRTSCILM,TOTALSL", start_date, end_date)


@command("/economy/financial_conditions", providers=("fred",),
         summary="Financial stress and conditions indices")
def financial_conditions(start_date: Optional[str] = None, end_date: Optional[str] = None,
                         provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return _fred_result("NFCI,STLFSI4,VIXCLS,RECPROUSM156N", start_date, end_date)


# --------------------------------------------------------------------------- #
# External sector & fiscal
# --------------------------------------------------------------------------- #
@command("/economy/balance_of_payments", providers=("worldbank", "imf"),
         summary="Current account balance")
def balance_of_payments(country: str = "USA", provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("worldbank", "imf"))
    if src == "imf":
        return Result(intl.imf("current_account", country), provider=src)
    return Result(intl.worldbank("current_account", intl.country_iso3(country)), provider=src)


@command("/economy/trade", providers=("worldbank",), summary="Exports, imports and trade balance")
def trade(country: str = "USA", provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("worldbank",))
    iso = intl.country_iso3(country)
    frames = []
    for alias in ("exports", "imports", "trade_balance"):
        try:
            frames.append(intl.worldbank(alias, iso))
        except Exception:  # noqa: BLE001 - not every country reports all three
            continue
    if not frames:
        raise EmptyDataError("No trade data published for {}".format(country))
    return Result(pd.concat(frames, ignore_index=True), provider="worldbank")


@command("/economy/government_debt", providers=("worldbank", "imf", "fred"),
         summary="Government debt and deficits")
def government_debt(country: str = "USA", provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("worldbank", "imf", "fred"))
    if src == "fred":
        return _fred_result("GFDEBTN,GFDEGDQ188S,FYFSD", None, None)
    if src == "imf":
        return Result(intl.imf("government_debt", country), provider=src)
    return Result(intl.worldbank("government_debt", intl.country_iso3(country)), provider=src)


@command("/economy/country_profile", providers=("worldbank",),
         summary="Headline macro snapshot for a country")
def country_profile(country: str = "USA", provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("worldbank",))
    iso = intl.country_iso3(country)
    profile: Dict[str, Any] = {"country": country, "iso3": iso}
    warnings: List[str] = []
    for alias in ("gdp_nominal", "gdp_growth", "gdp_per_capita", "inflation", "unemployment",
                  "population", "government_debt", "current_account"):
        try:
            df = intl.worldbank(alias, iso)
            latest = df.iloc[-1]
            profile[alias] = latest["value"]
            profile[alias + "_year"] = latest["date"].year
        except Exception as exc:  # noqa: BLE001
            warnings.append("{}: {}".format(alias, exc))
    if len(profile) <= 2:
        raise EmptyDataError("World Bank has no indicators for {}".format(country))
    return Result(profile, provider="worldbank", warnings=warnings)


# --------------------------------------------------------------------------- #
# Calendars & surveys
# --------------------------------------------------------------------------- #
@command("/economy/calendar", providers=("yahoo", "fred"), summary="Economic release calendar")
def economy_calendar(start_date: Optional[str] = None, end_date: Optional[str] = None,
                     limit: int = 200, provider: Optional[str] = None) -> Result:
    """Yahoo's macro-events calendar; the FRED provider gives official release
    dates instead but needs a free API key."""
    src = resolve_provider(provider, ("yahoo", "fred"))
    if src == "fred":
        return Result(fred.release_dates(start_date, end_date, limit), provider=src)
    return Result(yahoo.market_calendar("economic", start_date, end_date).head(limit), provider=src)


@command("/economy/survey", providers=("fred",), summary="Sentiment and business surveys")
def survey(name: str = "consumer_sentiment", start_date: Optional[str] = None,
           end_date: Optional[str] = None, provider: Optional[str] = None) -> Result:
    """``name``: consumer_sentiment, inflation_expectation, chicago_activity,
    financial_conditions, sloos or recession_probability."""
    resolve_provider(provider, ("fred",))
    mapping = {
        "consumer_sentiment": "UMCSENT", "inflation_expectation": "MICH",
        "chicago_activity": "CFNAI", "financial_conditions": "NFCI",
        "sloos": "DRTSCILM", "recession_probability": "RECPROUSM156N",
    }
    series_id = mapping.get(name)
    if not series_id:
        raise ValueError("name must be one of {}".format(", ".join(sorted(mapping))))
    return _fred_result(series_id, start_date, end_date, label=name)


@command("/economy/primary_dividend_yield", providers=("multpl",),
         summary="S&P 500 dividend yield history")
def primary_dividend_yield(frequency: str = "month", provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("multpl",))
    return Result(markets.multpl("dividend_yield", frequency), provider="multpl")


@command("/economy/oecd_dataset", providers=("oecd",), summary="Any OECD SDMX dataflow (advanced)")
def oecd_dataset(dataflow: str, key: str = "all", start_date: Optional[str] = None,
                 end_date: Optional[str] = None, provider: Optional[str] = None) -> Result:
    """Browse dataflow identifiers at https://data-explorer.oecd.org."""
    resolve_provider(provider, ("oecd",))
    return Result(intl.oecd_dataset(dataflow, key, start_date, end_date), provider="oecd")
