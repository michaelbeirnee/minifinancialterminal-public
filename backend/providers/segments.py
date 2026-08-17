"""Revenue disaggregated the way the filer reports it.

The XBRL *company-facts* API behind every other fundamental command here
publishes one number per concept per period with the dimensions stripped off —
and a segment breakdown is nothing but dimensions. So segment revenue is not in
it, and no free vendor sells it either.

It is in the filing. A 10-K's XBRL instance carries the same revenue concepts a
second time, tagged against a *context* that names an axis and a member::

    us-gaap:StatementBusinessSegmentsAxis = aapl:AmericasSegmentMember

Reading the instance recovers what the API drops, from the filer's own tagging,
with the accession it came out of. Three axes carry revenue, and a filer may use
any or all of them: ASC 280 reportable segments, geography, and product or
service lines.

Three things need care and are handled below.

* A fact tagged on *two* breakdown axes at once — segment × product — is one
  cell of a cross-tab. Those are dropped: added to the single-axis rows they
  would count the same revenue twice.
* Filers tag two different *levels* of one axis. Microsoft puts Product/Service
  on the income statement and eleven product lines in the revenue note, both on
  ``srt:ProductOrServiceAxis``; kept side by side the group sums to twice
  revenue. Which table a member belongs to is recorded in the filing's
  ``MetaLinks.json``, so the levels are separated by asking it, and the finest
  table that still adds up to revenue is the one kept.
* One table can nest inside itself: NVIDIA presents Data Center with its Compute
  and Networking split beneath it, in the same table. What the tables cannot
  settle, arithmetic does — a member that is exactly the sum of its neighbours
  is a roll-up of them and gives way to the finer split.

Coverage starts where XBRL does (2009-2011, phased in by filer size) and stops
where the filer's tagging does: a single-segment company has nothing to
disaggregate, and plenty of others disclose geography but not product. The first
call for a symbol downloads a few megabytes of filing documents; they are
immutable once filed, so it is cached hard afterwards.
"""
from __future__ import annotations

import html
import re
import string
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from ..core.caching import TTL_REFERENCE, cached
from ..core.errors import EmptyDataError
from ..core.http import fetch, get_json
from . import sec

NAME = "sec"

XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
XLINK = "{http://www.w3.org/1999/xlink}"

# Revenue, most specific tag first. Rank breaks a tie when a filer tags one
# member twice under two concepts in the same filing.
REVENUE_TAGS: Tuple[str, ...] = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    # "Total net revenue" — what a bank's segment table reports, and the only
    # revenue line JPMorgan and its peers tag at all. Several banks present the
    # segment table on a fully-taxable-equivalent basis and tag only that, so it
    # follows the plain measure rather than replacing it: the adjustment is well
    # under a percent, and a bank with no segment revenue at all is worse.
    "RevenuesNetOfInterestExpense",
    "RevenuesNetOfInterestExpenseFullTaxEquivalentBasis",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "SalesRevenueServicesNet",
    "RevenueFromExternalCustomers",
    # IFRS filers (20-F/40-F) tag the same idea in their own taxonomy.
    "RevenueFromContractsWithCustomers",
    "Revenue",
)
_RANK = {tag: i for i, tag in enumerate(REVENUE_TAGS)}

# Which breakdown an axis represents, matched on the lower-cased local name so
# one rule covers the prefix moving between taxonomies (``us-gaap`` -> ``srt``
# in 2021) and the IFRS spellings. Order matters: a geographical axis is checked
# before the catch-all "segment" rule, which would otherwise claim
# ``StatementGeographicalAxis`` for a filer that calls its regions segments.
DIMENSIONS: Tuple[Tuple[str, str, str], ...] = (
    ("geographic", "Geography", "geograph"),
    ("product", "Products & services", "productorservice"),
    ("product", "Products & services", "productsandservices"),
    ("product", "Products & services", "productline"),
    ("business", "Reportable segments", "segment"),
)
SECTIONS: Dict[str, str] = {key: label for key, label, _ in DIMENSIONS}
ORDER: Tuple[str, ...] = ("business", "geographic", "product")

# Axes that qualify a fact rather than split it, with the members that keep the
# fact usable. Segment revenue is routinely tagged on the segments axis *and* on
# ConsolidationItems; anything other than an operating segment there is a
# reconciling item or an intersegment elimination, which is not a segment.
QUALIFIERS: Dict[str, frozenset] = {
    "consolidationitemsaxis": frozenset(
        {"operatingsegmentsmember", "reportablesegmentsmember", "reportablesubsegmentsmember"}
    ),
}

# How far back to walk the filing list. Each 10-K carries the current year plus
# two comparatives, so four of them already reach six years; 10-Qs overlap far
# less, so quarterly needs more of them.
MAX_FILINGS: Dict[str, int] = {"annual": 6, "quarter": 9}

# A group whose members add up to more than this much of revenue is reporting
# two levels of the same axis at once, not one breakdown.
_OVERCOUNT = 1.02

# Bounds on the roll-up search below, so a filer with an unusually wide axis
# cannot turn it into an exponential one.
_ROLLUP_MAX_MEMBERS = 18
_ROLLUP_MAX_SUMS = 200_000

_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
_MEMBER_SUFFIX = re.compile(r"\s*\[(?:member|domain)\]\s*$", re.I)


def _headers() -> Dict[str, str]:
    return sec._headers()  # noqa: SLF001 - one User-Agent policy for all of EDGAR


def _local(name: str) -> str:
    """``us-gaap:Revenues`` / ``{ns}Revenues`` -> ``Revenues``."""
    return name.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _element_id(member: str) -> str:
    """``aapl:IPhoneMember`` -> ``aapl_IPhoneMember``, how linkbases key it."""
    return member.replace(":", "_")


def _folder(cik: Any, accession: str) -> str:
    return "{}/Archives/edgar/data/{}/{}".format(
        sec.BASE, int(str(cik).lstrip("0") or 0), accession.replace("-", "")
    )


# --------------------------------------------------------------------------- #
# Instance parsing
# --------------------------------------------------------------------------- #
def _dimension_of(axis: str) -> Optional[str]:
    axis = _local(axis).lower()
    for key, _label, needle in DIMENSIONS:
        if needle in axis:
            return key
    return None


def _contexts(root: ET.Element) -> Dict[str, Dict[str, Any]]:
    """``{context id: {dims, start, end}}`` for every duration context."""
    out: Dict[str, Dict[str, Any]] = {}
    for el in root:
        if _local(el.tag) != "context":
            continue
        dims: Dict[str, str] = {}
        start = end = None
        typed = False
        for sub in el.iter():
            tag = _local(sub.tag)
            if tag == "explicitMember":
                dims[sub.attrib.get("dimension", "")] = (sub.text or "").strip()
            elif tag == "typedMember":
                # A typed dimension carries a value rather than a named member,
                # so there is nothing to label a row with.
                typed = True
            elif tag == "startDate":
                start = (sub.text or "").strip()
            elif tag == "endDate":
                end = (sub.text or "").strip()
        if typed or not (start and end):
            continue
        out[el.attrib.get("id", "")] = {"dims": dims, "start": start, "end": end}
    return out


def _currencies(root: ET.Element) -> Dict[str, str]:
    """``{unit id: ISO code}``, so non-monetary facts can be skipped."""
    out: Dict[str, str] = {}
    for el in root:
        if _local(el.tag) != "unit":
            continue
        measures = [(m.text or "").strip() for m in el.iter() if _local(m.tag) == "measure"]
        # A rate or a per-share unit divides one measure by another; revenue is
        # a single monetary measure.
        if len(measures) == 1 and measures[0].startswith("iso4217:"):
            out[el.attrib.get("id", "")] = measures[0].split(":", 1)[-1]
    return out


def _breakdown(dims: Dict[str, str]) -> Optional[Tuple[str, str]]:
    """The one axis a fact is split on, or ``None`` if it is not a clean split.

    Rejects a cross-tab cell sitting on two breakdown axes, a fact qualified as
    a reconciling item or an intersegment elimination, and anything on an axis
    this module does not read — a forecast scenario or a class of stock, say.
    """
    found: Optional[Tuple[str, str]] = None
    for axis, member in dims.items():
        allowed = QUALIFIERS.get(_local(axis).lower())
        if allowed is not None:
            if _local(member).lower() not in allowed:
                return None
            continue
        key = _dimension_of(axis)
        if key is None or found is not None:
            return None
        found = (key, member)
    return found


def _instance_name(names: Sequence[str], primary_document: str) -> Optional[str]:
    """The XBRL instance in a filing folder.

    Inline filings (2019 onwards for most filers) keep their facts inside the
    primary HTML document; EDGAR extracts them into ``<doc>_htm.xml`` alongside
    it. Older filings shipped a separate instance named for the period.
    """
    stem = primary_document.rsplit(".", 1)[0]
    for candidate in ("{}_htm.xml".format(stem), "{}.xml".format(stem)):
        if candidate in names:
            return candidate
    extracted = [n for n in names if n.endswith("_htm.xml")]
    if extracted:
        return extracted[0]
    skip = ("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml", "_ref.xml")
    plain = [
        n for n in names
        if n.endswith(".xml")
        and not n.endswith(skip)
        and n != "FilingSummary.xml"
        and not re.match(r"^R\d+\.xml$", n)
        and "-index" not in n
    ]
    return plain[0] if plain else None


# The version in the cache key is the parser's, not the filing's: a filing is
# immutable and cached for a week, so widening REVENUE_TAGS or the axis rules
# has to invalidate what the previous rules extracted.
@cached("sec.segments.facts.v2", ttl=TTL_REFERENCE)
def filing_facts(cik: str, accession: str, primary_document: str) -> List[Dict[str, Any]]:
    """Every revenue fact in one filing, dimensioned or not.

    A filed document never changes, so this is memoised on the accession rather
    than on the symbol: later filings restate earlier periods, and both versions
    stay individually addressable.
    """
    folder = _folder(cik, accession)
    listing = get_json(folder + "/index.json", headers=_headers(), ttl=TTL_REFERENCE)
    names = [item.get("name", "") for item in listing.get("directory", {}).get("item", [])]
    instance = _instance_name(names, primary_document)
    if not instance:
        return []

    root = ET.fromstring(fetch(folder + "/" + instance, headers=_headers(), ttl=TTL_REFERENCE))
    contexts = _contexts(root)
    units = _currencies(root)

    rows: List[Dict[str, Any]] = []
    for el in root:
        rank = _RANK.get(_local(el.tag))
        if rank is None or el.attrib.get(XSI_NIL) == "true":
            continue
        context = contexts.get(el.attrib.get("contextRef", ""))
        currency = units.get(el.attrib.get("unitRef", ""))
        if context is None or currency is None:
            continue
        try:
            value = float((el.text or "").strip())
        except ValueError:
            continue
        split = _breakdown(context["dims"]) if context["dims"] else None
        if context["dims"] and split is None:
            continue
        rows.append(
            {
                "dimension": split[0] if split else None,
                "member": split[1] if split else None,
                "concept": _local(el.tag),
                "rank": rank,
                "currency": currency,
                "start": context["start"],
                "end": context["end"],
                "value": value,
                "accession": accession,
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Labels and tables
# --------------------------------------------------------------------------- #
@cached("sec.segments.taxonomy", ttl=TTL_REFERENCE)
def filing_taxonomy(cik: str, accession: str, primary_document: str) -> Dict[str, Any]:
    """What the filing calls its members, and which table each appears in.

    The instance names members in QName form (``aapl:IPhoneMember``); the name
    the filer prints in the table — "iPhone" — is in the linkbases filed
    alongside it. ``MetaLinks.json``, which EDGAR generates for every inline
    filing, has both those labels *and* the presentation roles each member
    appears under, which is what tells one revenue table from another. Filings
    old enough to predate it fall back to the label linkbase, which carries the
    names but not the tables.

    Returns ``{"labels": {element: text}, "roles": {element: [role]}, "tables":
    {role: short name}}``.
    """
    folder = _folder(cik, accession)
    try:
        return _from_metalinks(get_json(folder + "/MetaLinks.json", headers=_headers(),
                                        ttl=TTL_REFERENCE))
    except Exception:  # noqa: BLE001 - pre-2019 filings have no MetaLinks
        pass
    listing = get_json(folder + "/index.json", headers=_headers(), ttl=TTL_REFERENCE)
    names = [item.get("name", "") for item in listing.get("directory", {}).get("item", [])]
    return {"labels": _from_label_linkbase(folder, names), "roles": {}, "tables": {}}


def _role_name(role: str) -> str:
    """``http://www.microsoft.com/20260630/taxonomy/role/RoleXYZ`` -> ``RoleXYZ``.

    A filer's role URIs carry the taxonomy date, so the same table has a
    different URI every year. The trailing name is what stays put, and this is
    read across several filings at once.
    """
    return role.rstrip("/").rsplit("/", 1)[-1]


def _from_metalinks(payload: Dict[str, Any]) -> Dict[str, Any]:
    labels: Dict[str, str] = {}
    roles: Dict[str, List[str]] = {}
    tables: Dict[str, str] = {}
    for instance in (payload.get("instance") or {}).values():
        for report in (instance.get("report") or {}).values():
            if isinstance(report, dict) and report.get("role") and report.get("shortName"):
                tables.setdefault(_role_name(str(report["role"])), str(report["shortName"]))
        for element, node in (instance.get("tag") or {}).items():
            if not isinstance(node, dict):
                continue
            named = ((node.get("lang") or {}).get("en-us") or {}).get("role") or {}
            # The terse label is what a filer writes in a table heading
            # ("iPhone"); the standard label is the same thing plus "[Member]".
            text = named.get("terseLabel") or named.get("label")
            if text:
                labels.setdefault(element, str(text))
            presented = [_role_name(str(r)) for r in (node.get("presentation") or [])]
            if presented:
                roles.setdefault(element, presented)
    return {"labels": labels, "roles": roles, "tables": tables}


def _from_label_linkbase(folder: str, names: Sequence[str]) -> Dict[str, str]:
    """Member names out of a filing's label linkbase (pre-MetaLinks filings)."""
    name = next((n for n in names if n.endswith("_lab.xml")), None)
    if not name:
        return {}
    try:
        root = ET.fromstring(fetch(folder + "/" + name, headers=_headers(), ttl=TTL_REFERENCE))
    except Exception:  # noqa: BLE001 - labels are cosmetic, never the point
        return {}

    locators: Dict[str, str] = {}
    arcs: List[Tuple[str, str]] = []
    labels: Dict[Tuple[str, str], str] = {}
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "loc":
            locators[el.attrib.get(XLINK + "label", "")] = (
                el.attrib.get(XLINK + "href", "").split("#")[-1]
            )
        elif tag == "labelArc":
            arcs.append((el.attrib.get(XLINK + "from", ""), el.attrib.get(XLINK + "to", "")))
        elif tag == "label":
            role = el.attrib.get(XLINK + "role", "").rsplit("/", 1)[-1]
            labels[(el.attrib.get(XLINK + "label", ""), role)] = (el.text or "").strip()

    out: Dict[str, str] = {}
    for source, target in arcs:
        element = locators.get(source)
        text = labels.get((target, "terseLabel")) or labels.get((target, "label"))
        if element and text:
            out.setdefault(element, text)
    return out


def _member_label(member: str, labels: Dict[str, str]) -> str:
    """A readable name for an XBRL member.

    The filing answers for almost every member; the fallback splits the QName,
    which is right often enough ("AmericasSegmentMember" -> "Americas") to beat
    printing the identifier.
    """
    # Labels are filed as XML text, so an ampersand arrives escaped.
    text = html.unescape(_MEMBER_SUFFIX.sub("", labels.get(_element_id(member), ""))).strip()
    if text:
        # Country members are labelled in capitals in the SEC's own taxonomy.
        return string.capwords(text.lower()) if text.isupper() and " " in text else text
    local = _local(member)
    for suffix in ("Member", "Segment", "Axis"):
        if local.endswith(suffix) and local != suffix:
            local = local[: -len(suffix)]
    words = _CAMEL.findall(local)
    return " ".join(words) if words else local


# --------------------------------------------------------------------------- #
# Separating the levels of one axis
# --------------------------------------------------------------------------- #
def _restated(frame: pd.DataFrame) -> pd.DataFrame:
    """One filing's version of each period, not several stitched together.

    Filers rename segments as readily as they re-cut them: Microsoft's FY2026
    10-K restates the same FY2025 product lines its FY2025 10-K reported, with
    two of them renamed. Both filings are in hand, and taken together they show
    that year's revenue twice under two sets of headings.

    The newest filing is the one that restates, so it is the one kept — unless
    all it mentions of that period is a single member, which is a passing
    reference in a note rather than the table.
    """
    keep: List[pd.DataFrame] = []
    # Grouped on the exact span, not just its end: a 10-Q reports the quarter
    # and the year to date against the same date, and the year-to-date figure
    # is what a fiscal Q4 is later worked out from.
    for _, group in frame.groupby(["dimension", "start", "end"], sort=False):
        members = group.groupby("filed")["member"].nunique().sort_index()
        tables = members[members > 1]
        keep.append(group[group["filed"] == (tables.index[-1] if len(tables)
                                             else members.index[-1])])
    return pd.concat(keep) if keep else frame


def _choose_table(values: Dict[str, float], roles: Dict[str, Sequence[str]],
                  total: Optional[float]) -> Optional[Tuple[str, List[str]]]:
    """The members of the single best revenue table on this axis.

    Called only when the axis over-counts, which means the filer tagged two
    tables against it. Candidate tables are ranked on adding up to revenue
    first and on being the finer split second, so the eleven product lines win
    over the two-line Product/Service split that sums to the same total.
    """
    grouped: Dict[str, List[str]] = {}
    for member in values:
        for role in roles.get(member, ()):
            grouped.setdefault(role, []).append(member)
    candidates = {r: m for r, m in grouped.items() if len(m) > 1}
    if not candidates:
        return None

    def score(candidate: Tuple[str, List[str]]) -> Tuple[int, int, float]:
        covered = sum(values[m] for m in candidate[1])
        complete = bool(total) and abs(covered - total) <= total * (_OVERCOUNT - 1)
        return (int(complete), len(candidate[1]), -abs(covered - (total or covered)))

    role, best = max(candidates.items(), key=score)
    # Only worth taking if it actually resolves the over-count.
    if total and sum(values[m] for m in best) > total * _OVERCOUNT:
        return None
    return role, best


def _subset_sums_to(values: Iterable[float], target: float) -> bool:
    """Can two or more of ``values`` add up to exactly ``target``?

    Values equal to the target are excluded first, so a hit always involves at
    least two members: one member matching another exactly is a coincidence
    between siblings, not evidence that one contains the other.
    """
    goal = round(target)
    if goal <= 0:
        return False
    # Negative members are intersegment eliminations, which are never part of a
    # parent line; leaving them out only makes the test more conservative.
    reachable = {0}
    for value in (round(v) for v in values):
        if not 0 < value < goal:
            continue
        reachable |= {s + value for s in reachable if s + value <= goal}
        if goal in reachable:
            return True
        if len(reachable) > _ROLLUP_MAX_SUMS:
            break
    return goal in reachable


def _drop_rollups(values: Dict[str, float], total: Optional[float]) -> Tuple[List[str], List[str]]:
    """Members to keep, and members that are a roll-up of their neighbours.

    Runs only while the group over-counts. A breakdown that adds up is left
    exactly as filed, whatever coincidences its numbers contain.
    """
    kept = dict(values)
    dropped: List[str] = []
    if not total or len(kept) > _ROLLUP_MAX_MEMBERS:
        return list(kept), dropped
    while len(kept) > 2 and sum(kept.values()) > total * _OVERCOUNT:
        parent = next(
            (
                name for name, value in sorted(kept.items(), key=lambda kv: -kv[1])
                if _subset_sums_to([v for k, v in kept.items() if k != name], value)
            ),
            None,
        )
        if parent is None:
            break
        dropped.append(parent)
        kept.pop(parent)
    return list(kept), dropped


def _resolve(values: Dict[str, float], roles: Dict[str, Sequence[str]],
             total: Optional[float]) -> Tuple[List[str], List[str], Optional[str]]:
    """One level of one axis for one period.

    Returns what to keep, what it replaced, and the table it was read out of
    when one had to be picked.
    """
    if not total or sum(values.values()) <= total * _OVERCOUNT:
        return list(values), [], None
    chosen = _choose_table(values, roles, total)
    dropped: List[str] = []
    role = None
    if chosen is not None:
        role, members = chosen
        dropped = [m for m in values if m not in members]
        values = {m: values[m] for m in members}
    kept, rolled_up = _drop_rollups(values, total)
    return kept, dropped + rolled_up, role


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
def _period_series(frame: pd.DataFrame, period: str) -> pd.Series:
    """One value per period end, from facts that may span several lengths."""
    if frame.empty:
        return pd.Series(dtype="float64")
    frame = frame.copy()
    frame["end"] = pd.to_datetime(frame["end"], errors="coerce")
    frame["start"] = pd.to_datetime(frame["start"], errors="coerce")
    frame = frame.dropna(subset=["end", "start"])
    frame["days"] = (frame["end"] - frame["start"]).dt.days
    frame["val"] = pd.to_numeric(frame["value"], errors="coerce")
    if period == "annual":
        frame = frame[frame["days"].between(*sec._ANNUAL_DAYS)]  # noqa: SLF001
        if frame.empty:
            return pd.Series(dtype="float64")
        # Newest filing wins a restatement; the preferred concept wins a tie.
        frame = frame.sort_values(["filed", "rank"], ascending=[True, False])
        return frame.drop_duplicates("end", keep="last").set_index("end")["val"].sort_index()
    # Quarterly segment data is filed as three-month *and* year-to-date spans,
    # and fiscal Q4 is never filed at all — the same arithmetic the income
    # statement needs, so it runs through the same helper. Its own de-duplication
    # keeps the last row of each span, which this ordering makes the best-ranked
    # concept from the newest filing.
    frame = frame.sort_values(["rank", "filed"], ascending=[False, True])
    return sec._quarterize(frame)  # noqa: SLF001


def _fill_totals(series: pd.Series, symbol: str, period: str,
                 periods: Sequence[Any]) -> pd.Series:
    """Top up consolidated revenue from company-facts where a filing lacks it.

    The instance documents lead — they are the documents the segment rows came
    out of, so a total taken from them is on the same basis — and the API is
    only asked when one of the periods on show has no total at all. That call
    downloads the filer's entire fact history, which is worth not doing when the
    filings have already answered.
    """
    if all(_value(series, stamp) is not None for stamp in periods):
        return series
    try:
        filed = sec.statement(symbol, "income", period, 40)
    except Exception:  # noqa: BLE001 - the filings' own totals are enough
        return series
    if "revenue" not in filed.columns:
        return series
    fallback = pd.to_numeric(filed["revenue"], errors="coerce").dropna()
    fallback.index = pd.to_datetime(fallback.index, errors="coerce")
    return series.combine_first(fallback)


def _collect(symbol: str, period: str, limit: int) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    """Walk filings newest-first until enough periods are covered."""
    forms = "10-K,20-F,40-F" if period == "annual" else "10-Q,10-K,20-F"
    index = sec.filings(symbol, form_type=forms, limit=MAX_FILINGS[period])

    collected: List[Dict[str, Any]] = []
    parsed: List[Dict[str, Any]] = []
    span = sec._ANNUAL_DAYS if period == "annual" else (65, 400)  # noqa: SLF001
    covered: set = set()
    annual_seen = False
    for row in index.itertuples():
        annual_seen = annual_seen or str(row.form).upper().startswith(("10-K", "20-F", "40-F"))
        entry = {"accession": str(row.accession_number), "form": str(row.form),
                 "filed": pd.Timestamp(row.filing_date).date().isoformat(), "url": str(row.url)}
        try:
            # Copied because the parse is memoised: the filing metadata stamped
            # on below belongs to this walk, not to the cached document.
            facts = [dict(f) for f in filing_facts(str(row.cik), str(row.accession_number),
                                                   str(row.primary_document))]
        except Exception as exc:  # noqa: BLE001 - one unreadable filing is not fatal
            parsed.append(dict(entry, error=str(exc)))
            continue
        segmented = 0
        for fact in facts:
            fact["filed"] = pd.Timestamp(row.filing_date)
            fact["cik"] = str(row.cik)
            fact["primary_document"] = str(row.primary_document)
            if fact["member"]:
                segmented += 1
                length = (pd.Timestamp(fact["end"]) - pd.Timestamp(fact["start"])).days
                if span[0] <= length <= span[1]:
                    covered.add(fact["end"])
        collected.extend(facts)
        parsed.append(dict(entry, segment_facts=segmented))
        # One period more than asked for, so the oldest column still has
        # something behind it to be compared against. Quarterly also waits for
        # an annual report: nobody files fiscal Q4 on its own, and it is the
        # full year less the nine months that recovers it.
        if len(covered) >= limit + 1 and (period == "annual" or annual_seen):
            break
    return pd.DataFrame(collected), parsed


def _describe(facts: pd.DataFrame, members: Iterable[str]) -> Dict[str, Any]:
    """Labels and table membership, from as few filings as will answer.

    The newest filing names almost every member; older ones are opened only for
    members it has never heard of — a segment retired three years ago, say.
    """
    wanted = set(members)
    labels: Dict[str, str] = {}
    roles: Dict[str, List[str]] = {}
    tables: Dict[str, str] = {}
    seen: set = set()
    for row in facts.sort_values("filed", ascending=False).itertuples():
        if not wanted:
            break
        if row.accession in seen:
            continue
        seen.add(row.accession)
        try:
            found = filing_taxonomy(str(row.cik), str(row.accession), str(row.primary_document))
        except Exception:  # noqa: BLE001 - fall back to the QName
            continue
        for member in list(wanted):
            text = found["labels"].get(_element_id(member))
            if text:
                labels.setdefault(_element_id(member), text)
                wanted.discard(member)
        for element, presented in found["roles"].items():
            roles.setdefault(element, presented)
        tables.update({k: v for k, v in found["tables"].items() if k not in tables})
    return {"labels": labels, "roles": roles, "tables": tables}


def revenue_segments(symbol: str, period: str = "annual", limit: int = 8,
                     dimension: str = "all") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Revenue by reportable segment, geography and product line, as filed.

    Returns ``(rows, meta)``: one row per segment with one column per period,
    newest first — the shape the statements use — plus a subtotal per breakdown
    and consolidated revenue to read them against.
    """
    symbol = symbol.upper().strip()
    if period not in ("annual", "quarter"):
        raise ValueError("period must be annual or quarter")
    if dimension not in ("all",) + ORDER:
        raise ValueError("dimension must be all, {} or {}".format(", ".join(ORDER[:-1]), ORDER[-1]))

    facts, parsed = _collect(symbol, period, limit)
    if facts.empty or facts["member"].isna().all():
        raise EmptyDataError(
            "{} files no disaggregated revenue in XBRL. Single-segment companies "
            "have nothing to split, and filings before roughly 2010 predate the "
            "tagging.".format(symbol)
        )

    segmented = facts[facts["member"].notna()]
    if dimension != "all":
        segmented = segmented[segmented["dimension"] == dimension]
        if segmented.empty:
            raise EmptyDataError("{} reports no revenue on the {} axis".format(symbol, dimension))

    segmented = _restated(segmented)
    taxonomy = _describe(segmented, set(segmented["member"]))
    labels = {m: _member_label(m, taxonomy["labels"]) for m in set(segmented["member"])}

    # {dimension: {segment: series}} — each on its own period axis, because
    # segments are created, renamed and retired between filings.
    series: Dict[str, Dict[str, pd.Series]] = {}
    roles: Dict[str, List[str]] = {}
    for (dim, member), group in segmented.groupby(["dimension", "member"], sort=False):
        values = _period_series(group, period)
        if values.empty:
            continue
        name = labels[member]
        # A filer that re-tags a segment under a new QName has renamed the
        # element, not the business: same label, same row, one period axis.
        held = series.setdefault(dim, {}).get(name)
        series[dim][name] = values if held is None else values.combine_first(held)
        # Keyed by breakdown as well as name: McDonald's calls a reportable
        # segment "U.S." and a geography "U.S.", and they are not one row.
        roles.setdefault((dim, name), []).extend(taxonomy["roles"].get(_element_id(member), []))

    periods = sorted(
        {p for members in series.values() for s in members.values() for p in s.index},
        reverse=True,
    )[:limit]
    if not periods:
        raise EmptyDataError("No {} segment revenue reported by {}".format(period, symbol))

    totals = _fill_totals(_period_series(facts[facts["member"].isna()], period),
                          symbol, period, periods)

    rows: List[Dict[str, Any]] = []
    summary: List[Dict[str, Any]] = []
    superseded: List[str] = []
    for dim in ORDER:
        members = series.get(dim)
        if not members:
            continue
        # Resolved per period: a filer that re-cut its segments mid-history
        # reports the coarse split in the older years and the finer one since.
        kept_by_period: Dict[Any, List[str]] = {}
        table = None
        for stamp in periods:
            present = {name: float(s[stamp]) for name, s in members.items()
                       if stamp in s.index and pd.notna(s[stamp])}
            kept, dropped, role = _resolve(
                present, {name: roles.get((dim, name), []) for name in present},
                _value(totals, stamp))
            kept_by_period[stamp] = kept
            superseded.extend(dropped)
            if table is None and role:
                table = taxonomy["tables"].get(role)

        emitted: List[Dict[str, Any]] = []
        for name, values in members.items():
            row: Dict[str, Any] = {
                "dimension": dim, "section": SECTIONS[dim], "segment": name,
                "indent": 1, "weight": "", "derived": False,
            }
            for stamp in periods:
                keep = name in kept_by_period.get(stamp, ())
                row[_key(stamp)] = (float(values[stamp])
                                    if keep and stamp in values.index and pd.notna(values[stamp])
                                    else None)
            if any(row[_key(p)] is not None for p in periods):
                emitted.append(row)
        if not emitted:
            continue
        emitted.sort(key=lambda r: -max((r[_key(p)] or 0) for p in periods))

        subtotal: Dict[str, Any] = {
            "dimension": dim, "section": SECTIONS[dim], "segment": "Total disclosed",
            "indent": 0, "weight": "subtotal", "derived": True,
        }
        for stamp in periods:
            reported = [r[_key(stamp)] for r in emitted if r[_key(stamp)] is not None]
            subtotal[_key(stamp)] = sum(reported) if reported else None
        for row in emitted + [subtotal]:
            row["revenue_share"] = _share(row, periods, totals)

        rows.extend(emitted + [subtotal])
        summary.append({"dimension": dim, "section": SECTIONS[dim], "members": len(emitted),
                        "coverage": subtotal["revenue_share"], "table": table})

    total_row: Dict[str, Any] = {
        "dimension": "total", "section": "", "segment": "Total revenue",
        "indent": 0, "weight": "total", "derived": False, "revenue_share": 1.0,
    }
    for stamp in periods:
        total_row[_key(stamp)] = _value(totals, stamp)
    if any(total_row[_key(p)] is not None for p in periods):
        rows.append(total_row)

    currencies = sorted(set(facts["currency"].dropna()))
    meta = {
        "warnings": _coverage_warnings(summary),
        "symbol": symbol,
        "period": period,
        "periods": [_key(p) for p in periods],
        "currency": currencies[0] if len(currencies) == 1 else currencies,
        "dimensions": summary,
        "filings": parsed,
        "superseded": sorted(set(superseded)),
    }
    return rows, meta


def _coverage_warnings(summary: Sequence[Dict[str, Any]]) -> List[str]:
    """Say so where a breakdown does not add up to the revenue it splits.

    Neither direction is an error, and both change what the percentages mean:
    a filer need only disclose the segments it has, and segment revenue is
    reported *before* the sales segments make to each other are eliminated.
    """
    out: List[str] = []
    for entry in summary:
        coverage = entry.get("coverage")
        if coverage is None or 0.95 <= coverage <= 1.05:
            continue
        out.append(
            "{}: the rows add up to {:.0%} of consolidated revenue — {}.".format(
                entry["section"], coverage,
                "segment revenue includes sales between segments, or a segment is "
                "tagged alongside its own parts" if coverage > 1
                else "the filer discloses no more of the split than this"
            )
        )
    return out


def _key(stamp: Any) -> str:
    return str(pd.Timestamp(stamp).date())


def _value(series: pd.Series, stamp: Any) -> Optional[float]:
    if stamp is None or series.empty:
        return None
    hit = series.get(pd.Timestamp(stamp))
    return None if hit is None or pd.isna(hit) else float(hit)


def _share(row: Dict[str, Any], periods: Sequence[Any], totals: pd.Series) -> Optional[float]:
    """Share of consolidated revenue in the newest period the row reports."""
    for stamp in periods:
        value = row.get(_key(stamp))
        total = _value(totals, stamp)
        if value is not None and total:
            return round(value / total, 4)
    return None
