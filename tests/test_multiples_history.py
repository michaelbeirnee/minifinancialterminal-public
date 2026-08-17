"""Offline tests for the trailing-multiples history.

A valuation history is easy to build and easy to build wrong, and every way of
getting it wrong looks plausible on a chart. Canned filings and a canned price
series pin the three that matter: the publication lag, the share basis after a
split, and a quarter the filer never tagged.
"""
import numpy as np
import pandas as pd
import pytest

from backend.extensions import equity_fundamental as ef

QUARTER_ENDS = pd.to_datetime([
    "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
    "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31",
])
# 100m net income a quarter, on 100m shares as filed: $4.00 of trailing EPS.
NET_INCOME = [100e6] * 8
SHARES = [100e6] * 8


def _income(eps_gaps=()):
    frame = pd.DataFrame(
        {
            "net_income": NET_INCOME,
            "revenue": [500e6] * 8,
            "operating_income": [120e6] * 8,
            "eps_diluted": [1.0] * 8,
            "weighted_average_shares_diluted": list(SHARES),
        },
        index=QUARTER_ENDS,
    )
    frame.index.name = "period_ending"
    for i in eps_gaps:  # the Q4 hole a 10-K leaves behind
        frame.iloc[i, frame.columns.get_loc("eps_diluted")] = np.nan
        frame.iloc[i, frame.columns.get_loc("weighted_average_shares_diluted")] = np.nan
    return frame


def _prices(last="2026-03-31"):
    days = pd.bdate_range("2024-01-01", last)
    return pd.DataFrame({"close": [80.0] * len(days)}, index=days)


@pytest.fixture
def canned(monkeypatch):
    """Wire the command to canned filings, prices, splits and dividends."""
    state = {"splits": pd.Series(dtype="float64"), "income": _income()}

    def statement(symbol, kind, period="annual", limit=12):
        if kind == "income":
            return state["income"]
        return pd.DataFrame(index=QUARTER_ENDS)

    monkeypatch.setattr(ef.sec, "statement", statement)
    monkeypatch.setattr(ef.yahoo, "history", lambda *a, **k: _prices())
    monkeypatch.setattr(ef.yahoo, "splits", lambda *a, **k: state["splits"])
    monkeypatch.setattr(ef.yahoo, "dividends", lambda *a, **k: pd.Series(dtype="float64"))
    return state


def _frame(**kwargs):
    return ef.multiples_history("TEST", start_date="2024-01-01", provider="sec", **kwargs).data


def test_trailing_eps_sums_four_quarters(canned):
    out = _frame()
    # $4.00 of TTM EPS against an $80 close is 20x.
    assert out["eps_ttm"].iloc[-1] == pytest.approx(4.0)
    assert out["pe_trailing"].iloc[-1] == pytest.approx(20.0)


def test_a_quarter_is_not_used_before_it_was_filed(canned):
    """The window closing 2025-12-31 must not move the ratio until it is public."""
    out = _frame(lag_days=45)
    known_from = pd.Timestamp("2025-12-31") + pd.Timedelta(45, unit="D")
    before = out.loc[out.index < known_from]
    # Nothing in the canned filings changes quarter to quarter, so the guard is
    # that the last quarter is excluded until its date: with only 7 quarters
    # available the trailing sum still resolves, and it resolves identically.
    assert not before.empty
    assert out.loc[out.index >= known_from, "eps_ttm"].iloc[0] == pytest.approx(4.0)

    # A quarter that jumps proves the lag actually gates the value.
    canned["income"].iloc[-1, canned["income"].columns.get_loc("net_income")] = 400e6
    out = _frame(lag_days=45)
    assert out.loc[out.index < known_from, "eps_ttm"].max() == pytest.approx(4.0)
    assert out.loc[out.index >= known_from, "eps_ttm"].iloc[-1] == pytest.approx(7.0)


def test_missing_q4_eps_does_not_leave_a_hole(canned):
    """Filers tag Q4 EPS inconsistently; trailing EPS comes from net income."""
    canned["income"] = _income(eps_gaps=(3, 7))
    out = _frame()
    assert out["pe_trailing"].notna().all()
    assert out["eps_ttm"].iloc[-1] == pytest.approx(4.0)


def test_filings_are_restated_onto_todays_share_basis(canned):
    """A 4-for-1 split must not quarter the historical P/E.

    Yahoo already restates prices for splits, so only the filings need moving;
    doing it to both would divide twice.
    """
    canned["splits"] = pd.Series([4.0], index=pd.to_datetime(["2026-01-15"]))
    out = _frame()
    pre = out.loc[out.index < "2026-01-15"]
    # 400m shares in today's terms against 400m of trailing net income is $1.00,
    # so an $80 close is 80x on both sides of the split date.
    assert pre["eps_ttm"].iloc[-1] == pytest.approx(1.0)
    assert pre["pe_trailing"].iloc[-1] == pytest.approx(80.0)
    assert out["pe_trailing"].iloc[-1] == pytest.approx(80.0)


def test_a_change_of_filing_basis_is_ironed_out(canned):
    """XBRL keeps what each filing said, so a splitter's old quarters sit on the
    old share basis and its recent ones on the new — and the step lands at the
    filing vintage boundary, not the split date. NVDA's real filings step at Q2
    FY24 for a split eleven months later, which is why this cannot be done off
    the split calendar.
    """
    income = _income()
    # First four quarters as originally filed (pre 10-for-1), last four restated.
    income.iloc[:4, income.columns.get_loc("weighted_average_shares_diluted")] = 10e6
    canned["income"] = income
    canned["splits"] = pd.Series([10.0], index=pd.to_datetime(["2025-11-01"]))

    shares = ef._restate_shares(income["weighted_average_shares_diluted"])
    assert shares.iloc[:4].tolist() == pytest.approx([100e6] * 4)
    assert shares.iloc[4:].tolist() == pytest.approx([100e6] * 4)

    # And the multiple stays continuous straight through the boundary.
    out = _frame()
    assert out["pe_trailing"].max() == pytest.approx(out["pe_trailing"].min())


def test_a_buyback_is_not_mistaken_for_a_restatement(canned):
    """A real quarter moves the count a percent or two; only a step past 1.5x is
    a change of basis."""
    income = _income()
    income["weighted_average_shares_diluted"] = [100e6, 98e6, 96e6, 94e6, 92e6, 90e6, 88e6, 86e6]
    canned["income"] = income
    shares = ef._restate_shares(income["weighted_average_shares_diluted"])
    assert shares.tolist() == pytest.approx(income["weighted_average_shares_diluted"].tolist())


def test_a_loss_making_quarter_carries_no_multiple(canned):
    """A negative denominator makes a multiple that reads cheap and means the
    opposite, so those days carry no ratio."""
    canned["income"]["net_income"] = [-100e6] * 8
    out = _frame()
    assert out["pe_trailing"].isna().all()
