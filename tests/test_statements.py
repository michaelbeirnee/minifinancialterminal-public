"""The three statements, normalised into one presentable document.

The schema tests use frames written out here so that a change to the ordering,
the sign convention or the derived lines shows up as a failure rather than as a
slightly wrong statement on screen.
"""
import pandas as pd
import pytest

from backend.providers import sec
from backend.providers import statements as st


def _frame(rows):
    frame = pd.DataFrame(rows)
    frame.index = pd.to_datetime(frame.pop("period_ending"))
    frame.index.name = "period_ending"
    return frame


# --------------------------------------------------------------------------- #
# Shape and ordering
# --------------------------------------------------------------------------- #
def test_income_keeps_statement_order_not_alphabetical():
    frame = _frame([
        {"period_ending": "2024-12-31", "net_income": 20, "revenue": 100,
         "cost_of_revenue": 60, "gross_profit": 40, "operating_income": 25},
    ])
    rows = st._rows_for("income", frame, ["2024-12-31"], {})
    assert [r["line_item"] for r in rows] == [
        "revenue", "cost_of_revenue", "gross_profit", "operating_income", "net_income"]


def test_lines_the_filer_never_reported_are_left_out():
    frame = _frame([{"period_ending": "2024-12-31", "revenue": 100}])
    rows = st._rows_for("income", frame, ["2024-12-31"], {})
    assert [r["line_item"] for r in rows] == ["revenue"]


def test_a_line_of_only_nulls_is_not_a_line():
    """A column the provider returns but never fills must not print a blank row."""
    frame = _frame([{"period_ending": "2024-12-31", "revenue": 100, "inventory": None}])
    rows = st._rows_for("balance", frame, ["2024-12-31"], {})
    assert rows == []


def test_balance_sheet_is_split_into_its_three_sections():
    frame = _frame([
        {"period_ending": "2024-12-31", "cash_and_equivalents": 10, "total_assets": 100,
         "accounts_payable": 20, "total_liabilities": 60, "retained_earnings": 30,
         "total_equity": 40},
    ])
    sections = {r["line_item"]: r["section"] for r in st._rows_for("balance", frame, ["2024-12-31"], {})}
    assert sections["cash_and_equivalents"] == "Assets"
    assert sections["total_assets"] == "Assets"          # a total closes its own section
    assert sections["accounts_payable"] == "Liabilities"
    assert sections["total_liabilities"] == "Liabilities"
    assert sections["retained_earnings"] == "Shareholders' equity"


# --------------------------------------------------------------------------- #
# Sign convention
# --------------------------------------------------------------------------- #
def test_cash_outflows_are_negative_however_the_provider_signed_them():
    """SEC reports amounts *paid* as positive; Yahoo signs them negative."""
    as_sec = _frame([{"period_ending": "2024-12-31", "operating_cash_flow": 100,
                      "capital_expenditure": 30, "dividends_paid": 10}])
    as_yahoo = _frame([{"period_ending": "2024-12-31", "operating_cash_flow": 100,
                        "capital_expenditure": -30, "dividends_paid": -10}])
    for frame in (as_sec, as_yahoo):
        rows = {r["line_item"]: r["2024-12-31"] for r in st._rows_for("cash", frame, ["2024-12-31"], {})}
        assert rows["capital_expenditure"] == -30
        assert rows["dividends_paid"] == -10
        assert rows["operating_cash_flow"] == 100        # inflows are left alone
        assert rows["free_cash_flow"] == 70              # …and FCF is right either way


# --------------------------------------------------------------------------- #
# Derived lines
# --------------------------------------------------------------------------- #
def test_derived_lines_are_flagged_as_computed():
    frame = _frame([{"period_ending": "2024-12-31", "operating_cash_flow": 100,
                     "capital_expenditure": 30}])
    fcf = next(r for r in st._rows_for("cash", frame, ["2024-12-31"], {})
               if r["line_item"] == "free_cash_flow")
    assert fcf["derived"] is True
    assert fcf["2024-12-31"] == 70


def test_gross_profit_is_computed_only_when_it_was_not_filed():
    filed = _frame([{"period_ending": "2024-12-31", "revenue": 100,
                     "cost_of_revenue": 60, "gross_profit": 41}])
    rows = st._rows_for("income", filed, ["2024-12-31"], {})
    assert [r for r in rows if r["line_item"] == "gross_profit"][0]["2024-12-31"] == 41
    assert not [r for r in rows if r["line_item"] == "gross_profit_derived"]

    unfiled = _frame([{"period_ending": "2024-12-31", "revenue": 100, "cost_of_revenue": 60}])
    derived = [r for r in st._rows_for("income", unfiled, ["2024-12-31"], {})
               if r["line_item"] == "gross_profit_derived"]
    assert derived and derived[0]["2024-12-31"] == 40 and derived[0]["derived"] is True


def test_balance_sheet_derives_debt_and_working_capital():
    frame = _frame([{"period_ending": "2024-12-31", "cash_and_equivalents": 30,
                     "short_term_debt": 10, "long_term_debt": 50,
                     "total_current_assets": 90, "total_current_liabilities": 40}])
    rows = {r["line_item"]: r["2024-12-31"] for r in st._rows_for("balance", frame, ["2024-12-31"], {})}
    assert rows["total_debt"] == 60
    assert rows["net_cash"] == -30          # 30 of cash against 60 of debt
    assert rows["working_capital"] == 50


# --------------------------------------------------------------------------- #
# Provider tag selection
# --------------------------------------------------------------------------- #
def test_the_tag_reaching_the_newest_period_wins():
    """Filers migrate between synonyms and abandon the old tag mid-history."""
    facts = {"facts": {"us-gaap": {
        "Stale": {"units": {"USD": [
            {"form": "10-K", "start": "2016-01-01", "end": "2016-12-31", "val": 1, "filed": "2017-02-01"},
        ]}},
        "Current": {"units": {"USD": [
            {"form": "10-K", "start": "2023-01-01", "end": "2023-12-31", "val": 9, "filed": "2024-02-01"},
            {"form": "10-K", "start": "2024-01-01", "end": "2024-12-31", "val": 10, "filed": "2025-02-01"},
        ]}},
    }}}
    series = sec._fact_series(facts, ("Stale", "Current"), ("10-K",), "annual")
    assert list(series) == [9, 10]


def test_tag_order_still_breaks_an_exact_tie():
    """The preference list decides when two tags cover the same periods."""
    values = [{"form": "10-K", "start": "2024-01-01", "end": "2024-12-31",
               "val": 7, "filed": "2025-02-01"}]
    other = [dict(values[0], val=3)]
    facts = {"facts": {"us-gaap": {"Preferred": {"units": {"USD": values}},
                                   "Fallback": {"units": {"USD": other}}}}}
    series = sec._fact_series(facts, ("Preferred", "Fallback"), ("10-K",), "annual")
    assert list(series) == [7]


# --------------------------------------------------------------------------- #
# Year to date
# --------------------------------------------------------------------------- #
def _quarters(*values):
    """One value per quarter end, oldest first, on a December fiscal year."""
    ends = pd.date_range("2022-03-31", periods=len(values), freq="QE")
    return pd.Series(list(values), index=ends, dtype="float64")


def test_year_to_date_adds_up_the_quarters_filed_so_far():
    series = _quarters(1, 2, 3, 4)
    assert st._aggregate(series, list(series.index[:2]), "income", "revenue") == 3


def test_a_balance_sheet_is_carried_not_summed():
    """Two quarters of cash is not twice the cash — it is the later balance."""
    series = _quarters(10, 20)
    assert st._aggregate(series, list(series.index), "balance", "cash_and_equivalents") == 20


def test_share_counts_average_rather_than_add():
    series = _quarters(100, 90)
    column = next(iter(st.SHARE_COUNT_COLUMNS))
    assert st._aggregate(series, list(series.index), "income", column) == 95


def test_a_window_the_provider_never_reached_is_blank_not_short():
    """Yahoo carries about five quarters: half a year of history is not a half-year."""
    series = _quarters(float("nan"), 2)
    assert st._aggregate(series, list(series.index), "income", "revenue") is None


def test_a_quarter_the_filer_left_out_is_treated_as_nil():
    """A line tagged only when it happens must not blank the whole total."""
    series = _quarters(5, float("nan"), 7)
    assert st._aggregate(series, list(series.index[1:]), "income", "acquisitions") == 7


def test_the_estimate_follows_the_shape_of_earlier_years_not_the_run_rate():
    """Three quarters into a Q4-heavy year, four thirds of the year so far is too low."""
    history = [(60.0, 100.0), (30.0, 50.0)]        # each year took 60% by Q3
    value, how = st._project("income", "revenue", 90.0, 3, history)
    assert how == "seasonal" and value == pytest.approx(150.0)
    assert value > 90.0 * 4 / 3                     # …and above the run rate


def test_one_odd_year_does_not_set_the_estimate():
    """The median across years, so an acquisition or a write-off cannot swing it."""
    history = [(50.0, 100.0), (50.0, 500.0), (50.0, 102.0)]
    value, _how = st._project("income", "revenue", 50.0, 2, history)
    assert value == pytest.approx(102.0)            # the middle ratio, not the mean


def test_a_line_with_no_comparable_year_falls_back_to_a_run_rate():
    value, how = st._project("cash", "operating_cash_flow", 30.0, 2, [(None, 90.0)])
    assert how == "runrate" and value == pytest.approx(60.0)


def test_a_year_that_turned_around_is_not_scaled_by_a_sign_flip():
    """Last year lost money by Q2 and made it by Q4: that ratio is negative."""
    value, how = st._project("income", "net_income", 40.0, 2, [(-10.0, 100.0)])
    assert how == "runrate" and value == pytest.approx(80.0)


def test_the_balance_sheet_is_not_projected_at_all():
    assert st._project("balance", "total_assets", 100.0, 2, [(80.0, 120.0)]) == (None, "none")


def test_a_year_already_complete_is_not_extrapolated():
    value, how = st._project("income", "revenue", 100.0, 4, [(50.0, 110.0)])
    assert how == "complete" and value == 100.0


def test_a_fiscal_year_end_is_one_date_across_three_statements():
    """The three can be tagged a day or two apart; they are still one year end."""
    dates = pd.to_datetime(["2024-09-28", "2024-09-30", "2023-09-30"])
    assert st._cluster_dates(list(dates)) == [pd.Timestamp("2023-09-30"), pd.Timestamp("2024-09-30")]


def test_a_year_short_of_the_stretch_being_compared_returns_nothing():
    fy_ends = [pd.Timestamp("2023-12-31"), pd.Timestamp("2024-12-31")]
    index = list(pd.to_datetime(["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]))
    # 2024 has the three quarters asked for; 2023 has none of them.
    windows = st._year_windows(fy_ends, index, 3, 2)
    assert [len(w) for _end, w in windows] == [3, 0]


def test_a_statement_filing_behind_the_others_is_still_compared_like_for_like():
    """Two quarters read against a year-ago three is a decline that never happened."""
    filed = _quarters(10, 10, 10, 10, 12, 12)          # this statement stops at Q2
    quarterly = pd.DataFrame({"revenue": filed})
    annual = pd.DataFrame({"revenue": [40.0]}, index=pd.to_datetime(["2022-12-31"]))
    fy_ends = [pd.Timestamp("2022-12-31")]
    current = [i for i in quarterly.index if i > fy_ends[0]]

    windows = st._year_windows(fy_ends, list(quarterly.index), len(current), 3)
    frame, _basis = st._ytd_frame("income", quarterly, annual, current, windows, len(current))
    revenue = frame["revenue"]
    assert revenue[st.YTD] == 24                        # its own two quarters
    assert revenue[st.YTD_LY] == 20                     # against the year-ago two, not three
    assert revenue[st.PROJECTED] == 48                  # …so the year doubles, not 32


def test_the_projected_column_keeps_its_name():
    """It is longer than an ISO date, and used to be cut down to one."""
    assert st._period_key(st.PROJECTED) == st.PROJECTED
    assert st._period_key(pd.Timestamp("2024-12-31")) == "2024-12-31"


# --------------------------------------------------------------------------- #
# Live
# --------------------------------------------------------------------------- #
def test_statements_endpoint(auth_client):
    r = auth_client.get("/api/v1/equity/fundamental/statements?symbol=AAPL&period=annual&limit=5")
    assert r.status_code == 200
    body = r.json()
    rows, extra = body["results"], body["extra"]
    assert extra["periods"] == sorted(extra["periods"], reverse=True)   # newest first
    assert set(extra["statements"]) == {"income", "balance", "cash"}

    by_item = {r["line_item"]: r for r in rows}
    latest = extra["periods"][0]
    assert by_item["revenue"][latest] > 0
    # The balance sheet has to balance.
    assert by_item["total_assets"][latest] == pytest.approx(
        by_item["total_liabilities_and_equity"][latest], rel=1e-6)
    # Apple pays a dividend, and it must survive its 2017 tag migration.
    assert by_item["dividends_paid"][latest] < 0
    assert by_item["free_cash_flow"][latest] > 0


def test_quarterly_statements_are_three_month_periods(auth_client):
    r = auth_client.get("/api/v1/equity/fundamental/statements?symbol=AAPL&period=quarter&limit=6")
    assert r.status_code == 200
    body = r.json()
    periods = body["extra"]["periods"]
    assert len(periods) >= 4
    revenue = next(r for r in body["results"] if r["line_item"] == "revenue")
    quarterly = [revenue[p] for p in periods if revenue[p]]
    annual = 416_000_000_000
    assert all(v < annual / 2 for v in quarterly)   # a quarter, not a year-to-date run


def test_foreign_filer_falls_back_to_yahoo(auth_client):
    """TSM reports under IFRS and tags no us-gaap, so SEC has nothing to read."""
    r = auth_client.get("/api/v1/equity/fundamental/statements?symbol=TSM&period=annual&limit=4")
    assert r.status_code == 200
    body = r.json()
    assert body["extra"]["provider_by_statement"]["income"] == "yahoo"
    assert next(r for r in body["results"] if r["line_item"] == "revenue")


def test_year_to_date_endpoint(auth_client):
    r = auth_client.get("/api/v1/equity/fundamental/statements_ytd?symbol=AAPL")
    assert r.status_code == 200
    body = r.json()
    rows, extra = body["results"], body["extra"]
    assert extra["periods"] == ["ytd", "ytd_ly", "projected_fy", "fy_ly"]
    assert 1 <= extra["quarters_elapsed"] <= 4
    assert extra["ytd_through"] > extra["fiscal_year_opened"]
    # No statement may claim more of the year than the filer has filed.
    assert all(1 <= n <= extra["quarters_elapsed"]
               for n in extra["quarters_by_statement"].values())

    by_item = {r["line_item"]: r for r in rows}
    revenue = by_item["revenue"]
    # A part-year is smaller than the year it is part of, and the estimate for
    # the full year is not.
    assert 0 < revenue["ytd"] < revenue["projected_fy"]
    assert revenue["ytd"] > revenue["ytd_ly"] > 0          # Apple is growing
    assert revenue["projected_fy"] > revenue["fy_ly"] > 0
    assert revenue["projection_basis"] in ("seasonal", "runrate")

    # The balance sheet is carried at its latest date, not summed, and not
    # projected: total assets are a position, and one 10-Q is not four.
    assets = by_item["total_assets"]
    assert assets["projected_fy"] is None
    assert assets["ytd"] < revenue["fy_ly"] * 2            # a balance, not a running total
    assert by_item["total_assets"]["ytd"] == pytest.approx(
        by_item["total_liabilities_and_equity"]["ytd"], rel=1e-6)


def test_year_to_date_survives_a_provider_with_no_history(auth_client):
    """TSM comes from Yahoo, which carries about five quarters and no more."""
    r = auth_client.get("/api/v1/equity/fundamental/statements_ytd?symbol=TSM")
    assert r.status_code == 200
    extra = r.json()["extra"]
    assert extra["provider_by_statement"]["income"] == "yahoo"
    revenue = next(x for x in r.json()["results"] if x["line_item"] == "revenue")
    # Whatever it can reach, the year so far and the estimate are still there,
    # and a year-ago column it cannot fill is blank rather than short.
    assert revenue["ytd"] > 0 and revenue["projected_fy"] > revenue["ytd"]
    assert revenue["ytd_ly"] is None or revenue["ytd_ly"] > 0


def test_a_non_filer_is_a_clean_error(auth_client):
    r = auth_client.get("/api/v1/equity/fundamental/statements?symbol=SPY")
    assert r.status_code >= 400
    assert "detail" in r.json()
