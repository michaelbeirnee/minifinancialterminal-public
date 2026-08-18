"""The FOMC's own meeting calendar, read from federalreserve.gov.

Rate decisions are dated by the committee, not by the market: eight scheduled
meetings a year, most of them two days, with the statement released on the
second afternoon. Everything downstream of a hike — when the target series
moves, when the minutes land, which meeting carried projections — hangs off
those dates, and the Fed publishes them itself as HTML at a stable URL.

Two details of the page matter to the parser:

* A meeting can straddle two months (``Apr/May 30-1``) and is filed under the
  month it *starts* in, so the end date has to be rebuilt from the second half
  of both fields rather than assumed to share the first one's month.
* An asterisk on the date marks a meeting that also publishes the Summary of
  Economic Projections — the dot plot — which is why a "* meeting" moves
  markets more than a plain one. It is kept as a flag rather than dropped as
  punctuation.

**Coverage.** The page carries roughly the current year, the five before it and
the one ahead: enough for the schedule and the recent past, not a history. The
full record of what the committee *did* comes from FRED's target series instead
(:mod:`backend.extensions.fed`), which runs back to 1982 and needs no scraping.
"""
from __future__ import annotations

import html as html_lib
import re
from typing import Any, Dict, List, Optional

import pandas as pd

from ..core.caching import TTL_DAILY, TTL_FUNDAMENTAL, TTL_REFERENCE, cached
from ..core.errors import EmptyDataError, ProviderError
from ..core.http import fetch, get_html_tables

NAME = "federalreserve"

BASE = "https://www.federalreserve.gov"
CALENDAR_URL = BASE + "/monetarypolicy/fomccalendars.htm"

_YEAR = re.compile(r">(\d{4})\s+FOMC Meetings<")
# One meeting block: the month cell, the day cell, and everything up to the
# next month cell — which is where that meeting's links live.
_MEETING = re.compile(
    r"fomc-meeting__month[^>]*>\s*<strong>\s*(?P<month>[^<]+?)\s*</strong>"
    r".*?fomc-meeting__date[^>]*>\s*(?P<days>[^<]+?)\s*<"
    r"(?P<body>.*?)(?=fomc-meeting__month|$)",
    re.S,
)
_STATEMENT = re.compile(r'href="(/newsevents/pressreleases/monetary\d{8}a\.htm)"')
_MINUTES = re.compile(r'href="(/monetarypolicy/fomcminutes\d{8}\.htm)"')
# The Fed has published this link under both spellings over the years.
_PRESSCONF = re.compile(r'href="(/monetarypolicy/fomcpres+conf\d{8}\.htm)"')
_PROJECTIONS = re.compile(r'href="(/monetarypolicy/fomcprojtabl\d{8}\.htm)"')
_RELEASED = re.compile(r"\(Released\s+([^)]+)\)")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _month_number(name: str) -> Optional[int]:
    return _MONTHS.get(name.strip().lower()[:3])


def _url(path: Optional[str]) -> Optional[str]:
    return BASE + path if path else None


def _parse_meeting(year: int, month_text: str, day_text: str, body: str) -> Optional[Dict[str, Any]]:
    """One row from the three cells of a meeting block.

    ``month_text`` is either ``"June"`` or ``"Apr/May"``; ``day_text`` is
    ``"16-17"``, ``"17-18*"``, ``"30-1"``, ``"5"`` or ``"22 (notation vote)"``.
    """
    kind = "notation vote" if "notation" in day_text.lower() else "scheduled"
    unscheduled = "unscheduled" in day_text.lower()
    projections = "*" in day_text

    days = re.findall(r"\d{1,2}", re.sub(r"\([^)]*\)", "", day_text))
    months = [m for m in (_month_number(part) for part in month_text.split("/")) if m]
    if not days or not months:
        return None

    start_month, end_month = months[0], months[-1]
    # December to January is the only wrap the schedule can produce, and it has
    # not happened in the published window — handle it anyway rather than dating
    # a January meeting into the previous year.
    end_year = year + 1 if end_month < start_month else year
    try:
        start = pd.Timestamp(year=year, month=start_month, day=int(days[0]))
        end = pd.Timestamp(year=end_year, month=end_month, day=int(days[-1]))
    except ValueError:
        return None

    minutes_release = _RELEASED.search(body)
    released = pd.to_datetime(minutes_release.group(1), errors="coerce") if minutes_release else None
    statement, minutes, presser, projtabl = (
        r.search(body) for r in (_STATEMENT, _MINUTES, _PRESSCONF, _PROJECTIONS))

    return {
        # The decision day: the statement goes out on the last afternoon, so
        # that is the date this meeting belongs on in any calendar.
        "date": str(end.date()),
        "start_date": str(start.date()),
        "end_date": str(end.date()),
        "year": year,
        "days": (end - start).days + 1,
        "kind": "unscheduled" if unscheduled else kind,
        "projections": projections,
        "press_conference": bool(presser),
        "statement_url": _url(statement.group(1) if statement else None),
        "minutes_url": _url(minutes.group(1) if minutes else None),
        "minutes_released": None if released is None or pd.isna(released) else str(released.date()),
        "press_conference_url": _url(presser.group(1) if presser else None),
        "projections_url": _url(projtabl.group(1) if projtabl else None),
    }


def parse_calendar(html: str) -> pd.DataFrame:
    """Every meeting on the Fed's calendar page, oldest first.

    Split out from the fetch so the parser can be tested against a saved page:
    this is HTML written for humans, and the shape of it is the only thing that
    can break here.
    """
    blocks = _YEAR.split(html)
    rows: List[Dict[str, Any]] = []
    # split() gives [preamble, year, block, year, block, …]; the years are not
    # in order on the page (the coming year is listed last), so each block is
    # parsed under its own heading and the whole set sorted at the end.
    for year_text, block in zip(blocks[1::2], blocks[2::2]):
        for match in _MEETING.finditer(block):
            row = _parse_meeting(int(year_text), match.group("month"),
                                 match.group("days"), match.group("body"))
            if row:
                rows.append(row)
    if not rows:
        raise ProviderError(
            "No FOMC meetings found on {} — the page layout has changed".format(CALENDAR_URL)
        )
    return (pd.DataFrame(rows)
            .drop_duplicates(subset=["date"])
            .sort_values("date")
            .reset_index(drop=True))


@cached("fomc.meetings", ttl=TTL_FUNDAMENTAL)
def meetings() -> pd.DataFrame:
    """The published FOMC meeting calendar: scheduled dates, past and ahead."""
    html = fetch(CALENDAR_URL, ttl=TTL_FUNDAMENTAL).decode("utf-8", "replace")
    df = parse_calendar(html)
    if df.empty:
        raise EmptyDataError("The Fed's calendar page listed no meetings")
    return df


# --------------------------------------------------------------------------- #
# Statements and minutes — the text itself
# --------------------------------------------------------------------------- #
#: The Fed's press-release template puts the release body in this column div,
#: after a share bar and before the footer. Everything the committee actually
#: said is inside it.
_ARTICLE = re.compile(r'id="article".*?(<div class="col-xs-12 col-sm-8 col-md-8">)(?P<body>.*?)</div>',
                      re.S)
_PARAGRAPH = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
_TAG = re.compile(r"<[^>]+>")
#: Boilerplate that rides along on every release and says nothing about policy.
_BOILERPLATE = re.compile(
    r"(?i)(media inquiries|implementation note|last update|for release at|"
    r"^\s*$|share\b|email protected)")


def _text(fragment: str) -> str:
    """One HTML fragment as the sentence a reader would see."""
    return html_lib.unescape(_TAG.sub("", fragment)).replace("\xa0", " ").strip()


def paragraphs(page: str) -> List[str]:
    """The body paragraphs of a Fed release, boilerplate dropped.

    Split from the fetch so it can be tested on a saved page. The Fed's own
    markup is the only structure here: no class names on the paragraphs, so the
    body is located by its column div and the furniture is filtered by content.
    """
    article = _ARTICLE.search(page)
    scope = article.group("body") if article else page
    out = []
    for fragment in _PARAGRAPH.findall(scope):
        line = _text(fragment)
        if line and not _BOILERPLATE.search(line):
            out.append(line)
    return out


_VOTE = re.compile(r"by an?\s+(\d+)\s*[–\-—]\s*(\d+)\s+vote", re.I)


def parse_statement(page: str) -> Dict[str, Any]:
    """An FOMC statement as text plus the two things a reader looks for first.

    The **vote** ("by a 9 – 3 vote") and the **dissents** — who voted against
    and what they wanted instead — are the committee telling you how close the
    decision was, which is exactly what the minutes get read for three weeks
    later. Both are prose in the statement, so both are read out of it here.
    """
    lines = paragraphs(page)
    body = " ".join(lines)
    vote = _VOTE.search(body)
    dissent = [line for line in lines if line.lower().startswith("voting against")]
    return {
        "paragraphs": lines,
        "text": "\n\n".join(lines),
        "votes_for": int(vote.group(1)) if vote else None,
        "votes_against": int(vote.group(2)) if vote else None,
        "dissent": dissent[0] if dissent else None,
        "unanimous": (vote is not None and int(vote.group(2)) == 0) or (not dissent and vote is None),
    }


@cached("fomc.document", ttl=TTL_REFERENCE)
def document(url: str) -> Dict[str, Any]:
    """A statement or minutes page, parsed. Old text never changes: cached long."""
    if not url.startswith(BASE):
        raise ProviderError("Not a federalreserve.gov document: {}".format(url))
    page = fetch(url, ttl=TTL_REFERENCE).decode("utf-8", "replace")
    parsed = parse_statement(page)
    if not parsed["paragraphs"]:
        raise EmptyDataError("No release text found at {}".format(url))
    parsed["url"] = url
    return parsed


# --------------------------------------------------------------------------- #
# The Summary of Economic Projections, dot plot included
# --------------------------------------------------------------------------- #
#: Table 1 of the projection materials — every variable's median, central
#: tendency and range, by year and for the longer run.
_SEP_VARIABLES = ("Change in real GDP", "Unemployment rate", "PCE inflation",
                  "Core PCE inflation", "Federal funds rate")
#: The dot plot's own table: one row per rate level, one column per year,
#: holding the number of participants who put their dot there.
_DOT_HEADING = "Midpoint of target range"


#: Table 1 states each variable's current projection and, on the row beneath
#: it, the same projection from the previous SEP — "March projection" under the
#: June table. That row belongs to the variable above it, not to itself.
_PRIOR_ROW = re.compile(r"^[A-Z][a-z]+\s+projections?$")


def _sep_table(frame: pd.DataFrame) -> pd.DataFrame:
    """Table 1 flattened to one row per variable, horizon, measure and vintage."""
    rows: List[Dict[str, Any]] = []
    variable = None
    for _, record in frame.iterrows():
        values = list(record.items())
        label = re.sub(r"\d$", "", _text(str(values[0][1]))).strip()
        if not label or label.lower() == "nan":
            continue
        # "Memo: Projected appropriate policy path" is a section header spanning
        # the table; pandas fills a colspan by repeating it into every cell.
        if all(_text(str(value)) == label for _, value in values[1:]):
            continue
        if _PRIOR_ROW.match(label):
            vintage, month = "previous", label.split()[0]
        else:
            variable, vintage, month = label, "current", None
        if not variable:
            continue
        for (section, horizon), value in values[1:]:
            text = _text(str(value))
            if text in ("", "nan", "-", "–"):
                continue
            median = pd.to_numeric(text, errors="coerce")
            rows.append({
                "variable": variable,
                "horizon": str(horizon).strip(),
                "measure": re.sub(r"\d$", "", str(section)).strip().lower().replace(" ", "_"),
                "vintage": vintage,
                "previous_meeting_month": month,
                "value": text,
                "number": None if pd.isna(median) else round(float(median), 3),
            })
    return pd.DataFrame(rows)


def _dot_table(frame: pd.DataFrame) -> pd.DataFrame:
    """The dot plot: rate level, horizon, and how many participants chose it."""
    level_column = frame.columns[0]
    rows: List[Dict[str, Any]] = []
    for _, record in frame.iterrows():
        level = pd.to_numeric(record[level_column], errors="coerce")
        if pd.isna(level):
            continue
        for horizon in frame.columns[1:]:
            dots = pd.to_numeric(record[horizon], errors="coerce")
            if pd.isna(dots) or dots <= 0:
                continue
            rows.append({"horizon": str(horizon).strip(), "rate": round(float(level), 3),
                         "participants": int(dots)})
    return pd.DataFrame(rows)


@cached("fomc.projections", ttl=TTL_REFERENCE)
def projections(url: str) -> Dict[str, Any]:
    """One meeting's projection materials: the SEP table and the dot plot.

    Both come from the Fed's own accessible HTML rather than the PDF everyone
    screenshots — the dot plot is published as a table of counts by rate level,
    which is the chart's underlying data and needs no image parsing.
    """
    if not url.startswith(BASE):
        raise ProviderError("Not a federalreserve.gov document: {}".format(url))
    tables = get_html_tables(url, ttl=TTL_REFERENCE)
    if not tables:
        raise EmptyDataError("No projection tables published at {}".format(url))

    dots = pd.DataFrame()
    for table in tables:
        if _DOT_HEADING.lower() in str(table.columns[0]).lower():
            dots = _dot_table(table)
            break
    sep = _sep_table(tables[0])
    if sep.empty and dots.empty:
        raise EmptyDataError("The projection materials at {} parsed to nothing".format(url))
    return {"url": url, "sep": sep, "dots": dots}


# --------------------------------------------------------------------------- #
# Everything the Fed says between meetings
# --------------------------------------------------------------------------- #
#: The Board's own feeds. Speeches and testimony carry the speaker in the
#: title; the press feeds carry the actions — statements, minutes, facilities
#: and the bank-regulatory notices that show up in a stress episode.
FEEDS: Dict[str, str] = {
    "speech": BASE + "/feeds/speeches.xml",
    "testimony": BASE + "/feeds/testimony.xml",
    "monetary": BASE + "/feeds/press_monetary.xml",
    "banking": BASE + "/feeds/press_bcreg.xml",
    "other": BASE + "/feeds/press_other.xml",
}

#: "Cook, Outlook for the U.S. and Alaskan Economies" — the Board writes every
#: speech and testimony title this way, so the speaker is the first field.
_SPEAKER = re.compile(r"^([A-Z][A-Za-z'\-]+),\s+(.*)$", re.S)


@cached("fomc.communications", ttl=TTL_DAILY)
def communications(kinds: Optional[str] = None, limit: int = 60) -> pd.DataFrame:
    """Speeches, testimony and press releases from the Board's own feeds.

    RSS is a *published* record, so this is what has been said rather than what
    is scheduled — the calendar side of Fed communication is the meeting dates
    in :func:`meetings`.
    """
    from .newsfeeds import entry_date, parse_feed  # local: newsfeeds imports nothing from here

    wanted = [k.strip().lower() for k in (kinds or "").replace(" ", ",").split(",") if k.strip()]
    unknown = [k for k in wanted if k not in FEEDS]
    if unknown:
        raise ValueError("Unknown kind(s) {}. Available: {}".format(
            ", ".join(unknown), ", ".join(sorted(FEEDS))))

    rows: List[Dict[str, Any]] = []
    errors: List[str] = []
    for kind in (wanted or list(FEEDS)):
        try:
            entries = parse_feed(FEEDS[kind], source=kind, limit=limit)
        except (ProviderError, EmptyDataError) as exc:
            errors.append("{}: {}".format(kind, exc))
            continue
        for entry in entries:
            title = str(entry.get("title") or "").strip()
            speaker = None
            if kind in ("speech", "testimony"):
                match = _SPEAKER.match(title)
                if match:
                    speaker, title = match.group(1), match.group(2).strip()
            rows.append({
                "date": entry_date(entry.get("published")),
                "kind": kind,
                "speaker": speaker,
                "title": title,
                "summary": str(entry.get("summary") or "").strip() or None,
                "url": entry.get("url"),
            })

    df = pd.DataFrame([r for r in rows if r["date"]])
    if df.empty:
        raise EmptyDataError(
            "The Board's feeds returned nothing{}".format(
                " (" + "; ".join(errors) + ")" if errors else ""))
    df = df.drop_duplicates(subset=["url"]).sort_values("date", ascending=False)
    df.attrs["errors"] = errors
    return df.reset_index(drop=True)
