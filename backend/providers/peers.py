"""Who a company's comparables actually are, from three sources that disagree.

A peer list is an opinion, and the usual free one — the vendor's industry
bucket — is a single opinion with no evidence behind it. Yahoo files Apple under
"Consumer Electronics" alongside a hearing-aid maker; it files Amazon under
"Internet Retail", where its largest profit pool, AWS, has no comparable at all.
Used alone it is fine for a sanity check and poor for a comparison you intend to
act on.

So three sources are read and made to agree, none of them paid:

* **Classification** — Yahoo's industry (or sector) list, which is fast, covers
  every listed name, and carries each company's weight in the industry.
* **Registration** — every SEC registrant filing 10-Ks under the same SIC code.
  This is the *filer's own* classification, chosen when it registered, and it
  catches companies Yahoo files elsewhere.
* **Filings** — companies whose own 10-K names this one next to a competition
  phrase ("we compete with X", "our principal competitors include X"). This is
  the only source where somebody has actually said the two companies compete,
  and it is the mirror of the concentration mining in
  :mod:`backend.providers.supplychain`.

Agreement does most of the ranking: a name all three sources return outranks one
that a single source produced. Size does the rest, because the mentions are
asymmetric — everybody names the giant in their industry, so a $20m shell listing
Pfizer among its competitors is telling the truth about its ambitions and nothing
useful about Pfizer. Each order of magnitude between the two market caps halves
what a candidate's evidence is worth.

Every row carries the sources that found it, and a filings row carries the
newest filing that named it, so the reader can see why it is there and throw it
out. That last part matters: a peer group is a judgement, and the stock page
lets this one be edited.
"""
from __future__ import annotations

import math
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from ..core.caching import TTL_FUNDAMENTAL, TTL_REFERENCE, cached
from ..core.errors import EmptyDataError
from ..core.http import get_json, get_text
from . import sec, supplychain, yahoo

NAME = "yahoo"

EFTS = "https://efts.sec.gov/LATEST/search-index"
BROWSE = "https://www.sec.gov/cgi-bin/browse-edgar"

# How a 10-K introduces the people it competes with. ANDed with the subject's
# name one phrase at a time, because EDGAR full-text search has no OR.
COMPETITION_PHRASES: Tuple[str, ...] = (
    "compete with",
    "compete against",
    "competition from",
    "our competitors",
    "principal competitors",
    "competitors include",
)

# What each source is worth when they disagree. Being named in someone's 10-K
# outranks sharing a classification: one is a statement, the others are filing
# cabinets.
WEIGHTS: Dict[str, float] = {"filings": 3.0, "classification": 2.0, "registration": 1.0}

# Two of the sources come back in a meaningful order — the classification by
# weight in the industry, the filings by how many named the company — so a
# name's position in them is worth something. EDGAR's registrant list is
# ordered by nothing a reader cares about, and is scored flat.
ORDERED: frozenset = frozenset({"classification", "filings"})

# A registration-only name is a weak peer — the SIC list runs to hundreds of
# companies, most of them tiny — so those fill the tail of the list rather than
# competing for the top of it.
_SOLE_REGISTRATION_PENALTY = 0.5

# An industry holding fewer names than this is too thin to compare within.
_THIN_INDUSTRY = 12

# How many candidates get priced before the final ranking. Size matters to a
# comparison and none of the three sources knows it, but a market cap is a
# request per company, so only the contenders are looked up.
_PRICED = 24

_CIK = re.compile(r"<cik>(\d+)</cik>", re.I)


def _headers() -> Dict[str, str]:
    return sec._headers()  # noqa: SLF001 - one User-Agent policy for all of EDGAR


# --------------------------------------------------------------------------- #
# The subject
# --------------------------------------------------------------------------- #
def subject(symbol: str) -> Dict[str, Any]:
    """What the three sources need to know about the company being compared."""
    out: Dict[str, Any] = {"symbol": symbol.upper()}
    try:
        info = yahoo.info(symbol)
        out.update(
            name=info.get("longName") or info.get("shortName"),
            industry=info.get("industry"), industry_key=info.get("industryKey"),
            sector=info.get("sector"), sector_key=info.get("sectorKey"),
            market_cap=info.get("marketCap"),
        )
    except Exception:  # noqa: BLE001 - the filings still classify it
        pass
    try:
        cik = sec.cik_for(symbol)
        filed = sec.submissions(cik)
        out.update(cik=cik, sic=str(filed.get("sic") or "") or None,
                   sic_description=filed.get("sicDescription"),
                   name=out.get("name") or filed.get("name"))
    except Exception:  # noqa: BLE001 - a foreign listing may not file at all
        pass
    return out


# --------------------------------------------------------------------------- #
# Source 1 — the classification
# --------------------------------------------------------------------------- #
def by_classification(subject_info: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Yahoo's industry list, widened to the sector where the industry is thin.

    Some industries are barely populated — Yahoo files Apple under "Consumer
    Electronics", where the next names down are a headset maker and a
    soundbar company. Where the bucket holds fewer than
    :data:`_THIN_INDUSTRY` names, the sector list is appended behind it: a
    coarser match, ranked lower, but better than a list of minnows.
    """
    rows: List[Dict[str, Any]] = []
    if subject_info.get("industry_key"):
        rows = _classified(lambda: yahoo.industry(subject_info["industry_key"], "top_companies"))
    if len(rows) < _THIN_INDUSTRY and subject_info.get("sector_key"):
        seen = {row["symbol"] for row in rows}
        rows += [row for row in _classified(
            lambda: yahoo.sector(subject_info["sector_key"], "top_companies"))
            if row["symbol"] not in seen]
    if not rows:
        raise EmptyDataError("Yahoo does not classify {} into an industry".format(
            subject_info["symbol"]))
    return rows


def _classified(fetch: Callable[[], Any]) -> List[Dict[str, Any]]:
    """One of Yahoo's ranked company lists, as rows."""
    try:
        frame = pd.DataFrame(fetch())
    except Exception:  # noqa: BLE001 - an empty list is handled by the caller
        return []
    if frame.empty:
        return []
    frame = frame.reset_index()
    symbol_col = frame.columns[0]
    name_col = next((c for c in frame.columns if str(c).lower() in ("name", "company name")), None)
    weight_col = next((c for c in frame.columns if "weight" in str(c).lower()), None)
    return [
        {
            "symbol": str(row[symbol_col]).upper(),
            "company": str(row[name_col]) if name_col else None,
            "industry_weight": (None if weight_col is None or pd.isna(row[weight_col])
                                else float(row[weight_col])),
        }
        for _, row in frame.iterrows()
        if str(row[symbol_col]).strip()
    ]


# --------------------------------------------------------------------------- #
# Source 2 — the registration
# --------------------------------------------------------------------------- #
@cached("peers.sic", ttl=TTL_REFERENCE)
def _sic_registrants(sic: str, pages: int = 3) -> List[str]:
    """Every CIK filing 10-Ks under one SIC code, oldest registration first.

    EDGAR's company browser is the only free index of who registered under
    which code. Its Atom output is half-broken — the company name comes back as
    a Perl array reference — but the CIK is sound, and the ticker map turns that
    into a symbol and a name.
    """
    found: List[str] = []
    for page in range(pages):
        try:
            body = get_text(
                BROWSE,
                params={"action": "getcompany", "SIC": sic, "type": "10-K", "dateb": "",
                        "owner": "include", "count": 100, "start": page * 100,
                        "output": "atom"},
                headers=_headers(), ttl=TTL_REFERENCE,
            )
        except Exception:  # noqa: BLE001 - a partial list is still a list
            break
        page_ciks = _CIK.findall(body)
        found.extend(page_ciks)
        if len(page_ciks) < 100:
            break
    return found


def by_registration(subject_info: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Listed companies that registered with the SEC under the same SIC code."""
    sic = subject_info.get("sic")
    if not sic:
        raise EmptyDataError("{} has no SEC registration to match".format(
            subject_info["symbol"]))
    registrants = _sic_registrants(str(sic))
    if not registrants:
        raise EmptyDataError("EDGAR lists no other 10-K filers under SIC {}".format(sic))
    listed = _listed()
    rows: List[Dict[str, Any]] = []
    for cik in registrants:
        hit = listed.get(str(cik).zfill(10))
        if hit:
            rows.append({"symbol": hit[0], "company": hit[1], "sic": str(sic)})
    return rows


@cached("peers.listed", ttl=TTL_REFERENCE)
def _listed() -> Dict[str, Tuple[str, str]]:
    """``{cik: (symbol, company)}`` — the first ticker each registrant lists.

    A company with several share classes appears once per ticker; the first is
    the one a reader means by "the stock".
    """
    frame = sec.company_map()
    out: Dict[str, Tuple[str, str]] = {}
    for row in frame.itertuples():
        out.setdefault(str(row.cik), (str(row.symbol).upper(), str(row.name)))
    return out


# --------------------------------------------------------------------------- #
# Source 3 — the filings
# --------------------------------------------------------------------------- #
@cached("peers.competitors", ttl=TTL_FUNDAMENTAL)
def _competitor_hits(name: str, years: int) -> List[Dict[str, Any]]:
    """Filings naming ``name`` alongside a competition phrase."""
    end = pd.Timestamp.today().normalize()
    start = end - pd.DateOffset(years=years)

    def one(phrase: str) -> List[Dict[str, Any]]:
        try:
            body = get_json(
                EFTS,
                params={"q": '"{}" "{}"'.format(name, phrase), "forms": "10-K",
                        "startdt": str(start.date()), "enddt": str(end.date())},
                headers=_headers(), ttl=TTL_FUNDAMENTAL,
            )
        except Exception:  # noqa: BLE001 - one dead phrase must not sink the search
            return []
        return body.get("hits", {}).get("hits", [])

    with ThreadPoolExecutor(max_workers=len(COMPETITION_PHRASES)) as pool:
        pages = list(pool.map(one, COMPETITION_PHRASES))
    return [hit for page in pages for hit in page]


def by_filings(subject_info: Dict[str, Any], years: int = 3) -> List[Dict[str, Any]]:
    """Companies whose annual report names this one as competition.

    Full-text search matches a phrase and a name anywhere in the same document,
    which is loose: a filing can mention both without meaning that they compete.
    The filter is the filer's own SIC code, which comes back with the hit for
    nothing — a biotech naming a chip designer in some other context is not a
    comparable, and its registration says so.
    """
    name = subject_info.get("search_name") or subject_info.get("name")
    if not name:
        raise EmptyDataError("No name to search EDGAR with")
    group = str(subject_info.get("sic") or "")[:2]

    merged: Dict[str, Dict[str, Any]] = {}
    for hit in _competitor_hits(str(name), years):
        source = hit.get("_source") or {}
        cik = (source.get("ciks") or [""])[0]
        symbol = _live_ticker(cik, (source.get("display_names") or [""])[0])
        sic = str((source.get("sics") or [""])[0] or "")
        if not cik or not symbol or symbol == subject_info["symbol"]:
            continue
        if group and sic[:2] != group:
            continue
        row = merged.setdefault(symbol, {
            "symbol": symbol, "company": _display_name(source), "sic": sic or None,
            "mentions": 0, "form": None, "filed": None, "filing_url": None,
        })
        row["mentions"] += 1
        filed = source.get("file_date")
        if filed and (row["filed"] is None or filed > row["filed"]):
            row.update(filed=filed, form=source.get("form"),
                       filing_url=_document_url(hit, cik))
    return sorted(merged.values(), key=lambda r: -r["mentions"])


def _live_ticker(cik: str, display: str) -> Optional[str]:
    """The symbol a filer trades under *now*.

    A search hit names the filer as it was written on the filing, and tickers
    change: SMART Global Holdings filed as SGH and trades as PENG. The ticker
    in brackets is used when it is still listed — it disambiguates a company
    with several share classes — and the CIK map answers when it is not.
    """
    quoted = supplychain._ticker_from_display(display)  # noqa: SLF001
    if quoted and quoted in _tickers():
        return quoted
    hit = _listed().get(str(cik).zfill(10))
    return hit[0] if hit else None


@cached("peers.tickers", ttl=TTL_REFERENCE)
def _tickers() -> frozenset:
    """Every symbol currently registered with the SEC."""
    return frozenset(str(s).upper() for s in sec.company_map()["symbol"])


def _display_name(source: Dict[str, Any]) -> Optional[str]:
    """``"NVIDIA CORP  (NVDA)  (CIK 0001045810)"`` -> ``"NVIDIA CORP"``."""
    raw = (source.get("display_names") or [None])[0]
    return None if not raw else str(raw).split("  (")[0].strip()


def _document_url(hit: Dict[str, Any], cik: str) -> str:
    adsh = (hit.get("_source") or {}).get("adsh", "")
    document = str(hit.get("_id", "")).split(":")[-1]
    return "https://www.sec.gov/Archives/edgar/data/{}/{}/{}".format(
        str(cik).lstrip("0"), adsh.replace("-", ""), document)


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #
def _safely(fetch: Callable[[], List[Dict[str, Any]]]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    try:
        return fetch(), None
    except Exception as exc:  # noqa: BLE001 - the message is shown to the user
        return [], str(exc)


@cached("peers.group", ttl=TTL_FUNDAMENTAL)
def peer_group(symbol: str, limit: int = 12, years: int = 3
               ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Ranked comparables for ``symbol``, with the evidence for each.

    Returns ``(rows, meta)``. The three sources are gathered concurrently and
    none of them is allowed to fail the others: a company Yahoo does not
    classify still gets its SIC peers, and one that nobody names as competition
    still gets both classifications.
    """
    symbol = symbol.upper().strip()
    info = subject(symbol)
    try:
        info["search_name"], _ = supplychain.subject_names(symbol)
    except Exception:  # noqa: BLE001 - fall back to whatever name we have
        pass

    legs: Dict[str, Callable[[], List[Dict[str, Any]]]] = {
        "classification": lambda: by_classification(info),
        "registration": lambda: by_registration(info),
        "filings": lambda: by_filings(info, years),
    }
    with ThreadPoolExecutor(max_workers=len(legs)) as pool:
        gathered = dict(zip(legs, pool.map(_safely, legs.values())))

    merged: Dict[str, Dict[str, Any]] = {}
    sources: Dict[str, Any] = {}
    for source, (rows, error) in gathered.items():
        sources[source] = {"found": len(rows), "error": error}
        for rank, row in enumerate(rows):
            candidate = str(row.get("symbol") or "").upper()
            if not candidate or candidate == symbol:
                continue
            held = merged.setdefault(candidate, {
                "symbol": candidate, "company": None, "sources": [],
                "industry_weight": None, "sic": None, "mentions": 0, "market_cap": None,
                "form": None, "filed": None, "filing_url": None, "score": 0.0,
            })
            held["sources"].append(source)
            held["company"] = held["company"] or row.get("company")
            held["sic"] = held["sic"] or row.get("sic")
            for field in ("industry_weight", "mentions", "form", "filed", "filing_url"):
                if row.get(field) is not None:
                    held[field] = row[field]
            held["score"] += WEIGHTS[source] / (1 + rank / 10 if source in ORDERED else 1)

    if not merged:
        raise EmptyDataError(
            "Nothing comparable to {}: it has no industry classification, no SIC "
            "code shared with another listed filer, and no filing names it as "
            "competition.".format(symbol)
        )

    for row in merged.values():
        if row["sources"] == ["registration"]:
            row["score"] *= _SOLE_REGISTRATION_PENALTY
        row["agreement"] = len(row["sources"])

    shortlist = sorted(merged.values(), key=lambda r: -r["score"])[:_PRICED]
    _price(shortlist)
    for row in shortlist:
        row["score"] = round(row["score"] * _proximity(info.get("market_cap"),
                                                       row.get("market_cap")), 4)
        row["why"] = _why(row)

    ranked = sorted(shortlist, key=lambda r: (-r["score"], r["symbol"]))
    meta = {"subject": info, "sources": sources, "candidates": len(merged)}
    return ranked[:limit], meta


def _price(rows: List[Dict[str, Any]]) -> None:
    """Stamp each candidate with its market cap, in place and best-effort."""
    def one(row: Dict[str, Any]) -> None:
        try:
            found = yahoo.info(row["symbol"])
        except Exception:  # noqa: BLE001 - an unpriced row is ranked neutrally
            return
        row["market_cap"] = found.get("marketCap")
        row["company"] = row.get("company") or found.get("longName") or found.get("shortName")

    if rows:
        with ThreadPoolExecutor(max_workers=min(8, len(rows))) as pool:
            list(pool.map(one, rows))


def _proximity(subject_cap: Optional[float], peer_cap: Optional[float]) -> float:
    """How much of a candidate's evidence survives the size difference.

    Everybody names the giant in their industry, so the mentions are asymmetric:
    a $20m shell that lists Pfizer among its competitors is telling the truth
    about its ambitions and nothing useful about Pfizer. An order of magnitude
    of difference halves the evidence, two thirds it, and a company with no
    market cap to compare is left where it was.
    """
    if not subject_cap or not peer_cap or subject_cap <= 0 or peer_cap <= 0:
        return 1.0
    return 1 / (1 + abs(math.log10(subject_cap / peer_cap)))


def _why(row: Dict[str, Any]) -> str:
    """One line a reader can judge the row by."""
    said: List[str] = []
    if "filings" in row["sources"]:
        said.append("named as competition in {} filing{}".format(
            row["mentions"], "" if row["mentions"] == 1 else "s"))
    if "classification" in row["sources"]:
        said.append("same industry")
    if "registration" in row["sources"]:
        said.append("same SIC code")
    return ", ".join(said)


def symbols_for(symbol: str, limit: int = 8) -> List[str]:
    """Just the tickers, for callers that want a peer list and nothing else."""
    rows, _meta = peer_group(symbol, limit=limit)
    return [row["symbol"] for row in rows]
