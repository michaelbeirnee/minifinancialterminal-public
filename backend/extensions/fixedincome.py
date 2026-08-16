"""Fixed income menu: government curves, corporate credit and reference rates."""
from __future__ import annotations

from typing import Optional

from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..providers import fred, intl, treasury


def _fred(series_id: str, start_date: Optional[str], end_date: Optional[str]) -> Result:
    df = fred.series(series_id, start_date, end_date)
    missing = df.attrs.get("missing_series") or []
    return Result(df, provider="fred", index_name="date",
                  warnings=["FRED returned no data for: " + ", ".join(missing)] if missing else [],
                  extra={"series_id": series_id})


# --------------------------------------------------------------------------- #
# Government
# --------------------------------------------------------------------------- #
@command("/fixedincome/government/treasury_rates", providers=("treasury", "fred"),
         summary="Daily Treasury constant-maturity rates")
def treasury_rates(start_date: Optional[str] = None, end_date: Optional[str] = None,
                   curve: str = "nominal", provider: Optional[str] = None) -> Result:
    """``curve``: nominal, bill, real, long_term or real_long_term."""
    src = resolve_provider(provider, ("treasury", "fred"))
    if src == "fred":
        return _fred("DGS1MO,DGS3MO,DGS6MO,DGS1,DGS2,DGS3,DGS5,DGS7,DGS10,DGS20,DGS30",
                     start_date, end_date)
    return Result(treasury.rates(start_date, end_date, curve), provider=src, index_name="date")


@command("/fixedincome/government/yield_curve", providers=("treasury", "ecb"),
         summary="Yield curve on a single date")
def yield_curve(date: Optional[str] = None, curve: str = "nominal", region: str = "us",
                provider: Optional[str] = None) -> Result:
    """``region``: ``us`` (Treasury par curve) or ``eu`` (ECB AAA spot curve)."""
    src = resolve_provider(provider, ("treasury", "ecb"), default="ecb" if region == "eu" else "treasury")
    if src == "ecb" or region.lower() in ("eu", "euro", "euro_area"):
        return Result(intl.ecb_yield_curve(date), provider="ecb")
    return Result(treasury.yield_curve(date, curve), provider=src)


@command("/fixedincome/government/treasury_auctions", providers=("treasury",),
         summary="Recent Treasury auction results")
def treasury_auctions(security_type: Optional[str] = None, limit: int = 100,
                      provider: Optional[str] = None) -> Result:
    """``security_type``: Bill, Note, Bond, TIPS, FRN or CMB."""
    src = resolve_provider(provider, ("treasury",))
    return Result(treasury.treasury_auctions(security_type, limit), provider=src)


@command("/fixedincome/government/debt_outstanding", providers=("treasury",),
         summary="Total US public debt, daily")
def debt_outstanding(start_date: Optional[str] = None, limit: int = 500,
                     provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("treasury",))
    return Result(treasury.debt_to_penny(start_date, limit), provider=src, index_name="record_date")


@command("/fixedincome/government/average_interest_rates", providers=("treasury",),
         summary="Average rate the Treasury pays by security type")
def average_interest_rates(start_date: Optional[str] = None, limit: int = 1000,
                           provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("treasury",))
    return Result(treasury.average_interest_rates(start_date, limit), provider=src)


@command("/fixedincome/government/inflation_protected", providers=("fred",),
         summary="TIPS real yields and breakevens")
def inflation_protected(start_date: Optional[str] = None, end_date: Optional[str] = None,
                        provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return _fred("DFII10,T10YIE,T5YIFR", start_date, end_date)


@command("/fixedincome/spreads", providers=("fred",), summary="Key curve and credit spreads")
def spreads(start_date: Optional[str] = None, end_date: Optional[str] = None,
            provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return _fred("T10Y2Y,T10Y3M,BAA10Y,BAMLH0A0HYM2,BAMLC0A0CM", start_date, end_date)


# --------------------------------------------------------------------------- #
# Corporate credit
# --------------------------------------------------------------------------- #
@command("/fixedincome/corporate/ice_bofa", providers=("fred",),
         summary="ICE BofA index option-adjusted spreads and yields")
def ice_bofa(category: str = "spread", start_date: Optional[str] = None,
             end_date: Optional[str] = None, provider: Optional[str] = None) -> Result:
    """``category``: spread (OAS) or yield (effective yield by rating bucket)."""
    resolve_provider(provider, ("fred",))
    series = {
        "spread": "BAMLC0A0CM,BAMLH0A0HYM2",
        "yield": "BAMLC0A1CAAAEY,BAMLC0A4CBBBEY,BAMLH0A3HYCEY",
    }.get(category)
    if not series:
        raise ValueError("category must be spread or yield")
    return _fred(series, start_date, end_date)


@command("/fixedincome/corporate/moody", providers=("fred",),
         summary="Moody's Aaa/Baa seasoned corporate bond yields")
def moody(start_date: Optional[str] = None, end_date: Optional[str] = None,
          provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return _fred("AAA,BAA,BAA10Y", start_date, end_date)


@command("/fixedincome/corporate/commercial_paper", providers=("fred",),
         summary="Commercial paper rates")
def commercial_paper(start_date: Optional[str] = None, end_date: Optional[str] = None,
                     provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return _fred("DCPN30,DCPF3M", start_date, end_date)


@command("/fixedincome/corporate/spot_rates", providers=("fred",),
         summary="High Quality Market (HQM) corporate spot curve")
def spot_rates(start_date: Optional[str] = None, end_date: Optional[str] = None,
               provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return _fred("HQMCB10YR,HQMCB30YR", start_date, end_date)


# --------------------------------------------------------------------------- #
# Reference rates
# --------------------------------------------------------------------------- #
_NYFED_RATES = {"sofr": "SOFR", "effr": "EFFR", "obfr": "OBFR", "bgcr": "BGCR", "tgcr": "TGCR"}
_FRED_RATES = {
    "iorb": "IORB", "dpcredit": "DPCREDIT", "ameribor": "AMERIBOR", "sonia": "SONIA",
    "estr": "ECBESTRVOLWGTTRMDMNRT", "prime": "DPRIME", "fed_funds": "DFF",
}


@command("/fixedincome/rate/reference", providers=("treasury", "fred"),
         summary="Any published reference rate")
def reference_rate(rate: str = "sofr", start_date: Optional[str] = None,
                   end_date: Optional[str] = None, limit: int = 250,
                   provider: Optional[str] = None) -> Result:
    """``rate``: sofr, effr, obfr, bgcr, tgcr (NY Fed) or iorb, dpcredit,
    ameribor, sonia, estr, prime, fed_funds (FRED)."""
    key = rate.lower().strip()
    if key in _NYFED_RATES:
        src = resolve_provider(provider, ("treasury", "fred"), default="treasury")
        if src == "treasury":
            return Result(treasury.reference_rate(key, start_date, end_date, limit),
                          provider="treasury", index_name="date")
        return _fred(_NYFED_RATES[key], start_date, end_date)
    if key in _FRED_RATES:
        resolve_provider(provider, ("fred",), default="fred")
        return _fred(_FRED_RATES[key], start_date, end_date)
    raise ValueError(
        "Unknown rate {!r}. Available: {}".format(
            rate, ", ".join(sorted(set(_NYFED_RATES) | set(_FRED_RATES)))
        )
    )


@command("/fixedincome/rate/all", providers=("treasury",),
         summary="Latest print for every NY Fed reference rate")
def all_reference_rates(provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("treasury",))
    return Result(treasury.all_reference_rates(), provider=src)


@command("/fixedincome/rate/policy", providers=("fred",),
         summary="Policy rates: fed funds, IORB and discount window")
def policy_rates(start_date: Optional[str] = None, end_date: Optional[str] = None,
                 provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return _fred("DFF,IORB,DPCREDIT,DPRIME", start_date, end_date)


@command("/fixedincome/rate/mortgage", providers=("fred",), summary="US mortgage rates")
def mortgage_rates(start_date: Optional[str] = None, end_date: Optional[str] = None,
                   provider: Optional[str] = None) -> Result:
    resolve_provider(provider, ("fred",))
    return _fred("MORTGAGE30US,MORTGAGE15US", start_date, end_date)
