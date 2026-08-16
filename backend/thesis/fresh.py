"""Fresh Form 4 detection — the path the bulk archive cannot cover.

The quarterly bulk file lags ~4.5 months, so anything recent has to come from
EDGAR itself. Two routes, both normalised into the *same* row schema as
:func:`backend.thesis.bulk.quarter` so the collapses and cluster logic work
unchanged on either source:

* **Per issuer** — the company's submissions feed lists its recent Form 4s;
  one fetch for the index, one per filing. Cheap enough to run per symbol on
  demand, which is what the thesis workflow needs when freezing evidence.
* **Market sweep** — the daily form index lists every Form 4 filed that day
  (~1,100-1,300 rows, but only ~550-640 distinct filings: the index repeats a
  filing under the issuer CIK *and* each reporting-owner CIK, so dedup on the
  accession is mandatory). Filtered to a universe of issuer CIKs before any
  document is fetched, a 500-name universe costs ~100 fetches per day swept.

Every fetched accession is immutable, so parsed filings cache at
``TTL_REFERENCE`` and a sweep's cost is paid once.

The seam with bulk is *filing date*: both sources are organised by the date a
filing was filed, so ``filing_date <= bulk watermark -> bulk, else fresh``
partitions them with no gap and no double counting; belt-and-braces, the union
is deduped on accession with bulk taking precedence.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set

import pandas as pd

from ..config import settings
from ..core.caching import TTL_INTRADAY, TTL_REFERENCE, cached
from ..core.errors import EmptyDataError
from ..core.http import fetch
from ..providers import sec
from .bulk import _AUTO_VEHICLE  # one definition of "automatic vehicle"

BASE = "https://www.sec.gov"

_XML_BLOCK = re.compile(rb"<XML>(.*?)</XML>", re.DOTALL)
_IDX_ROW = re.compile(r"^4(?:/A)?\s")
_ACCESSION = re.compile(r"(\d{10}-\d{2}-\d{6})\.txt")


def _headers() -> Dict[str, str]:
    return {"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}


def _text(node: Optional[ET.Element], path: str, default: str = "") -> str:
    if node is None:
        return default
    found = node.find(path)
    return (found.text or "").strip() if found is not None and found.text else default


def _flag(node: Optional[ET.Element], path: str) -> bool:
    return _text(node, path) in ("1", "true")


def parse_ownership_xml(xml_bytes: bytes, accession: str,
                        filing_date: Optional[str] = None) -> List[Dict[str, Any]]:
    """One Form 3/4/5 ``ownershipDocument`` -> normalised bulk-schema rows.

    Pure function — no network — so the parser is testable offline. Emits one
    row per (reporting owner x non-derivative transaction), matching how the
    bulk archive's SUBMISSION/REPORTINGOWNER/NONDERIV_TRANS join multiplies
    multi-owner filings.
    """
    # The <XML> wrapper leaves a leading newline, and ElementTree rejects an
    # XML declaration that is not at byte zero.
    root = ET.fromstring(xml_bytes.strip())
    doc_type = _text(root, "documentType")
    issuer = root.find("issuer")
    issuer_cik = _text(issuer, "issuerCik").lstrip("0") or "0"
    issuer_name = _text(issuer, "issuerName")
    symbol = _text(issuer, "issuerTradingSymbol").upper()

    aff_text = _text(root, "aff10b5One")
    aff: Optional[bool] = None
    if aff_text in ("1", "true"):
        aff = True
    elif aff_text in ("0", "false"):
        aff = False

    owners = []
    for owner_el in root.findall("reportingOwner"):
        rel = owner_el.find("reportingOwnerRelationship")
        owners.append({
            "owner_cik": _text(owner_el, "reportingOwnerId/rptOwnerCik").lstrip("0") or "0",
            "owner_name": _text(owner_el, "reportingOwnerId/rptOwnerName"),
            "is_officer": _flag(rel, "isOfficer"),
            "is_director": _flag(rel, "isDirector"),
            "is_ten_pct": _flag(rel, "isTenPercentOwner"),
            "is_other": _flag(rel, "isOther"),
            "title": _text(rel, "officerTitle"),
        })
    if not owners:
        return []

    rows: List[Dict[str, Any]] = []
    for txn in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        shares_text = _text(txn, "transactionAmounts/transactionShares/value")
        price_text = _text(txn, "transactionAmounts/transactionPricePerShare/value")
        after_text = _text(txn, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")
        try:
            shares = float(shares_text) if shares_text else None
            price = float(price_text) if price_text else None
            after = float(after_text) if after_text else None
        except ValueError:
            continue
        nature = _text(txn, "ownershipNature/natureOfOwnership/value")
        for owner in owners:
            rows.append({
                "accession": accession,
                "filing_date": pd.Timestamp(filing_date) if filing_date else pd.NaT,
                "trans_date": pd.Timestamp(_text(txn, "transactionDate/value") or None),
                "doc_type": doc_type,
                "issuer_cik": issuer_cik,
                "issuer_name": issuer_name,
                "symbol": symbol,
                **owner,
                "code": _text(txn, "transactionCoding/transactionCode").upper(),
                "shares": shares,
                "price": price,
                "value_usd": (shares * price) if shares is not None and price is not None else None,
                "acq_disp": _text(txn, "transactionAmounts/transactionAcquiredDisposedCode/value").upper(),
                "shares_after": after,
                "ownership_form": _text(txn, "ownershipNature/directOrIndirectOwnership/value").upper(),
                "nature": nature,
                "aff10b5one": aff,
                "auto_vehicle": bool(_AUTO_VEHICLE.search(nature)) if nature else False,
                "quarter": "fresh",
            })
    return rows


# v2: v1 briefly cached empty parses (XML declaration not at byte zero).
@cached("thesis.fresh.filing.v2", ttl=TTL_REFERENCE)
def _filing_rows(cik: str, accession: str, filing_date: str) -> List[Dict[str, Any]]:
    """Fetch one accession's full submission and parse the inline XML.

    The ``.txt`` full submission sidesteps the ``primaryDocument`` trap (that
    field points at the XSL-rendered HTML view, not the raw XML). Accessions
    are immutable, so this caches forever.
    """
    url = "{}/Archives/edgar/data/{}/{}.txt".format(BASE, int(cik), accession)
    body = fetch(url, headers=_headers(), ttl=TTL_REFERENCE)
    match = _XML_BLOCK.search(body)
    if not match:
        return []
    try:
        return parse_ownership_xml(match.group(1), accession, filing_date)
    except ET.ParseError:
        return []


def issuer_trades(symbol: str, days: int = 120) -> pd.DataFrame:
    """Recent Form 4 rows for one issuer, from its submissions feed.

    Mind the feed's cap: ``filings.recent`` holds ~1,000 rows, so for heavy
    filers a large ``days`` silently bottoms out (MGM's feed reaches back only
    to 2019). Fine for freshness — that is the one job this path has.
    """
    cik = sec.cik_for(symbol)
    payload = sec.submissions(cik)
    recent = payload.get("filings", {}).get("recent", {})
    if not recent:
        raise EmptyDataError("No filings indexed for {}".format(symbol))

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    wanted = [
        (recent["filingDate"][i], recent["accessionNumber"][i])
        for i in range(len(recent.get("form", [])))
        if recent["form"][i] in ("4", "4/A") and recent["filingDate"][i] >= cutoff
    ]
    rows: List[Dict[str, Any]] = []
    for filing_date, accession in wanted:
        rows.extend(_filing_rows(cik, accession, filing_date))
    if not rows:
        raise EmptyDataError(
            "No Form 4 filings for {} in the last {} days".format(symbol, days)
        )
    return pd.DataFrame(rows)


def _quarter_of(day: date) -> int:
    return (day.month - 1) // 3 + 1


@cached("thesis.fresh.dailyidx.v1", ttl=TTL_REFERENCE)
def _daily_index(day_iso: str) -> List[List[str]]:
    """Parsed Form 4 rows of one day's form index: [company, cik, accession]."""
    day = date.fromisoformat(day_iso)
    url = "{}/Archives/edgar/daily-index/{}/QTR{}/form.{}.idx".format(
        BASE, day.year, _quarter_of(day), day.strftime("%Y%m%d")
    )
    text = fetch(url, headers=_headers(), ttl=TTL_REFERENCE).decode("latin-1")
    rows: List[List[str]] = []
    for line in text.splitlines():
        if not _IDX_ROW.match(line):
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5:
            continue
        accession = _ACCESSION.search(parts[-1])
        if accession:
            rows.append([parts[1], parts[2], accession.group(1)])
    return rows


def sweep(universe_ciks: Iterable[str], days: int = 5) -> pd.DataFrame:
    """Form 4 rows for a universe over the last ``days`` business days.

    The daily index double-lists each filing (issuer row + one per owner);
    filtering on *issuer* CIKs before fetching keeps one row per filing and is
    what makes the sweep affordable: ~100 document fetches per day for a
    500-name universe, each cached forever after the first sweep.
    """
    wanted: Set[str] = {str(int(str(c).lstrip("0") or 0)) for c in universe_ciks}
    rows: List[Dict[str, Any]] = []
    day = date.today()
    seen_days = 0
    misses = 0
    while seen_days < days and misses < 6:  # weekends/holidays return 404
        day_iso = day.isoformat()
        day -= timedelta(days=1)
        try:
            index_rows = _daily_index(day_iso)
        except Exception:  # noqa: BLE001 - non-business day or not yet published
            misses += 1
            continue
        misses = 0
        seen_days += 1
        for _company, cik, accession in index_rows:
            # Long company names shift the fixed-width columns for a few rows
            # per day; a non-numeric "CIK" is one of those, not an error.
            try:
                cik_key = str(int(cik))
            except ValueError:
                continue
            if cik_key not in wanted:
                continue
            rows.extend(_filing_rows(cik_key, accession, day_iso))
    if not rows:
        raise EmptyDataError("No Form 4 filings for the universe in the swept days")
    frame = pd.DataFrame(rows)
    return frame.drop_duplicates(subset=["accession", "owner_cik", "trans_date", "code",
                                         "shares", "price"]).reset_index(drop=True)


@cached("thesis.fresh.universe.v1", ttl=86_400)
def top_universe_ciks(n: int = 500) -> List[str]:
    """Issuer CIKs for the ~n largest US listings, for bounding a sweep.

    Built from the Nasdaq screener's market caps joined to the SEC ticker
    register. The point is cost: filtering the daily index to these CIKs
    keeps a sweep at ~100 document fetches per day instead of ~550.
    """
    from ..providers import markets

    table = markets.nasdaq_screener(limit=10_000)
    cap_col = next((c for c in ("market_cap", "marketcap") if c in table.columns), None)
    if cap_col is None:
        raise EmptyDataError("Nasdaq screener carried no market-cap column")
    table = table.assign(_cap=pd.to_numeric(table[cap_col], errors="coerce"))
    top = table.dropna(subset=["_cap"]).nlargest(int(n), "_cap")
    symbols = {str(s).replace(".", "-").upper().strip() for s in top["symbol"]}
    register = sec.company_map()
    matched = register[register["symbol"].isin(symbols)]
    return [str(int(c)) for c in matched["cik"]]
