"""The Fed's other signals: projections, statement language, sheet and feeds.

The parsing here is of documents written for people — a projections table with
the previous SEP folded into it, a statement whose vote and dissent are prose,
an RSS title that puts the speaker before a comma. So the fixtures below are
those shapes, and what is tested is the reading of them: that a memo header
does not become a variable, that the previous vintage attaches to the variable
above it rather than to itself, that a sentence diff does not split "Beth M.
Hammack" into two changes, and that a weekly balance sheet does not turn into a
mostly-empty daily one when a daily facility is read beside it.

The live tests at the bottom follow the rest of the suite: they hit the Fed and
FRED, and they anchor on facts that do not get revised — the size of the
facilities in March 2023, the shape of an SEP.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.extensions import fed_signals as fs
from backend.providers import fomc


# --------------------------------------------------------------------------- #
# Statement language
# --------------------------------------------------------------------------- #
STATEMENT = """
    <div id="article"><div class="col-xs-12 col-sm-8 col-md-8">
    <p>The Federal Open Market Committee approved the following statement for release by a
       9 &#8211; 3 vote:</p>
    <p>The Committee decided to maintain the target range for the federal funds rate at
       3-1/2 to 3-3/4 percent. Inflation remains elevated relative to the Committee's 2 percent
       goal, and policy will stay restrictive for some time.</p>
    <p>Voting against the monetary policy action were Beth M. Hammack, Neel Kashkari, and
       Lorie K. Logan, who preferred to raise the target range by 1/4 percentage point.</p>
    <p>For media inquiries, please email <a href="#">media</a> or call 202-452-2955.</p>
    </div></div>
"""


def test_the_body_of_a_release_is_read_and_the_furniture_is_not():
    lines = fomc.paragraphs(STATEMENT)
    assert len(lines) == 3
    assert lines[0].startswith("The Federal Open Market Committee approved")
    assert not any("media inquiries" in line.lower() for line in lines)
    assert "–" in lines[0]                                   # the entity survived unescaping


def test_the_vote_and_the_dissent_are_read_out_of_the_prose():
    parsed = fomc.parse_statement(STATEMENT)
    assert (parsed["votes_for"], parsed["votes_against"]) == (9, 3)
    assert parsed["unanimous"] is False
    assert parsed["dissent"].startswith("Voting against")
    assert "Hammack" in parsed["dissent"]


def test_language_flags_are_evidence_rather_than_a_score():
    hits = fs._phrase_hits(fomc.parse_statement(STATEMENT)["text"], fs.HAWKISH)
    phrases = {h["phrase"] for h in hits}
    assert "restrictive" in phrases and "inflation remains elevated" in phrases
    assert all(h["count"] >= 1 for h in hits)
    assert fs._phrase_hits("nothing to see here", fs.HAWKISH) == []


def test_a_name_with_initials_is_one_sentence_not_three():
    """The dissent paragraph is where a naive splitter falls over, and it is
    exactly the paragraph a diff has to get right."""
    sentences = fs._sentences(fomc.parse_statement(STATEMENT)["paragraphs"])
    dissent = [s for s in sentences if s.startswith("Voting against")]
    assert len(dissent) == 1
    assert dissent[0].endswith("1/4 percentage point.")


def test_the_diff_reports_what_the_committee_rewrote():
    before = ["Policy is restrictive.", "Job gains have been solid."]
    after = ["Policy is well positioned.", "Job gains have been solid."]
    changes = fs._diff(before, after)
    assert changes["added"] == ["Policy is well positioned."]
    assert changes["removed"] == ["Policy is restrictive."]
    assert fs._diff(before, before) == {"added": [], "removed": []}


# --------------------------------------------------------------------------- #
# The projections table
# --------------------------------------------------------------------------- #
SEP_TABLE = pd.DataFrame(
    [
        ["Change in real GDP", "2.2", "2.3", "2.0–2.3"],
        ["March projection", "2.4", "2.3", "2.1–2.4"],
        # A section header spans the table; pandas fills a colspan by repeating.
        ["Memo: Projected appropriate policy path", "Memo: Projected appropriate policy path",
         "Memo: Projected appropriate policy path", "Memo: Projected appropriate policy path"],
        ["Federal funds rate", "3.8", "3.6", "3.6–4.1"],
        ["March projection", "3.4", "3.1", "3.1–3.6"],
    ],
    columns=pd.MultiIndex.from_tuples([
        ("Variable", "Variable"), ("Median1", "2026"), ("Median1", "2027"),
        ("Central Tendency2", "2026"),
    ]),
)


def test_the_previous_projection_belongs_to_the_variable_above_it():
    sep = fomc._sep_table(SEP_TABLE)
    funds = sep[(sep["variable"] == "Federal funds rate") & (sep["measure"] == "median")]
    assert set(funds["vintage"]) == {"current", "previous"}
    current = funds[(funds["vintage"] == "current") & (funds["horizon"] == "2026")].iloc[0]
    previous = funds[(funds["vintage"] == "previous") & (funds["horizon"] == "2026")].iloc[0]
    assert (current["number"], previous["number"]) == (3.8, 3.4)
    assert previous["previous_meeting_month"] == "March"


def test_a_spanning_memo_header_is_not_a_projected_variable():
    sep = fomc._sep_table(SEP_TABLE)
    assert not any("Memo" in v for v in sep["variable"].unique())
    assert set(sep["variable"]) == {"Change in real GDP", "Federal funds rate"}


def test_the_trailing_footnote_digit_is_not_part_of_the_measure():
    sep = fomc._sep_table(SEP_TABLE)
    assert set(sep["measure"]) == {"median", "central_tendency"}


DOT_TABLE = pd.DataFrame({
    "Midpoint of target range or target level (Percent)": [4.125, 3.875, 3.625, 3.375],
    "2026": [5, 3, 8, 1],
    "2027": [2, 5, 2, 3],
})


def test_the_dot_plot_reads_as_participants_at_a_level():
    dots = fomc._dot_table(DOT_TABLE)
    assert set(dots.columns) == {"horizon", "rate", "participants"}
    assert dots[dots["horizon"] == "2026"]["participants"].sum() == 17
    # Levels nobody chose are absent rather than present as a zero.
    assert (dots["participants"] > 0).all()


# --------------------------------------------------------------------------- #
# Picking a meeting
# --------------------------------------------------------------------------- #
MEETINGS = [
    {"date": "2026-01-28", "statement_url": "a", "projections_url": None},
    {"date": "2026-03-18", "statement_url": "b", "projections_url": "p1"},
    {"date": "2026-06-17", "statement_url": "c", "projections_url": "p2"},
]


def test_the_latest_document_of_that_kind_wins_and_carries_its_predecessor():
    latest, earlier = fs._pick_meeting(MEETINGS, None, "projections_url")
    assert (latest["date"], earlier["date"]) == ("2026-06-17", "2026-03-18")
    # A meeting with no projections is skipped, not returned empty.
    latest, earlier = fs._pick_meeting(MEETINGS, "2026-05-01", "projections_url")
    assert latest["date"] == "2026-03-18" and earlier is None


def test_asking_before_the_first_one_is_an_empty_result_not_a_wrong_one():
    from backend.core.errors import EmptyDataError

    with pytest.raises(EmptyDataError):
        fs._pick_meeting(MEETINGS, "2025-01-01", "projections_url")


# --------------------------------------------------------------------------- #
# The balance sheet frame
# --------------------------------------------------------------------------- #
def test_a_daily_facility_does_not_turn_a_weekly_sheet_into_a_daily_one(monkeypatch):
    """Left alone, the daily column would add four empty rows per week — and a
    "13-week change" computed on that frame would be a 13-day change."""
    index = pd.to_datetime(["2026-08-05", "2026-08-06", "2026-08-12", "2026-08-13"])
    raw = pd.DataFrame({"WALCL": [6_700_000, None, 6_760_000, None],
                        "RRPONTSYD": [0.4, 0.5, 0.7, 0.6]}, index=index)
    monkeypatch.setattr(fs.fred, "series", lambda *a, **k: raw)

    frame = fs._sheet_frame(None, {"WALCL": "total_assets"}, {"RRPONTSYD": "reverse_repo"})
    assert len(frame) == 2                                    # the two Wednesdays
    # Millions on the wire, billions in the answer — every level in this menu
    # means the same thing.
    assert frame["total_assets"].tolist() == [6700.0, 6760.0]
    assert frame["reverse_repo"].tolist() == [0.4, 0.7]


# --------------------------------------------------------------------------- #
# Live
# --------------------------------------------------------------------------- #
def test_the_sep_comes_back_with_the_revision_against_the_last_one(auth_client):
    r = auth_client.get("/api/v1/economy/fed/projections")
    body = r.json()
    assert r.status_code == 200
    rows = {(row["variable"], row["horizon"]): row for row in body["results"]}
    assert any(v == "Federal funds rate" for v, _ in rows)
    assert any(h == "Longer run" for _, h in rows)             # the neutral-rate view
    funds = [row for (v, _), row in rows.items() if v == "Federal funds rate"]
    assert all("median" in row for row in funds)
    assert body["extra"]["meeting"] <= str(pd.Timestamp.today().date())


def test_the_dot_plot_is_nineteen_people_or_fewer_at_a_rate_each(auth_client):
    body = auth_client.get("/api/v1/economy/fed/dot_plot").json()
    assert all(row["participants"] >= 1 for row in body["results"])
    for horizon, info in body["extra"]["horizons"].items():
        assert 1 <= info["participants"] <= 19, horizon
        assert info["low"] <= info["median_dot"] <= info["high"]


def test_a_statement_carries_its_vote_its_language_and_its_edits(auth_client):
    body = auth_client.get("/api/v1/economy/fed/statement").json()
    statement = body["results"]
    assert statement["paragraphs"] and statement["text"]
    assert isinstance(statement["hawkish_phrases"], list)
    assert isinstance(statement["guidance_phrases"], list)
    if statement.get("compared_with"):
        assert statement["compared_with"] < statement["meeting"]
        assert "sentences_added" in statement and "sentences_removed" in statement


def test_communications_parse_the_speaker_out_of_the_title(auth_client):
    r = auth_client.get("/api/v1/economy/fed/communications",
                        params={"kind": "speech", "days": 400, "limit": 20})
    rows = r.json()["results"]
    assert r.status_code == 200
    assert all(row["kind"] == "speech" for row in rows)
    assert any(row["speaker"] for row in rows)
    # "Cook, Outlook for the U.S. Economy" — the surname is not left in the title.
    for row in rows:
        if row["speaker"]:
            assert not row["title"].startswith(row["speaker"] + ",")


def test_congressional_testimony_is_flagged_as_such(auth_client):
    r = auth_client.get("/api/v1/economy/fed/communications",
                        params={"kind": "testimony", "days": 730, "limit": 20})
    if r.status_code == 404:
        pytest.skip("no testimony published in the feed window")
    rows = r.json()["results"]
    assert rows and any(row["congressional"] for row in rows)


def test_the_balance_sheet_is_weekly_billions_with_a_pace(auth_client):
    body = auth_client.get("/api/v1/economy/fed/balance_sheet",
                           params={"start_date": "2015-01-01"}).json()
    rows, extra = body["results"], body["extra"]
    assert 300 < len(rows) < 900                     # weekly, not daily, over a decade
    assert 1_000 < extra["total_assets_bn"] < 20_000
    assert extra["treasuries_bn"] + extra["mbs_bn"] <= extra["total_assets_bn"]
    assert "change_13w" in rows[-1]
    assert extra["regime"] in ("shrinking — balance-sheet runoff", "expanding — asset purchases",
                               "roughly flat — reserve management")


def test_the_facilities_remember_march_2023(auth_client):
    """The Bank Term Funding Program is the cleanest test of this command: it
    did not exist, then it held nine figures, and none of that gets revised."""
    body = auth_client.get("/api/v1/economy/fed/liquidity",
                           params={"start_date": "2007-01-01"}).json()
    facilities = body["extra"]["facilities"]
    assert facilities["bank_term_funding"]["peak_bn"] > 100
    assert facilities["bank_term_funding"]["peak_date"] >= "2023-03-01"
    assert facilities["discount_window"]["peak_bn"] > 100
    assert isinstance(body["extra"]["elevated"], list)


def test_repricing_days_name_the_events_that_landed_on_them(auth_client):
    r = auth_client.get("/api/v1/economy/fed/data_reaction",
                        params={"days": 400, "min_move_bps": 8})
    body = r.json()
    assert r.status_code == 200
    assert all(abs(row["two_year_change_bps"]) >= 8 for row in body["results"])
    assert body["extra"]["with_a_known_event"] >= 1
    assert body["extra"]["event_sources"]
    # Biggest first by default.
    moves = [abs(row["two_year_change_bps"]) for row in body["results"]]
    assert moves == sorted(moves, reverse=True)


def test_fed_speeches_are_a_calendar_type_of_their_own(auth_client):
    types = {t["key"]: t for t in auth_client.get("/api/v1/calendar/event_types").json()["results"]}
    assert types["fedspeak"]["available"] is True

    r = auth_client.get("/api/v1/calendar/events",
                        params={"start_date": "2026-07-01", "end_date": "2026-07-31",
                                "types": "fedspeak", "min_importance": 1})
    if r.status_code == 404:
        pytest.skip("the Board's feeds no longer reach that window")
    rows = r.json()["results"]
    assert all(row["type"] == "fedspeak" for row in rows)


def test_the_minutes_land_on_the_calendar_three_weeks_after_the_meeting(auth_client):
    r = auth_client.get("/api/v1/calendar/events",
                        params={"start_date": "2023-08-01", "end_date": "2023-08-31",
                                "types": "fomc"})
    titles = [row["title"] for row in r.json()["results"]]
    assert "FOMC minutes" in titles          # the July 2023 account, released 16 August


def test_a_bad_kind_is_a_400_not_a_502(auth_client):
    assert auth_client.get("/api/v1/economy/fed/communications",
                           params={"kind": "gossip"}).status_code == 400
    assert auth_client.get("/api/v1/economy/fed/projections",
                           params={"measure": "vibes"}).status_code == 400
