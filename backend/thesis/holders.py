"""Filer-side 13F: what notable institutions actually hold.

13F-HR filings live under the *institution's* CIK, so an issuer's own filing
feed never contains them — asking "does a sovereign fund own MGM?" from MGM's
side is structurally unanswerable (the gap that stalled the original MGM
thesis's third leg). This module works from the filer's side instead: a
watchlist of funds whose positions are worth knowing, each resolved to its
CIK at runtime and its newest information table parsed into rows.

Honest limits, stated once here and repeated in the command output:

* A 13F reports long US-listed equity positions only, up to 45 days after
  quarter end — it is a *lagged quarterly snapshot*, never breaking news.
* Institutions below $100M in 13F securities do not file at all, and several
  big sovereigns (ADIA, QIA, KIA, GIC) have no findable 13F filer — absence
  of a filing is not absence of a position.
* Holdings name issuers by free-text name and CUSIP, not ticker; matching to
  a symbol goes through the SEC registrant name and is conservative.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ..config import settings
from ..core.caching import TTL_FUNDAMENTAL, TTL_REFERENCE, cached
from ..core.errors import EmptyDataError
from ..core.http import fetch
from ..providers import sec

BASE = "https://www.sec.gov"

_XML_BLOCKS = re.compile(rb"<XML>(.*?)</XML>", re.DOTALL)

#: Funds whose 13F is worth watching. ``query`` is the phrase resolved against
#: EDGAR full-text search over 13F-HR filings; resolution is name-filtered so
#: a fund merely *mentioned* in someone else's filing does not match.
WATCHLIST: Dict[str, Dict[str, str]] = {
    "pif": {"query": "Public Investment Fund", "label": "PIF (Saudi Arabia)", "kind": "sovereign"},
    "norges": {"query": "Norges Bank", "label": "Norges Bank (Norway)", "kind": "sovereign"},
    "temasek": {"query": "Temasek Holdings", "label": "Temasek (Singapore)", "kind": "sovereign"},
    "mubadala": {"query": "Mubadala Investment", "label": "Mubadala (Abu Dhabi)", "kind": "sovereign"},
    "gic": {"query": "GIC Private Limited", "label": "GIC (Singapore)", "kind": "sovereign"},
    "berkshire": {"query": "Berkshire Hathaway", "label": "Berkshire Hathaway", "kind": "strategic"},
    "icahn": {"query": "Icahn Carl", "label": "Carl Icahn", "kind": "activist"},
    "pershing": {"query": "Pershing Square Capital", "label": "Pershing Square", "kind": "activist"},
    "corvex": {"query": "Corvex Management", "label": "Corvex (Meister)", "kind": "activist"},
    "elliott": {"query": "Elliott Investment Management", "label": "Elliott", "kind": "activist"},
    "starboard": {"query": "Starboard Value", "label": "Starboard Value", "kind": "activist"},
    "thirdpoint": {"query": "Third Point LLC", "label": "Third Point", "kind": "activist"},
}


def _headers() -> Dict[str, str]:
    return {"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}


def _browse_filer(query: str) -> Optional[Dict[str, str]]:
    """EDGAR's company browse, which searches *filer names* directly.

    Needed because full-text search looks inside documents: querying
    "Berkshire Hathaway" over 13F-HRs matches every manager that *holds*
    Berkshire shares (the info tables name it as an issuer), and the actual
    filer drowns in those hits. The browse endpoint matches the company
    register instead.
    """
    from ..core.http import get_xml
    from ..core.http import strip_ns

    try:
        root = get_xml(
            BASE + "/cgi-bin/browse-edgar",
            params={"action": "getcompany", "company": query, "type": "13F-HR",
                    "output": "atom", "count": "10"},
            headers=_headers(), ttl=TTL_REFERENCE,
        )
    except Exception:  # noqa: BLE001 - resolution failure is a soft miss
        return None
    needle = query.upper()
    for element in root.iter():
        if strip_ns(element.tag) != "company-info":
            continue
        name = cik = None
        for child in element.iter():
            tag = strip_ns(child.tag)
            if tag == "conformed-name":
                name = (child.text or "").strip()
            elif tag == "cik":
                cik = (child.text or "").strip()
        if name and cik and needle in name.upper():
            return {"cik": str(int(cik)), "name": name}
    return None


@cached("thesis.holders.filer.v2", ttl=TTL_REFERENCE)
def resolve_filer(query: str) -> Optional[Dict[str, str]]:
    """Find a fund's filer CIK, name-filtered so mentions don't match.

    Two routes: full-text search over 13F-HRs (finds funds whose *own*
    filings carry the name), then the company-register browse as fallback
    for funds whose name floods the document index because everyone else
    holds their shares. A fund merely mentioned in another manager's filing
    never resolves — which is why ADIA correctly resolves to nothing.
    """
    try:
        hits = sec.full_text_search('"{}"'.format(query), forms="13F-HR", limit=20)
        needle = query.upper()
        matched = hits[hits["company"].str.upper().str.contains(needle, na=False, regex=False)]
        if not matched.empty:
            top = matched.groupby("cik").size().idxmax()
            name = matched[matched.cik == top].company.iloc[0]
            return {"cik": str(top), "name": re.sub(r"\s*\(CIK.*\)$", "", name).strip()}
    except EmptyDataError:
        pass
    return _browse_filer(query)


def parse_information_table(xml_bytes: bytes) -> List[Dict[str, Any]]:
    """One 13F ``informationTable`` -> position dicts. Pure, offline-testable.

    Values are as filed — whole dollars for periods since 2023 (thousands
    before the SEC's 2023 rule change; this module only reads recent filings).
    """
    root = ET.fromstring(xml_bytes.strip())
    if root.tag.split("}")[-1] != "informationTable":
        return []
    ns = root.tag.split("}")[0].lstrip("{") if "}" in root.tag else ""

    def text(node: ET.Element, path: str) -> str:
        if ns:
            path = "/".join("{%s}%s" % (ns, part) for part in path.split("/"))
        found = node.find(path)
        return (found.text or "").strip() if found is not None and found.text else ""

    rows: List[Dict[str, Any]] = []
    for entry in root.findall("{%s}infoTable" % ns if ns else "infoTable"):
        try:
            value = float(text(entry, "value") or 0)
            shares = float(text(entry, "shrsOrPrnAmt/sshPrnamt") or 0)
        except ValueError:
            continue
        rows.append({
            "issuer": text(entry, "nameOfIssuer"),
            "class": text(entry, "titleOfClass"),
            "cusip": text(entry, "cusip"),
            "value_usd": value,
            "shares": shares,
            "put_call": text(entry, "putCall") or None,
        })
    return rows


@cached("thesis.holders.13f.v1", ttl=TTL_FUNDAMENTAL)
def latest_13f(cik: str) -> Dict[str, Any]:
    """The newest 13F-HR for a filer CIK, parsed.

    The full ``.txt`` submission carries the cover page and the information
    table as separate ``<XML>`` blocks; the one whose root is
    ``informationTable`` is the holdings.
    """
    payload = sec.submissions(cik)
    recent = payload.get("filings", {}).get("recent", {})
    filings = [
        (recent["filingDate"][i], recent["accessionNumber"][i],
         (recent.get("reportDate") or [""] * len(recent["form"]))[i])
        for i in range(len(recent.get("form", [])))
        if recent["form"][i] == "13F-HR"
    ]
    if not filings:
        raise EmptyDataError("CIK {} has no 13F-HR on file".format(cik))
    filed, accession, period = filings[0]

    body = fetch("{}/Archives/edgar/data/{}/{}.txt".format(BASE, int(cik), accession),
                 headers=_headers(), ttl=TTL_REFERENCE)
    positions: List[Dict[str, Any]] = []
    for block in _XML_BLOCKS.findall(body):
        try:
            positions = parse_information_table(block)
        except ET.ParseError:
            continue
        if positions:
            break
    return {"cik": str(cik), "filed": filed, "period": period, "positions": positions}


def _tokens(name: str) -> List[str]:
    return re.sub(r"[^A-Z0-9 ]", " ", str(name).upper()).split()


#: Abbreviations 13F info tables use where the register spells it out.
_STOPWORDS = {"INC", "CORP", "CO", "LTD", "PLC", "GROUP", "HOLDINGS", "INTL",
              "INTERNATIONAL", "COMPANY", "CORPORATION", "INCORPORATED", "THE",
              "NEW", "CL", "A", "B", "COM", "SHS", "ADR"}


def name_match(registrant: str, holding_name: str) -> bool:
    """Conservative match between a registrant name and a 13F issuer name.

    The distinctive tokens (everything that is not boilerplate like INC/CORP)
    of one side must all appear, in order, at the start of the other's. "MGM
    Resorts International" matches "MGM RESORTS INTL"; it does not match
    "MGM Growth Properties".
    """
    a = [t for t in _tokens(registrant) if t not in _STOPWORDS]
    b = [t for t in _tokens(holding_name) if t not in _STOPWORDS]
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer[: len(shorter)] == shorter


def who_holds(symbol: str, funds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Which watched funds report a position in ``symbol``, one row per fund.

    Funds are always all reported — a fund with no match reports
    ``holds: False`` — because "PIF does not file MGM" is exactly as much of
    an answer as a position would be.
    """
    registrant_row = sec.company_map()
    match = registrant_row[registrant_row.symbol == str(symbol).upper().strip()]
    if match.empty:
        raise EmptyDataError("No SEC registrant for {!r}".format(symbol))
    registrant = str(match.iloc[0]["name"])

    slugs = [s for s in (funds or list(WATCHLIST)) if s in WATCHLIST]
    out: List[Dict[str, Any]] = []
    for slug in slugs:
        meta = WATCHLIST[slug]
        resolved = resolve_filer(meta["query"])
        if resolved is None:
            out.append({"fund": meta["label"], "kind": meta["kind"], "holds": None,
                        "note": "no 13F filer found under this name"})
            continue
        try:
            filing = latest_13f(resolved["cik"])
        except (EmptyDataError, Exception):  # noqa: BLE001 - report, don't fail the scan
            out.append({"fund": meta["label"], "kind": meta["kind"], "holds": None,
                        "note": "13F could not be fetched"})
            continue
        hits = [p for p in filing["positions"] if name_match(registrant, p["issuer"])]
        row: Dict[str, Any] = {
            "fund": meta["label"], "kind": meta["kind"],
            "filer": resolved["name"], "period": filing["period"],
            "filed": filing["filed"], "holds": bool(hits),
            "positions": len(filing["positions"]),
        }
        if hits:
            row["value_usd"] = round(sum(h["value_usd"] for h in hits), 0)
            row["shares"] = round(sum(h["shares"] for h in hits), 0)
        out.append(row)
    return out
