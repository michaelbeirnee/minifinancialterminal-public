"""Reading the two filings whose difference is the signal.

Two of the nine flag types are not computable from tagged data at all. Nobody
tags a risk factor, and the concentration a filer is required to disclose is
required as a *sentence* — "one customer accounted for 21% of net sales" — with
the percentage in prose and the counterparty very often not named. Those two
have to be read out of the document.

Reading them means three things this module does and the rest of the package
does not have to know about:

* **Getting paragraph boundaries back.** The plain-text pass used elsewhere here
  collapses all whitespace, which is right for sentence matching and fatal for a
  paragraph diff — a whole Item 1A arrives as one string with no seams. So the
  block-level tags are turned into line breaks *before* the markup is stripped.
* **Finding Item 1A.** The phrase "Item 1A. Risk Factors" appears at least twice
  in every 10-K, once in the table of contents. The longest span between an
  opening marker and a closing one is the section; the table-of-contents hit
  spans a line and loses.
* **Deciding two paragraphs are the same paragraph.** A filer re-uses last
  year's risk factor with the numbers updated and three sentences re-ordered.
  That is not a new risk factor, and an exact-match diff would call it one — so
  matching is on overlapping five-word shingles, which survives an edit and does
  not survive a rewrite.

Documents are megabytes and never change once filed, so nothing here caches the
document: it caches what was extracted from it, keyed by URL and by a parser
version that invalidates every stored parse when the rules below move.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import settings
from ..core.caching import TTL_REFERENCE, cached
from ..core.errors import EmptyDataError
from ..core.http import get_text
from ..providers import sec, supplychain

#: Bump when any rule in this module changes, so parses cached under the old
#: rules stop being served. A filing never changes; only these do.
PARSER_VERSION = 6

#: Words a comparable unit needs before it is diffed. Below this the line is a
#: heading, a page number, a table cell or a cross-reference, and a diff over
#: those reports the filer's page breaks moving.
MIN_PARAGRAPH_WORDS = 12

#: Word count that makes a line a plausible risk-factor heading rather than a
#: paragraph. Headings are what a reader wants printed; they are too short to
#: diff reliably, so they are carried alongside the paragraph instead.
HEADING_WORDS: Tuple[int, int] = (3, 30)

#: Shingle length, in words. Bigrams, and the choice was made against real
#: filings rather than by taste: risk factors are edited a word at a time
#: ("hosting facilities" becomes "providers", "find" becomes "identify"), and
#: every such edit kills ``n`` shingles on each side. At five words a fifteen-
#: word bullet with two edits keeps a tenth of its overlap and reads as new; at
#: two words it keeps half. Bigrams still encode phrasing, which is what keeps
#: two different paragraphs on the same topic apart — shared vocabulary alone
#: would not.
SHINGLE = 2

#: Bigram Jaccard above which two paragraphs are "the same paragraph, edited".
#: On a filer that rewrote its whole Item 1A (Salesforce, FY2026 against
#: FY2025), unrelated pairs top out near 0.25 — legal boilerplate shares a lot
#: of "of our" and "could harm" — while every pair a reader would call an edit
#: sits at 0.3 or above. The margin is thin and deliberate: erring toward
#: "edited" under-reports change, and an under-reported change is a missed
#: row, where an over-reported one is a false claim with a filing date on it.
SAME_PARAGRAPH = 0.30

#: The second way two paragraphs can be the same one: enough of the *content
#: words* in common, whatever the phrasing. Bigrams miss a bullet reworded end
#: to end ("efforts by hackers or sophisticated groups" becoming "efforts by
#: threat actors, including criminal organizations") that still names the same
#: things; content-word Jaccard at 0.4 catches those and, on the same filer,
#: nothing that was not an edit. Either criterion is sufficient.
SAME_CONTENT = 0.40

#: Where Item 1A starts and stops, per form. 20-F puts risk factors under Item
#: 3.D and runs into Item 4, so it needs its own pair rather than a loose
#: "Risk Factors" search that would also match the table of contents.
_SECTION_MARKERS: Dict[str, Tuple[str, str]] = {
    "10-K": (r"item\s*1a\b", r"item\s*1b\b|item\s*2\b"),
    "20-F": (r"item\s*3\.?\s*d\b|\brisk\s+factors\b", r"item\s*4\b"),
    "40-F": (r"\brisk\s+factors\b", r"\bitem\s*\d"),
}

_BLOCK = re.compile(r"(?is)</(p|div|tr|li|h[1-6]|table|section)\s*>|<br\s*/?>")
_TAG = re.compile(r"<[^>]+>")
_ENTITY = re.compile(r"&#\d+;|&#x[0-9a-fA-F]+;|&[a-zA-Z]+;")
_WORD = re.compile(r"[a-z0-9]+")

#: Page furniture that survives tag stripping and would otherwise be diffed —
#: and, worse, would be taken for the heading above the next paragraph. Running
#: headers are short lines naming the form ("Apple Inc. | 2025 Form 10-K | 6");
#: a real paragraph that mentions the form is long.
_FURNITURE = re.compile(
    r"^\s*(table\s+of\s+contents|page\s*\d+|\d+\s*$)|"
    r"^.{0,60}\bform\s+(?:10-[kq]|20-f|40-f)\b.{0,30}$",
    re.I,
)

#: Function words, dropped before the content-word comparison. Short and
#: deliberately generic: the point is to strip what every risk factor shares.
_STOPWORDS = frozenset("""
a an the and or but of to in on at by for with from as is are was were be been
being have has had do does did will would could should may might can shall must
our we us its it their they them this that these those which who whom whose
what when where how than then there here such other any all some more most
into over under about not no nor if so also including include includes among
within without across between per via each because while during upon
""".split())

# --------------------------------------------------------------------------- #
# Concentration wording
# --------------------------------------------------------------------------- #
#: The counterparty role a concentration sentence is about. Checked in order, so
#: the more specific noun wins over the generic "customer" a reseller's
#: disclosure also contains. Carriers, retailers and channel partners are
#: customers who happen to be described by their trade — Apple's receivable
#: concentration is stated in terms of "cellular network carriers", and a
#: parser that only knows the word "customer" reads that as no disclosure.
_ROLES: Tuple[Tuple[str, str], ...] = (
    ("distributor", r"\bdistributors?\b|\bresellers?\b|\bwholesalers?\b"),
    ("supplier", r"\bsuppliers?\b|\bvendors?\b|\bcontract\s+manufacturers?\b|"
                 r"\bfoundr(?:y|ies)\b"),
    ("customer", r"\bcustomers?\b|\bclients?\b|\bend\s+users?\b|\bcarriers?\b|"
                 r"\bretailers?\b|\bchannel\s+partners?\b|\bpayors?\b|\bpayers?\b"),
)

#: "No customer accounted for more than 10% of revenue" is the *absence* of a
#: concentration stated as a sentence, and it is the single most useful thing in
#: this diff: a filer that said this last year and does not say it this year has
#: acquired a dependency.
_NEGATED = re.compile(
    r"\bno\s+(?:single\s+|individual\s+|one\s+)?(?:customer|client|supplier|vendor|"
    r"distributor|reseller)\b|\bnone\s+of\s+(?:our|the)\s+(?:customers|clients|suppliers)\b",
    re.I,
)

#: A percentage of revenue by *place* is a geographic split, not a counterparty
#: — "customers headquartered outside the United States accounted for 31%" has
#: the noun and the verb of a concentration disclosure and none of the meaning.
_GEOGRAPHIC = re.compile(
    r"headquartered|located\s+(?:in|outside)|outside\s+(?:of\s+)?the\s+u(?:nited\s+states|\.s\.)|"
    r"international|geographic|by\s+region|\b(?:americas|emea|apac|europe|asia|china)\b",
    re.I,
)

#: Four-digit years, used to tell this year's disclosure from the comparative
#: printed beside it. A note states both ("17% as of January 25, 2026 and 16%
#: as of January 26, 2025"), and a sentence that names only earlier years is
#: last year's fact — the one the previous filing already carries.
_YEAR = re.compile(r"\b(20\d{2}|19\d{2})\b")

#: How many unnamed counterparties the sentence is about, which is what makes
#: "one customer was 21%" and "two customers were 21% and 14%" different facts.
_QUANTIFIER = re.compile(
    r"\b(one|a\s+single|two|three|four|five|our\s+largest|our\s+top|the\s+largest)\b",
    re.I,
)

#: The disclosure threshold as filers phrase it — "10% or more", "more than
#: 10%", "in excess of 10%". That number is the rule, not the exposure, and it
#: is nearly always the first percentage in the sentence ("one customer that
#: represented 10% or more of trade receivables, which accounted for 12%"), so
#: it has to be set aside before the exposure is read.
_THRESHOLD = re.compile(
    r"(?:more\s+than|at\s+least|in\s+excess\s+of|exceed(?:ed|ing|s)?|greater\s+than|"
    r"over)\s+(\d{1,3}(?:\.\d+)?)\s*(?:%|percent)|"
    r"(\d{1,3}(?:\.\d+)?)\s*(?:%|percent)\s+or\s+(?:more|greater)",
    re.I,
)

#: A cell boundary that starts a new sentence. Tables render as " | " between
#: cells, and a note that follows a table with no full stop between them reads
#: as one enormous "sentence" that the length cap then discards — taking the
#: disclosure with it. Splitting where a cell boundary is followed by a
#: capital keeps "Apple Inc. | 12 % | 17 %" whole (a digit follows) and frees
#: the prose that comes after a table.
_CELL_SENTENCE = re.compile(r"(?<=[.;])\s+(?=[A-Z(\"])|\s\|\s(?=[A-Z][a-z])")

#: Who audited the filing. Since fiscal 2021 the auditor's name, PCAOB firm id
#: and location are inline-XBRL facts in the document itself, tagged in the
#: ``dei`` taxonomy — and unlike the numeric facts they are *not* echoed by the
#: companyfacts API, so the document is the only place to read them. The tag
#: is read straight out of the HTML: prose regexes over the rendered text break
#: on every table cell and entity, and the tag does not. The PCAOB id is the
#: better key of the two, because a firm that renames itself keeps its number.
_IX_AUDITOR = re.compile(
    r'name="dei:(AuditorName|AuditorFirmId|AuditorLocation)"[^>]*>(.*?)</ix:nonNumeric>',
    re.I | re.S,
)
#: The same facts as some filers print them in prose, for a document whose tags
#: were stripped or that predates the requirement.
_AUDITOR_ID = re.compile(r"PCAOB\s+(?:Firm\s+)?I\.?D\.?\s*(?:No\.?)?\s*:?\s*0*(\d{1,5})\b", re.I)
#: The audit report signature — how every filing before fiscal 2021 says it.
_AUDITOR_SIGNATURE = re.compile(
    r"/s/\s*([A-Z][A-Za-z&.,'’\- ]{2,60}?(?:LLP|LLC|L\.L\.P\.|P\.?C\.?|LTD|S\.?A\.?|GmbH|AG))\b"
)
#: 8-K item number for a change of certifying accountant. The item list is on
#: the filing index, so this costs no download at all.
AUDITOR_ITEM = "4.01"


def _headers() -> Dict[str, str]:
    return {"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}


# --------------------------------------------------------------------------- #
# Which two filings
# --------------------------------------------------------------------------- #
def annual_pair(symbol: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """The two most recent annual reports, newest first.

    Raises rather than degrading: a diff needs both halves, and a "change"
    computed against nothing is the one output this package must never produce.
    """
    frame = sec.filings(symbol, form_type="10-K,20-F,40-F", limit=6)
    rows = [
        {
            "form": str(r["form"]).upper(),
            "filing_date": str(r["filing_date"])[:10],
            "period_ending": str(r.get("report_date") or "")[:10] or None,
            "accession_number": str(r["accession_number"]),
            "url": str(r["url"]),
            "filing_index": str(r.get("filing_index") or ""),
        }
        for _, r in frame.iterrows()
    ]
    # Amendments (10-K/A) restate a filing rather than succeed it; diffing one
    # against its own original reports the amendment, which is a different
    # question from the year-on-year change this package is about.
    rows = [r for r in rows if not r["form"].endswith("/A")]
    if len(rows) < 2:
        raise EmptyDataError(
            "{} has fewer than two annual reports on file — nothing to diff "
            "against".format(symbol.upper())
        )
    return rows[0], rows[1]


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #
def paragraph_text(html: str) -> List[str]:
    """Filing HTML as lines, with the paragraph boundaries still in it."""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = _BLOCK.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = _ENTITY.sub(" ", text)
    lines = []
    for line in text.split("\n"):
        clean = re.sub(r"[\s ]+", " ", line).strip(" | ")
        if clean and not _FURNITURE.match(clean):
            lines.append(clean)
    return lines


#: A line that *is* an item heading rather than one that mentions an item.
#: "Item 1A. Risk Factors" is a heading; "as described in Item 1A of this
#: report, our business…" is a cross-reference, and there are dozens of those
#: scattered through the MD&A. Headings are short.
HEADING_MAX_CHARS = 90


def section(lines: Sequence[str], form: str) -> List[str]:
    """The risk-factor section of a filing, as lines.

    Every heading-shaped opener is paired with the first heading-shaped closer
    after it, and the longest such span wins. That is what discards the table
    of contents without having to recognise one: the entry there is followed
    within a line or two by the next item's entry, while the section itself
    runs for hundreds. An opener with no closer after it is not a section
    start — it is the last cross-reference in the document — and is skipped
    rather than allowed to claim everything to the end of the filing.
    """
    opener, closer = _SECTION_MARKERS.get(form.upper(), _SECTION_MARKERS["10-K"])

    def heading(pattern: str) -> List[int]:
        return [i for i, line in enumerate(lines)
                if len(line) <= HEADING_MAX_CHARS and re.search(pattern, line, re.I)]

    open_at, close_at = heading(opener), heading(closer)
    best: Tuple[int, int, int] = (0, 0, 0)
    for start in open_at:
        ends = [i for i in close_at if i > start]
        if not ends:
            continue
        end = ends[0]
        if end - start > best[0]:
            best = (end - start, start, end)
    if best[0] == 0:
        return []
    return list(lines[best[1] + 1: best[2]])


def paragraphs(lines: Sequence[str]) -> List[Dict[str, str]]:
    """Comparable units, each with the heading that was sitting above it.

    The heading is carried rather than diffed. A risk-factor heading is the one
    line a reader actually wants printed, and it is also too short for shingle
    matching to tell "We depend on a limited number of customers" from "We
    depend on a limited number of suppliers".
    """
    out: List[Dict[str, str]] = []
    heading = ""
    for line in lines:
        words = line.split()
        if len(words) < MIN_PARAGRAPH_WORDS:
            if HEADING_WORDS[0] <= len(words) <= HEADING_WORDS[1]:
                heading = line
            continue
        out.append({"heading": heading, "text": line})
    return out


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def shingles(text: str, size: int = SHINGLE) -> frozenset:
    """Overlapping word n-grams — the unit similarity is measured in."""
    words = _WORD.findall(text.lower())
    if len(words) < size:
        return frozenset([" ".join(words)]) if words else frozenset()
    return frozenset(
        " ".join(words[i: i + size]) for i in range(len(words) - size + 1)
    )


def content_words(text: str) -> frozenset:
    """The words that carry a paragraph's subject, function words dropped."""
    return frozenset(w for w in _WORD.findall(text.lower())
                     if len(w) > 2 and w not in _STOPWORDS)


def similarity(left: frozenset, right: frozenset) -> float:
    """Jaccard overlap of two sets."""
    if not left or not right:
        return 0.0
    shared = len(left & right)
    return shared / (len(left) + len(right) - shared)


class _Index:
    """Inverted index from token to the paragraphs carrying it.

    Scoring every new paragraph against every old one is quadratic in the
    length of Item 1A, which runs to several hundred paragraphs; through the
    index only pairs that share at least one token are ever scored, and the
    Jaccard is computed from the shared count and the two set sizes.
    """

    def __init__(self, bags: Sequence[frozenset]) -> None:
        self.bags = bags
        self.by_token: Dict[str, List[int]] = {}
        for i, bag in enumerate(bags):
            for token in bag:
                self.by_token.setdefault(token, []).append(i)

    def best(self, bag: frozenset) -> Tuple[float, Optional[int]]:
        overlap: Dict[int, int] = {}
        for token in bag:
            for i in self.by_token.get(token, ()):
                overlap[i] = overlap.get(i, 0) + 1
        best, at = 0.0, None
        for i, shared in overlap.items():
            score = shared / (len(bag) + len(self.bags[i]) - shared)
            if score > best:
                best, at = score, i
        return best, at


def match_paragraphs(new: Sequence[Dict[str, str]], old: Sequence[Dict[str, str]],
                     threshold: float = SAME_PARAGRAPH,
                     content_threshold: float = SAME_CONTENT) -> Dict[str, List[Dict[str, Any]]]:
    """Which paragraphs are in one filing and not the other.

    Every new paragraph is scored against its best counterpart in the old
    filing on two measures — bigram overlap, which sees phrasing, and
    content-word overlap, which sees subject — and is "the same paragraph" if
    either clears its threshold. Old paragraphs nobody matched to are the
    removals; the reverse pass is not a second sweep, it is the forward pass's
    own bookkeeping.

    ``best_match`` is returned on every reported row: the higher of the two
    scores against the nearest old paragraph, so a reader can see how near a
    miss it was and judge the thresholds for themselves.
    """
    bigrams = _Index([shingles(p["text"]) for p in old])
    words = _Index([content_words(p["text"]) for p in old])

    added: List[Dict[str, Any]] = []
    matched_old: set = set()
    for i, para in enumerate(new):
        b_score, b_at = bigrams.best(shingles(para["text"]))
        if b_score >= threshold:
            matched_old.add(b_at)
            continue
        c_score, c_at = words.best(content_words(para["text"]))
        if c_score >= content_threshold:
            matched_old.add(c_at)
            continue
        added.append({**para, "best_match": round(max(b_score, c_score), 3)})

    removed = [
        {**old[i], "best_match": None}
        for i in range(len(old)) if i not in matched_old
    ]
    return {"added": added, "removed": removed,
            "compared": {"new": len(new), "old": len(old)}}


# --------------------------------------------------------------------------- #
# Concentration sentences
# --------------------------------------------------------------------------- #
def _role(sentence: str) -> Optional[str]:
    for role, pattern in _ROLES:
        if re.search(pattern, sentence, re.I):
            return role
    return None


def _quantifier(sentence: str) -> str:
    hit = _QUANTIFIER.search(sentence)
    return re.sub(r"\s+", " ", hit.group(1).lower()) if hit else "unspecified"


def concentration_statements(text: str, register: Optional[Dict[str, Any]] = None,
                             self_cik: Optional[str] = None,
                             period_year: Optional[int] = None) -> List[Dict[str, Any]]:
    """Concentration disclosures in one filing, as comparable facts.

    Each statement is reduced to a ``key`` — role, basis, and either the named
    counterparty or the quantifier that stood in for one — because that is what
    survives a filer rewording the sentence. Diffing the sentences themselves
    would report every editorial change as a disclosure appearing and vanishing
    at once.

    ``register`` is the SEC registrant index, passed in so the caller can fetch
    it once for both filings. Without it the counterparty is left unnamed, which
    is also what happens for the majority of real disclosures — the rule
    requires the percentage, never the name.

    ``period_year`` is the fiscal year the filing reports on. A note states the
    comparative beside the current figure, in its own sentence as often as not,
    and a sentence naming only earlier years is that comparative — last year's
    disclosure, which the previous filing already carries and which would
    otherwise keep a vanished key alive for one more year.
    """
    found: Dict[str, Dict[str, Any]] = {}
    for sentence in _CELL_SENTENCE.split(text):
        if len(sentence) > supplychain._MAX_SENTENCE:
            continue
        role = _role(sentence)
        if role is None or supplychain._NOT_A_RELATIONSHIP.search(sentence):
            continue
        if _GEOGRAPHIC.search(sentence):
            continue
        if period_year:
            years = [int(y) for y in _YEAR.findall(sentence)]
            if years and max(years) < period_year:
                continue
        negated = bool(_NEGATED.search(sentence))
        threshold_spans = [m.span() for m in _THRESHOLD.finditer(sentence)]
        thresholds = [float(m.group(1) or m.group(2)) for m in _THRESHOLD.finditer(sentence)]
        percentages = [
            float(m.group(1)) for m in supplychain._PCT.finditer(sentence)
            if not any(a <= m.start() < b for a, b in threshold_spans)
        ]
        percentages = [p for p in percentages if 0 < p <= 100]
        if not percentages and not (negated and thresholds):
            continue
        if not negated and not supplychain._CONCENTRATION.search(sentence):
            continue

        basis = supplychain._basis(sentence)
        # The first exposure, not the largest: a filing states this year before
        # last ("38% and 41% as of 2025 and 2024, respectively"). A negated
        # sentence carries no exposure — its number is the threshold.
        named = None
        pct = (thresholds[0] if thresholds else percentages[0]) if negated else percentages[0]
        if register is not None:
            for candidate in supplychain._COMPANY_NAME.finditer(sentence):
                entry = supplychain._lookup(register, candidate.group(1))
                if entry is None or (self_cik and entry[2] == self_cik):
                    continue
                near = supplychain._pct_near(sentence, candidate)
                if near is not None:
                    named, pct = entry[0], near
                    break

        key = "{}|{}|{}".format(
            role, basis, named or ("none" if negated else _quantifier(sentence)))
        clean = re.sub(r"\s*\|\s*", " ", sentence).strip()
        # First sentence per key wins. The notes state the current year and
        # then the comparative, so a later sentence with the same key is
        # last year's figure — the one the *previous* filing already carries.
        if key not in found:
            found[key] = {
                "key": key,
                "role": role,
                "exposure_basis": basis,
                "counterparty": named,
                "negated": negated,
                "exposure_pct": None if negated else pct,
                "threshold_pct": pct if negated else None,
                "quote": clean[:400],
            }
    return sorted(found.values(),
                  key=lambda s: (s["negated"], -(s["exposure_pct"] or 0)))


def diff_concentration(new: Sequence[Dict[str, Any]],
                       old: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Which disclosures appeared, which vanished, and which merely moved.

    A key present on both sides is not a change of disclosure however much the
    percentage moved, so it is returned separately as ``held``: the reader gets
    the movement without a flag claiming a dependency arrived that was there all
    along.
    """
    new_by_key = {s["key"]: s for s in new}
    old_by_key = {s["key"]: s for s in old}
    appeared = [new_by_key[k] for k in new_by_key if k not in old_by_key]
    vanished = [old_by_key[k] for k in old_by_key if k not in new_by_key]
    held = [
        {**new_by_key[k],
         "prior_exposure_pct": old_by_key[k]["exposure_pct"],
         "change_pct_points": (
             None if new_by_key[k]["exposure_pct"] is None
             or old_by_key[k]["exposure_pct"] is None
             else round(new_by_key[k]["exposure_pct"] - old_by_key[k]["exposure_pct"], 2)
         )}
        for k in new_by_key if k in old_by_key
    ]
    return {"appeared": appeared, "vanished": vanished, "held": held}


# --------------------------------------------------------------------------- #
# Who signed it
# --------------------------------------------------------------------------- #
def _untag(fragment: str) -> str:
    text = _TAG.sub(" ", fragment)
    text = text.replace("&amp;", "&").replace("&nbsp;", " ")
    text = _ENTITY.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" .,")


def auditor_in(html: str, text: str) -> Dict[str, Any]:
    """The audit firm named by one filing.

    Three readings, tried in order of how much they can be trusted. Since
    fiscal 2021 the firm's PCAOB identifier and name are inline-XBRL facts in
    the document, and the identifier is the only stable key here — audit firms
    rename, merge their member firms and restyle their punctuation, and the
    name alone turns each of those into an apparent change of auditor. Failing
    the tag, the id is sometimes printed in prose ("PCAOB Firm ID No. 42"). And
    before 2021 there is neither: the only statement of who audited the filing
    is the signature under the audit report, which is read last and marked as
    the weaker source it is.
    """
    tagged: Dict[str, str] = {}
    for tag, body in _IX_AUDITOR.findall(html):
        clean = _untag(body)
        if clean and tag not in tagged:
            tagged[tag] = clean
    if tagged.get("AuditorFirmId") or tagged.get("AuditorName"):
        return {
            "auditor_firm_id": (tagged.get("AuditorFirmId") or "").lstrip("0") or None,
            "auditor": tagged.get("AuditorName"),
            "auditor_location": tagged.get("AuditorLocation"),
            "auditor_source": "inline XBRL (dei:AuditorName)",
        }
    firm_id = _AUDITOR_ID.search(text)
    signature = _AUDITOR_SIGNATURE.search(text)
    if firm_id or signature:
        return {
            "auditor_firm_id": firm_id.group(1) if firm_id else None,
            "auditor": _untag(signature.group(1)) if signature else None,
            "auditor_location": None,
            "auditor_source": ("PCAOB id in prose" if firm_id
                               else "audit report signature"),
        }
    return {"auditor_firm_id": None, "auditor": None,
            "auditor_location": None, "auditor_source": None}


def auditor_key(reading: Optional[Dict[str, Any]]) -> Optional[str]:
    """What two filings' auditors are compared on, or ``None`` if unreadable.

    The PCAOB id when there is one, otherwise the name folded to letters — so
    "Ernst & Young LLP" and "ERNST &YOUNG, LLP" are one firm and not two.
    """
    if not reading:
        return None
    if reading.get("auditor_firm_id"):
        return "pcaob:{}".format(reading["auditor_firm_id"])
    name = reading.get("auditor")
    if not name:
        return None
    return "name:{}".format("".join(_WORD.findall(name.lower())))


# --------------------------------------------------------------------------- #
# I/O — cached on what was extracted, never on the document
# --------------------------------------------------------------------------- #
def _document(url: str) -> str:
    """One filing's HTML. Uncached on purpose — see the module docstring."""
    try:
        return get_text(url, headers=_headers(), ttl=None, use_cache=False, retries=2)
    except Exception as exc:  # noqa: BLE001 - a dead document is an empty read
        raise EmptyDataError("Could not read the filing at {}: {}".format(url, exc))


@cached("flagged.read.v{}".format(PARSER_VERSION), ttl=TTL_REFERENCE)
def read(url: str, form: str, self_cik: Optional[str] = None,
         period_year: Optional[int] = None) -> Dict[str, Any]:
    """Everything the document detectors need, from one download.

    Three extractions share a filing because the filing is the expensive part:
    a 10-K is several megabytes over a rate-limited connection, and pulling it
    once per detector would triple the cost of the one command that runs them
    together. The parsed result is what gets cached — a few kilobytes against
    the document's several megabytes — keyed by URL and parser version, so a
    second look at the same filing costs nothing and a change to the rules above
    invalidates every stored parse.
    """
    html = _document(url)
    lines = paragraph_text(html)
    flat = supplychain._plain_text(html)
    try:
        register = supplychain._register_index()
    except Exception:  # noqa: BLE001 - naming is an improvement, not a dependency
        register = None
    body = section(lines, form)
    return {
        "url": url,
        "form": form,
        "risk_factors": paragraphs(body) if body else [],
        "risk_section_lines": len(body),
        "concentration": concentration_statements(flat, register, self_cik, period_year),
        "auditor": auditor_in(html, flat),
    }
