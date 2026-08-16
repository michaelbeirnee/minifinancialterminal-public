"""Regulators menu: SEC EDGAR reference data and CFTC Commitments of Traders."""
from __future__ import annotations

from typing import Optional

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import one_symbol
from ..providers import finra, sec


# --------------------------------------------------------------------------- #
# SEC
# --------------------------------------------------------------------------- #
@command("/regulators/sec/cik_map", providers=("sec",), summary="Ticker to CIK lookup")
def cik_map(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("sec",))
    sym = one_symbol(symbol)
    return Result({"symbol": sym, "cik": sec.cik_for(sym)}, provider=src)


@command("/regulators/sec/symbol_map", providers=("sec",), summary="CIK to ticker lookup")
def symbol_map(cik: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("sec",))
    symbol = sec.symbol_for(cik)
    if not symbol:
        raise EmptyDataError("No ticker registered for CIK {}".format(cik))
    return Result({"cik": cik, "symbol": symbol}, provider=src)


@command("/regulators/sec/institutions_search", providers=("sec",),
         summary="Search the SEC registrant register")
def institutions_search(query: str, limit: int = 25, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("sec",))
    return Result(sec.search_companies(query, limit), provider=src)


@command("/regulators/sec/filing_search", providers=("sec",),
         summary="EDGAR full-text search across filing documents")
def filing_search(query: str, forms: Optional[str] = None, start_date: Optional[str] = None,
                  end_date: Optional[str] = None, limit: int = 100,
                  provider: Optional[str] = None) -> Result:
    """Searches the text of filings since 2001, e.g. ``query="going concern"``."""
    src = resolve_provider(provider, ("sec",))
    return Result(sec.full_text_search(query, forms, start_date, end_date, limit), provider=src)


@command("/regulators/sec/sic_search", providers=("sec",), summary="SIC industry code lookup")
def sic_search(query: Optional[str] = None, limit: int = 100, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("sec",))
    df = sec.sic_codes()
    if query:
        q = query.lower()
        mask = df.apply(lambda row: row.astype(str).str.lower().str.contains(q, regex=False).any(), axis=1)
        df = df[mask]
        if df.empty:
            raise EmptyDataError("No SIC code matches {!r}".format(query))
    return Result(df.head(limit), provider=src)


@command("/regulators/sec/press_releases", providers=("sec",), summary="SEC press-release feed")
def press_releases(limit: int = 40, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("sec",))
    return Result(sec.press_releases(limit), provider=src)


@command("/regulators/sec/schema_files", providers=("sec",),
         summary="SEC bulk Financial Statement Data Set archives")
def schema_files(provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("sec",))
    return Result(sec.financial_statement_datasets(), provider=src)


@command("/regulators/sec/company_register", providers=("sec",),
         summary="Every SEC-registered ticker with CIK and exchange")
def company_register(limit: int = 20000, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("sec",))
    return Result(sec.company_map().head(limit), provider=src)


# --------------------------------------------------------------------------- #
# CFTC
# --------------------------------------------------------------------------- #
@command("/regulators/cftc/cot", providers=("cftc",), summary="Commitments of Traders report")
def cot(market: Optional[str] = None, report: str = "legacy", start_date: Optional[str] = None,
        limit: int = 500, provider: Optional[str] = None) -> Result:
    """``report``: legacy, disaggregated, financial or supplemental
    (each also has a ``_combined`` futures-and-options variant)."""
    src = resolve_provider(provider, ("cftc",))
    return Result(finra.cot(market, report, start_date, limit), provider=src)


@command("/regulators/cftc/cot_search", providers=("cftc",), summary="Search COT market names")
def cot_search(query: Optional[str] = None, report: str = "legacy", limit: int = 200,
               provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("cftc",))
    df = finra.cot_markets(report)
    if query:
        q = query.lower()
        df = df[df["market_and_exchange_names"].str.lower().str.contains(q, na=False, regex=False)]
        if df.empty:
            raise EmptyDataError("No COT market matches {!r}".format(query))
    return Result(df.head(limit), provider=src)
