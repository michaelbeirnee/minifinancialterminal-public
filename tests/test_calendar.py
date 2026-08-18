"""The unified calendar: normalisation, filtering and the honesty guarantees.

Five feeds land on one row shape, so most of what can break is in the mapping
between a provider's frame and that shape — dates in four spellings, a dividend
row that is really two events, an IPO table dated by a different column per
bucket. Those are tested against frames written out here.

The properties that matter beyond "does it parse" get their own tests: an event
with no usable date must be dropped rather than defaulted onto today, a row cap
that bites must be reported rather than leaving the tail of the window looking
empty, and a named symbol must survive a size floor that would otherwise hide
the company the user asked about.

The network-backed tests at the bottom follow the rest of the suite and hit the
live calendars.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.extensions import calendar as cal


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
def test_every_spelling_these_feeds_use_reads_as_one_date():
    assert cal._iso("8/17/2026") == "2026-08-17"          # Nasdaq
    assert cal._iso("2026-08-17") == "2026-08-17"
    assert cal._iso(pd.Timestamp("2026-08-17 20:00:00+00:00")) == "2026-08-17"  # Yahoo


def test_a_missing_date_is_none_and_never_today():
    """``pd.to_datetime`` turns several of these into "now", which would silently
    file an undated event onto today's cell."""
    for blank in (None, "", "--", "-", "N/A", "NaT", "nan", float("nan"), pd.NaT):
        assert cal._iso(blank) is None


def test_a_row_with_no_date_is_dropped_rather_than_placed():
    assert cal._row(None, "earnings", symbol="AAPL", title="AAPL reports") is None
    assert cal._row("2026-08-17", "earnings", symbol="AAPL")["date"] == "2026-08-17"


def test_money_parses_out_of_the_strings_nasdaq_serves():
    assert cal._number("$20,297,189,413") == 20297189413.0
    assert cal._number("1.2796") == 1.2796
    assert cal._number("--") is None
    assert cal._number(None) is None


# --------------------------------------------------------------------------- #
# Normalising each feed
# --------------------------------------------------------------------------- #
NASDAQ_EARNINGS = pd.DataFrame([{
    "calendar_date": "2026-08-17", "symbol": "FN", "name": "Fabrinet",
    "time": "time-after-hours", "market_cap": "$20,297,189,413",
    "eps_forecast": "$3.69", "no_of_ests": "3", "fiscal_quarter_ending": "Jun/2026",
}, {
    "calendar_date": "2026-08-17", "symbol": "TINY", "name": "Tiny Corp",
    "time": "time-pre-market", "market_cap": "$150,000,000",
    "eps_forecast": "$0.01", "no_of_ests": "1", "fiscal_quarter_ending": "Jun/2026",
}])


def test_session_timing_reads_as_words_not_feed_codes():
    rows = cal._nasdaq_earnings(NASDAQ_EARNINGS)
    assert [r["time"] for r in rows] == ["after-hours", "pre-market"]
    assert cal._timing("AMC") == "after-hours"
    assert cal._timing("BMO") == "pre-market"
    # "time not supplied" is an absence, not a session.
    assert cal._timing("time-not-supplied") is None


def test_importance_tiers_on_market_cap():
    rows = cal._nasdaq_earnings(NASDAQ_EARNINGS)
    assert rows[0]["importance"] == 2   # $20bn
    assert rows[1]["importance"] == 1   # $150m
    assert cal._cap_tier(60e9) == 3
    assert cal._cap_tier(None) == 1


NASDAQ_DIVIDENDS = pd.DataFrame([{
    "calendar_date": "2026-08-17", "symbol": "ARTNA", "company_name": "Artesian Resources",
    "dividend_ex_date": "8/17/2026", "payment_date": "8/28/2026",
    "record_date": "8/17/2026", "dividend_rate": 0.3199,
    "indicated_annual_dividend": 1.2796, "announcement_date": "8/06/2026",
}])


def test_one_dividend_disclosure_becomes_two_dated_events():
    """The ex-date and the payment date are different days that a holder cares
    about differently; collapsing them to one row loses the second entirely."""
    rows = cal._nasdaq_dividends(NASDAQ_DIVIDENDS, {"dividend_ex", "dividend_pay"})
    assert [(r["type"], r["date"]) for r in rows] == [
        ("dividend_ex", "2026-08-17"), ("dividend_pay", "2026-08-28")]


def test_only_the_requested_half_of_a_dividend_is_emitted():
    rows = cal._nasdaq_dividends(NASDAQ_DIVIDENDS, {"dividend_ex"})
    assert [r["type"] for r in rows] == ["dividend_ex"]


NASDAQ_IPO = pd.DataFrame([
    {"dealID": "1", "proposedTickerSymbol": "VOGX", "companyName": "Vogenx, Inc.",
     "proposedExchange": "NASDAQ Capital", "proposedSharePrice": "13.00",
     "dollarValueOfSharesOffered": "$81,250,000", "status": "priced",
     "pricedDate": "8/12/2026", "expectedPriceDate": None, "filedDate": None,
     "withdrawDate": None},
    {"dealID": "2", "proposedTickerSymbol": "LATER", "companyName": "Later Corp",
     "proposedExchange": "NYSE", "proposedSharePrice": None,
     "dollarValueOfSharesOffered": None, "status": "upcoming",
     "pricedDate": None, "expectedPriceDate": "9/02/2026", "filedDate": None,
     "withdrawDate": None},
])


def test_each_ipo_bucket_is_dated_by_its_own_column():
    """Priced, upcoming, filed and withdrawn are four tables stacked together,
    and each carries its date in a different field."""
    rows = cal._nasdaq_ipo(NASDAQ_IPO)
    assert [(r["symbol"], r["date"]) for r in rows] == [
        ("VOGX", "2026-08-12"), ("LATER", "2026-09-02")]


YAHOO_EARNINGS = pd.DataFrame(
    [{"Company": "Oracle Corporation", "Marketcap": 4.335e11,
      "Event Name": "Q1 2027 Earnings Announcement",
      "Event Start Date": pd.Timestamp("2026-09-10 20:00:00+00:00"),
      "Timing": "AMC", "EPS Estimate": 1.74, "Reported EPS": None, "Surprise(%)": None}],
    index=pd.Index(["ORCL"], name="Symbol"))


def test_yahoo_indexes_by_symbol_and_the_index_is_the_symbol():
    rows = cal._yahoo_earnings(YAHOO_EARNINGS)
    assert rows[0]["symbol"] == "ORCL"
    assert rows[0]["date"] == "2026-09-10"
    assert rows[0]["time"] == "after-hours"
    assert rows[0]["importance"] == 3        # $433bn
    assert "EPS est 1.74" in rows[0]["detail"]


YAHOO_ECONOMIC = pd.DataFrame(
    [{"Region": "US", "Event Time": pd.Timestamp("2026-08-20 00:00:00+00:00"),
      "For": "Jul", "Actual": None, "Expected": None, "Last": 209.0, "Revised": None},
     {"Region": "BH", "Event Time": pd.Timestamp("2026-08-20 00:00:00+00:00"),
      "For": "Jul", "Actual": None, "Expected": None, "Last": 0.9, "Revised": None}],
    index=pd.Index(["Initial Jobless Clm", "CPI MM"], name="Event"))


def test_macro_rank_needs_both_a_headline_series_and_a_market_moving_region():
    """The same indicator from a small economy is real data that does not
    reprice a US book, so it cannot share the top tier."""
    rows = cal._yahoo_economic(YAHOO_ECONOMIC)
    by_title = {r["title"]: r for r in rows}
    assert by_title["US · Initial Jobless Clm"]["importance"] == 3
    assert by_title["BH · CPI MM"]["importance"] == 2   # headline series, minor region
    assert cal._economic_importance("Retail Sales YY", "SG") == 2
    assert cal._economic_importance("Forex Reserves", "PH") == 1


# --------------------------------------------------------------------------- #
# The event-type catalogue
# --------------------------------------------------------------------------- #
def test_types_with_no_free_source_are_published_not_hidden():
    """A filter that silently omits corporate access reads as "nothing
    scheduled" rather than "no source", so the catalogue carries both."""
    keys = {t["key"] for t in cal.EVENT_TYPES}
    assert {"corporate_access", "deal_roadshow", "earnings_call"} <= keys
    for t in cal.EVENT_TYPES:
        if not t["available"]:
            assert t.get("why"), "{} must explain why it is unavailable".format(t["key"])


def test_asking_for_an_unsourceable_type_says_so():
    with pytest.raises(ValueError, match="No free source"):
        cal._requested_types("corporate_access")


def test_an_unknown_type_is_a_client_error_listing_the_real_ones():
    # ValueError, not ProviderError: the API layer maps this to 400 rather than
    # blaming the upstream feed with a 502.
    with pytest.raises(ValueError, match="Unknown event type"):
        cal._requested_types("earnigns")


def test_custom_notes_are_catalogued_but_not_fetched_by_the_platform_command():
    """They live behind auth at /api/user/calendar, so the stateless command
    must not try to serve them."""
    assert "custom" in cal.AVAILABLE_TYPES
    assert "custom" not in cal.PLATFORM_TYPES


# --------------------------------------------------------------------------- #
# Window, dedup and the row cap
# --------------------------------------------------------------------------- #
def test_a_reversed_window_is_read_the_way_round_it_was_meant():
    first, last = cal._window("2026-09-30", "2026-09-01")
    assert (str(first), str(last)) == ("2026-09-01", "2026-09-30")


def test_the_window_is_clamped_rather_than_walking_forever():
    first, last = cal._window("2026-01-01", "2030-01-01")
    assert (last - first).days == cal.MAX_WINDOW_DAYS


def test_the_same_event_from_two_feeds_appears_once():
    rows = [cal._row("2026-08-17", "earnings", symbol="FN", title="FN reports", source="nasdaq"),
            cal._row("2026-08-17", "earnings", symbol="FN", title="FN reports", source="yahoo")]
    events, dropped = cal._sorted_events(rows, pd.Timestamp("2026-08-01").date(),
                                         pd.Timestamp("2026-08-31").date(), 100)
    assert len(events) == 1 and dropped == 0


def test_events_outside_the_window_are_not_returned():
    rows = [cal._row("2026-07-15", "earnings", symbol="A", title="A"),
            cal._row("2026-08-17", "earnings", symbol="B", title="B")]
    events, _ = cal._sorted_events(rows, pd.Timestamp("2026-08-01").date(),
                                   pd.Timestamp("2026-08-31").date(), 100)
    assert [e["symbol"] for e in events] == ["B"]


def test_a_row_cap_reports_what_it_cut_off():
    """Truncation takes the end of the window, so a capped month goes blank from
    wherever the limit ran out — indistinguishable, on a grid, from a quiet
    fortnight. The count is what lets the caller say which it was."""
    rows = [cal._row("2026-08-{:02d}".format(d), "earnings", symbol="S{}".format(d),
                     title="S{} reports".format(d)) for d in range(1, 21)]
    events, dropped = cal._sorted_events(rows, pd.Timestamp("2026-08-01").date(),
                                         pd.Timestamp("2026-08-31").date(), 5)
    assert len(events) == 5 and dropped == 15
    assert events[-1]["date"] == "2026-08-05"   # the tail is missing, not empty


def test_a_symbol_filter_drops_macro_rather_than_letting_it_ride_along():
    rows = [cal._row("2026-08-17", "earnings", symbol="AAPL", title="AAPL reports"),
            cal._row("2026-08-17", "economic", symbol=None, title="US · CPI")]
    assert [r["type"] for r in cal._symbol_filter(rows, "AAPL")] == ["earnings"]


def test_the_symbol_filter_is_case_and_separator_insensitive():
    rows = [cal._row("2026-08-17", "earnings", symbol="AAPL", title="A"),
            cal._row("2026-08-17", "earnings", symbol="MSFT", title="M"),
            cal._row("2026-08-17", "earnings", symbol="NVDA", title="N")]
    assert len(cal._symbol_filter(rows, "aapl, msft")) == 2
    assert len(cal._symbol_filter(rows, "aapl msft nvda")) == 3


# --------------------------------------------------------------------------- #
# Live
# --------------------------------------------------------------------------- #
def test_the_catalogue_is_served(auth_client):
    d = auth_client.get("/api/v1/calendar/event_types").json()
    assert d["extra"]["available"] >= 6
    assert any(not t["available"] for t in d["results"])


def test_a_named_symbol_is_not_hidden_by_the_size_floor(auth_client):
    """Naming a company is a more specific intent than a market-cap floor, so
    it wins — otherwise filtering to something you hold returns nothing
    whenever it is smaller than the floor."""
    r = auth_client.get("/api/v1/calendar/events", params={
        "types": "earnings", "symbols": "NVDA", "min_importance": 3,
        "start_date": "2026-08-16", "end_date": "2026-11-16", "provider": "yahoo"})
    assert r.status_code in (200, 404)   # 404 only if NVDA has nothing scheduled
    if r.status_code == 200:
        assert any("overrides" in w for w in r.json()["warnings"])


def test_the_unified_feed_returns_one_row_shape_across_types(auth_client):
    r = auth_client.get("/api/v1/calendar/events", params={
        "types": "earnings,split,ipo", "provider": "yahoo", "min_importance": 3,
        "start_date": "2026-08-16", "end_date": "2026-09-16", "limit": 500})
    assert r.status_code == 200
    rows = r.json()["results"]
    assert rows
    for row in rows:
        assert set(row) == {"date", "time", "type", "type_label", "symbol", "name",
                            "title", "detail", "importance", "source"}
        assert "2026-08-16" <= row["date"] <= "2026-09-16"


def test_the_macro_calendar_ranks_and_filters_by_region(auth_client):
    r = auth_client.get("/api/v1/calendar/economic", params={
        "start_date": "2026-08-16", "end_date": "2026-08-30",
        "region": "US", "min_importance": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["extra"]["regions"] == ["US"]
    assert all(row["importance"] >= 2 for row in body["results"])


def test_a_bad_event_type_is_a_400_not_a_502(auth_client):
    assert auth_client.get("/api/v1/calendar/events",
                           params={"types": "not_a_type"}).status_code == 400


# --------------------------------------------------------------------------- #
# The user's own notes
# --------------------------------------------------------------------------- #
def test_notes_round_trip_and_are_scoped_to_their_window(auth_client):
    made = auth_client.post("/api/user/calendar", json={
        "event_date": "2026-08-20", "title": "Powell speaks", "symbol": "tlt"})
    assert made.status_code == 201
    assert made.json()["symbol"] == "TLT"     # upper-cased on the way in

    inside = auth_client.get("/api/user/calendar", params={
        "start_date": "2026-08-01", "end_date": "2026-08-31"}).json()
    assert [n["title"] for n in inside] == ["Powell speaks"]
    outside = auth_client.get("/api/user/calendar", params={
        "start_date": "2026-09-01", "end_date": "2026-09-30"}).json()
    assert outside == []

    event_id = made.json()["id"]
    assert auth_client.delete("/api/user/calendar/{}".format(event_id)).status_code == 204
    assert auth_client.get("/api/user/calendar").json() == []


def test_a_note_needs_a_real_date(auth_client):
    assert auth_client.post("/api/user/calendar", json={
        "event_date": "20/08/2026", "title": "wrong way round"}).status_code == 422


def test_another_users_note_is_not_reachable(auth_client, client):
    made = auth_client.post("/api/user/calendar", json={
        "event_date": "2026-08-20", "title": "mine"}).json()
    import uuid

    other = "user_{}".format(uuid.uuid4().hex[:8])
    client.post("/api/auth/register", json={
        "username": other, "email": "{}@example.com".format(other), "password": "secret123"})
    tok = client.post("/api/auth/login", data={
        "username": other, "password": "secret123"}).json()["access_token"]
    client.headers.update({"Authorization": "Bearer {}".format(tok)})
    assert client.delete("/api/user/calendar/{}".format(made["id"])).status_code == 404
