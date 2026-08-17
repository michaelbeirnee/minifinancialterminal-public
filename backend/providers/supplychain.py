"""Supply-chain relationships mined out of SEC filings.

Bloomberg's SPLC screen is built on a proprietary research database. There is
no free equivalent, but there is a free *primary source*: US filers must
disclose any counterparty that crosses a concentration threshold — ASC 280-10-50
for customers above 10% of revenue, plus the risk-factor and credit-risk notes
that name the same companies in prose. Those disclosures name the other side of
the relationship, quantify it as a percentage, and are filed annually.

So the graph is assembled by reading them:

* **Who supplies X** — EDGAR full-text search for filings that name X alongside
  a revenue-concentration phrase. A filer saying "sales to X accounted for 27%
  of our net sales" has just told us it is a supplier of X, and by how much.
* **Who buys from X** — the same corpus read the other way. A filer saying
  "products purchased from vendors … X 12%" is a *customer* of X. Distributors
  and resellers land here, which is what fills the right-hand column.
* **X's own disclosures** — X's latest annual report, read for the counterparties
  X itself names.

Everything carries the sentence it came from and a link to the filing, because
a mined relationship is only as good as the reader's ability to check it.

Coverage is therefore bounded by who files with the SEC: US registrants and
foreign issuers with an ADR (20-F). A privately held contract manufacturer that
files nowhere cannot appear, no matter how large the relationship.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from ..config import settings
from ..core.caching import TTL_FUNDAMENTAL, TTL_REFERENCE, cached
from ..core.errors import EmptyDataError
from ..core.http import get_json, get_text
from . import sec

NAME = "sec"

EFTS = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data"

# Concentration disclosures are boilerplate, and the boilerplate is finite.
# Each phrase is ANDed with the subject's name in a separate search because
# EDGAR full-text search has no OR; the union is the candidate pool, and how
# many phrases a filer matches is a decent first-pass relevance score.
DISCLOSURE_PHRASES: Tuple[str, ...] = (
    "of our net revenue",
    "of our net revenues",
    "of our total revenue",
    "of our revenue",
    "of our revenues",
    "of our net sales",
    "of our total sales",
    "of our sales",
    "of net revenues",
    "of total revenues",
    "of total revenue",
    "of total net revenue",
    "of our consolidated revenue",
    "of consolidated net revenue",
    "of our total purchases",
    "our largest customer",
    "our largest end customer",
    "our largest supplier",
    "of our accounts receivable",
)

# Legal-form noise to strip when working out how a filing would refer to the
# subject in prose: "QORVO, INC." is "Qorvo" in a sentence.
_SUFFIX = re.compile(
    r"[,.]?\s+(?:inc|incorporated|corp|corporation|company|co|ltd|limited|llc|lp|"
    r"plc|holdings?|group|technologies|international|n\.?v|s\.?a|a\.?g|s\.?p\.?a)\.?$",
    re.I,
)

# EDGAR's registrant index is a filing-cabinet label, not a name anyone writes:
# "NVIDIA CORP", "QUALCOMM INC/DE", "FORD MOTOR CO". Searching for the label as
# a phrase finds nothing, because no filing says "NVIDIA CORP" — so expand it
# back into prose before searching.
_REGISTER_TAIL = re.compile(r"\s*/[A-Z]{2,4}/?\s*$|\s+-ADR\s*$|\s+\(?NEW\)?\s*$", re.I)
_ABBREVIATIONS = {
    "CORP": "Corporation", "CO": "Company", "INC": "Inc", "LTD": "Ltd",
    "HLDGS": "Holdings", "HLDG": "Holdings", "GRP": "Group", "GP": "Group",
    "INTL": "International", "TECH": "Technologies", "TECHS": "Technologies",
    "SYS": "Systems", "SVCS": "Services", "SVC": "Services", "MFG": "Manufacturing",
    "IND": "Industries", "INDS": "Industries", "LABS": "Laboratories",
    "PHARM": "Pharmaceuticals", "RES": "Resources", "FIN": "Financial",
    "BK": "Bank", "COMM": "Communications", "ENTPR": "Enterprises",
}

_PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(?:%|percent)")
_CONTEXT = re.compile(r"revenue|net sales|receivab|customer|purchas|supplier|vendor", re.I)

# "Purchase" and "share" show up all over a 10-K in contexts that have nothing
# to do with trade between two companies — equity plans, buybacks, the price
# paid in an acquisition. Those sentences carry a company name and a percentage
# and would otherwise read as a relationship.
_NOT_A_RELATIONSHIP = re.compile(
    r"stock\s+purchase\s+plan|employee\s+stock|espp|repurchas|purchase\s+price|"
    r"purchase\s+agreement|shares?\s+of\s+(?:our\s+)?common\s+stock|"
    r"beneficial(?:ly)?\s+own|voting\s+power|purchase\s+obligations",
    re.I,
)

# Which way the money flows. The filer is the one writing the sentence, so
# "we bought from X" makes the filer X's customer and "we sold to X" makes it
# X's supplier. Buy-side cues are checked first because they are the more
# specific wording — a reseller's disclosure mentions "customer" too. {alias}
# is filled in with the subject's names before the template is compiled.
_BUYS_FROM_TEMPLATE = (
    r"purchas\w+\s+from|from\s+(?:our\s+)?vendors?|our\s+vendors?|"
    r"we\s+(?:purchase|buy|source|procure)|"
    # "Tesla, Inc. accounted for 87% of our energy storage system purchases" —
    # the subject is what was bought, so the filer sits downstream of it.
    r"of\s+our\s+[^.;]{0,45}?purchases\b|"
    r"resell|resale|reseller|"
    # "…derived from the sales of pre-owned Apple products", "sales of Apple
    # Inc. products and services comprised 12% of our revenue" — the filer is
    # moving the subject's goods, so it is downstream of the subject.
    # [^;] rather than [^.;]: the full stop in "Apple Inc." sits between the
    # name and the noun it qualifies.
    r"(?:sales?|revenues?)\s+of\s+[^;]{0,40}?(?:{alias})[^;]{0,25}?\bproducts?\b|"
    r"distribut\w*\s+(?:of|for)\s+(?:{alias})|"
    r"(?:{alias})\s+(?:is|as)\s+(?:one\s+of\s+)?(?:our|a)\s+"
    r"(?:largest\s+|significant\s+|key\s+|principal\s+)?(?:supplier|vendor)"
)
_SELLS_TO = re.compile(
    r"sales?\s+to\b|revenues?\s+from|customer|shipped\s+to|billed\s+to|"
    r"attributable\s+to|derived\s+from|"
    # The concentration formula itself — "…accounted for 9% of our revenues" —
    # names the counterparty without ever using the word "customer".
    r"of\s+(?:our\s+|the\s+compan(?:y'?s?|ies)\s+)?"
    r"(?:total\s+|consolidated\s+|net\s+|annual\s+)*(?:revenues?|sales|receivables?)\b",
    re.I,
)

# A percentage twenty pages away from the company name is not about that
# company, so the two have to appear within this many characters of each other.
_PCT_WINDOW = 260
_MAX_SENTENCE = 600

# Proximity alone is not enough: "NVIDIA noted … compute growing 56%" puts a
# name and a number in one sentence without either being about the other. A
# concentration figure is always *phrased* as one — either a share-of-the-whole
# verb runs into it, or "of our revenue" runs out of it.
_CONCENTRATION = re.compile(
    r"accounted\s+for|represent\w*|comprised?|contributed?|generated?|constitut\w*|"
    r"portion\s+of|percentage\s+of|attributable\s+to|made\s+up|approximately|"
    r"of\s+(?:our|total|consolidated|net|the\s+compan(?:y'?s?|ies))[^.;]{0,40}?"
    r"(?:revenues?|sales|receivables?|purchases)",
    re.I,
)
_CUE_WINDOW = 90

# A percentage printed right next to the company name is a table row —
# "Apple, Inc. | 11 % | 17 % | 19 %" — where the concentration wording lives in
# the caption above rather than beside the number.
_TABLE_GAP = 28

# Sentence boundary: a full stop followed by something that starts a sentence.
# Deliberately *not* a table cell — the concentration figure is usually in a
# table ("Apple, Inc. | 11 % | 17 %"), and splitting on the cell divider would
# strand the company name in a row of its own with no percentage left in it.
_SENTENCE = re.compile(r"(?<=[.;])\s+(?=[A-Z(\"])")

# Parsed filings are cached for a week, and a filing never changes — so the only
# thing that can make a cached parse wrong is a change to the rules above.
# Bump this when one of them moves, and yesterday's parses stop being served.
_PARSER_VERSION = 7


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #
def aliases_for(legal_name: str) -> List[str]:
    """How a filing is likely to write ``legal_name``, longest form first."""
    name = re.sub(r"\s+", " ", str(legal_name or "")).strip().strip(".")
    if not name:
        return []
    out = [name]
    short = name
    for _ in range(3):  # "Alphabet Inc." -> "Alphabet"; "Foo Group Holdings" -> "Foo"
        trimmed = _SUFFIX.sub("", short).strip().strip(",")
        if trimmed == short or len(trimmed) < 4:
            break
        short = trimmed
    if short.lower() != name.lower():
        out.append(short)
    return out


def _expand_register_name(registered: str) -> str:
    """``"NVIDIA CORP"`` -> ``"NVIDIA Corporation"``, the form a filing uses."""
    name = _REGISTER_TAIL.sub("", str(registered or "")).strip()
    return " ".join(
        _ABBREVIATIONS.get(word.upper().strip(".,"), word) for word in name.split()
    )


def subject_names(symbol: str) -> Tuple[str, List[str]]:
    """``(phrase to search EDGAR with, every alias to look for inside filings)``.

    Yahoo's long name is the closest thing to how a counterparty's lawyer would
    write the company down, so it leads when it is available; the expanded
    registrant name is the fallback that always is.
    """
    from . import yahoo  # local: only this function needs it

    registered = str(sec.submissions(sec.cik_for(symbol)).get("name") or "")
    expanded = _expand_register_name(registered)
    try:
        long_name = str(yahoo.info(symbol).get("longName") or "").strip()
    except Exception:  # noqa: BLE001 - Yahoo is an improvement here, not a dependency
        long_name = ""

    phrase = long_name or expanded
    aliases: List[str] = []
    for candidate in (long_name, expanded, registered):
        for alias in aliases_for(candidate):
            if alias and alias.lower() not in {a.lower() for a in aliases}:
                aliases.append(alias)
    return phrase, aliases[:4]


def _alias_pattern(aliases: Sequence[str]) -> Optional[re.Pattern]:
    if not aliases:
        return None
    # Commas inside a legal name are optional in prose ("Qorvo, Inc." /
    # "Qorvo Inc"), and so is the trailing period.
    parts = [re.escape(a).replace(r",\ ", r",?\s+").replace(r"\ ", r"\s+") for a in aliases]
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.I)


def _named_here(match: re.Match) -> bool:
    """Reject a lowercase hit — the guard that keeps "Target" off "we target"."""
    return bool(match.group(0)[:1].isupper())


# --------------------------------------------------------------------------- #
# Candidate discovery
# --------------------------------------------------------------------------- #
def _headers() -> Dict[str, str]:
    return {"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}


def _ticker_from_display(display: str) -> Optional[str]:
    """``"Qorvo, Inc.  (QRVO)  (CIK 0001604778)"`` -> ``"QRVO"``."""
    for group in re.findall(r"\(([^)]+)\)", display or ""):
        if group.upper().startswith("CIK"):
            continue
        first = group.split(",")[0].strip().upper()
        if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,6}", first):
            return first
    return None


@cached("supplychain.candidates.v{}".format(_PARSER_VERSION), ttl=TTL_FUNDAMENTAL)
def _candidates(name: str, forms: str, start: str, end: str, pages: int) -> List[Dict[str, Any]]:
    """Filings naming ``name`` next to a concentration phrase, best match first."""

    def one(phrase: str) -> List[Dict[str, Any]]:
        hits: List[Dict[str, Any]] = []
        for offset in range(0, pages * 10, 10):
            try:
                body = get_json(
                    EFTS,
                    params={"q": '"{}" "{}"'.format(name, phrase), "forms": forms,
                            "startdt": start, "enddt": end, "from": offset or None},
                    headers=_headers(),
                    ttl=TTL_FUNDAMENTAL,
                )
            except Exception:  # noqa: BLE001 - one dead phrase must not sink the search
                break
            page = body.get("hits", {}).get("hits", [])
            hits.extend(page)
            if len(page) < 10:
                break
        return hits

    with ThreadPoolExecutor(max_workers=6) as pool:
        pages_out = list(pool.map(one, DISCLOSURE_PHRASES))

    merged: Dict[str, Dict[str, Any]] = {}
    for page in pages_out:
        for hit in page:
            src = hit.get("_source") or {}
            cik = (src.get("ciks") or [""])[0]
            adsh = src.get("adsh") or ""
            if not cik or not adsh:
                continue
            row = merged.setdefault(cik, {"cik": cik, "matches": 0})
            row["matches"] += 1
            # Keep the newest filing per company: a 2019 10-K describes a
            # relationship that may not exist any more.
            if src.get("file_date", "") >= row.get("filing_date", ""):
                display = (src.get("display_names") or [""])[0]
                row.update(
                    filing_date=src.get("file_date"),
                    form=src.get("form"),
                    # "Qorvo, Inc.  (QRVO)  (CIK 0001604778)" -> "Qorvo, Inc."
                    company=re.sub(r"(?:\s*\([^)]*\))+\s*$", "", display).strip(),
                    symbol=_ticker_from_display(display),
                    sic=(src.get("sics") or [None])[0],
                    location=(src.get("biz_locations") or [None])[0],
                    filing_url="{}/{}/{}/{}".format(
                        ARCHIVE, str(cik).lstrip("0"), adsh.replace("-", ""),
                        str(hit.get("_id", "")).split(":")[-1]),
                )
    # Most phrase hits first, then most recently filed — a company matching six
    # of the boilerplate phrases is far likelier to be a real relationship than
    # one that happened to print the subject's name near the word "revenue".
    return sorted(merged.values(),
                  key=lambda r: (r["matches"], r.get("filing_date") or ""), reverse=True)


# --------------------------------------------------------------------------- #
# Reading the filing
# --------------------------------------------------------------------------- #
def _plain_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<t[dh][^>]*>", " | ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&#\d+;|&[a-zA-Z]+;", " ", text)
    return re.sub(r"[\s ]+", " ", text)


def _pct_near(sentence: str, at: re.Match) -> Optional[float]:
    """The percentage this sentence attaches to the named company.

    Disclosures read "<name> … 27%" far more often than the reverse, and a
    multi-year table ("Apple, Inc. 12% 12% 11%") puts the current year first,
    so the first percentage *after* the name wins. A percentage before the name
    is accepted only when nothing follows it, and always ranks lower.
    """
    best: Optional[Tuple[int, float]] = None
    for pm in _PCT.finditer(sentence):
        gap = pm.start() - at.end()
        # Rank forward references ahead of backward ones by offsetting the
        # latter past the whole window.
        rank = gap if gap >= 0 else (at.start() - pm.end()) + _PCT_WINDOW
        if not 0 <= rank <= 2 * _PCT_WINDOW or (gap >= 0 and gap > _PCT_WINDOW):
            continue
        value = float(pm.group(1))
        if not 0 < value <= 100:
            continue
        near = sentence[max(0, pm.start() - _CUE_WINDOW): pm.end() + _CUE_WINDOW]
        if rank > _TABLE_GAP and not _CONCENTRATION.search(near):
            continue
        if best is None or rank < best[0]:
            best = (rank, value)
    return None if best is None else best[1]


def _basis(sentence: str) -> str:
    low = sentence.lower()
    if "receivab" in low:
        return "accounts receivable"
    if "net sales" in low:
        return "net sales"
    if "purchas" in low or "vendor" in low:
        return "purchases"
    return "revenue"


def _buys_from(aliases: Sequence[str]) -> re.Pattern:
    alt = "|".join(re.escape(a) for a in aliases)
    return re.compile(_BUYS_FROM_TEMPLATE.replace("{alias}", alt), re.I)



def disclosures_in(text: str, aliases: Sequence[str]) -> List[Dict[str, Any]]:
    """Concentration statements about ``aliases`` in one filing's plain text.

    The whole reading pass, with no I/O in it — everything that decides whether
    a sentence is a relationship, which way it points and what number it carries
    lives here, so it can be exercised against a paragraph directly.
    """
    pattern = _alias_pattern(aliases)
    if pattern is None:
        return []
    buys_from = _buys_from(aliases)

    found: List[Dict[str, Any]] = []
    seen: set = set()
    for hit in pattern.finditer(text):
        if not _named_here(hit):
            continue
        window = text[max(0, hit.start() - _MAX_SENTENCE): hit.start() + _MAX_SENTENCE]
        for sentence in _SENTENCE.split(window):
            if len(sentence) > _MAX_SENTENCE or not _CONTEXT.search(sentence):
                continue
            if _NOT_A_RELATIONSHIP.search(sentence):
                continue
            # A sentence that names the company and quotes a number but never
            # says money moved between the two is market commentary, not a
            # relationship: "NVIDIA noted … compute growing 56% year-on-year".
            buys = buys_from.search(sentence)
            if not buys and not _SELLS_TO.search(sentence):
                continue
            at = pattern.search(sentence)
            if at is None or not _named_here(at):
                continue
            pct = _pct_near(sentence, at)
            if pct is None:
                continue
            clean = re.sub(r"\s*\|\s*", " ", sentence).strip()
            key = clean[:90]
            if key in seen:
                continue
            seen.add(key)
            found.append({
                "exposure_pct": pct,
                "exposure_basis": _basis(sentence),
                "relationship": "customer" if buys else "supplier",
                "quote": clean,
            })
    # The strongest statement first: a bigger disclosed share is the one worth
    # showing on the node.
    found.sort(key=lambda d: -d["exposure_pct"])
    return found[:6]


@cached("supplychain.disclosures.v{}".format(_PARSER_VERSION), ttl=TTL_REFERENCE)
def _disclosures(url: str, aliases: Tuple[str, ...]) -> List[Dict[str, Any]]:
    """:func:`disclosures_in` applied to one filing on EDGAR.

    A 10-K is several megabytes of HTML and never changes once filed, so the
    document itself is fetched uncached and only the handful of sentences that
    come back is kept. Caching the raw filings instead would put a gigabyte of
    boilerplate on disk for every few dozen companies looked at.
    """
    try:
        html = get_text(url, headers=_headers(), ttl=None, use_cache=False, retries=2)
    except Exception:  # noqa: BLE001 - a missing document just yields no evidence
        return []
    return disclosures_in(_plain_text(html), aliases)


# Any capitalised run, with or without a legal form on the end — filings write
# "Apple Inc." in the concentration table and plain "Apple" in the paragraph
# above it. Nothing is treated as a company until it matches a real registrant,
# which is what makes scanning this loosely safe.
_COMPANY_NAME = re.compile(r"\b([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,4}\.?)")

# Capitalised runs a filing is full of that no registrant should claim. A few
# of these genuinely are registered names ("Fiscal", "Item"), which is exactly
# why the list exists.
_NOT_A_COMPANY = frozenset((
    "united states", "north america", "south america", "latin america",
    "european union", "united kingdom", "greater china", "asia pacific",
    "fiscal year", "annual report", "common stock", "the company", "our company",
    "risk factors", "table of contents", "financial statements", "form",
    "item", "fiscal", "note", "total", "revenue", "revenues", "customer",
    "customers", "supplier", "suppliers", "products", "services", "company",
    "management", "board", "directors", "securities", "exchange", "commission",
    "internal revenue service", "federal reserve", "december", "january",
    "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "china", "japan", "korea", "taiwan", "germany",
    "france", "india", "canada", "mexico", "brazil", "americas", "europe",
))


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", str(name).lower()).strip()


@cached("supplychain.register", ttl=TTL_REFERENCE)
def _register_index() -> Dict[str, Tuple[str, str, str]]:
    """Every SEC registrant keyed by the ways a filing might write its name.

    Both the full name and the suffix-stripped short form are indexed, because
    "Apple Inc." in the register turns up as "Apple Inc.", "Apple Inc" or plain
    "Apple" in someone else's sentence. Short forms that two registrants share
    are dropped rather than guessed at.

    Deliberately takes no arguments: this is a ten-thousand-entry map of the
    whole register, and keying it by the company being looked at would store
    another copy of it for every company anyone opens.
    """
    register = sec.company_map()
    index: Dict[str, Tuple[str, str, str]] = {}
    shorts: Dict[str, int] = {}
    rows = [
        (s, n, c) for s, n, c in
        zip(register["symbol"], register["name"], register["cik"])
        if len(str(n)) >= 5
    ]
    for _, name, _cik in rows:
        short = _norm_name(_SUFFIX.sub("", str(name)))
        if len(short) >= 5:
            shorts[short] = shorts.get(short, 0) + 1
    for symbol, name, cik in rows:
        index.setdefault(_norm_name(name), (symbol, name, cik))
        short = _norm_name(_SUFFIX.sub("", str(name)))
        if len(short) >= 5 and shorts.get(short) == 1:
            index.setdefault(short, (symbol, name, cik))
    return index


def _lookup(index: Dict[str, Tuple[str, str, str]], written: str) -> Optional[Tuple[str, str, str]]:
    name = re.sub(r"\s+", " ", written).strip().strip(",.")
    for form in (_norm_name(name), _norm_name(_SUFFIX.sub("", name))):
        if len(form) >= 4 and form not in _NOT_A_COMPANY and form in index:
            return index[form]
    return None


def _side_of(found: Sequence[Dict[str, Any]]) -> str:
    """Which side of the subject one filer sits on, across all its disclosures.

    A distributor usually says it both twice ("products purchased from vendors"
    in the concentration table, "sales of X products comprised 12% of revenue"
    in the risk factors), so a vote beats reading whichever sentence happened to
    carry the largest number. Ties go to ``customer``: ``supplier`` is the
    fallback label applied when no cue fired at all, so it is the weaker claim.
    """
    buyers = sum(1 for d in found if d["relationship"] == "customer")
    return "customer" if buyers * 2 >= len(found) and buyers else "supplier"


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def counterparties(
    symbol: str,
    years: int = 4,
    max_candidates: int = 40,
    limit: int = 15,
) -> pd.DataFrame:
    """Companies whose filings disclose a quantified relationship with ``symbol``.

    Returns one row per counterparty with the disclosed percentage, which side
    of the relationship it sits on, the sentence it was read from, and a link to
    the filing.
    """
    symbol = symbol.upper().strip()
    cik = sec.cik_for(symbol)
    phrase, aliases = subject_names(symbol)
    if not aliases:
        raise EmptyDataError("No SEC-registered company name for {}".format(symbol))

    today = date.today()
    start = today.replace(year=today.year - max(1, years)).isoformat()
    pool = _candidates(phrase, "10-K,20-F", start, today.isoformat(), 2)
    pool = [c for c in pool if c["cik"] != cik][:max_candidates]
    if not pool:
        raise EmptyDataError(
            "No SEC filing in the last {} years names {} in a concentration "
            "disclosure".format(years, phrase)
        )

    alias_key = tuple(aliases)
    with ThreadPoolExecutor(max_workers=8) as executor:
        evidence = list(executor.map(lambda c: _disclosures(c["filing_url"], alias_key), pool))

    rows: List[Dict[str, Any]] = []
    for cand, found in zip(pool, evidence):
        if not found:
            continue  # named but never quantified — not a relationship we can show
        side = _side_of(found)
        # Quote the largest disclosure that agrees with the side we settled on,
        # so the sentence on screen is the evidence for the arrow drawn.
        top = next(d for d in found if d["relationship"] == side)
        rows.append({
            "relationship": side,
            "symbol": cand.get("symbol"),
            "company": cand.get("company"),
            "exposure_pct": top["exposure_pct"],
            "exposure_basis": top["exposure_basis"],
            # The counterparty wrote the sentence, so the share is of *its* books.
            "pct_of": cand.get("symbol") or cand.get("company"),
            "disclosed_by": "counterparty",
            "quote": top["quote"],
            "form": cand.get("form"),
            "filing_date": cand.get("filing_date"),
            "filing_url": cand.get("filing_url"),
            "cik": cand.get("cik"),
            "sic": cand.get("sic"),
            "location": cand.get("location"),
            "disclosures": len(found),
        })
    if not rows:
        raise EmptyDataError(
            "{} is named in {} recent filings but none of them quantify the "
            "relationship".format(phrase, len(pool))
        )
    df = pd.DataFrame(rows).sort_values(
        ["relationship", "exposure_pct"], ascending=[True, False]
    )
    return df.groupby("relationship", group_keys=False).head(limit).reset_index(drop=True)


def subject_disclosures(symbol: str, limit: int = 15) -> pd.DataFrame:
    """Counterparties ``symbol`` names in its own latest annual report.

    The mirror image of :func:`counterparties`: instead of reading what other
    filers say about this company, read what this company says about them. Most
    large caps disclose a concentration percentage without naming anyone
    ("one customer accounted for 12%"), so this is often empty — an unnamed
    counterparty is a fact about the filing, not a company we can plot.
    """
    symbol = symbol.upper().strip()
    annual = sec.filings(symbol, form_type="10-K,20-F", limit=1)
    if annual.empty:
        raise EmptyDataError("No annual report on file for {}".format(symbol))
    row = annual.iloc[0]
    try:
        html = get_text(str(row["url"]), headers=_headers(), ttl=None, use_cache=False, retries=2)
    except Exception as exc:  # noqa: BLE001
        raise EmptyDataError("Could not read {}'s latest {}: {}".format(
            symbol, row["form"], exc))
    text = _plain_text(html)
    known = _register_index()
    self_cik = sec.cik_for(symbol)

    rows: Dict[str, Dict[str, Any]] = {}
    for sentence in _SENTENCE.split(text):
        if len(sentence) > _MAX_SENTENCE or not _PCT.search(sentence):
            continue
        if not _CONTEXT.search(sentence) or _NOT_A_RELATIONSHIP.search(sentence):
            continue
        for cand in _COMPANY_NAME.finditer(sentence):
            entry = _lookup(known, cand.group(1))
            if entry is None or entry[2] == self_cik:   # the filer naming itself
                continue
            pct = _pct_near(sentence, cand)
            if pct is None:
                continue
            other, full, other_cik = entry
            keep = rows.get(other)
            if keep and keep["exposure_pct"] >= pct:
                continue
            # Mirror of counterparties(): here the subject is the narrator, so a
            # buy-side cue makes the *other* company the supplier.
            side = "supplier" if _buys_from(aliases_for(full)).search(sentence) else "customer"
            rows[other] = {
                "relationship": side,
                "symbol": other,
                "company": full,
                "exposure_pct": pct,
                "exposure_basis": _basis(sentence),
                # The subject wrote this one, so the share is of the subject's
                # own books — the opposite of what counterparties() returns.
                "pct_of": symbol,
                "disclosed_by": "subject",
                "quote": re.sub(r"\s*\|\s*", " ", sentence).strip(),
                "form": row["form"],
                "filing_date": str(row["filing_date"])[:10],
                "filing_url": str(row["url"]),
                "cik": other_cik,
                "disclosures": 1,
            }
    if not rows:
        raise EmptyDataError(
            "{}'s latest {} does not name a counterparty with a disclosed "
            "percentage".format(symbol, row["form"])
        )
    return (pd.DataFrame(list(rows.values()))
            .sort_values("exposure_pct", ascending=False)
            .head(limit)
            .reset_index(drop=True))
