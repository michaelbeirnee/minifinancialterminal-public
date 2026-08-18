"""The published ETF basket, and the arithmetic done on top of it.

The parsing tests run against the shape State Street actually serves — a short
preamble above the table, weights in percentage points, and three kinds of line
that are not stocks: a currency balance, a money-market sweep and an index
future, each identified by a pseudo-CUSIP rather than by anything in its name.
Those, and the concentration and contribution maths, are properties of this
code; the sponsor re-wording a fund name next quarter should not turn them red.

The network-backed tests at the bottom follow the rest of the suite and hit
State Street and Yahoo live.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.core.errors import EmptyDataError
from backend.core.registry import execute
from backend.extensions import basket
from backend.providers import spdr

# The file as pandas hands it over with header=None: three preamble rows, a
# blank, the header, then the holdings.
FILE_ROWS = [
    ["Fund Name:", "State Street® Energy Select Sector SPDR® ETF", None, None, None, None, None, None],
    ["Ticker Symbol:", "XLE", None, None, None, None, None, None],
    ["Holdings:", "As of 14-Aug-2026", None, None, None, None, None, None],
    [None, None, None, None, None, None, None, None],
    ["Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held", "Local Currency"],
    ["EXXONMOBIL HOLDINGS CORP", "XOM", "30233Q108", "BVSRPD7", 20.0, "-", 51865259.0, "USD"],
    ["BERKSHIRE HATHAWAY INC CL B", "BRK.B", "084670702", "2073390", 30.0, "-", 1000.0, "USD"],
    ["CHEVRON CORP", "CVX", "166764100", "2838555", 49.0, "-", 30360658.0, "USD"],
    ["SSI US GOV MONEY MARKET CLASS", "-", "924QSGII3", "-", 0.7, "-", 38938943.0, "USD"],
    ["XAE ENERGY        SEP26", "XAE6", "ADI394BH0", "-", 0.2, "-", 120600.0, "USD"],
    ["US DOLLAR", "-", "999USDZ92", "-", -0.1, "-", -1659467.0, "USD"],
    ["CONTRA HOLOGIC INCORPO", "2602335D", "436CVR021", "-", 0.2, "-", 2578626.0, "USD"],
]


@pytest.fixture()
def holdings(monkeypatch):
    """``spdr.fund_holdings`` reading the fixture instead of the network."""
    monkeypatch.setattr(spdr, "get_excel", lambda *a, **k: pd.DataFrame(FILE_ROWS))
    # The provider is cached on disk; go around the decorator for the fixture.
    return spdr.fund_holdings.__wrapped__("xle")


# --------------------------------------------------------------------------- #
# Which lines are stocks
# --------------------------------------------------------------------------- #
def test_a_currency_balance_is_cash_whatever_it_is_called():
    assert spdr._line_type("-", "US DOLLAR", "999USDZ92") == "cash"
    assert spdr._line_type("-", "POUND STERLING", "999GBPZ94") == "cash"
    # The money-market sweep has a real-looking identifier and no currency name.
    assert spdr._line_type("-", "SSI US GOV MONEY MARKET CLASS", "924QSGII3") == "cash"


def test_a_future_is_recognised_by_its_identifier_not_its_name():
    """"XAK TECHNOLOGY SEP26" says nothing about being a future; ADI does."""
    assert spdr._line_type("XAK6", "XAK TECHNOLOGY    SEP26", "ADI394XJ2") == "futures"
    assert spdr._line_type("XAI6", "XAI EMINI INDUSTR SEP26", "ADI391815") == "futures"


def test_a_corporate_action_stub_is_neither_cash_nor_a_holding():
    """A contra/CVR line carries a ticker no price source will answer to."""
    assert spdr._line_type("2602335D", "CONTRA HOLOGIC INCORPO", "436CVR021") == "other"
    assert spdr._line_type("-", "CONTRA WALGREENS BOOTS", "931CVR013") == "other"


def test_an_ordinary_share_is_a_holding():
    assert spdr._line_type("XOM", "EXXONMOBIL HOLDINGS CORP", "30233Q108") == "equity"
    assert spdr._line_type("BRK-B", "BERKSHIRE HATHAWAY INC CL B", "084670702") == "equity"


def test_the_as_of_line_becomes_a_date():
    assert spdr._as_of("As of 14-Aug-2026") == "2026-08-14"
    assert spdr._as_of("as of 1-Jan-2025") == "2025-01-01"
    assert spdr._as_of(None) is None
    assert spdr._as_of("unavailable") is None


# --------------------------------------------------------------------------- #
# Reading the file
# --------------------------------------------------------------------------- #
def test_the_header_is_found_under_the_preamble(holdings):
    assert list(holdings.columns) == [
        "symbol", "name", "cusip", "sedol", "weight", "shares_held", "currency", "line_type"]
    assert holdings.attrs["as_of"] == "2026-08-14"
    assert holdings.attrs["ticker"] == "XLE"
    assert "Energy Select Sector" in holdings.attrs["fund_name"]


def test_percentage_points_become_fractions_and_sort_heaviest_first(holdings):
    assert holdings.iloc[0]["symbol"] == "CVX"
    assert holdings.iloc[0]["weight"] == pytest.approx(0.49)
    assert holdings["weight"].is_monotonic_decreasing
    # Cash can be negative — an unsettled trade — and must not be dropped.
    assert holdings.loc[holdings["name"] == "US DOLLAR", "weight"].iloc[0] == pytest.approx(-0.001)


def test_a_share_class_is_written_the_way_prices_are_looked_up(holdings):
    assert "BRK-B" in set(holdings["symbol"])
    assert "BRK.B" not in set(holdings["symbol"])


def test_only_stocks_keep_a_symbol(holdings):
    """A cash or futures ticker would otherwise be sent to a price provider."""
    assert set(holdings.loc[holdings["line_type"] != "equity", "symbol"]) == {""}
    assert set(holdings.loc[holdings["line_type"] == "equity", "symbol"]) == {"XOM", "BRK-B", "CVX"}


def test_a_fund_state_street_does_not_publish_says_so(monkeypatch):
    from backend.core.errors import ProviderError

    def missing(*_a, **_k):
        raise ProviderError("Request failed (HTTP 404): ...")

    monkeypatch.setattr(spdr, "get_excel", missing)
    with pytest.raises(EmptyDataError, match="SPDR"):
        spdr.fund_holdings.__wrapped__("QQQ")


# --------------------------------------------------------------------------- #
# Concentration
# --------------------------------------------------------------------------- #
def test_an_equally_weighted_basket_is_as_many_names_as_it_says():
    stats = basket._concentration_stats([0.1] * 10)
    assert stats["holdings"] == 10
    assert stats["effective_holdings"] == pytest.approx(10.0)
    assert stats["holdings_to_half"] == 5


def test_concentration_counts_the_weight_not_the_names():
    """Twenty names, but one of them is half the fund."""
    stats = basket._concentration_stats([0.5] + [0.5 / 19] * 19)
    assert stats["holdings"] == 20
    assert stats["holdings_to_half"] == 1
    assert stats["effective_holdings"] < 4
    assert stats["top_1_weight"] == pytest.approx(0.5)


def test_top_n_is_capped_at_the_number_of_holdings():
    stats = basket._concentration_stats([0.4, 0.35, 0.25])
    assert stats["top_5_weight"] == stats["top_25_weight"] == pytest.approx(1.0)


def test_an_empty_basket_produces_no_statistics():
    assert basket._concentration_stats([]) == {}


# --------------------------------------------------------------------------- #
# Who moved it
# --------------------------------------------------------------------------- #
def _contribution(monkeypatch, weights, returns, fund_return):
    """Run the decomposition over a hand-made basket and price history."""
    rows = pd.DataFrame(
        {
            "symbol": list(weights), "name": list(weights),
            "cusip": "", "sedol": "", "weight": list(weights.values()),
            "shares_held": 1.0, "currency": "USD", "line_type": "equity",
        }
    )
    rows.attrs.update(fund_name="Test Fund", as_of="2026-08-14", ticker="TST")
    monkeypatch.setattr(spdr, "fund_holdings", lambda _sym: rows)
    monkeypatch.setattr(basket, "_holding_returns", lambda syms, *a: (dict(returns), []))
    monkeypatch.setattr(
        basket.yahoo, "history",
        lambda *a, **k: pd.DataFrame({"close": [100.0, 100.0 * (1 + fund_return)]}),
    )
    return basket.basket_contribution(symbol="TST", start_date="2026-05-17")


def test_contribution_uses_the_weight_the_position_started_at(monkeypatch):
    """A name that doubled was half its current size when the window opened."""
    result = _contribution(
        monkeypatch, {"UP": 0.5, "FLAT": 0.5}, {"UP": 1.0, "FLAT": 0.0}, 0.5)
    by_symbol = {r["symbol"]: r for r in result.data}
    # UP is 50% now; before doubling it was 25% of a smaller basket, so a third.
    assert by_symbol["UP"]["start_weight"] == pytest.approx(1 / 3, abs=1e-6)
    assert by_symbol["FLAT"]["start_weight"] == pytest.approx(2 / 3, abs=1e-6)
    assert by_symbol["UP"]["contribution"] == pytest.approx(1 / 3, abs=1e-6)
    assert by_symbol["FLAT"]["contribution"] == 0.0


def test_the_contributions_add_up_to_what_the_fund_did(monkeypatch):
    result = _contribution(
        monkeypatch, {"A": 0.6, "B": 0.3, "C": 0.1}, {"A": 0.2, "B": -0.1, "C": 0.5}, 0.11)
    total = sum(r["contribution"] for r in result.data)
    assert result.extra["total_contribution"] == pytest.approx(total, abs=1e-6)
    # The reconstruction is the fund's own return, up to the rebalancing it
    # cannot see — which is why the gap is reported rather than hidden.
    assert result.extra["unexplained"] == pytest.approx(
        result.extra["fund_return"] - total, abs=1e-6)
    assert abs(result.extra["unexplained"]) < 0.02


def test_the_biggest_contributor_leads_and_the_counts_agree(monkeypatch):
    result = _contribution(
        monkeypatch, {"A": 0.6, "B": 0.3, "C": 0.1}, {"A": 0.2, "B": -0.1, "C": 0.5}, 0.11)
    assert result.data[0]["symbol"] == "A"
    assert result.data[-1]["symbol"] == "B"
    assert (result.extra["advancers"], result.extra["decliners"]) == (2, 1)


def test_a_basket_with_no_priced_holdings_is_an_error(monkeypatch):
    with pytest.raises(EmptyDataError):
        _contribution(monkeypatch, {"A": 1.0}, {}, 0.0)


# --------------------------------------------------------------------------- #
# Overlap
# --------------------------------------------------------------------------- #
def test_overlap_is_the_smaller_of_the_two_weights(monkeypatch):
    def basket_for(sym):
        weights = {"XLK": {"AAPL": 0.6, "MSFT": 0.4},
                   "SPY": {"AAPL": 0.2, "XOM": 0.8}}[sym]
        rows = pd.DataFrame(
            {"symbol": list(weights), "name": list(weights), "cusip": "", "sedol": "",
             "weight": list(weights.values()), "shares_held": 1.0, "currency": "USD",
             "line_type": "equity"}
        )
        rows.attrs.update(fund_name=sym, as_of="2026-08-14", ticker=sym)
        return rows

    monkeypatch.setattr(spdr, "fund_holdings", basket_for)
    result = basket.basket_overlap(symbol="XLK", versus="SPY")
    assert [r["symbol"] for r in result.data] == ["AAPL"]
    assert result.data[0]["shared_weight"] == pytest.approx(0.2)
    assert result.extra["overlap_weight"] == pytest.approx(0.2)
    # The same shared weight is a fifth of one fund and a fifth of the other
    # here only because both sum to 1; the point is that it is measured twice.
    assert result.extra["share_of_xlk"] == pytest.approx(0.2)
    assert result.extra["only_in_xlk"] == pytest.approx(0.4)


def test_a_fund_is_not_compared_with_itself():
    with pytest.raises(ValueError, match="two different"):
        basket.basket_overlap(symbol="XLK", versus="xlk")


# --------------------------------------------------------------------------- #
# Live
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("symbol", ["XLK", "XLE"])
def test_live_the_whole_basket_comes_back_and_adds_up(symbol):
    df = spdr.fund_holdings(symbol)
    assert len(df) > 20
    assert df["weight"].sum() == pytest.approx(1.0, abs=0.01)
    assert df["weight"].is_monotonic_decreasing
    assert (df["line_type"] == "equity").sum() > 15
    assert df.attrs["as_of"]


def test_live_the_basket_is_more_than_yahoos_top_ten():
    """The whole point: Yahoo stops at ten rows and a sector fund does not."""
    result = execute("/etf/basket/holdings", symbol="XLK")
    rows = result.results
    assert len(rows) > 30
    assert rows[0]["rank"] == 1
    assert rows[-1]["cumulative_weight"] == pytest.approx(
        result.extra["equity_weight"], abs=0.001)
    # Industry comes from the index membership table, joined on the ticker.
    assert any(row["industry"] for row in rows)


def test_live_the_sector_funds_can_be_ranked_against_each_other():
    result = basket.basket_concentration(symbol="XLE,XLI")
    by_symbol = {r["symbol"]: r for r in result.data}
    # Energy is two dozen names; industrials is eighty-odd. The ranking is on
    # weight, not on the length of the list.
    assert by_symbol["XLE"]["top_10_weight"] > by_symbol["XLI"]["top_10_weight"]
    assert by_symbol["XLE"]["holdings"] < by_symbol["XLI"]["holdings"]
    assert result.data[0]["symbol"] == "XLE"


def test_live_a_sector_fund_splits_into_several_industries():
    result = basket.basket_industries(symbol="XLF")
    assert len(result.data) > 5
    assert sum(r["weight"] for r in result.data) == pytest.approx(1.0, abs=0.02)
    assert result.data[0]["weight"] >= result.data[-1]["weight"]


def test_live_a_sector_fund_sits_inside_the_index_fund():
    result = basket.basket_overlap(symbol="XLK", versus="SPY")
    # Every Select Sector holding is an S&P 500 member by construction.
    assert result.extra["only_in_xlk"] == pytest.approx(0.0, abs=0.001)
    assert 0.05 < result.extra["share_of_spy"] < 0.6


def test_live_the_rest_endpoint_serves_a_basket(auth_client):
    r = auth_client.get("/api/v1/etf/basket/concentration", params={"symbol": "XLU"})
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "ssga"
    assert body["results"][0]["holdings"] > 10
