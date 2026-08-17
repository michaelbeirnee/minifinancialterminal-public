"""Peer selection and the comparison built on top of it.

The ranking rules are tested against candidate rows written out here, because
what matters is that agreement between the three sources beats any one of them
and that size keeps a minnow's opinion in proportion — properties of the merge,
which a filer re-wording its competition paragraph next year should not be able
to turn red.

The network-backed tests at the bottom follow the rest of the suite and hit
EDGAR and Yahoo live.
"""
import pytest

from backend.core.errors import EmptyDataError
from backend.extensions import compare
from backend.providers import peers


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
def test_size_difference_discounts_the_evidence():
    """Everybody names the giant, so the giant learns nothing from being named."""
    same = peers._proximity(100e9, 100e9)
    ten_times = peers._proximity(100e9, 10e9)
    thousand_times = peers._proximity(100e9, 100e6)
    assert same == 1.0
    assert ten_times == pytest.approx(0.5)
    assert thousand_times < ten_times < same
    # The discount does not care which way round the two companies are.
    assert peers._proximity(10e9, 100e9) == pytest.approx(peers._proximity(100e9, 10e9))


def test_an_unpriced_candidate_is_not_penalised():
    assert peers._proximity(100e9, None) == 1.0
    assert peers._proximity(None, 100e9) == 1.0
    assert peers._proximity(100e9, 0) == 1.0


def test_every_source_is_named_in_the_reason():
    assert peers._why({"sources": ["filings", "classification", "registration"], "mentions": 4}) == (
        "named as competition in 4 filings, same industry, same SIC code")
    assert peers._why({"sources": ["filings"], "mentions": 1}) == "named as competition in 1 filing"
    assert peers._why({"sources": ["registration"], "mentions": 0}) == "same SIC code"


def test_the_filings_outrank_a_shared_filing_cabinet():
    """One is somebody's statement; the others are a classification."""
    assert peers.WEIGHTS["filings"] > peers.WEIGHTS["classification"] > peers.WEIGHTS["registration"]
    assert peers.ORDERED == frozenset({"classification", "filings"})


# --------------------------------------------------------------------------- #
# Reading a hit
# --------------------------------------------------------------------------- #
def test_a_renamed_filer_is_found_under_the_ticker_it_trades_as_now(monkeypatch):
    """SMART Global Holdings filed as SGH and trades as PENG."""
    monkeypatch.setattr(peers, "_tickers", lambda: frozenset({"PENG", "NVDA"}))
    monkeypatch.setattr(peers, "_listed", lambda: {"0001616533": ("PENG", "Penguin Solutions")})
    assert peers._live_ticker("0001616533", "SMART Global Holdings  (SGH)  (CIK 0001616533)") == "PENG"


def test_a_live_ticker_in_the_filing_wins(monkeypatch):
    """It disambiguates a company with several share classes; the map cannot."""
    monkeypatch.setattr(peers, "_tickers", lambda: frozenset({"SMCI", "SMCIP"}))
    monkeypatch.setattr(peers, "_listed", lambda: {"0001375365": ("SMCIP", "Super Micro")})
    assert peers._live_ticker("0001375365", "Super Micro Computer  (SMCI)  (CIK 0001375365)") == "SMCI"


def test_the_filer_name_loses_its_ticker_and_cik():
    assert peers._display_name(
        {"display_names": ["NVIDIA CORP  (NVDA)  (CIK 0001045810)"]}) == "NVIDIA CORP"


# --------------------------------------------------------------------------- #
# The comparison table
# --------------------------------------------------------------------------- #
def test_a_dividend_yield_is_computed_rather_than_guessed():
    """0.35 is either Apple's 0.35% or a REIT's 35%; the rate settles it."""
    computed = compare._augment({"dividendRate": 1.04, "currentPrice": 230.0,
                                 "dividendYield": 0.45})
    assert computed["_dividend_yield"] == pytest.approx(0.00452, abs=1e-5)
    # With no rate to check against, the vendor's number is read as points
    # above 1 and as a fraction below it.
    assert compare._augment({"dividendYield": 3.9})["_dividend_yield"] == pytest.approx(0.039)
    assert compare._augment({"dividendYield": 0.039})["_dividend_yield"] == pytest.approx(0.039)


def test_a_missing_market_cap_is_rebuilt_from_shares_and_price():
    found = compare._augment({"sharesOutstanding": 1e9, "currentPrice": 42.0})
    assert found["_market_cap"] == pytest.approx(42e9)
    # A reported cap is never second-guessed.
    assert compare._augment({"marketCap": 5e9, "sharesOutstanding": 1e9,
                             "currentPrice": 42.0})["_market_cap"] == 5e9
    assert compare._augment({})["_market_cap"] is None


def test_percentage_point_fields_are_brought_back_to_fractions():
    assert compare._clean("debtToEquity", 78.4) == pytest.approx(0.784)
    assert compare._clean("trailingPE", 34.2) == pytest.approx(34.2)   # not a percentage
    assert compare._clean("marketCap", None) is None
    assert compare._clean("marketCap", "n/a") is None


def test_the_median_is_of_the_peers_not_the_group():
    """A company cannot be read against a median it is inside."""
    values = {"AAPL": 100.0, "MSFT": 20.0, "GOOGL": 10.0, "AMZN": 30.0}
    assert compare._median(values, exclude="AAPL") == pytest.approx(20.0)
    assert compare._median({"AAPL": 100.0}, exclude="AAPL") is None
    assert compare._median({"AAPL": 100.0, "MSFT": None}, exclude="AAPL") is None


def test_a_comparison_of_one_is_not_a_comparison():
    with pytest.raises(ValueError):
        compare.compare_table(symbol="AAPL")


# --------------------------------------------------------------------------- #
# The revenue mix
# --------------------------------------------------------------------------- #
def _segment_rows(period):
    return [
        {"dimension": "business", "segment": "Compute", "weight": "", period: 60.0},
        {"dimension": "business", "segment": "Graphics", "weight": "", period: 30.0},
        {"dimension": "business", "segment": "Total disclosed", "weight": "subtotal", period: 90.0},
        {"dimension": "product", "segment": "Chips", "weight": "", period: 100.0},
        {"dimension": "total", "segment": "Total revenue", "weight": "total", period: 100.0},
    ]


def test_reportable_segments_are_preferred_to_the_other_splits():
    """They are the split the company manages itself by."""
    period = "2026-01-25"
    meta = {"periods": [period],
            "dimensions": [{"dimension": "product"}, {"dimension": "business"}]}
    rows = compare._mix_rows("NVDA", _segment_rows(period), meta, "best", 8)
    assert {r["dimension"] for r in rows} == {"business"}
    assert [r["segment"] for r in rows[:2]] == ["Compute", "Graphics"]
    assert rows[0]["share"] == pytest.approx(0.6)


def test_the_undisclosed_tail_keeps_the_shares_a_whole():
    period = "2026-01-25"
    meta = {"periods": [period], "dimensions": [{"dimension": "business"}]}
    rows = compare._mix_rows("NVDA", _segment_rows(period), meta, "best", 8)
    assert rows[-1]["segment"] == "Other / undisclosed"
    assert rows[-1]["share"] == pytest.approx(0.1)
    assert sum(r["share"] for r in rows) == pytest.approx(1.0)


def test_a_split_that_adds_up_gets_no_tail():
    period = "2026-01-25"
    rows = [
        {"dimension": "business", "segment": "A", "weight": "", period: 70.0},
        {"dimension": "business", "segment": "B", "weight": "", period: 30.0},
        {"dimension": "total", "segment": "Total revenue", "weight": "total", period: 100.0},
    ]
    meta = {"periods": [period], "dimensions": [{"dimension": "business"}]}
    found = compare._mix_rows("X", rows, meta, "best", 8)
    assert [r["segment"] for r in found] == ["A", "B"]


def test_asking_for_a_split_a_company_does_not_file_returns_nothing():
    period = "2026-01-25"
    meta = {"periods": [period], "dimensions": [{"dimension": "business"}]}
    assert compare._mix_rows("NVDA", _segment_rows(period), meta, "geographic", 8) == []


# --------------------------------------------------------------------------- #
# Live: EDGAR and Yahoo
# --------------------------------------------------------------------------- #
def test_coca_colas_peers_are_the_companies_that_say_they_compete_with_it():
    rows, meta = peers.peer_group("KO", limit=10)
    symbols = {row["symbol"] for row in rows}
    # Both name Coca-Cola in their 10-K and sit in the same industry; between
    # them they are the most stable peer claim in the market.
    assert symbols & {"PEP", "KDP", "MNST"}
    assert "KO" not in symbols
    assert meta["subject"]["sic"] == "2080"
    assert [row["score"] for row in rows] == sorted((r["score"] for r in rows), reverse=True)
    for row in rows:
        assert row["sources"] and row["why"]
        assert set(row["sources"]) <= {"classification", "registration", "filings"}
        if "filings" in row["sources"]:
            assert row["filing_url"].startswith("https://www.sec.gov/")
            assert row["mentions"] >= 1


def test_a_company_nobody_classifies_still_returns_something():
    """Every leg is allowed to fail without taking the others down."""
    rows, meta = peers.peer_group("JPM", limit=6)
    assert rows
    assert set(meta["sources"]) == {"classification", "registration", "filings"}


def test_peers_endpoint(auth_client):
    r = auth_client.get("/api/v1/equity/compare/peers?symbol=NVDA&limit=6")
    assert r.status_code == 200
    body = r.json()
    assert body["results"] and body["extra"]["subject"]["symbol"] == "NVDA"
    assert {"AMD", "INTC", "AVGO"} & {row["symbol"] for row in body["results"]}


def test_compare_table_endpoint(auth_client):
    r = auth_client.get("/api/v1/equity/compare/table?symbol=KO,PEP,KDP")
    assert r.status_code == 200
    body = r.json()
    rows, extra = body["results"], body["extra"]
    assert extra["subject"] == "KO"
    assert extra["symbols"] == ["KO", "PEP", "KDP"]
    assert {row["section"] for row in rows} <= set(extra["sections"])

    by_metric = {row["metric"]: row for row in rows}
    assert by_metric["market_cap"]["KO"] > 1e10
    assert by_metric["gross_margin"]["KO"] < 1        # a fraction, not points
    # The subject is measured against itself, so it correlates perfectly.
    assert by_metric["correlation"]["KO"] == pytest.approx(1.0)
    assert by_metric["beta_to_subject"]["KO"] == pytest.approx(1.0)
    # Every row carries how it should be read.
    assert {row["format"] for row in rows} <= {"money", "multiple", "percent", "number"}


def test_revenue_mix_endpoint(auth_client):
    r = auth_client.get("/api/v1/equity/compare/revenue_mix?symbol=NVDA,AMD")
    assert r.status_code == 200
    body = r.json()
    rows = body["results"]
    assert {row["symbol"] for row in rows} <= {"NVDA", "AMD"}
    for symbol in body["extra"]["covered"]:
        shares = [row["share"] for row in rows if row["symbol"] == symbol]
        assert sum(shares) == pytest.approx(1.0, abs=0.01)


def test_an_etf_has_no_revenue_to_split(auth_client):
    r = auth_client.get("/api/v1/equity/compare/revenue_mix?symbol=SPY,QQQ")
    assert r.status_code == 404
