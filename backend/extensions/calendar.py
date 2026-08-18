"""One calendar over many event feeds.

Earnings, dividends, splits, IPOs and macro releases each arrive in a different
shape from a different source — Nasdaq serves a day at a time with US corporate
detail, Yahoo serves a date range with global coverage and a twelve-row default.
Read separately they are five tables that happen to have dates in them. This
module normalises all of them onto one row shape so a single view can ask "what
lands between these two dates, of these types, for these symbols" and get one
sorted answer.

The normalised row is deliberately small — ``date``, ``time``, ``type``,
``symbol``, ``name``, ``title``, ``detail``, ``importance``, ``source`` — and
every feed's extra columns survive in ``detail`` as text rather than widening
the schema for one source's benefit. A row is a thing that happens on a day.

**What this cannot show.** A terminal's event-type list usually also carries
corporate access, analyst marketing, deal roadshows, shareholder meetings and
broadcast appearances. Those come from investor-relations feeds and broker
calendars that have no free public equivalent, so they are published in the
catalogue as unavailable with the reason attached rather than quietly missing —
an empty "Corporate Access" filter that looks selectable reads as "nothing
scheduled", which is a lie about the data rather than a gap in it.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from ..core.errors import EmptyDataError, ProviderError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..providers import markets, yahoo

# --------------------------------------------------------------------------- #
# The event-type catalogue
# --------------------------------------------------------------------------- #
# ``available`` is what separates a type this platform can actually fill from
# one a terminal user expects to see. The UI renders both; only the first is
# selectable. ``why`` explains the second.
EVENT_TYPES: List[Dict[str, Any]] = [
    {"key": "earnings", "label": "Earnings Release", "group": "Company",
     "badge": "E", "available": True, "sources": ["nasdaq", "yahoo"],
     "description": "Scheduled results, with the session timing and the consensus EPS estimate."},
    {"key": "dividend_ex", "label": "Ex-Dividend", "group": "Company",
     "badge": "D", "available": True, "sources": ["nasdaq"],
     "description": "The first session a buyer no longer receives the declared dividend."},
    {"key": "dividend_pay", "label": "Dividend Payable", "group": "Company",
     "badge": "D", "available": True, "sources": ["nasdaq"],
     "description": "The day a declared dividend is actually paid."},
    {"key": "split", "label": "Stock Split", "group": "Company",
     "badge": "S", "available": True, "sources": ["nasdaq", "yahoo"],
     "description": "Splits and reverse splits, on the execution date."},
    {"key": "ipo", "label": "IPO", "group": "Market",
     "badge": "I", "available": True, "sources": ["nasdaq", "yahoo"],
     "description": "Priced, expected, filed and withdrawn offerings."},
    {"key": "economic", "label": "Economic Release", "group": "Macro",
     "badge": "M", "available": True, "sources": ["yahoo", "fred"],
     "description": "Scheduled macro statistics, with consensus and prior where published."},
    {"key": "fomc", "label": "FOMC Decision", "group": "Macro",
     "badge": "F", "available": True, "sources": ["federalreserve"],
     "description": "Fed rate decisions on the committee's own published schedule — "
                    "the statement day, whether the meeting carries the dot plot, and "
                    "what a past meeting did to the target range. Minutes releases, "
                    "three weeks later, ride on the same type."},
    {"key": "fedspeak", "label": "Fed Speeches", "group": "Macro",
     "badge": "S", "available": True, "sources": ["federalreserve"],
     "description": "Speeches, congressional testimony and Board press releases, from "
                    "the Fed's own feeds. Published records rather than a schedule: "
                    "these land on the calendar after they happen."},
    {"key": "custom", "label": "Custom / Notes", "group": "Yours",
     "badge": "N", "available": True, "sources": ["user"],
     "description": "Your own dated notes, stored per account and never sent anywhere."},
    # Present, explained, not selectable.
    {"key": "earnings_call", "label": "Earnings Call", "group": "Company",
     "badge": "C", "available": False, "sources": [],
     "why": "No free feed publishes call times as data. The release row carries "
            "the session timing (pre-market / after-hours), which is the part "
            "the public sources do give."},
    {"key": "sales_result", "label": "Sales Result", "group": "Company",
     "badge": "S", "available": False, "sources": [],
     "why": "Monthly and quarterly sales updates are issuer-scheduled and only "
            "aggregated by paid vendors."},
    {"key": "guidance", "label": "TV / Conf / Pres", "group": "Company",
     "badge": "T", "available": False, "sources": [],
     "why": "Conference appearances and broadcast slots come from IR and broker "
            "calendars, which have no public feed."},
    {"key": "shareholder_mtg", "label": "Shareholder Mtgs", "group": "Company",
     "badge": "H", "available": False, "sources": [],
     "why": "The meeting date is inside the DEF 14A proxy as prose, not as a "
            "dated field, so it cannot be read reliably enough to schedule on."},
    {"key": "corporate_access", "label": "Corporate Access", "group": "Broker",
     "badge": "A", "available": False, "sources": [],
     "why": "Broker-hosted and distributed to that broker's clients only."},
    {"key": "analyst_marketing", "label": "Analyst Marketing", "group": "Broker",
     "badge": "A", "available": False, "sources": [],
     "why": "Broker-hosted and distributed to that broker's clients only."},
    {"key": "deal_roadshow", "label": "Deal Roadshow", "group": "Broker",
     "badge": "R", "available": False, "sources": [],
     "why": "Syndicate calendars are private to the deal's participants."},
]

TYPES_BY_KEY = {t["key"]: t for t in EVENT_TYPES}
AVAILABLE_TYPES = [t["key"] for t in EVENT_TYPES if t["available"]]
# ``custom`` lives in the database behind authentication, so it is filled by
# /api/user/calendar rather than here. It stays in the catalogue because the
# checkbox list is one list.
PLATFORM_TYPES = [t for t in AVAILABLE_TYPES if t != "custom"]

MAX_WINDOW_DAYS = 400


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _iso(value: Any) -> Optional[str]:
    """Any of the four date spellings these feeds use, as ``YYYY-MM-DD``.

    Nasdaq writes ``8/17/2026``, Yahoo hands back tz-aware timestamps, and both
    use several placeholders for "no date" that ``pd.to_datetime`` would happily
    turn into today.
    """
    if value is None or value == "" or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if text.lower() in ("nat", "nan", "none", "n/a", "--", "-"):
        return None
    try:
        stamp = pd.to_datetime(text, errors="coerce")
    except (ValueError, TypeError):
        return None
    if stamp is None or pd.isna(stamp):
        return None
    return str(stamp.date())


def _clean(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> Optional[float]:
    """A float out of ``$20,297,189,413`` / ``1.2796`` / ``--``."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = re.sub(r"[^0-9.\-]", "", str(value))
    if text in ("", "-", ".", "-."):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _window(start_date: Optional[str], end_date: Optional[str],
            default_days: int = 14) -> tuple:
    """The requested window, clamped, as two dates."""
    first = pd.Timestamp(start_date).date() if start_date else date.today()
    last = pd.Timestamp(end_date).date() if end_date else first + timedelta(days=default_days)
    if last < first:
        first, last = last, first
    span = (last - first).days
    if span > MAX_WINDOW_DAYS:
        last = first + timedelta(days=MAX_WINDOW_DAYS)
    return first, last


def _row(date_: Optional[str], type_: str, *, symbol: Optional[str] = None,
         name: Optional[str] = None, title: str = "", detail: Optional[str] = None,
         time: Optional[str] = None, importance: int = 1,
         source: str = "") -> Optional[Dict[str, Any]]:
    """One normalised event, or ``None`` when the feed gave no usable date.

    A dateless row cannot go on a calendar, and putting it on today's cell
    because today is what ``to_datetime`` returns for junk would be worse than
    dropping it.
    """
    if not date_:
        return None
    return {
        "date": date_,
        "time": time,
        "type": type_,
        "type_label": TYPES_BY_KEY.get(type_, {}).get("label", type_),
        "symbol": symbol,
        "name": name,
        "title": title,
        "detail": detail,
        "importance": importance,
        "source": source,
    }


def _cap_tier(market_cap: Optional[float]) -> int:
    """Importance from size: a mega-cap print moves an index, a micro-cap does not."""
    if market_cap is None:
        return 1
    if market_cap >= 50e9:
        return 3
    if market_cap >= 2e9:
        return 2
    return 1


_SESSION_TIMING = {
    "time-pre-market": "pre-market",
    "time-after-hours": "after-hours",
    "time-not-supplied": None,
    "bmo": "pre-market",
    "amc": "after-hours",
    "tns": None,
    "tas": None,
}


def _timing(value: Any) -> Optional[str]:
    text = (_clean(value) or "").lower()
    return _SESSION_TIMING.get(text, text or None)


# --------------------------------------------------------------------------- #
# Normalisers — one per (source, type)
# --------------------------------------------------------------------------- #
def _nasdaq_earnings(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    out = []
    for r in frame.to_dict("records"):
        cap = _number(r.get("market_cap"))
        est = _clean(r.get("eps_forecast"))
        bits = []
        if est:
            bits.append("EPS est {}".format(est))
        if _clean(r.get("fiscal_quarter_ending")):
            bits.append("quarter ending {}".format(_clean(r.get("fiscal_quarter_ending"))))
        if _clean(r.get("no_of_ests")):
            bits.append("{} estimates".format(_clean(r.get("no_of_ests"))))
        out.append(_row(
            _iso(r.get("calendar_date")), "earnings",
            symbol=_clean(r.get("symbol")), name=_clean(r.get("name")),
            title="{} reports".format(_clean(r.get("symbol")) or _clean(r.get("name")) or "Earnings"),
            detail=" · ".join(bits) or None, time=_timing(r.get("time")),
            importance=_cap_tier(cap), source="nasdaq"))
    return [r for r in out if r]


def _nasdaq_dividends(frame: pd.DataFrame, wanted: set) -> List[Dict[str, Any]]:
    """One disclosure row becomes up to two events.

    The ex-date and the payment date are different days and a holder cares about
    them differently — the ex-date is the one that has to be owned through, the
    payment date is when the cash lands. Emitting one row on the ex-date and
    labelling it "dividend" would hide the second date entirely.
    """
    out = []
    for r in frame.to_dict("records"):
        symbol, name = _clean(r.get("symbol")), _clean(r.get("company_name"))
        rate = _number(r.get("dividend_rate"))
        annual = _number(r.get("indicated_annual_dividend"))
        detail = []
        if rate is not None:
            detail.append("${:,.4f} per share".format(rate).replace(".0000", ""))
        if annual:
            detail.append("${:,.2f} indicated annual".format(annual))
        detail_text = " · ".join(detail) or None

        if "dividend_ex" in wanted:
            out.append(_row(
                _iso(r.get("dividend_ex_date")), "dividend_ex", symbol=symbol, name=name,
                title="{} goes ex-dividend".format(symbol or name or "Ex-dividend"),
                detail=detail_text, importance=2, source="nasdaq"))
        if "dividend_pay" in wanted:
            out.append(_row(
                _iso(r.get("payment_date")), "dividend_pay", symbol=symbol, name=name,
                title="{} pays dividend".format(symbol or name or "Dividend"),
                detail=detail_text, importance=1, source="nasdaq"))
    return [r for r in out if r]


def _nasdaq_splits(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    out = []
    for r in frame.to_dict("records"):
        symbol, ratio = _clean(r.get("symbol")), _clean(r.get("ratio"))
        out.append(_row(
            _iso(r.get("execution_date")) or _iso(r.get("calendar_date")), "split",
            symbol=symbol, name=_clean(r.get("name")),
            title="{} splits {}".format(symbol or "Split", ratio or ""),
            detail="Ratio {}".format(ratio) if ratio else None,
            importance=2, source="nasdaq"))
    return [r for r in out if r]


_IPO_STATUS_DATE = {"priced": "pricedDate", "upcoming": "expectedPriceDate",
                    "filed": "filedDate", "withdrawn": "withdrawDate"}


def _nasdaq_ipo(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    """The IPO feed is four tables stacked, each dated by a different column."""
    out = []
    for r in frame.to_dict("records"):
        status = _clean(r.get("status")) or "upcoming"
        when = _iso(r.get(_IPO_STATUS_DATE.get(status, "expectedPriceDate")))
        if not when:  # fall back across the other three before giving up
            for col in _IPO_STATUS_DATE.values():
                when = when or _iso(r.get(col))
        symbol = _clean(r.get("proposedTickerSymbol"))
        price, value = _clean(r.get("proposedSharePrice")), _clean(r.get("dollarValueOfSharesOffered"))
        bits = [b for b in (
            _clean(r.get("proposedExchange")),
            "${} per share".format(price) if price else None,
            "{} raised".format(value) if value else None) if b]
        out.append(_row(
            when, "ipo", symbol=symbol, name=_clean(r.get("companyName")),
            title="{} IPO — {}".format(symbol or _clean(r.get("companyName")) or "Offering", status),
            detail=" · ".join(bits) or None, importance=2, source="nasdaq"))
    return [r for r in out if r]


def _yahoo_frame(frame: pd.DataFrame, index_name: str) -> List[Dict[str, Any]]:
    """Yahoo indexes these calendars by symbol/event rather than a column."""
    if frame.empty:
        return []
    out = frame.reset_index()
    if out.columns[0] in ("index", 0):
        out = out.rename(columns={out.columns[0]: index_name})
    return out.to_dict("records")


def _yahoo_earnings(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    out = []
    for r in _yahoo_frame(frame, "Symbol"):
        cap = _number(r.get("Marketcap"))
        est = r.get("EPS Estimate")
        symbol = _clean(r.get("Symbol"))
        bits = []
        if est is not None and not pd.isna(est):
            bits.append("EPS est {:.2f}".format(float(est)))
        if _clean(r.get("Event Name")):
            bits.append(_clean(r.get("Event Name")))
        out.append(_row(
            _iso(r.get("Event Start Date")), "earnings",
            symbol=symbol, name=_clean(r.get("Company")),
            title="{} reports".format(symbol or _clean(r.get("Company")) or "Earnings"),
            detail=" · ".join(bits) or None, time=_timing(r.get("Timing")),
            importance=_cap_tier(cap), source="yahoo"))
    return [r for r in out if r]


def _yahoo_splits(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    out = []
    for r in _yahoo_frame(frame, "Symbol"):
        symbol = _clean(r.get("Symbol"))
        old, new = _number(r.get("Old Share Worth")), _number(r.get("Share Worth"))
        ratio = "{:g} : {:g}".format(old, new) if old and new else None
        out.append(_row(
            _iso(r.get("Payable On")), "split", symbol=symbol, name=_clean(r.get("Company")),
            title="{} splits {}".format(symbol or "Split", ratio or ""),
            detail="Ratio {}".format(ratio) if ratio else None,
            importance=2, source="yahoo"))
    return [r for r in out if r]


def _yahoo_ipo(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    out = []
    for r in _yahoo_frame(frame, "Symbol"):
        symbol, action = _clean(r.get("Symbol")), _clean(r.get("Action"))
        price = _number(r.get("Price"))
        bits = [b for b in (
            _clean(r.get("Exchange")),
            "${:g} per share".format(price) if price else None) if b]
        out.append(_row(
            _iso(r.get("Date")), "ipo", symbol=symbol, name=_clean(r.get("Company")),
            title="{} IPO — {}".format(symbol or _clean(r.get("Company")) or "Offering",
                                       (action or "expected").lower()),
            detail=" · ".join(bits) or None, importance=2, source="yahoo"))
    return [r for r in out if r]


# The releases that reliably move rates and equities, versus the long tail of
# national statistics the same feed carries for every country it covers.
_MAJOR_RELEASES = (
    "fomc", "fed funds", "interest rate", "cpi", "core cpi", "ppi", "pce",
    "non-farm", "nonfarm", "payroll", "unemployment", "gdp", "retail sales",
    "ism", "pmi", "jobless", "consumer confidence", "consumer sentiment",
    "durable goods", "housing starts", "trade balance", "industrial production",
)
_MAJOR_REGIONS = ("US", "EU", "GB", "CN", "JP", "DE")


def _economic_importance(event: str, region: Optional[str]) -> int:
    """Three tiers, because a hundred-row global feed with no ranking is noise.

    A release is a 3 only if it is one of the headline series *and* comes from a
    region whose data moves other markets. The same indicator from a small
    economy is a 2 at most — it is real data, it just is not what a US book
    reprices on.
    """
    name = (event or "").lower()
    headline = any(k in name for k in _MAJOR_RELEASES)
    major_region = (region or "").upper() in _MAJOR_REGIONS
    if headline and major_region:
        return 3
    if headline or major_region:
        return 2
    return 1


def _yahoo_economic(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    out = []
    for r in _yahoo_frame(frame, "Event"):
        event = _clean(r.get("Event"))
        region = _clean(r.get("Region"))
        bits = []
        for label, key in (("actual", "Actual"), ("est", "Expected"), ("prior", "Last")):
            value = r.get(key)
            if value is not None and not pd.isna(value):
                bits.append("{} {:g}".format(label, float(value)))
        period = _clean(r.get("For"))
        if period:
            bits.append("for {}".format(period))
        out.append(_row(
            _iso(r.get("Event Time")), "economic", symbol=None, name=region,
            title="{}{}".format("{} · ".format(region) if region else "", event or "Release"),
            detail=" · ".join(bits) or None,
            importance=_economic_importance(event or "", region), source="yahoo"))
    return [r for r in out if r]


def _fomc_detail(meeting: Dict[str, Any]) -> Optional[str]:
    """What the meeting did, or what it will carry — whichever it has yet."""
    bits: List[str] = []
    lower, upper = meeting.get("target_lower"), meeting.get("target_upper")
    band = "{:g}-{:g}%".format(lower, upper) if lower is not None and upper is not None else None
    decision, bps = meeting.get("decision"), meeting.get("change_bps")
    if decision in ("hiked", "cut") and bps:
        bits.append("{} {:g} bps{}".format(
            "Raised" if decision == "hiked" else "Cut", abs(bps),
            " to {}".format(band) if band else ""))
    elif decision == "held":
        bits.append("Held{}".format(" at {}".format(band) if band else ""))
    if meeting.get("projections"):
        bits.append("Summary of Economic Projections")
    if meeting.get("press_conference"):
        bits.append("press conference")
    if meeting.get("days") == 2:
        bits.append("second day of a two-day meeting")
    return " · ".join(bits) or None


def _collect_fomc(first: date, last: date, warnings: List[str]) -> List[Dict[str, Any]]:
    """Rate decisions from the Fed's own calendar.

    Neither market feed carries these: Yahoo's macro calendar lists the release
    of a statistic, not the meeting that sets the rate every statistic is read
    against. The committee publishes its own schedule, so this reads that.
    """
    from .fed import meetings as fed_meetings  # local: keeps the import graph one-way

    try:
        rows = fed_meetings(limit=1000).data
    except (EmptyDataError, ProviderError) as exc:
        warnings.append("fomc: {}".format(exc))
        return []

    out = []
    for meeting in rows:
        window = str(first) <= meeting["date"] <= str(last)
        notation = meeting.get("kind") == "notation vote"
        if window:
            out.append(_row(
                _iso(meeting["date"]), "fomc", symbol=None, name="US",
                title="FOMC {}".format("notation vote" if notation else "rate decision"),
                detail=_fomc_detail(meeting),
                # The statement has gone out at 2pm Eastern for every scheduled
                # meeting in the published record.
                time=None if notation else "2:00 PM ET",
                importance=3, source="federalreserve.gov"))
        # The minutes are their own event three weeks later, and the Fed only
        # publishes that date once they are out — so these are always past.
        released = meeting.get("minutes_released")
        if released and str(first) <= released <= str(last):
            out.append(_row(
                _iso(released), "fomc", symbol=None, name="US", title="FOMC minutes",
                detail="Account of the {} meeting".format(meeting["date"]),
                time="2:00 PM ET", importance=3, source="federalreserve.gov"))
    return [r for r in out if r]


#: A speech is a speech; testimony to Congress, the symposium at Jackson Hole
#: and the policy releases themselves are the ones that move a curve.
_MAJOR_SPEAK = ("congressional", "jackson_hole")


def _collect_fedspeak(first: date, last: date, warnings: List[str],
                      covered: Optional[set] = None) -> List[Dict[str, Any]]:
    """Speeches, testimony and Board releases, from the Fed's own feeds.

    ``covered`` is the set of dates the FOMC type already filled. The press
    feed announces the statement and the minutes too, so those rows are dropped
    where the decision row already says what happened — but an FOMC statement
    on a date with no scheduled meeting is kept, because that one is the news.
    """
    from .fed_signals import communications as fed_communications

    span = max(1, (date.today() - first).days + 1)
    try:
        rows = fed_communications(days=span, limit=400).data
    except (EmptyDataError, ProviderError, ValueError) as exc:
        warnings.append("fedspeak: {}".format(exc))
        return []

    out = []
    for row in rows:
        if not (str(first) <= row["date"] <= str(last)):
            continue
        if row.get("document") and row["date"] in (covered or set()):
            continue
        major = any(row.get(flag) for flag in _MAJOR_SPEAK) or row.get("document")
        speaker = row.get("speaker")
        out.append(_row(
            _iso(row["date"]), "fedspeak", symbol=None, name="US",
            title="{}: {}".format(speaker or "Fed", row["title"]),
            detail=" · ".join(filter(None, (
                "congressional testimony" if row.get("congressional") else None,
                "Jackson Hole" if row.get("jackson_hole") else None,
                "issued between scheduled meetings" if row.get("off_calendar") else None,
                row.get("summary")))) or None,
            importance=3 if major else 2, source="federalreserve.gov"))
    return [r for r in out if r]


def _fred_economic(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    out = []
    for r in frame.to_dict("records"):
        name = _clean(r.get("release_name")) or _clean(r.get("name"))
        out.append(_row(
            _iso(r.get("date")), "economic", symbol=None, name="US",
            title="US · {}".format(name or "Release"),
            detail="FRED release {}".format(_clean(r.get("release_id")) or "").strip() or None,
            importance=_economic_importance(name or "", "US"), source="fred"))
    return [r for r in out if r]


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
def _requested_types(types: Optional[str]) -> List[str]:
    if not types:
        return list(PLATFORM_TYPES)
    asked = [t.strip().lower() for t in str(types).replace(" ", ",").split(",") if t.strip()]
    # A bad `types` value is the caller's mistake, not the upstream feed's, so
    # these are ValueErrors — the API layer turns those into a 400 rather than
    # the 502 a ProviderError would imply.
    unknown = [t for t in asked if t not in TYPES_BY_KEY]
    if unknown:
        raise ValueError("Unknown event type(s): {}. Available: {}".format(
            ", ".join(unknown), ", ".join(AVAILABLE_TYPES)))
    unavailable = [t for t in asked if not TYPES_BY_KEY[t]["available"]]
    if unavailable:
        raise ValueError("No free source for: {}. {}".format(
            ", ".join(unavailable),
            " ".join(TYPES_BY_KEY[t].get("why", "") for t in unavailable)))
    return [t for t in asked if t in PLATFORM_TYPES]


def _symbol_filter(rows: List[Dict[str, Any]], symbols: Optional[str]) -> List[Dict[str, Any]]:
    if not symbols:
        return rows
    wanted = {s.strip().upper() for s in str(symbols).replace(" ", ",").split(",") if s.strip()}
    if not wanted:
        return rows
    # Macro releases have no symbol; a symbol filter is a question about
    # companies, so they drop out rather than riding along on every request.
    return [r for r in rows if (r.get("symbol") or "").upper() in wanted]


def _collect_nasdaq(wanted: List[str], first: date, last: date,
                    max_days: int, warnings: List[str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    plan = [
        ("earnings", {"earnings"}, _nasdaq_earnings),
        ("dividends", {"dividend_ex", "dividend_pay"}, None),
        ("splits", {"split"}, _nasdaq_splits),
        ("ipo", {"ipo"}, _nasdaq_ipo),
    ]
    for kind, covers, normalise in plan:
        if not covers & set(wanted):
            continue
        try:
            if kind == "ipo":
                # The IPO feed is monthly, so one call per month in the window.
                months = pd.date_range(first, last, freq="MS").union(
                    pd.DatetimeIndex([pd.Timestamp(first)]))
                frames = []
                for month in months:
                    try:
                        frames.append(markets.nasdaq_calendar("ipo", str(month.date())))
                    except (EmptyDataError, ProviderError):
                        continue
                if not frames:
                    continue
                frame = pd.concat(frames, ignore_index=True)
                # One deal appears in consecutive months while it is pending.
                # Nasdaq's own id is the reliable key, but the four buckets do
                # not all carry it, so fall back to the whole row.
                if "dealID" in frame.columns:
                    frame = frame.drop_duplicates(subset=["dealID"])
            else:
                frame = markets.nasdaq_calendar_range(kind, str(first), str(last), max_days=max_days)
                dropped = frame.attrs.get("days_truncated") or 0
                if dropped:
                    warnings.append(
                        "{}: only the first {} weekdays of the window were read "
                        "({} more not fetched). Narrow the dates or raise "
                        "`max_days` to see the rest.".format(kind, max_days, dropped))
        except (EmptyDataError, ProviderError) as exc:
            warnings.append("{}: {}".format(kind, exc))
            continue
        rows.extend(_nasdaq_dividends(frame, set(wanted)) if normalise is None
                    else normalise(frame))
    return rows


#: Importance floors as market caps, applied by Yahoo inside the query.
_CAP_FLOOR = {2: 2e9, 3: 50e9}


def _collect_yahoo(wanted: List[str], first: date, last: date, limit: int,
                   warnings: List[str], min_importance: int = 1) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    plan = [
        ("earnings", "earnings", _yahoo_earnings),
        ("split", "splits", _yahoo_splits),
        ("ipo", "ipo", _yahoo_ipo),
        ("economic", "economic", _yahoo_economic),
    ]
    for key, kind, normalise in plan:
        if key not in wanted:
            continue
        try:
            frame = yahoo.market_calendar(
                kind, str(first), str(last), limit=limit, most_active=False,
                market_cap=_CAP_FLOOR.get(int(min_importance)) if kind == "earnings" else None)
        except (EmptyDataError, ProviderError, ValueError) as exc:
            warnings.append("{}: {}".format(kind, exc))
            continue
        rows.extend(normalise(frame))
    return rows


def _sorted_events(rows: List[Dict[str, Any]], first: date, last: date,
                   limit: int) -> tuple:
    """Deduplicated, date-sorted, capped — plus how many the cap dropped.

    The count matters more than it looks. Truncation takes the *end* of the
    window, so a month view that hits the cap goes blank from whatever date the
    limit ran out, which on a calendar is indistinguishable from "nothing is
    scheduled". The caller turns a non-zero drop into a warning.
    """
    lo, hi = str(first), str(last)
    inside = [r for r in rows if lo <= r["date"] <= hi]
    # A feed can hand back the same event twice (a Nasdaq day-walk overlapping a
    # monthly IPO pull, most often). Key on what makes an event distinct.
    seen, unique = set(), []
    for r in sorted(inside, key=lambda x: (x["date"], -x["importance"],
                                           x["type"], x["symbol"] or x["title"])):
        key = (r["date"], r["type"], r["symbol"], r["title"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return unique[:limit], max(0, len(unique) - limit)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@command("/calendar/event_types", providers=("mft",),
         summary="Every event type the calendar can show, and the ones it cannot")
def event_types(available_only: bool = False, provider: Optional[str] = None) -> Result:
    """The catalogue behind the event-type filter.

    Types this platform cannot source are returned too, with ``available:
    false`` and a ``why``. That is deliberate: a filter that silently omits
    corporate access looks like a calendar with nothing scheduled, while one
    that lists it greyed out tells the truth about where the gap is.
    """
    resolve_provider(provider, ("mft",))
    rows = [t for t in EVENT_TYPES if t["available"]] if available_only else list(EVENT_TYPES)
    return Result(rows, provider="mft", extra={
        "available": len(AVAILABLE_TYPES), "total": len(EVENT_TYPES)})


@command("/calendar/events", providers=("nasdaq", "yahoo"),
         summary="One dated feed across earnings, dividends, splits, IPOs and macro")
def calendar_events(start_date: Optional[str] = None, end_date: Optional[str] = None,
                    types: Optional[str] = None, symbols: Optional[str] = None,
                    min_importance: int = 1, limit: int = 500, max_days: int = 45,
                    provider: Optional[str] = None) -> Result:
    """Every event of the requested ``types`` between two dates, one row shape.

    ``types`` is a comma-separated list from ``/calendar/event_types``; omitted,
    it means every type this platform can fill. ``symbols`` narrows to named
    companies and drops macro releases along the way, since a symbol filter is a
    question about issuers.

    The two providers are not interchangeable and the choice is real:

    * ``nasdaq`` serves one **day** per request with US corporate detail — EPS
      forecasts, dividend rates, split ratios — and is the only source here with
      a dividend calendar at all. A month costs a month of requests, so
      ``max_days`` caps the walk and any truncation is reported rather than
      silently applied.
    * ``yahoo`` serves a **date range** in one request with global coverage and
      the macro calendar, but publishes no dividends.

    Neither covers everything, and ``provider`` is a preference about *how* to
    fetch rather than permission to drop a type that was explicitly asked for.
    So each falls through to the other for what it lacks: ask ``nasdaq`` for
    ``economic`` and the macro rows come from Yahoo; ask ``yahoo`` for
    ``dividend_ex`` and the dividend rows come from Nasdaq, with a warning that
    that part of the window is a day walk.

    ``min_importance`` filters 1-3, where 3 is a mega-cap print or a headline
    macro release from a major economy. It exists because an unranked global
    feed is a hundred rows of national statistics with the FOMC somewhere in
    the middle.
    """
    src = resolve_provider(provider, ("nasdaq", "yahoo"))
    first, last = _window(start_date, end_date)
    wanted = _requested_types(types)
    limit = max(1, min(int(limit), 5000))
    warnings: List[str] = []

    # Naming symbols is a stronger and more specific intent than a size floor,
    # so it wins. Without this, filtering to a company you actually hold returns
    # nothing whenever it happens to be smaller than the floor — the filter
    # would silently answer a question nobody asked.
    if symbols and int(min_importance) > 1:
        warnings.append(
            "`symbols` overrides `min_importance`: named companies are returned "
            "whatever their size.")
        min_importance = 1

    if not wanted:
        raise EmptyDataError(
            "No platform event types requested. `custom` events come from "
            "/api/user/calendar, which is per-account.")

    # Neither provider covers every type, and the provider argument is a
    # preference about *how* to fetch, not permission to drop a type the caller
    # explicitly asked for. So each falls through to the other for the types it
    # does not carry: Nasdaq has no macro calendar, Yahoo has no dividends.
    walk_days = max(1, min(int(max_days), 120))
    # The Fed's own events belong to neither market feed — they come from the
    # Board whichever provider is serving the rest of the window.
    rows: List[Dict[str, Any]] = _collect_fomc(first, last, warnings) if "fomc" in wanted else []
    if "fedspeak" in wanted:
        rows += _collect_fedspeak(first, last, warnings, {r["date"] for r in rows})
    feeds = [t for t in wanted if t not in ("fomc", "fedspeak")]
    if src == "nasdaq":
        rows += _collect_nasdaq(feeds, first, last, walk_days, warnings)
        if "economic" in feeds:
            rows.extend(_collect_yahoo(["economic"], first, last, limit, warnings,
                                       min_importance))
    else:
        rows += _collect_yahoo(feeds, first, last, limit, warnings, min_importance)
        dividends = [t for t in feeds if t.startswith("dividend_")]
        if dividends:
            warnings.append(
                "Yahoo publishes no dividend calendar, so ex-dividend and "
                "payment dates came from Nasdaq — one request per day, which is "
                "slower than the rest of this window.")
            rows.extend(_collect_nasdaq(dividends, first, last, walk_days, warnings))

    rows = _symbol_filter(rows, symbols)
    if int(min_importance) > 1:
        rows = [r for r in rows if r["importance"] >= int(min_importance)]
    events, dropped = _sorted_events(rows, first, last, limit)

    if not events:
        raise EmptyDataError("No {} events between {} and {}{}".format(
            ", ".join(wanted), first, last,
            " ({})".format("; ".join(warnings)) if warnings else ""))
    if dropped:
        warnings.append(
            "{} more events matched than the {}-row limit allows, so this window "
            "stops at {}. Later dates are cut off, not empty — raise `limit`, "
            "raise `min_importance`, or ask for fewer types.".format(
                dropped, limit, events[-1]["date"]))

    counts: Dict[str, int] = {}
    for r in events:
        counts[r["type"]] = counts.get(r["type"], 0) + 1
    return Result(events, provider=src, warnings=warnings, extra={
        "start_date": str(first), "end_date": str(last), "types": wanted,
        "counts": counts, "total": len(events), "truncated": dropped})


@command("/calendar/economic", providers=("yahoo", "fred"),
         summary="Scheduled macro releases, ranked and filterable by region")
def calendar_economic(start_date: Optional[str] = None, end_date: Optional[str] = None,
                      region: Optional[str] = None, min_importance: int = 1,
                      limit: int = 300, provider: Optional[str] = None) -> Result:
    """The macro calendar on the same row shape as every other event type.

    Distinct from ``/economy/calendar``, which hands back each provider's frame
    unchanged: this one normalises, ranks and filters so it can be merged into
    ``/calendar/events``. Reach for that one to see a provider's raw columns and
    this one to put releases on a calendar beside earnings.

    ``region`` takes the feed's two-letter codes (``US``, ``EU``, ``GB``,
    ``JP``…), comma-separated. Yahoo's calendar is global and unranked, so
    without a region or an importance floor the honest description of the
    output is "every national statistic on earth, in date order".

    The ``fred`` provider is the official US release schedule — more
    authoritative for US dates, US-only, and it needs a free API key
    (``MFT_FRED_API_KEY``). Yahoo needs no key and carries consensus and prior
    values, which FRED's schedule does not.
    """
    src = resolve_provider(provider, ("yahoo", "fred"))
    first, last = _window(start_date, end_date, default_days=14)
    limit = max(1, min(int(limit), 1000))
    warnings: List[str] = []

    if src == "fred":
        from ..providers import fred

        frame = fred.release_dates(str(first), str(last), limit)
        rows = _fred_economic(frame)
        warnings.append(
            "FRED publishes the release schedule, not the numbers: consensus "
            "and prior columns are empty. Use provider=yahoo for those.")
    else:
        frame = yahoo.market_calendar("economic", str(first), str(last), limit=limit)
        rows = _yahoo_economic(frame)

    if region:
        wanted = {r.strip().upper() for r in str(region).replace(" ", ",").split(",") if r.strip()}
        rows = [r for r in rows if (r.get("name") or "").upper() in wanted]
    if int(min_importance) > 1:
        rows = [r for r in rows if r["importance"] >= int(min_importance)]

    events, dropped = _sorted_events(rows, first, last, limit)
    if not events:
        raise EmptyDataError("No macro releases between {} and {} matching that filter".format(
            first, last))
    if dropped:
        warnings.append(
            "{} more releases matched than the {}-row limit allows, so this "
            "window stops at {} rather than running to {}.".format(
                dropped, limit, events[-1]["date"], last))

    regions = sorted({r["name"] for r in events if r.get("name")})
    return Result(events, provider=src, warnings=warnings, extra={
        "start_date": str(first), "end_date": str(last), "regions": regions,
        "total": len(events), "truncated": dropped})
