"""The Fed's other levers: what it projects, what it says, and what it holds.

The target range is one signal and it moves eight times a year at most. Between
those meetings policy still moves — through the path officials pencil in, the
language the statement is written in, what the chair says to Congress, and the
size and composition of the balance sheet. This module is those signals, each
read from the Fed's own publications:

* **Projections and the dot plot** — the Summary of Economic Projections, four
  times a year, with each participant's assessment of the appropriate policy
  rate. The Fed publishes the dot plot as a *table of counts by rate level*, so
  the chart everyone screenshots is available as data, and the previous SEP
  rides along in the same table — which makes "the dots moved up" a subtraction
  rather than an impression.
* **The statement, and what changed in it** — forward guidance lives in the
  wording, so a statement is most informative against the last one. The diff is
  sentence-level; the language flags are an explicit phrase list, and every
  match is returned so the score can be checked by eye.
* **Everything said between meetings** — speeches, testimony and press releases
  from the Board's own feeds, with the speaker parsed out, congressional
  testimony and Jackson Hole flagged, and a monetary release that lands away
  from a scheduled meeting marked as what it is: the Fed saying something
  unscheduled.
* **The balance sheet** — the second instrument. Runoff pace against the level,
  Treasuries against MBS, reserves and the reverse repo facility.
* **Liquidity facilities** — the discount window, the Bank Term Funding
  Program, other credit extensions, central bank swaps and repo. Emergency
  lending is not announced as a number; it shows up as *usage*, which is why
  March 2023 is legible in these series and nowhere else.

**What is inferred rather than reported.** Nothing here scores tone with a
model, and nothing converts language into a probability. ``statement`` counts
phrases from a published list and shows them; ``data_reaction`` reports how the
2-year Treasury moved on a day and what was scheduled that day, and stops there
— the yield moved, and the event happened; the causal claim is the reader's.
"""
from __future__ import annotations

import difflib
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ..core.errors import EmptyDataError, MissingCredentialError, ProviderError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..providers import fomc, fred, govstats
from .fed import _policy_path, _moves

# --------------------------------------------------------------------------- #
# Policy language
# --------------------------------------------------------------------------- #
#: Phrases that have carried the direction of policy in FOMC statements. A
#: phrase list rather than a lexicon score: these are terms of art whose
#: meaning does not survive being averaged with ordinary sentiment words, and
#: every hit is returned so a reader can disagree with any one of them.
HAWKISH: Tuple[str, ...] = (
    "restrictive", "additional firming", "additional policy firming", "further tightening",
    "raise the target range", "increase the target range", "tighter financial conditions",
    "inflation remains elevated", "upside risks to inflation", "elevated inflation",
    "vigilant", "resolutely", "deliver price stability", "strongly committed",
    "sufficiently restrictive", "extent of additional",
)
DOVISH: Tuple[str, ...] = (
    "lower the target range", "reduce the target range", "cut", "easing",
    "downside risks to employment", "softened", "moderating", "inflation has eased",
    "labor market has cooled", "accommodative", "weakened", "slowed",
    "greater confidence", "moving sustainably toward",
)
#: Guidance wording — not direction, but how committed the committee is to one.
GUIDANCE: Tuple[str, ...] = (
    "data dependent", "data-dependent", "meeting by meeting", "carefully assess",
    "well positioned", "patient", "gradual", "in determining the extent",
    "for some time", "additional information", "totality of the incoming data",
)

#: Sentence break: a full stop or semicolon before a capital — but not after an
#: initial or an abbreviation, or the dissent paragraph splits into "Beth M." /
#: "Hammack, Neel Kashkari, and Lorie K." and the diff reads as three changes.
_SENTENCE = re.compile(r"(?<!\s[A-Z]\.)(?<!\.[A-Z]\.)(?<=[.;])\s+(?=[A-Z])")


def _phrase_hits(text: str, phrases: Tuple[str, ...]) -> List[Dict[str, Any]]:
    """Every listed phrase present, with how often — the evidence, not a score."""
    lowered = text.lower()
    return [{"phrase": p, "count": lowered.count(p)} for p in phrases if p in lowered]


def _sentences(paragraphs: List[str]) -> List[str]:
    out: List[str] = []
    for para in paragraphs:
        out.extend(s.strip() for s in _SENTENCE.split(para) if s.strip())
    return out


def _diff(previous: List[str], current: List[str]) -> Dict[str, List[str]]:
    """Sentences added and dropped between two statements.

    Sentence-level rather than word-level on purpose: the committee rewrites
    whole clauses, and a word diff of "will be" against "is" reads as a change
    in guidance when it is a change in tense.
    """
    matcher = difflib.SequenceMatcher(a=previous, b=current, autojunk=False)
    added, removed = [], []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(previous[i1:i2])
        if tag in ("replace", "insert"):
            added.extend(current[j1:j2])
    return {"added": added, "removed": removed}


# --------------------------------------------------------------------------- #
# Meeting lookup
# --------------------------------------------------------------------------- #
def _meetings() -> List[Dict[str, Any]]:
    return fomc.meetings().to_dict("records")


def _pick_meeting(rows: List[Dict[str, Any]], when: Optional[str],
                  needs: str) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """The meeting on or before ``when`` that published ``needs``, and the one
    before it — which is what any "what changed" question is asked against."""
    have = [r for r in rows if r.get(needs)]
    if not have:
        raise EmptyDataError(
            "No meeting on the Fed's published calendar has {} yet".format(needs.replace("_", " ")))
    if when:
        target = str(pd.Timestamp(when).date())
        have = [r for r in have if r["date"] <= target]
        if not have:
            raise EmptyDataError("No meeting with {} on or before {}".format(
                needs.replace("_", " "), when))
    return have[-1], (have[-2] if len(have) > 1 else None)


# --------------------------------------------------------------------------- #
# Projections
# --------------------------------------------------------------------------- #
@command("/economy/fed/projections", providers=("federalreserve",),
         summary="Summary of Economic Projections, against the previous one")
def projections(meeting: Optional[str] = None, variable: Optional[str] = None,
                measure: str = "median", provider: Optional[str] = None) -> Result:
    """The SEP: GDP, unemployment, inflation and the policy rate, by year.

    Four meetings a year publish projections. Each row carries the current
    median and the one from the previous SEP, so ``change`` is the revision —
    which is the part that moves markets, especially on the policy-rate rows and
    on the ``Longer run`` horizon, the committee's own estimate of neutral.

    ``measure``: ``median``, ``central_tendency``, ``range`` or ``all``.
    ``meeting`` picks the SEP by date; omitted, it is the most recent one.
    """
    resolve_provider(provider, ("federalreserve",))
    wanted = measure.strip().lower()
    if wanted not in ("median", "central_tendency", "range", "all"):
        raise ValueError("measure must be median, central_tendency, range or all")

    chosen, _ = _pick_meeting(_meetings(), meeting, "projections_url")
    sep = fomc.projections(chosen["projections_url"])["sep"]
    if sep.empty:
        raise EmptyDataError("No projection table published for {}".format(chosen["date"]))

    if variable:
        needle = variable.strip().lower()
        sep = sep[sep["variable"].str.lower().str.contains(needle)]
        if sep.empty:
            raise ValueError("No projected variable matching {!r}".format(variable))

    rows: List[Dict[str, Any]] = []
    previous_month = None
    for (name, horizon), group in sep.groupby(["variable", "horizon"], sort=False):
        row: Dict[str, Any] = {"variable": name, "horizon": horizon}
        for _, entry in group.iterrows():
            key = entry["measure"] if entry["vintage"] == "current" else "previous_" + entry["measure"]
            row[key] = entry["value"]
            if entry["vintage"] == "previous":
                previous_month = previous_month or entry["previous_meeting_month"]
        current, prior = _number(row.get("median")), _number(row.get("previous_median"))
        row["change"] = None if current is None or prior is None else round(current - prior, 3)
        rows.append(row)

    if wanted != "all":
        keep = ("variable", "horizon", wanted, "previous_" + wanted, "change")
        rows = [{k: v for k, v in row.items() if k in keep} for row in rows]

    return Result(rows, provider="federalreserve", extra={
        "meeting": chosen["date"], "previous_projection": previous_month,
        "url": chosen["projections_url"], "measure": wanted,
        "source": "federalreserve.gov"})


def _number(value: Any) -> Optional[float]:
    number = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(number) else float(number)


@command("/economy/fed/dot_plot", providers=("federalreserve",),
         summary="Where every participant put their dot, and how the path shifted")
def dot_plot(meeting: Optional[str] = None, provider: Optional[str] = None) -> Result:
    """The dot plot as data: participants at each rate level, by horizon.

    One row per level per horizon, with how many of the nineteen participants
    chose it. The median dot is computed from the distribution itself and
    reported in ``extra`` beside the SEP's stated median, along with the shift
    against the previous projection — "the 2027 dot moved up 50bp" is the
    sentence this command exists to make checkable.
    """
    resolve_provider(provider, ("federalreserve",))
    chosen, _ = _pick_meeting(_meetings(), meeting, "projections_url")
    materials = fomc.projections(chosen["projections_url"])
    dots, sep = materials["dots"], materials["sep"]
    if dots.empty:
        raise EmptyDataError("No dot-plot table published for {}".format(chosen["date"]))

    rows = dots.to_dict("records")
    horizons: Dict[str, Any] = {}
    for horizon, group in dots.groupby("horizon", sort=False):
        spread = [r["rate"] for _, r in group.iterrows() for _ in range(int(r["participants"]))]
        stated = sep[(sep["variable"].str.contains("Federal funds", case=False)) &
                     (sep["horizon"] == horizon) & (sep["measure"] == "median")]
        current = stated[stated["vintage"] == "current"]["number"]
        prior = stated[stated["vintage"] == "previous"]["number"]
        horizons[horizon] = {
            "participants": int(group["participants"].sum()),
            "median_dot": round(float(pd.Series(spread).median()), 3),
            "sep_median": None if current.empty else float(current.iloc[0]),
            "previous_median": None if prior.empty else float(prior.iloc[0]),
            "low": float(group["rate"].min()), "high": float(group["rate"].max()),
        }
        shift = horizons[horizon]
        if shift["sep_median"] is not None and shift["previous_median"] is not None:
            shift["change"] = round(shift["sep_median"] - shift["previous_median"], 3)

    return Result(rows, provider="federalreserve", extra={
        "meeting": chosen["date"], "horizons": horizons,
        "url": chosen["projections_url"], "source": "federalreserve.gov"})


# --------------------------------------------------------------------------- #
# The statement
# --------------------------------------------------------------------------- #
@command("/economy/fed/statement", providers=("federalreserve",),
         summary="The FOMC statement, its vote and dissents, and what changed in it")
def statement(meeting: Optional[str] = None, compare: bool = True,
              provider: Optional[str] = None) -> Result:
    """One statement, read the way the desk reads it.

    Four things come back: the text; the **vote** and any **dissent**, which is
    the committee saying how close the decision was; the **language flags** —
    which phrases from the hawkish, dovish and guidance lists appear, listed
    individually rather than rolled into a number; and, with ``compare``, the
    **sentences added and dropped** against the previous statement. Guidance
    changes are edits: "restrictive" leaving the statement is the change.
    """
    resolve_provider(provider, ("federalreserve",))
    chosen, earlier = _pick_meeting(_meetings(), meeting, "statement_url")
    parsed = fomc.document(chosen["statement_url"])

    warnings: List[str] = []
    hawkish = _phrase_hits(parsed["text"], HAWKISH)
    dovish = _phrase_hits(parsed["text"], DOVISH)
    payload: Dict[str, Any] = {
        "meeting": chosen["date"],
        "url": chosen["statement_url"],
        "votes_for": parsed["votes_for"],
        "votes_against": parsed["votes_against"],
        "unanimous": parsed["unanimous"],
        "dissent": parsed["dissent"],
        "hawkish_phrases": hawkish,
        "dovish_phrases": dovish,
        "guidance_phrases": _phrase_hits(parsed["text"], GUIDANCE),
        "language_balance": sum(h["count"] for h in hawkish) - sum(d["count"] for d in dovish),
        "paragraphs": parsed["paragraphs"],
        "text": parsed["text"],
    }

    if compare and earlier:
        try:
            before = fomc.document(earlier["statement_url"])
            changes = _diff(_sentences(before["paragraphs"]), _sentences(parsed["paragraphs"]))
            payload.update({
                "compared_with": earlier["date"],
                "sentences_added": changes["added"],
                "sentences_removed": changes["removed"],
                "unchanged": not changes["added"] and not changes["removed"],
            })
        except (EmptyDataError, ProviderError) as exc:
            warnings.append("The previous statement did not load, so no diff: {}".format(exc))
    elif compare:
        warnings.append("No earlier statement on the published calendar to compare against.")

    return Result(payload, provider="federalreserve", warnings=warnings,
                  extra={"source": "federalreserve.gov"})


# --------------------------------------------------------------------------- #
# Communications
# --------------------------------------------------------------------------- #
_JACKSON_HOLE = re.compile(r"(?i)jackson hole|economic policy symposium")
_TESTIMONY_TO_CONGRESS = re.compile(r"(?i)semiannual monetary policy report|before the committee|"
                                    r"u\.s\. (house|senate)|congress")
_STATEMENT_TITLE = re.compile(r"(?i)fomc statement")
_MINUTES_TITLE = re.compile(r"(?i)minutes of the federal open market committee")


@command("/economy/fed/communications", providers=("federalreserve",),
         summary="Speeches, testimony and press releases from the Board's own feeds")
def communications(kind: str = "all", speaker: Optional[str] = None,
                   query: Optional[str] = None, days: int = 120, limit: int = 60,
                   provider: Optional[str] = None) -> Result:
    """What the Fed has said, newest first, with the speaker parsed out.

    ``kind``: ``speech``, ``testimony``, ``monetary`` (policy press releases —
    statements, minutes, facility announcements), ``banking`` (supervision and
    financial-stability actions) or ``other``; ``all`` reads every feed.
    ``speaker`` matches a surname — the chair, a governor or a regional
    president — and ``query`` matches the title and summary.

    Three flags do the work a reader would otherwise do by eye:
    ``congressional`` marks testimony to the House or Senate, including the
    semiannual monetary policy report; ``jackson_hole`` marks the Kansas City
    Fed's symposium; and ``off_calendar`` marks a monetary press release issued
    on a day with no scheduled meeting — an intermeeting statement, which is
    how an emergency move is announced.

    These are RSS feeds, so they carry what has been published: recent months,
    not an archive, and nothing scheduled ahead. Meeting dates are in
    ``/economy/fed/meetings``.
    """
    resolve_provider(provider, ("federalreserve",))
    wanted = kind.strip().lower()
    if wanted not in ("all",) + tuple(fomc.FEEDS):
        raise ValueError("kind must be all, {}".format(", ".join(sorted(fomc.FEEDS))))

    frame = fomc.communications(None if wanted == "all" else wanted, limit=max(20, int(limit)))
    warnings = list(frame.attrs.get("errors") or [])
    rows = frame.to_dict("records")

    if days:
        floor = str(date.today() - timedelta(days=max(1, int(days))))
        rows = [r for r in rows if r["date"] >= floor]
    if speaker:
        needle = speaker.strip().lower()
        rows = [r for r in rows if needle in str(r.get("speaker") or "").lower()]
    if query:
        needle = query.strip().lower()
        rows = [r for r in rows
                if needle in (r["title"] or "").lower() or needle in (r["summary"] or "").lower()]
    if not rows:
        raise EmptyDataError("No Fed communications match that filter in the last {} days".format(days))

    meeting_dates = set()
    try:
        meeting_dates = {m["date"] for m in _meetings()}
    except (EmptyDataError, ProviderError) as exc:
        warnings.append("Meeting dates unavailable, so nothing is flagged off-calendar: {}".format(exc))

    for row in rows:
        text = " ".join(filter(None, (row["title"], row["summary"])))
        is_statement = row["kind"] == "monetary" and bool(_STATEMENT_TITLE.search(text))
        row.update({
            "congressional": row["kind"] == "testimony" and bool(_TESTIMONY_TO_CONGRESS.search(text)),
            "jackson_hole": bool(_JACKSON_HOLE.search(text)),
            "document": ("statement" if is_statement
                         else "minutes" if _MINUTES_TITLE.search(text) else None),
            # An FOMC statement dated away from a scheduled meeting is the
            # committee acting between meetings — the thing worth catching.
            "off_calendar": bool(is_statement and meeting_dates and row["date"] not in meeting_dates),
        })

    rows = rows[:max(1, int(limit))]
    speakers = sorted({r["speaker"] for r in rows if r.get("speaker")})
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    return Result(rows, provider="federalreserve", warnings=warnings, extra={
        "total": len(rows), "speakers": speakers, "by_kind": counts,
        "window_days": int(days), "source": "federalreserve.gov"})


# --------------------------------------------------------------------------- #
# The balance sheet
# --------------------------------------------------------------------------- #
#: H.4.1 series, all weekly and all in millions of dollars except the two repo
#: facilities, which FRED publishes daily in billions.
_SHEET = {
    "WALCL": "total_assets", "TREAST": "treasuries", "WSHOMCB": "mbs",
    "WSHOSHO": "securities_held_outright", "WRESBAL": "reserves",
}
_BILLIONS = {"RRPONTSYD": "reverse_repo", "RPONTSYD": "repo"}


def _sheet_frame(start_date: Optional[str], series: Dict[str, str],
                 billions: Dict[str, str]) -> pd.DataFrame:
    """The named H.4.1 series as one weekly frame, everything in $bn.

    The balance sheet is published every Wednesday; the repo facilities are
    published daily. Left as they arrive, the daily columns would turn a weekly
    frame into a mostly-empty daily one — and a "13-week change" computed on it
    would be a 13-*day* change. So the weekly release sets the index and the
    daily series are read on those same Wednesdays.
    """
    raw = fred.series(",".join(list(series) + list(billions)), start_date)
    out = pd.DataFrame(index=raw.index)
    for code, name in series.items():
        if code in raw.columns:
            out[name] = (pd.to_numeric(raw[code], errors="coerce") / 1000).round(1)
    for code, name in billions.items():
        if code in raw.columns:
            out[name] = pd.to_numeric(raw[code], errors="coerce").round(1)
    weekly = [name for code, name in series.items() if name in out.columns]
    if weekly:
        out = out.dropna(subset=weekly, how="all")
    out.index.name = "date"
    return out.dropna(how="all")


@command("/economy/fed/balance_sheet", providers=("fred",),
         summary="Balance-sheet size, composition and the pace it is changing")
def balance_sheet(start_date: Optional[str] = None, provider: Optional[str] = None) -> Result:
    """The second policy instrument, in $ billions and weekly.

    Total assets with the composition underneath them — Treasuries, MBS,
    reserves, the reverse repo facility — plus the *pace*: change over 4, 13 and
    52 weeks. Quantitative tightening is a runoff rate, not an announcement, so
    the pace column is the policy: a monthly cap raised or lowered shows up here
    weeks before anyone calls it a change.

    ``extra`` names the regime from the 13-week change (shrinking, expanding or
    steady) and the monthly runoff it implies.
    """
    resolve_provider(provider, ("fred",))
    frame = _sheet_frame(start_date or "2003-01-01", _SHEET, {"RRPONTSYD": "reverse_repo"})
    if frame.empty or "total_assets" not in frame.columns:
        raise EmptyDataError("FRED returned no balance-sheet observations for that window")

    assets = frame["total_assets"]
    for weeks in (4, 13, 52):
        frame["change_{}w".format(weeks)] = (assets - assets.shift(weeks)).round(1)

    quarterly = frame["change_13w"].dropna()
    latest_change = float(quarterly.iloc[-1]) if not quarterly.empty else 0.0
    monthly = round(latest_change / 3, 1)
    # A portfolio this size drifts by a few billion a month just from currency
    # in circulation, so the neutral band is wide enough that ordinary reserve
    # management does not get labelled as a policy decision either way.
    regime = ("shrinking — balance-sheet runoff" if monthly < -20
              else "expanding — asset purchases" if monthly > 20
              else "roughly flat — reserve management")

    latest = frame.iloc[-1]
    return Result(frame, provider="fred", index_name="date", extra={
        "as_of": str(frame.index[-1].date()),
        "total_assets_bn": _cell(latest, "total_assets"),
        "treasuries_bn": _cell(latest, "treasuries"),
        "mbs_bn": _cell(latest, "mbs"),
        "reserves_bn": _cell(latest, "reserves"),
        "reverse_repo_bn": _cell(latest, "reverse_repo"),
        "change_13w_bn": round(latest_change, 1),
        "monthly_pace_bn": monthly, "regime": regime,
        "peak_assets_bn": round(float(assets.max()), 1),
        "off_peak_bn": round(float(assets.iloc[-1] - assets.max()), 1),
        "units": "$ billions", "source": "FRED (H.4.1)"})


def _cell(row: pd.Series, name: str) -> Optional[float]:
    if name not in row.index or pd.isna(row[name]):
        return None
    return round(float(row[name]), 1)


# --------------------------------------------------------------------------- #
# Liquidity facilities
# --------------------------------------------------------------------------- #
#: What the Fed lends through when something breaks. Each is a standing series,
#: near zero in calm weeks — which is what makes a spike readable.
_FACILITIES = {
    "WLCFLPCL": "discount_window",
    "H41RESPPALDKNWW": "bank_term_funding",
    "WLCFOCEL": "other_credit_extensions",
    "SWPT": "central_bank_swaps",
}


@command("/economy/fed/liquidity", providers=("fred",),
         summary="Emergency lending: the facilities, and when they were used")
def liquidity(start_date: Optional[str] = None, provider: Optional[str] = None) -> Result:
    """Facility usage in $ billions, weekly — the Fed's response to stress.

    An emergency action is announced in a press release and *measured* here.
    The discount window is always open and normally near-empty; the Bank Term
    Funding Program was created over one weekend in March 2023 and shows up in
    the following Wednesday's H.4.1; ``other_credit_extensions`` is the bridge
    banks. Central bank swaps are the same instrument pointed at dollar funding
    abroad.

    ``extra`` flags each facility against its own history: ``elevated`` means
    above the 95th percentile of the window, which is what "the window is being
    used" looks like when the baseline is a rounding error.
    """
    resolve_provider(provider, ("fred",))
    frame = _sheet_frame(start_date or "2003-01-01", _FACILITIES, {"RPONTSYD": "repo"})
    if frame.empty:
        raise EmptyDataError("FRED returned no facility observations for that window")

    latest = frame.iloc[-1]
    status: Dict[str, Any] = {}
    for name in frame.columns:
        series = frame[name].dropna()
        if series.empty:
            continue
        value = _cell(latest, name)
        threshold = float(series.quantile(0.95))
        status[name] = {
            "latest_bn": value,
            "peak_bn": round(float(series.max()), 1),
            "peak_date": str(series.idxmax().date()),
            "elevated": bool(value is not None and value > max(threshold, 1.0)),
        }
    stressed = [name for name, info in status.items() if info["elevated"]]
    return Result(frame, provider="fred", index_name="date", extra={
        "as_of": str(frame.index[-1].date()), "facilities": status,
        "elevated": stressed,
        "reading": "elevated usage: " + ", ".join(stressed) if stressed
                   else "no facility above its 95th percentile — no visible stress",
        "units": "$ billions", "source": "FRED (H.4.1)"})


# --------------------------------------------------------------------------- #
# How the expected path repriced
# --------------------------------------------------------------------------- #
@command("/economy/fed/data_reaction", providers=("fred",),
         summary="Days the expected policy path moved, and what landed on them")
def data_reaction(days: int = 180, min_move_bps: float = 5, sort: str = "move",
                  provider: Optional[str] = None) -> Result:
    """The 2-year Treasury's daily move, next to that day's scheduled events.

    A CPI print is not a Fed action, but it is where the *expected* path gets
    rewritten, and the 2-year is where that shows: it is the market's average
    policy rate over the next two years. So this lists the days it moved by more
    than ``min_move_bps`` and names what happened on them — an FOMC decision,
    the minutes, testimony, a speech, a BEA release.

    **What dates this can see.** The Fed's own calendar and feeds, and the BEA's
    (which covers PCE and GDP). The BLS blocks automated readers, so CPI and the
    jobs report have no key-free schedule — set a free ``MFT_FRED_API_KEY`` and
    the official US release calendar fills them in. Days with no known event are
    still listed, marked ``none``: a blank means "nothing this platform can
    date", not "nothing happened".
    """
    resolve_provider(provider, ("fred",))
    if sort.strip().lower() not in ("move", "date"):
        raise ValueError("sort must be move or date")
    window = max(5, min(int(days), 3650))
    first = str(date.today() - timedelta(days=window))

    yields = fred.series("DGS2,DGS10", first)
    two_year = pd.to_numeric(yields.get("DGS2"), errors="coerce").dropna()
    if two_year.empty:
        raise EmptyDataError("FRED returned no 2-year Treasury observations for that window")
    ten_year = pd.to_numeric(yields.get("DGS10"), errors="coerce") if "DGS10" in yields else None

    events, warnings, sources = _event_index(first)
    path = _policy_path(first)

    rows: List[Dict[str, Any]] = []
    changes = (two_year.diff() * 100).round(1)
    for stamp, change in changes.dropna().items():
        if abs(float(change)) < float(min_move_bps):
            continue
        day = str(stamp.date())
        level = path["target_midpoint"].asof(stamp) if not path.empty else None
        rows.append({
            "date": day,
            "two_year_change_bps": float(change),
            "two_year": round(float(two_year.loc[stamp]), 4),
            "ten_year_change_bps": (None if ten_year is None or stamp not in ten_year.index
                                    else _diff_bps(ten_year, stamp)),
            "target_midpoint": None if level is None or pd.isna(level) else round(float(level), 4),
            "events": "; ".join(e["label"] for e in events.get(day, [])) or None,
            "event_kinds": ", ".join(sorted({e["kind"] for e in events.get(day, [])})) or "none",
        })
    if not rows:
        raise EmptyDataError(
            "The 2-year moved less than {}bp on every day of the last {} days".format(
                min_move_bps, window))

    if sort.strip().lower() == "move":
        rows.sort(key=lambda r: abs(r["two_year_change_bps"]), reverse=True)
    else:
        rows.reverse()
    explained = sum(1 for r in rows if r["event_kinds"] != "none")
    return Result(rows, provider="fred", warnings=warnings, extra={
        "days": window, "moves": len(rows), "with_a_known_event": explained,
        "event_sources": sources,
        "note": "The yield moved and the event happened; the link between them is not "
                "asserted here.",
        "source": "FRED"})


def _diff_bps(series: pd.Series, stamp: Any) -> Optional[float]:
    position = series.index.get_loc(stamp)
    if not isinstance(position, int) or position == 0:
        return None
    change = (series.iloc[position] - series.iloc[position - 1]) * 100
    return None if pd.isna(change) else round(float(change), 1)


def _event_index(first: str) -> Tuple[Dict[str, List[Dict[str, str]]], List[str], List[str]]:
    """Every dated event this platform can put on a day, keyed by date."""
    index: Dict[str, List[Dict[str, str]]] = {}
    warnings: List[str] = []
    sources: List[str] = []

    def add(day: Optional[str], kind: str, label: str) -> None:
        if day and day >= first:
            index.setdefault(day, []).append({"kind": kind, "label": label})

    try:
        moves = {m["date"]: m for m in _moves(_policy_path()).to_dict("records")}
        for meeting in _meetings():
            add(meeting["date"], "fomc", "FOMC decision")
            add(meeting.get("minutes_released"), "minutes", "FOMC minutes")
        for day, move in moves.items():
            add(day, "fomc", "target {} {:g}bp".format(
                "raised" if move["change_bps"] > 0 else "cut", abs(move["change_bps"])))
        sources.append("federalreserve.gov (meetings, decisions, minutes)")
    except (EmptyDataError, ProviderError) as exc:
        warnings.append("Fed meeting dates unavailable: {}".format(exc))

    try:
        for row in fomc.communications(limit=200).to_dict("records"):
            label = "{}{}".format(row["speaker"] + ": " if row["speaker"] else "", row["title"])
            add(row["date"], row["kind"], label[:120])
        sources.append("federalreserve.gov (speeches, testimony, releases)")
    except (EmptyDataError, ProviderError) as exc:
        warnings.append("Fed communications unavailable: {}".format(exc))

    try:
        for row in govstats.bea_releases().to_dict("records"):
            add(row["date"], "data", row["release"])
        sources.append("bea.gov (PCE, GDP, trade)")
    except (EmptyDataError, ProviderError) as exc:
        warnings.append("BEA releases unavailable: {}".format(exc))

    try:
        schedule = fred.release_dates(first, str(date.today()), 1000)
        for row in schedule.to_dict("records"):
            add(str(row.get("date")), "data", str(row.get("release_name") or "US release"))
        sources.append("FRED release calendar (key configured)")
    except MissingCredentialError:
        warnings.append(
            "CPI and the jobs report have no key-free release schedule — the BLS blocks "
            "automated readers. Set MFT_FRED_API_KEY (free) to fill in the official US "
            "release calendar.")
    except (EmptyDataError, ProviderError) as exc:
        warnings.append("FRED release calendar unavailable: {}".format(exc))

    return index, warnings, sources
