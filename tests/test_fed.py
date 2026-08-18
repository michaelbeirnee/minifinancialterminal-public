"""Fed policy: the splice, the decisions, and the cycles they group into.

Almost everything that can go wrong here is arithmetic on a step function, so
the maths is tested against frames written out below rather than against FRED:
that the two target eras join into one continuous series, that a change is only
a change when the level actually moves, that a run of hikes with a year-long
gap in the middle is still one cycle, and that a meeting is only credited with
the decision that follows it a day later.

The Fed's own calendar page is HTML written for humans, so its parser gets the
awkward rows — a meeting that straddles two months, the asterisk that marks the
dot plot, a one-day notation vote — as a fixture.

The network-backed tests at the bottom follow the rest of the suite and hit the
live series, checking it against history that is not going to be revised.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.extensions import calendar as cal
from backend.extensions import fed
from backend.providers import fomc


# --------------------------------------------------------------------------- #
# Splicing the two target eras
# --------------------------------------------------------------------------- #
def _frame(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows).set_index("date")
    df.index = pd.to_datetime(df.index)
    return df


ACROSS_2008 = _frame([
    # The single target, then the range that replaced it in December 2008.
    {"date": "2008-10-29", "DFEDTAR": 1.00, "DFEDTARU": None, "DFEDTARL": None, "DFF": 0.36},
    {"date": "2008-12-15", "DFEDTAR": 1.00, "DFEDTARU": None, "DFEDTARL": None, "DFF": 0.13},
    {"date": "2008-12-16", "DFEDTAR": None, "DFEDTARU": 0.25, "DFEDTARL": 0.00, "DFF": 0.17},
    {"date": "2008-12-17", "DFEDTAR": None, "DFEDTARU": 0.25, "DFEDTARL": 0.00, "DFF": 0.14},
])


def test_the_two_target_eras_join_into_one_continuous_series():
    path = fed._splice(ACROSS_2008)
    assert list(path.columns) == ["target_lower", "target_upper", "target_midpoint",
                                  "effective_rate"]
    # Before the range existed the single target is its own upper *and* lower.
    assert path.loc["2008-10-29", "target_upper"] == 1.00
    assert path.loc["2008-10-29", "target_lower"] == 1.00
    assert path.loc["2008-12-16", "target_lower"] == 0.00
    assert path.loc["2008-12-16", "target_midpoint"] == 0.125
    assert not path["target_upper"].isna().any()


def test_a_series_missing_from_the_window_is_not_a_missing_column():
    """FRED omits a series with no rows in the window instead of returning it
    empty, so a modern-only window has no ``DFEDTAR`` column at all."""
    modern = ACROSS_2008.drop(columns=["DFEDTAR"]).iloc[2:]
    path = fed._splice(modern)
    assert path["target_upper"].tolist() == [0.25, 0.25]


def test_the_december_2008_move_reads_as_the_cut_it_was_called():
    """Announcements are written in the upper bound: 1.00% to "0 to 0.25%" was
    reported as 75bp, which is what measuring on midpoints would not give."""
    moves = fed._moves(fed._splice(ACROSS_2008))
    assert moves["date"].tolist() == ["2008-12-16"]
    assert moves.iloc[0]["change_bps"] == -75.0
    assert moves.iloc[0]["direction"] == "cut"


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #
HIKES = _frame([
    {"date": "2022-03-16", "DFEDTARU": 0.25, "DFEDTARL": 0.00, "DFF": 0.08},
    {"date": "2022-03-17", "DFEDTARU": 0.50, "DFEDTARL": 0.25, "DFF": 0.33},
    {"date": "2022-03-18", "DFEDTARU": 0.50, "DFEDTARL": 0.25, "DFF": 0.33},
    {"date": "2022-05-05", "DFEDTARU": 1.00, "DFEDTARL": 0.75, "DFF": 0.83},
    {"date": "2022-06-16", "DFEDTARU": 1.75, "DFEDTARL": 1.50, "DFF": 1.58},
])


def test_only_a_day_the_target_actually_moved_is_a_decision():
    moves = fed._moves(fed._splice(HIKES))
    assert moves["date"].tolist() == ["2022-03-17", "2022-05-05", "2022-06-16"]
    assert moves["change_bps"].tolist() == [25.0, 50.0, 75.0]
    assert set(moves["direction"]) == {"hike"}


def test_the_first_observation_can_never_be_a_change():
    """There is nothing before it to compare against — which is why the
    commands read the whole history and filter afterwards."""
    assert fed._moves(fed._splice(HIKES.iloc[1:])).iloc[0]["date"] == "2022-05-05"


def test_a_flat_series_produces_no_decisions_rather_than_an_error():
    flat = fed._moves(fed._splice(HIKES.iloc[2:3]))
    assert flat.empty


# --------------------------------------------------------------------------- #
# Cycles
# --------------------------------------------------------------------------- #
MIXED = pd.DataFrame([
    # A tightening run with a twelve-month gap inside it — still one cycle.
    {"date": "2015-12-17", "direction": "hike", "change_bps": 25.0,
     "previous_upper": 0.25, "target_upper": 0.50},
    {"date": "2016-12-15", "direction": "hike", "change_bps": 25.0,
     "previous_upper": 0.50, "target_upper": 0.75},
    {"date": "2017-03-16", "direction": "hike", "change_bps": 25.0,
     "previous_upper": 0.75, "target_upper": 1.00},
    # The reversal opens the next one.
    {"date": "2019-08-01", "direction": "cut", "change_bps": -25.0,
     "previous_upper": 1.00, "target_upper": 0.75},
])


def test_a_long_hold_inside_a_run_does_not_split_the_cycle():
    cycles = fed._cycles(MIXED, as_of="2020-01-01")
    assert len(cycles) == 2
    first = cycles[0]
    assert (first["kind"], first["moves"], first["total_bps"]) == ("tightening", 3, 75.0)
    assert first["start_date"] == "2015-12-17" and first["end_date"] == "2017-03-16"
    assert (first["from_rate"], first["to_rate"]) == (0.25, 1.00)


def test_hold_days_measure_the_wait_for_the_reversal():
    """Last hike to first cut — the number most questions about a hiking cycle
    are actually asking."""
    cycles = fed._cycles(MIXED, as_of="2020-01-01")
    assert cycles[0]["hold_days"] == (pd.Timestamp("2019-08-01") - pd.Timestamp("2017-03-16")).days
    assert cycles[0]["status"] == "complete"
    # The open cycle's hold runs to today rather than to a reversal that has
    # not happened.
    assert cycles[1]["status"] == "current"
    assert cycles[1]["hold_days"] == (pd.Timestamp("2020-01-01") - pd.Timestamp("2019-08-01")).days


def test_a_cycle_is_labelled_the_way_it_is_written_about():
    assert fed._label("tightening", "2022-03-17", "2023-07-27") == "2022-2023 tightening"
    assert fed._label("easing", "2020-03-03", "2020-03-16") == "2020 easing"


def test_no_moves_means_no_cycles():
    assert fed._cycles(pd.DataFrame()) == []


# --------------------------------------------------------------------------- #
# Meetings joined to decisions
# --------------------------------------------------------------------------- #
def test_a_decision_is_credited_to_the_meeting_it_followed():
    """The target moves the morning after the statement, so the search runs
    forward from the meeting — but not so far that the next month's move gets
    filed under this month's meeting."""
    by_date = {"2022-03-17": {"direction": "hike", "change_bps": 25.0}}
    assert fed._decision_for(by_date, "2022-03-16")["change_bps"] == 25.0
    assert fed._decision_for(by_date, "2022-03-17")["change_bps"] == 25.0
    assert fed._decision_for(by_date, "2022-02-25") is None


def test_a_meeting_that_held_still_reports_the_rate_in_force():
    path = fed._splice(HIKES)
    assert fed._level_at(path, "2022-04-10") == {"target_lower": 0.25, "target_upper": 0.50}
    assert fed._level_at(pd.DataFrame(), "2022-04-10") == {}


# --------------------------------------------------------------------------- #
# Thinning a long path for a chart
# --------------------------------------------------------------------------- #
def test_weekly_thinning_keeps_the_latest_rate_on_its_real_date():
    path = fed._splice(HIKES)
    weekly = fed._resample(path, "w")
    assert len(weekly) < len(path)
    # The week in progress ends in the future; it is dated by the last real
    # observation rather than dropped.
    assert str(weekly.index[-1].date()) == "2022-06-16"
    assert weekly["target_upper"].iloc[-1] == 1.75


def test_an_unknown_frequency_is_the_callers_mistake():
    with pytest.raises(ValueError):
        fed._resample(fed._splice(HIKES), "fortnightly")


def test_a_cycle_that_predates_a_fund_is_empty_rather_than_truncated():
    closes = pd.Series([10.0, 11.0, 12.0],
                       index=pd.to_datetime(["2005-01-03", "2005-06-01", "2006-06-29"]))
    assert fed._window_return(closes, "2004-06-30", "2006-06-29") is None
    assert fed._window_return(closes, "2005-01-03", "2006-06-29") == 20.0


# --------------------------------------------------------------------------- #
# The Fed's calendar page
# --------------------------------------------------------------------------- #
CALENDAR_HTML = """
<div class="panel panel-default"><div class="panel-heading"><h4><a id="1">2024 FOMC Meetings</a></h4></div>
  <div class="row fomc-meeting">
    <div class="fomc-meeting__month col-md-2"><strong>March</strong></div>
    <div class="fomc-meeting__date col-lg-1">19-20*</div>
    <div class="col-lg-2"><a href="/newsevents/pressreleases/monetary20240320a.htm">HTML</a></div>
    <div class="col-lg-3"><a href="/monetarypolicy/fomcpresconf20240320.htm">Press Conference</a></div>
    <div class="fomc-meeting__minutes"><a href="/monetarypolicy/fomcminutes20240320.htm">HTML</a>
      <br> (Released April 10, 2024)</div>
  </div>
  <div class="row fomc-meeting">
    <div class="fomc-meeting__month col-md-2"><strong>Apr/May</strong></div>
    <div class="fomc-meeting__date col-lg-1">30-1</div>
    <div class="col-lg-2"><a href="/newsevents/pressreleases/monetary20240501a.htm">HTML</a></div>
  </div>
</div>
<div class="panel panel-default"><div class="panel-heading"><h4><a id="2">2025 FOMC Meetings</a></h4></div>
  <div class="row fomc-meeting">
    <div class="fomc-meeting__month col-md-2"><strong>August</strong></div>
    <div class="fomc-meeting__date col-lg-1">22 (notation vote)</div>
    <div class="col-lg-2"></div>
  </div>
</div>
"""


def test_a_meeting_that_straddles_two_months_ends_in_the_second_one():
    rows = fomc.parse_calendar(CALENDAR_HTML).set_index("date")
    assert rows.loc["2024-05-01", "start_date"] == "2024-04-30"
    assert rows.loc["2024-05-01", "days"] == 2


def test_the_asterisk_is_the_dot_plot_and_not_punctuation():
    rows = fomc.parse_calendar(CALENDAR_HTML).set_index("date")
    assert bool(rows.loc["2024-03-20", "projections"]) is True
    assert bool(rows.loc["2024-05-01", "projections"]) is False


def test_a_one_day_notation_vote_is_kept_and_labelled():
    rows = fomc.parse_calendar(CALENDAR_HTML).set_index("date")
    assert rows.loc["2025-08-22", "kind"] == "notation vote"
    assert rows.loc["2025-08-22", "days"] == 1


def test_the_meeting_carries_its_statement_minutes_and_release_date():
    row = fomc.parse_calendar(CALENDAR_HTML).set_index("date").loc["2024-03-20"]
    assert row["statement_url"].endswith("/monetary20240320a.htm")
    assert row["minutes_url"].endswith("/fomcminutes20240320.htm")
    assert row["minutes_released"] == "2024-04-10"
    assert bool(row["press_conference"]) is True


def test_a_page_that_stopped_looking_like_itself_is_an_error_not_an_empty_frame():
    from backend.core.errors import ProviderError

    with pytest.raises(ProviderError):
        fomc.parse_calendar("<html><body>nothing here</body></html>")


# --------------------------------------------------------------------------- #
# The calendar row
# --------------------------------------------------------------------------- #
def test_a_past_meeting_says_what_it_did_and_an_upcoming_one_what_it_carries():
    did = cal._fomc_detail({"decision": "hiked", "change_bps": 25.0, "target_lower": 5.25,
                            "target_upper": 5.5, "projections": False,
                            "press_conference": True, "days": 2})
    assert did.startswith("Raised 25 bps to 5.25-5.5%")

    held = cal._fomc_detail({"decision": "held", "change_bps": 0, "target_lower": 4.25,
                             "target_upper": 4.5, "projections": True,
                             "press_conference": True, "days": 2})
    assert held.startswith("Held at 4.25-4.5%")
    assert "Summary of Economic Projections" in held

    ahead = cal._fomc_detail({"decision": None, "change_bps": None, "target_lower": None,
                              "target_upper": None, "projections": True,
                              "press_conference": False, "days": 2})
    assert ahead == "Summary of Economic Projections · second day of a two-day meeting"


# --------------------------------------------------------------------------- #
# Live
# --------------------------------------------------------------------------- #
def test_the_published_record_of_the_2022_hiking_cycle(auth_client):
    """Eleven hikes, 525bp, 0-0.25% to 5.25-5.50%. None of that gets revised."""
    r = auth_client.get("/api/v1/economy/fed/cycles", params={"kind": "tightening"})
    assert r.status_code == 200
    cycles = {c["cycle"]: c for c in r.json()["results"]}
    cycle = cycles["2022-2023 tightening"]
    assert cycle["moves"] == 11
    assert cycle["total_bps"] == 525.0
    assert (cycle["from_rate"], cycle["to_rate"]) == (0.25, 5.5)
    assert cycle["start_date"] == "2022-03-17"


def test_rate_changes_filter_to_one_direction_and_report_the_rest(auth_client):
    r = auth_client.get("/api/v1/economy/fed/rate_changes",
                        params={"start_date": "2022-01-01", "end_date": "2023-12-31",
                                "move": "hike", "limit": 50})
    body = r.json()
    assert r.status_code == 200
    assert all(row["direction"] == "hike" for row in body["results"])
    assert body["extra"]["hikes"] == 11
    # Newest first: a rate history is read from the present backwards.
    assert body["results"][0]["date"] > body["results"][-1]["date"]


def test_a_big_move_filter_finds_the_outsized_ones(auth_client):
    r = auth_client.get("/api/v1/economy/fed/rate_changes",
                        params={"move": "hike", "min_bps": 75, "limit": 20})
    assert r.status_code == 200
    assert all(row["change_bps"] >= 75 for row in r.json()["results"])


def test_the_policy_path_is_continuous_across_the_2008_regime_change(auth_client):
    r = auth_client.get("/api/v1/economy/fed/policy_rate",
                        params={"start_date": "2008-11-01", "end_date": "2009-02-01"})
    rows = r.json()["results"]
    assert r.status_code == 200
    assert all(row["target_upper"] is not None for row in rows)
    assert rows[0]["target_upper"] == 1.0 and rows[-1]["target_upper"] == 0.25


def test_stance_reads_the_current_range_and_the_move_that_set_it(auth_client):
    body = auth_client.get("/api/v1/economy/fed/stance").json()
    stance = body["results"]
    assert stance["target_upper"] >= stance["target_lower"]
    assert stance["last_move"] in ("hike", "cut")
    assert stance["days_since_last_move"] >= 0
    assert stance["cycle_kind"] in ("tightening", "easing")


def test_a_full_year_of_meetings_is_eight_scheduled_ones(auth_client):
    r = auth_client.get("/api/v1/economy/fed/meetings", params={"year": 2023})
    rows = r.json()["results"]
    assert r.status_code == 200
    assert len([m for m in rows if m["kind"] == "scheduled"]) == 8
    assert all(m["status"] == "past" for m in rows)
    # Four of the eight publish projections, and July 2023 was the last hike.
    assert sum(1 for m in rows if m["projections"]) == 4
    july = next(m for m in rows if m["date"].startswith("2023-07"))
    assert july["decision"] == "hiked" and july["target_upper"] == 5.5


def test_a_meeting_that_held_reports_the_rate_it_held_at(auth_client):
    rows = auth_client.get("/api/v1/economy/fed/meetings", params={"year": 2024}).json()["results"]
    held = [m for m in rows if m["decision"] == "held"]
    assert held and all(m["target_upper"] is not None for m in held)


def test_cycle_performance_measures_each_run_for_each_symbol(auth_client):
    r = auth_client.get("/api/v1/economy/fed/cycle_performance",
                        params={"symbols": "SPY,TLT", "kind": "tightening",
                                "start_date": "2000-01-01"})
    rows = r.json()["results"]
    assert r.status_code == 200
    assert all("SPY" in row and "TLT" in row for row in rows)
    assert any(row["cycle"] == "2022-2023 tightening" for row in rows)


def test_fomc_decisions_land_on_the_shared_calendar(auth_client):
    r = auth_client.get("/api/v1/calendar/events",
                        params={"start_date": "2023-07-01", "end_date": "2023-07-31",
                                "types": "fomc"})
    events = {e["title"]: e for e in r.json()["results"]}
    assert r.status_code == 200
    decision = events["FOMC rate decision"]
    assert decision["date"] == "2023-07-26"
    assert decision["importance"] == 3
    assert "Raised 25 bps" in decision["detail"]
    # The account of the June meeting was published on the 5th, and the minutes
    # are an event of their own.
    assert events["FOMC minutes"]["date"] == "2023-07-05"


def test_the_event_catalogue_offers_fomc_as_a_filter(auth_client):
    types = {t["key"]: t for t in auth_client.get("/api/v1/calendar/event_types").json()["results"]}
    assert types["fomc"]["available"] is True
    assert types["fomc"]["group"] == "Macro"


def test_a_bad_direction_is_a_400_not_a_502(auth_client):
    assert auth_client.get("/api/v1/economy/fed/rate_changes",
                           params={"move": "sideways"}).status_code == 400
    assert auth_client.get("/api/v1/economy/fed/cycles",
                           params={"kind": "sideways"}).status_code == 400
