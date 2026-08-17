"""SEC EDGAR provider — public-domain US regulatory data, no API key.

Covers the pieces of a terminal that Yahoo does badly or not at all: audited
XBRL fundamentals straight from 10-K/10-Q filings, the full filing index,
full-text search, Form 4 insider activity, 13F institutional filings, and the
fails-to-deliver files.

SEC asks automated clients to identify themselves; set ``MFT_SEC_USER_AGENT``
to a real name/e-mail before running this at any volume.
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from ..config import settings
from ..core.caching import TTL_DAILY, TTL_FUNDAMENTAL, TTL_REFERENCE, cached
from ..core.errors import EmptyDataError
from ..core.http import fetch, get_json, get_text, get_xml, strip_ns

NAME = "sec"

BASE = "https://www.sec.gov"
DATA = "https://data.sec.gov"
EFTS = "https://efts.sec.gov/LATEST/search-index"


def _headers() -> Dict[str, str]:
    return {"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}


def _pad(cik: Any) -> str:
    return str(int(str(cik).lstrip("CIK").lstrip("0") or 0)).zfill(10)


# --------------------------------------------------------------------------- #
# Company reference
# --------------------------------------------------------------------------- #
@cached("sec.company_map", ttl=TTL_REFERENCE)
def company_map() -> pd.DataFrame:
    """Every SEC-registered ticker with its CIK, company name and exchange."""
    payload = get_json(
        BASE + "/files/company_tickers_exchange.json", headers=_headers(), ttl=TTL_REFERENCE
    )
    df = pd.DataFrame(payload["data"], columns=payload["fields"])
    df = df.rename(columns={"cik": "cik", "name": "name", "ticker": "symbol", "exchange": "exchange"})
    df["cik"] = df["cik"].map(_pad)
    return df[["symbol", "name", "cik", "exchange"]]


def cik_for(symbol: str) -> str:
    """Resolve a ticker to a zero-padded CIK."""
    symbol = symbol.upper().strip()
    if symbol.isdigit() or symbol.upper().startswith("CIK"):
        return _pad(symbol)
    hits = company_map()
    row = hits[hits["symbol"] == symbol]
    if row.empty:
        raise EmptyDataError("No SEC CIK registered for ticker {!r}".format(symbol))
    return str(row.iloc[0]["cik"])


def symbol_for(cik: str) -> Optional[str]:
    hits = company_map()
    row = hits[hits["cik"] == _pad(cik)]
    return None if row.empty else str(row.iloc[0]["symbol"])


def search_companies(query: str, limit: int = 25) -> pd.DataFrame:
    """Substring search across the SEC ticker/company register."""
    df = company_map()
    q = query.strip().lower()
    mask = df["name"].str.lower().str.contains(q, na=False, regex=False) | df["symbol"].str.lower().eq(q)
    out = df[mask].head(limit)
    if out.empty:
        raise EmptyDataError("No SEC registrant matches {!r}".format(query))
    return out


# --------------------------------------------------------------------------- #
# Filings
# --------------------------------------------------------------------------- #
@cached("sec.submissions", ttl=TTL_DAILY)
def submissions(cik: str) -> Dict[str, Any]:
    return get_json(
        "{}/submissions/CIK{}.json".format(DATA, _pad(cik)), headers=_headers(), ttl=TTL_DAILY
    )


def filings(
    symbol: str,
    form_type: Optional[str] = None,
    limit: int = 100,
    start_date: Optional[str] = None,
) -> pd.DataFrame:
    """Recent filings for a company, newest first."""
    cik = cik_for(symbol)
    payload = submissions(cik)
    recent = payload.get("filings", {}).get("recent", {})
    if not recent:
        raise EmptyDataError("No filings indexed for {}".format(symbol))
    df = pd.DataFrame(recent)
    df["cik"] = cik
    df["company"] = payload.get("name")
    df["filing_date"] = pd.to_datetime(df["filingDate"], errors="coerce")
    df["report_date"] = pd.to_datetime(df.get("reportDate"), errors="coerce")
    df["url"] = [
        "{}/Archives/edgar/data/{}/{}/{}".format(BASE, int(cik), str(a).replace("-", ""), d)
        for a, d in zip(df["accessionNumber"], df["primaryDocument"])
    ]
    df["filing_index"] = [
        "{}/Archives/edgar/data/{}/{}/{}-index.htm".format(BASE, int(cik), str(a).replace("-", ""), a)
        for a in df["accessionNumber"]
    ]
    if form_type:
        wanted = {f.strip().upper() for f in form_type.split(",")}
        df = df[df["form"].str.upper().isin(wanted)]
    if start_date:
        df = df[df["filing_date"] >= pd.Timestamp(start_date)]
    cols = ["filing_date", "report_date", "form", "accessionNumber", "primaryDocument",
            "items", "size", "isXBRL", "company", "cik", "url", "filing_index"]
    df = df[[c for c in cols if c in df.columns]].rename(
        columns={"accessionNumber": "accession_number", "primaryDocument": "primary_document",
                 "isXBRL": "is_xbrl"}
    )
    if df.empty:
        raise EmptyDataError("No {} filings for {}".format(form_type or "", symbol))
    return df.sort_values("filing_date", ascending=False).head(limit).reset_index(drop=True)


def full_text_search(
    query: str,
    forms: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 100,
) -> pd.DataFrame:
    """EDGAR full-text search across filing documents (2001-present)."""
    rows: List[Dict[str, Any]] = []
    for offset in range(0, min(limit, 1000), 10):
        params = {"q": query, "forms": forms, "from": offset,
                  "startdt": start_date, "enddt": end_date}
        body = get_json(EFTS, params=params, headers=_headers(), ttl=TTL_DAILY)
        hits = body.get("hits", {}).get("hits", [])
        if not hits:
            break
        for h in hits:
            src = h.get("_source", {})
            adsh = src.get("adsh", "")
            cik = (src.get("ciks") or [""])[0]
            doc = str(h.get("_id", "")).split(":")[-1]
            rows.append(
                {
                    "filing_date": src.get("file_date"),
                    "form": src.get("form"),
                    "company": (src.get("display_names") or [None])[0],
                    "cik": cik,
                    "accession_number": adsh,
                    "file_type": src.get("file_type"),
                    "description": src.get("file_description"),
                    "location": (src.get("biz_locations") or [None])[0],
                    "sic": (src.get("sics") or [None])[0],
                    "url": "{}/Archives/edgar/data/{}/{}/{}".format(
                        BASE, str(cik).lstrip("0"), adsh.replace("-", ""), doc
                    ),
                }
            )
        if len(rows) >= limit:
            break
    if not rows:
        raise EmptyDataError("EDGAR full-text search found nothing for {!r}".format(query))
    return pd.DataFrame(rows).head(limit)


@cached("sec.latest_filings", ttl=600)
def latest_filings(form_type: Optional[str] = None, limit: int = 40) -> pd.DataFrame:
    """The live EDGAR "filings received today" feed."""
    params = {"action": "getcurrent", "type": form_type or "", "company": "", "dateb": "",
              "owner": "include", "count": min(limit, 100), "output": "atom"}
    root = get_xml(BASE + "/cgi-bin/browse-edgar", params=params, headers=_headers(), ttl=600)
    rows = []
    for entry in root:
        if strip_ns(entry.tag) != "entry":
            continue
        item: Dict[str, Any] = {}
        for child in entry:
            tag = strip_ns(child.tag)
            if tag == "link":
                item["url"] = child.attrib.get("href")
            elif tag in ("title", "summary", "updated", "category"):
                item[tag] = child.attrib.get("term") if tag == "category" else (child.text or "").strip()
        title = item.get("title", "")
        match = re.match(r"^(?P<form>[^-]+) - (?P<company>.+?) \((?P<cik>\d+)\)", title)
        rows.append(
            {
                "form": (match.group("form").strip() if match else item.get("category")),
                "company": match.group("company").strip() if match else title,
                "cik": match.group("cik") if match else None,
                "updated": item.get("updated"),
                "url": item.get("url"),
            }
        )
    if not rows:
        raise EmptyDataError("EDGAR returned no current filings")
    return pd.DataFrame(rows).head(limit)


def insider_filings(symbol: str, limit: int = 100) -> pd.DataFrame:
    """Form 3/4/5 insider-ownership filings for a company."""
    return filings(symbol, form_type="3,4,5", limit=limit)


def institutional_filings(symbol: str, limit: int = 40) -> pd.DataFrame:
    """13F/13D/13G filings referencing a company (or filed by an institution)."""
    return filings(symbol, form_type="13F-HR,13F-HR/A,SC 13D,SC 13G", limit=limit)


# --------------------------------------------------------------------------- #
# XBRL fundamentals
# --------------------------------------------------------------------------- #
@cached("sec.facts", ttl=TTL_FUNDAMENTAL)
def company_facts(cik: str) -> Dict[str, Any]:
    return get_json(
        "{}/api/xbrl/companyfacts/CIK{}.json".format(DATA, _pad(cik)),
        headers=_headers(),
        ttl=TTL_FUNDAMENTAL,
    )


def concept(symbol: str, tag: str, taxonomy: str = "us-gaap", units: Optional[str] = None) -> pd.DataFrame:
    """Every reported value of one XBRL concept, e.g. ``Revenues``."""
    cik = cik_for(symbol)
    payload = get_json(
        "{}/api/xbrl/companyconcept/CIK{}/{}/{}.json".format(DATA, _pad(cik), taxonomy, tag),
        headers=_headers(),
        ttl=TTL_FUNDAMENTAL,
    )
    frames = []
    for unit, values in (payload.get("units") or {}).items():
        if units and unit != units:
            continue
        f = pd.DataFrame(values)
        f["unit"] = unit
        frames.append(f)
    if not frames:
        raise EmptyDataError("No {} data reported by {}".format(tag, symbol))
    df = pd.concat(frames, ignore_index=True)
    df["label"] = payload.get("label")
    df["end"] = pd.to_datetime(df["end"], errors="coerce")
    return df.sort_values("end")


def frames(tag: str, period: str, taxonomy: str = "us-gaap", unit: str = "USD") -> pd.DataFrame:
    """Cross-section of one concept across every filer for a period.

    ``period`` uses SEC frame notation: ``CY2023`` (annual), ``CY2023Q4``
    (quarterly duration) or ``CY2023Q4I`` (instantaneous, for balance items).
    """
    payload = get_json(
        "{}/api/xbrl/frames/{}/{}/{}/{}.json".format(DATA, taxonomy, tag, unit, period),
        headers=_headers(),
        ttl=TTL_FUNDAMENTAL,
    )
    data = payload.get("data") or []
    if not data:
        raise EmptyDataError("No {} frame data for {}".format(tag, period))
    df = pd.DataFrame(data)
    df["cik"] = df["cik"].map(_pad)
    return df


# Concept maps: first tag present wins. US GAAP filers use several synonyms for
# the same line item, and small caps often report only the older ones.
INCOME_TAGS: Dict[str, Sequence[str]] = {
    "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"),
    "cost_of_revenue": ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"),
    "gross_profit": ("GrossProfit",),
    "research_and_development": ("ResearchAndDevelopmentExpense",),
    "selling_general_and_admin": ("SellingGeneralAndAdministrativeExpense",
                                  "GeneralAndAdministrativeExpense"),
    "total_operating_expenses": ("OperatingExpenses", "CostsAndExpenses"),
    "operating_income": ("OperatingIncomeLoss",),
    "interest_expense": ("InterestExpense", "InterestIncomeExpenseNet"),
    "pretax_income": ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                      "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"),
    "income_tax_expense": ("IncomeTaxExpenseBenefit",),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "eps_basic": ("EarningsPerShareBasic",),
    "eps_diluted": ("EarningsPerShareDiluted",),
    "weighted_average_shares_basic": ("WeightedAverageNumberOfSharesOutstandingBasic",),
    "weighted_average_shares_diluted": ("WeightedAverageNumberOfDilutedSharesOutstanding",),
}

BALANCE_TAGS: Dict[str, Sequence[str]] = {
    "cash_and_equivalents": ("CashAndCashEquivalentsAtCarryingValue",),
    # Only balance-sheet concepts belong here. NVIDIA, for one, stopped tagging
    # any of these after FY2025, and the neighbouring tags are not substitutes:
    # its maturity schedule (securities due within a year) and its total
    # available-for-sale balance bracket the reported current line rather than
    # matching it. A blank line is honest; a number from a different concept is
    # not.
    "short_term_investments": ("MarketableSecuritiesCurrent", "ShortTermInvestments",
                               "AvailableForSaleSecuritiesDebtSecuritiesCurrent"),
    "accounts_receivable": ("AccountsReceivableNetCurrent",),
    "inventory": ("InventoryNet",),
    "total_current_assets": ("AssetsCurrent",),
    "property_plant_equipment": ("PropertyPlantAndEquipmentNet",),
    "goodwill": ("Goodwill",),
    "intangible_assets": ("IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet"),
    "long_term_investments": ("MarketableSecuritiesNoncurrent", "LongTermInvestments"),
    "total_assets": ("Assets",),
    "accounts_payable": ("AccountsPayableCurrent",),
    "short_term_debt": ("LongTermDebtCurrent", "CommercialPaper", "DebtCurrent"),
    "deferred_revenue": ("ContractWithCustomerLiabilityCurrent", "DeferredRevenueCurrent"),
    "total_current_liabilities": ("LiabilitiesCurrent",),
    "long_term_debt": ("LongTermDebtNoncurrent", "LongTermDebt"),
    "total_liabilities": ("Liabilities",),
    "common_stock_and_apic": ("CommonStocksIncludingAdditionalPaidInCapital", "CommonStockValue"),
    "retained_earnings": ("RetainedEarningsAccumulatedDeficit",),
    "accumulated_oci": ("AccumulatedOtherComprehensiveIncomeLossNetOfTax",),
    "total_equity": ("StockholdersEquity",
                     "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    "total_liabilities_and_equity": ("LiabilitiesAndStockholdersEquity",),
}

CASH_TAGS: Dict[str, Sequence[str]] = {
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    # "Depreciation" last: it excludes amortisation, so it is only right for a
    # filer that tags nothing broader. Microsoft is one of them.
    "depreciation_and_amortization": ("DepreciationDepletionAndAmortization",
                                      "DepreciationAmortizationAndAccretionNet",
                                      "DepreciationAndAmortization",
                                      "DepreciationAmortizationAndOther",
                                      "Depreciation"),
    "stock_based_compensation": ("ShareBasedCompensation",),
    "deferred_income_tax": ("DeferredIncomeTaxExpenseBenefit",),
    "change_in_working_capital": ("IncreaseDecreaseInOperatingCapital",),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",
                            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    "capital_expenditure": ("PaymentsToAcquirePropertyPlantAndEquipment",
                            "PaymentsToAcquireProductiveAssets"),
    "acquisitions": ("PaymentsToAcquireBusinessesNetOfCashAcquired",),
    "investing_cash_flow": ("NetCashProvidedByUsedInInvestingActivities",),
    "dividends_paid": ("PaymentsOfDividendsCommonStock", "PaymentsOfDividends"),
    "share_repurchase": ("PaymentsForRepurchaseOfCommonStock",),
    "debt_issued": ("ProceedsFromIssuanceOfLongTermDebt",),
    "debt_repaid": ("RepaymentsOfLongTermDebt",),
    "financing_cash_flow": ("NetCashProvidedByUsedInFinancingActivities",),
    "net_change_in_cash": (
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
        "CashAndCashEquivalentsPeriodIncreaseDecrease",
    ),
}

STATEMENT_TAGS = {"income": INCOME_TAGS, "balance": BALANCE_TAGS, "cash": CASH_TAGS}


# Fiscal calendars run 52 or 53 weeks, so period lengths wobble around the
# nominal 91/364 days.
_QUARTER_DAYS = (65, 115)
_ANNUAL_DAYS = (320, 400)


def _dedup(frame: pd.DataFrame) -> pd.Series:
    # A period can be restated in later filings: keep the newest filing.
    frame = frame.sort_values("filed").drop_duplicates("end", keep="last")
    return pd.to_numeric(frame.set_index("end")["val"], errors="coerce").sort_index()


def _quarterize(frame: pd.DataFrame) -> pd.Series:
    """Three-month values from a mix of quarterly, YTD and full-year spans.

    10-Qs file the income statement both ways but the cash-flow statement
    YTD-only, and no one files fiscal Q4 on its own — difference spans that
    share a fiscal-year start to recover the missing quarters
    (Q2 = 6m - 3m, Q3 = 9m - 6m, Q4 = FY - 9m).
    """
    frame = frame.copy()
    frame["start"] = pd.to_datetime(frame["start"], errors="coerce")
    frame["val"] = pd.to_numeric(frame["val"], errors="coerce")
    frame = frame.dropna(subset=["start", "val"])
    frame = frame.sort_values("filed").drop_duplicates(["start", "end"], keep="last")
    quarters: Dict[Any, float] = {
        r.end: r.val for r in frame.itertuples()
        if _QUARTER_DAYS[0] <= r.days <= _QUARTER_DAYS[1]
    }
    longer = frame[frame["days"] > _QUARTER_DAYS[1]].sort_values("days")
    for r in longer.itertuples():
        if r.end in quarters:
            continue
        prior = frame[((frame["start"] - r.start).abs().dt.days <= 7)
                      & ((r.end - frame["end"]).dt.days.between(60, 120))]
        if len(prior):
            quarters[r.end] = r.val - prior.sort_values("end").iloc[-1]["val"]
    # Last resort for Q4 when the nine-month YTD was never tagged.
    for r in longer[longer["days"].between(*_ANNUAL_DAYS)].itertuples():
        if r.end in quarters:
            continue
        # Timestamps explicitly: the keys can arrive as numpy datetimes, and
        # subtracting from one of those goes through numpy's deprecated
        # generic-unit timedelta path.
        end = pd.Timestamp(r.end)
        window_opens = end - pd.Timedelta(340, unit="D")
        three = [v for e, v in quarters.items() if window_opens < pd.Timestamp(e) < end]
        if len(three) == 3:
            quarters[r.end] = r.val - sum(three)
    return pd.Series(quarters, dtype="float64").sort_index().dropna()


def _fact_series(facts: Dict[str, Any], tags: Sequence[str], forms: Sequence[str],
                 period: str = "annual") -> pd.Series:
    """Best available tag as {period_end: value}.

    Tag order is a preference list, not a priority queue: filers migrate
    between synonyms and leave the old tag behind with a few stale years on it.
    Apple last tagged ``PaymentsOfDividendsCommonStock`` in 2017 and has used
    ``PaymentsOfDividends`` ever since — taking the first tag with *any* data
    would put a 2017 dividend on a 2025 cash-flow statement, and leave every
    year since blank.

    So every tag is evaluated and the one reaching the most recent period wins,
    with more history breaking a tie on that. Order still decides an exact tie,
    which is what keeps a deliberately-last fallback (``Depreciation``, which
    excludes amortisation) behind the broader tag it stands in for.
    """
    gaap = facts.get("facts", {}).get("us-gaap", {})
    quarterly = period != "annual"
    best = pd.Series(dtype="float64")
    best_rank: Optional[Tuple[Any, int]] = None
    for tag in tags:
        node = gaap.get(tag)
        if not node:
            continue
        for unit, unit_values in (node.get("units") or {}).items():
            rows = [r for r in unit_values if r.get("form") in forms and r.get("val") is not None]
            if not rows:
                continue
            frame = pd.DataFrame(rows)
            frame["end"] = pd.to_datetime(frame["end"], errors="coerce")
            frame = frame.dropna(subset=["end"])
            if "start" not in frame.columns:
                # Instantaneous facts (balance-sheet items) have no duration.
                series = _dedup(frame)
            else:
                # Duration facts: a 10-Q carries three-month and YTD values with
                # the same end date, a 10-K the full year — keep only the span
                # that was asked for.
                frame["days"] = (frame["end"] - pd.to_datetime(frame["start"], errors="coerce")).dt.days
                if not quarterly:
                    series = _dedup(frame[frame["days"].between(*_ANNUAL_DAYS)])
                elif "/" in unit or unit == "shares":
                    # Per-share figures and weighted-average share counts are
                    # not additive, so YTD spans cannot be differenced: keep
                    # only spans filed as three months (Q4 stays empty).
                    series = _dedup(frame[frame["days"].between(*_QUARTER_DAYS)])
                else:
                    series = _quarterize(frame)
            if series.empty:
                continue
            rank = (series.index.max(), len(series))
            if best_rank is None or rank > best_rank:
                best, best_rank = series, rank
    return best


def statement(symbol: str, kind: str = "income", period: str = "annual", limit: int = 12) -> pd.DataFrame:
    """Build a financial statement from the filer's own XBRL facts.

    Quarterly income and cash-flow columns are true three-month figures;
    fiscal Q4 is derived from the 10-K full year, so per-share and share-count
    columns (which cannot be subtracted) are left empty for Q4 periods.
    """
    if kind not in STATEMENT_TAGS:
        raise ValueError("kind must be income, balance or cash")
    forms = ("10-K", "20-F", "40-F") if period == "annual" else ("10-Q", "10-K")
    facts = company_facts(cik_for(symbol))
    columns: Dict[str, pd.Series] = {}
    for label, tags in STATEMENT_TAGS[kind].items():
        series = _fact_series(facts, tags, forms, period)
        if not series.empty:
            columns[label] = series
    if not columns:
        raise EmptyDataError("No XBRL {} statement data for {}".format(kind, symbol))
    df = pd.DataFrame(columns).sort_index()
    df.index.name = "period_ending"
    df.insert(0, "symbol", symbol.upper())
    df.insert(1, "fiscal_period", "FY" if period == "annual" else "Q")
    return df.tail(limit)


def employee_count(symbol: str) -> pd.DataFrame:
    """``dei:EntityNumberOfEmployees`` as reported on the filing cover page."""
    facts = company_facts(cik_for(symbol))
    node = facts.get("facts", {}).get("dei", {}).get("EntityNumberOfEmployees")
    if not node:
        raise EmptyDataError("{} does not tag employee count in XBRL".format(symbol))
    rows: List[Dict[str, Any]] = []
    for unit_values in (node.get("units") or {}).values():
        rows.extend(unit_values)
    df = pd.DataFrame(rows).sort_values("filed").drop_duplicates("end", keep="last")
    df["end"] = pd.to_datetime(df["end"], errors="coerce")
    return (
        df[["end", "val", "form", "filed"]]
        .rename(columns={"end": "period_ending", "val": "employees"})
        .sort_values("period_ending")
        .reset_index(drop=True)
    )


# --------------------------------------------------------------------------- #
# Short interest / fails to deliver
# --------------------------------------------------------------------------- #
@cached("sec.ftd_slice", ttl=TTL_REFERENCE)
def _ftd_slice(symbol: str, year: int, month: int, half: str) -> pd.DataFrame:
    """One ticker's rows out of a national fails-to-deliver file.

    The published files cover every CNS-eligible security and run to millions
    of rows, so only the requested slice is cached — the raw archive is already
    memoised by the HTTP layer, which keeps a repeat lookup cheap without
    storing a multi-hundred-megabyte frame per file.
    """
    url = "{}/files/data/fails-deliver-data/cnsfails{:04d}{:02d}{}.zip".format(BASE, year, month, half)
    body = fetch(url, headers=_headers(), ttl=TTL_REFERENCE)
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as fh:
            df = pd.read_csv(fh, sep="|", encoding="latin-1", on_bad_lines="skip")
    df.columns = [str(c).strip().lower() for c in df.columns]
    sym_col = next((c for c in df.columns if "symbol" in c), None)
    if sym_col is None:
        return pd.DataFrame()
    return df[df[sym_col].astype(str).str.upper() == symbol.upper()].copy()


def fails_to_deliver(symbol: str, months: int = 3) -> pd.DataFrame:
    """CNS fails-to-deliver history for a ticker (SEC publishes twice monthly).

    Each additional month means downloading another national archive, so the
    first call for a given window is slow; subsequent ones are served from cache.
    """
    symbol = symbol.upper()
    today = date.today()
    rows: List[pd.DataFrame] = []
    errors: List[str] = []
    for i in range(months):
        y, m = divmod(today.year * 12 + (today.month - 1) - i, 12)
        m += 1
        for half in ("a", "b"):
            try:
                hit = _ftd_slice(symbol, y, m, half)
            except Exception as exc:  # noqa: BLE001 - the newest half-month may not exist yet
                errors.append("{}-{:02d}{}: {}".format(y, m, half, exc))
                continue
            if not hit.empty:
                rows.append(hit)
    if not rows:
        raise EmptyDataError("No fails-to-deliver records for {} in the last {} months".format(symbol, months))
    out = pd.concat(rows, ignore_index=True)
    out = out.rename(columns={"settlement date": "settlement_date", "cusip": "cusip",
                              "quantity (fails)": "quantity", "description": "description",
                              "price": "price"})
    if "settlement_date" in out.columns:
        out["settlement_date"] = pd.to_datetime(out["settlement_date"], format="%Y%m%d", errors="coerce")
        out = out.sort_values("settlement_date")
    return out.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Reference lists & feeds
# --------------------------------------------------------------------------- #
@cached("sec.rss", ttl=1800)
def press_releases(limit: int = 40) -> pd.DataFrame:
    root = get_xml(BASE + "/news/pressreleases.rss", headers=_headers(), ttl=1800)
    rows = []
    for item in root.iter("item"):
        rows.append({strip_ns(c.tag): (c.text or "").strip() for c in item})
    if not rows:
        raise EmptyDataError("SEC press-release feed was empty")
    df = pd.DataFrame(rows)
    if "pubDate" in df.columns:
        df["date"] = pd.to_datetime(df["pubDate"], errors="coerce", utc=True)
    return df.head(limit)


@cached("sec.sic", ttl=TTL_REFERENCE)
def sic_codes() -> pd.DataFrame:
    """SIC industry classification list used throughout EDGAR."""
    url = BASE + "/corpfin/division-of-corporation-finance-standard-industrial-classification-sic-code-list"
    tables = pd.read_html(io.StringIO(get_text(url, headers=_headers(), ttl=TTL_REFERENCE)))
    df = max(tables, key=len)
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    return df


@cached("sec.datasets", ttl=TTL_REFERENCE)
def financial_statement_datasets() -> pd.DataFrame:
    """Index of SEC's quarterly "Financial Statement Data Sets" bulk archives."""
    today = date.today()
    rows = []
    for year in range(2009, today.year + 1):
        for q in range(1, 5):
            if year == today.year and (q - 1) * 3 + 1 > today.month:
                continue
            rows.append(
                {
                    "year": year,
                    "quarter": "Q{}".format(q),
                    "url": "{}/files/dera/data/financial-statement-data-sets/{}q{}.zip".format(BASE, year, q),
                }
            )
    return pd.DataFrame(rows)
