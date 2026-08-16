"""Offline tests for the shared snapshot and /hedge/exposures.

Step 1 of docs/hedge-construction.md. No network: price history and live
quotes are canned. AAA's daily returns are exactly 2x the benchmark's and
BBB's exactly 0.5x, so the book's beta and its risk decomposition are known
in advance rather than asserted loosely against live data.
"""
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from backend.portfolio import analytics, hedges, narrative, snapshot

DATES = pd.bdate_range("2025-11-03", periods=200)
BENCH_RETURNS = 0.0005 + 0.012 * np.sin(0.7 * np.arange(len(DATES)))


def _price_series(start_price: float, factor: float) -> pd.Series:
    return pd.Series(start_price * np.cumprod(1.0 + factor * BENCH_RETURNS), index=DATES)


PRICES = {
    "SPY": _price_series(500.0, 1.0),
    "AAA": _price_series(100.0, 2.0),
    "BBB": _price_series(50.0, 0.5),
}


def _fake_price_panel(symbols, start, end=None):
    cols = {s.upper(): PRICES[s.upper()] for s in symbols if s.upper() in PRICES}
    panel = pd.DataFrame(cols)
    return panel[panel.index >= pd.Timestamp(start)], []


def _fake_live_quotes(symbols):
    quotes = {}
    for symbol in sorted(set(symbols)):
        series = PRICES[symbol]
        quotes[symbol] = {
            "symbol": symbol,
            "last_price": float(series.iloc[-1]),
            "prev_close": float(series.iloc[-2]),
            "change_percent": None,
            "name": symbol,
            "currency": "USD",
        }
    return quotes, []


def _fake_get_history(symbol, start=None, end=None):
    return pd.DataFrame({"close": PRICES[symbol.upper()]})


@pytest.fixture()
def offline_market(monkeypatch):
    monkeypatch.setattr(analytics, "price_panel", _fake_price_panel)
    monkeypatch.setattr(analytics, "live_quotes", _fake_live_quotes)
    monkeypatch.setattr(snapshot, "get_history", _fake_get_history)


@pytest.fixture()
def other_client(client):
    """A second signed-in user, for the ownership-isolation tests."""
    import uuid

    from fastapi.testclient import TestClient

    second = TestClient(client.app)
    username = "h_{}".format(uuid.uuid4().hex[:10])
    second.post(
        "/api/auth/register",
        json={"username": username, "email": "{}@example.com".format(username),
              "password": "secret123"},
    )
    token = second.post(
        "/api/auth/login", data={"username": username, "password": "secret123"}
    ).json()["access_token"]
    second.headers.update({"Authorization": "Bearer {}".format(token)})
    return second


@pytest.fixture()
def book(auth_client, offline_market):
    """100k deposited; ~40k into AAA (2x beta), ~40k into BBB (0.5x), 20k cash."""
    pid = auth_client.post("/api/portfolios", json={"name": "Hedged book"}).json()["id"]
    day0 = DATES[0].date().isoformat() + "T00:00:00"
    day1 = DATES[1].date().isoformat() + "T00:00:00"
    auth_client.post(
        "/api/portfolios/{}/transactions".format(pid),
        json={"side": "deposit", "quantity": 100_000, "trade_date": day0},
    )
    for symbol, quantity in (("AAA", 400), ("BBB", 800)):
        auth_client.post(
            "/api/portfolios/{}/transactions".format(pid),
            json={
                "side": "buy",
                "symbol": symbol,
                "quantity": quantity,
                "price": float(PRICES[symbol].iloc[1]),
                "trade_date": day1,
            },
        )
    return pid


# --------------------------------------------------------------------------- #
# Estimators
# --------------------------------------------------------------------------- #
def test_market_exposure_recovers_a_known_beta():
    bench = pd.Series(BENCH_RETURNS, index=DATES)
    portfolio = 1.5 * bench + 0.0001  # constant alpha must not bias the slope
    out = hedges.market_exposure(portfolio, bench, 1_000_000)
    assert out["beta"] == pytest.approx(1.5, abs=1e-6)
    assert out["beta_dollars"] == pytest.approx(1_500_000, abs=1)
    assert out["r_squared"] == pytest.approx(1.0, abs=1e-6)
    assert out["beta_ci95"][0] <= out["beta"] <= out["beta_ci95"][1]
    assert out["observations"] == len(DATES)


def test_market_exposure_is_absent_rather_than_guessed():
    bench = pd.Series(BENCH_RETURNS, index=DATES)
    assert hedges.market_exposure(bench, None, 1000.0) is None
    short = bench.iloc[:10]
    assert hedges.market_exposure(short, short, 1000.0) is None
    flat = pd.Series(0.0, index=DATES)
    assert hedges.market_exposure(bench, flat, 1000.0) is None


def test_tail_loss_matches_directly_computed_statistics():
    returns = pd.Series(BENCH_RETURNS, index=DATES)
    value, level, horizon = 100_000.0, 0.05, 21
    out = hedges.tail_loss(returns, value, var_level=level, horizon_days=horizon)

    scale = np.sqrt(horizon)
    expected_var = float(returns.quantile(level)) * scale
    tail = returns[returns <= returns.quantile(level)]
    expected_cvar = float(tail.mean()) * scale

    assert out["var_pct"] == pytest.approx(expected_var, abs=1e-6)
    assert out["cvar_pct"] == pytest.approx(expected_cvar, abs=1e-6)
    assert out["cvar_amount"] == pytest.approx(expected_cvar * value, abs=1)
    assert out["cvar_pct"] <= out["var_pct"] < 0
    low, high = out["cvar_pct_ci95"]
    assert low <= out["cvar_pct"] <= high


def test_tail_loss_is_deterministic():
    returns = pd.Series(BENCH_RETURNS, index=DATES)
    first = hedges.tail_loss(returns, 50_000.0)
    second = hedges.tail_loss(returns, 50_000.0)
    assert first == second


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #
def test_exposures_reports_all_three_targets(auth_client, book):
    body = auth_client.get("/api/portfolios/{}/hedge/exposures".format(book)).json()

    assert body["estimator_version"] == hedges.ESTIMATOR_VERSION
    assert body["benchmark"] == "SPY"
    assert body["currency"] == "USD"
    assert body["window"]["observations"] >= 190
    assert body["warnings"] == []
    assert body["source_timestamps"]["prices_through"] == body["as_of"]

    market = body["targets"]["beta_dollars"]
    # ~40% of the book at 2x beta, ~40% at 0.5x, ~20% cash -> beta near 1.0.
    assert 0.7 < market["beta"] < 1.4
    assert market["r_squared"] > 0.95
    assert market["beta_dollars"] == pytest.approx(market["beta"] * body["value"], rel=1e-3)
    assert market["beta_dollars_ci95"][0] <= market["beta_dollars"] <= market["beta_dollars_ci95"][1]

    tail = body["targets"]["tail_loss"]
    assert tail["horizon_days"] == 21
    assert tail["cvar_amount"] < 0
    assert tail["cvar_amount_ci95"][0] <= tail["cvar_amount"] <= tail["cvar_amount_ci95"][1]

    concentration = body["targets"]["single_name_concentration"]
    positions = {p["symbol"]: p for p in concentration["positions"]}
    assert set(positions) == {"AAA", "BBB"}
    # AAA carries 2x vol on equal dollars: ~80% of risk, so it is the target.
    assert positions["AAA"]["pct_of_risk"] > 0.6
    assert positions["AAA"]["dominant"] is True
    assert positions["BBB"]["dominant"] is False


def test_exposures_needs_history(auth_client, offline_market):
    pid = auth_client.post("/api/portfolios", json={"name": "Empty book"}).json()["id"]
    response = auth_client.get("/api/portfolios/{}/hedge/exposures".format(pid))
    assert response.status_code == 400


def test_risk_and_factors_unchanged_by_snapshot_refactor(auth_client, book):
    risk = auth_client.get("/api/portfolios/{}/risk".format(book)).json()
    var = risk["value_at_risk"]
    assert var["historical_pct"] < 0
    assert var["historical_amount"] == pytest.approx(
        var["historical_pct"] * risk["total_value"], rel=1e-3
    )
    contributions = risk["risk_contribution"]
    assert {c["symbol"] for c in contributions} == {"AAA", "BBB"}
    assert sum(c["pct_of_risk"] for c in contributions) == pytest.approx(1.0, abs=1e-4)

    factors = auth_client.get("/api/portfolios/{}/factors".format(book)).json()
    assert set(factors["factors"]) == {"MKT", "MOM", "LOWVOL"}
    assert {h["symbol"] for h in factors["holdings"]} == {"AAA", "BBB"}


# --------------------------------------------------------------------------- #
# POST /hedge/analyze — the whole pipeline, offline
# --------------------------------------------------------------------------- #
AS_OF = DATES[-1].date()
SPY_SPOT = float(PRICES["SPY"].iloc[-1])
FAR_EXPIRY = (AS_OF + timedelta(days=120)).isoformat()
NEAR_EXPIRY = (AS_OF + timedelta(days=20)).isoformat()
VIX_SERIES = pd.Series(20.0 - 40.0 * (PRICES["SPY"] / PRICES["SPY"].iloc[0] - 1.0), index=DATES)


def _option_row(symbol, spot, option_type, strike, expiration, iv=0.20, oi=5000, volume=800):
    """A quotable contract, priced off Black-Scholes with a 2% spread."""
    from backend.portfolio import pricing

    years = pricing.year_fraction(AS_OF, pd.Timestamp(expiration).date())
    fair = pricing.bs_price(spot, strike, years, iv, option_type=option_type)
    return {
        "option_type": option_type,
        "expiration": expiration,
        "strike": float(strike),
        "bid": round(fair * 0.99, 2),
        "ask": round(fair * 1.01, 2),
        "implied_volatility": iv,
        "volume": volume,
        "open_interest": oi,
        "last_trade_date": (AS_OF - timedelta(days=1)).isoformat(),
        "contract_symbol": "{}{}{}".format(symbol, option_type[0].upper(), int(strike)),
    }


def _fake_chain(symbol, expiration):
    """Strikes scaled to the requested underlying, so a collar on AAA is
    quoted around AAA's price rather than the index's."""
    spot = float(PRICES[symbol.upper()].iloc[-1])
    rows = []
    for moneyness in (0.75, 0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10):
        strike = round(moneyness * spot)
        rows.append(_option_row(symbol, spot, "put", strike, expiration))
        rows.append(_option_row(symbol, spot, "call", strike, expiration))
    return pd.DataFrame(rows)


@pytest.fixture()
def offline_options(monkeypatch, offline_market):
    """Chains, expirations and the vol index, all canned."""
    from backend.routers import hedge as hedge_router

    monkeypatch.setattr(hedge_router, "fetch_expirations", lambda s: [NEAR_EXPIRY, FAR_EXPIRY])
    monkeypatch.setattr(hedge_router, "fetch_chain", _fake_chain)
    monkeypatch.setattr(
        hedge_router, "fetch_vol_closes", lambda symbol, start, end: VIX_SERIES
    )
    return hedge_router


def _analyze(client, pid, **body):
    return client.post("/api/portfolios/{}/hedge/analyze".format(pid), json=body)


def test_analyze_ranks_candidates_with_the_cost_table(auth_client, book, offline_options):
    body = _analyze(auth_client, book, horizon_days=21, target_reduction_fraction=0.3).json()

    assert body["benchmark"] == "SPY"
    assert body["target"]["cvar_unhedged"] < 0
    assert body["target"]["reduction_sought"] > 0
    assert body["shocks"]["vol_symbol"] == "^VIX"
    assert body["shocks"]["independent_windows"] == body["shocks"]["windows"] // 21

    kinds = {row["kind"] for row in body["rows"]}
    assert {"protective_put", "put_spread", "short_etf"} <= kinds

    for row in body["rows"]:
        low, high = row["protection_bps_ci95"]
        assert low <= row["protection_bps"] <= high
        assert "cost_bps" in row and "upside_loss" in row
        assert "meets_target" in row
    # Candidates that reach the goal come first; within each group, cheapest
    # per unit of lower-bound protection leads.
    qualifying = [r["meets_target"] for r in body["rows"]]
    assert qualifying == sorted(qualifying, reverse=True)
    for meets in (True, False):
        ratios = [r["cost_per_unit_protection"] for r in body["rows"]
                  if r["meets_target"] is meets and r["cost_per_unit_protection"] is not None]
        assert ratios == sorted(ratios)

    assert body["verdict"]["action"] in {"hedge", "de_risk_by_selling"}
    # The tenor rule must have rejected the 20-day expiry.
    for row in body["rows"]:
        for leg in row.get("legs", []):
            assert leg["expiration"] == FAR_EXPIRY


def test_analyze_ships_a_drawable_curve_with_every_row(auth_client, book, offline_options):
    body = _analyze(auth_client, book, horizon_days=21).json()

    for row in body["rows"]:
        scenario = row["scenario"]
        assert scenario["underlying"] == row["underlying"]
        assert scenario["tail_shock"] < 0
        points = scenario["points"]
        assert [p["shock"] for p in points] == sorted(p["shock"] for p in points)
        # Every point is drawable: an exposure, a hedge, and their sum. Each
        # field rounds to cents on its own, so the sum can be two off.
        for point in points:
            assert point["hedged_pnl"] == pytest.approx(
                point["exposure_pnl"] + point["hedge_pnl"], abs=0.02
            )
        # The vol index is live in this fixture, so the paired-IV line is too.
        assert all("hedged_pnl_iv" in p for p in points)

    crash = next(p for p in body["rows"][0]["scenario"]["points"] if p["shock"] == -0.30)
    assert crash["exposure_pnl"] < 0


def test_analyze_flags_frozen_iv_when_the_vol_index_is_missing(
    auth_client, book, offline_options, monkeypatch
):
    monkeypatch.setattr(offline_options, "fetch_vol_closes", lambda symbol, start, end: None)
    body = _analyze(auth_client, book).json()

    assert body["shocks"]["vol_symbol"] is None
    assert any("understates" in w for w in body["warnings"])
    assert any("UNDERSTATED" in note for note in body["shocks"]["notes"])


def test_analyze_honours_the_instrument_filter(auth_client, book, offline_options):
    body = _analyze(auth_client, book, instruments=["short_etf"]).json()
    assert {row["kind"] for row in body["rows"]} == {"short_etf"}


def test_analyze_excludes_illiquid_candidates_with_a_reason(auth_client, book, offline_options):
    body = _analyze(auth_client, book, min_open_interest=100_000).json()
    assert all(row["kind"] == "short_etf" for row in body["rows"])
    assert any("liquidity floor" in item["reason"] for item in body["excluded"])


def test_analyze_recommends_selling_when_the_target_is_unreachable(
    auth_client, book, offline_options
):
    body = _analyze(auth_client, book, target_reduction_fraction=1.0).json()
    assert body["verdict"]["action"] == "de_risk_by_selling"
    assert "reason" in body["verdict"]


# --------------------------------------------------------------------------- #
# Lifecycle log — step 6
# --------------------------------------------------------------------------- #
def _record_body(**overrides):
    body = {
        "kind": "protective_put",
        "underlying": "SPY",
        "quantity": 4,
        "legs": [{"option_type": "put", "strike": 500.0, "expiration": "2026-12-18", "quantity": 4}],
        "quote_snapshot": {"spot": 520.0, "bid": 10.2, "ask": 10.6},
        "assumptions": {"level": 0.05, "rate": 0.0},
        "estimator_version": "exposures-v1",
        "target_exposure": {"cvar_unhedged": -22000.0},
        "expected_cvar_reduction": 5000.0,
        "expected_cvar_reduction_low": 4200.0,
        "expected_cvar_reduction_high": 5800.0,
        "cost_bps": 45.0,
        "protection_bps": 150.0,
        "portfolio_value_at_entry": 332_919.0,
        "entry_cost": 4240.0,
    }
    body.update(overrides)
    return body


def test_displaying_a_candidate_records_nothing(auth_client, book, offline_options):
    """The analyze endpoint must never write to the lifecycle log."""
    _analyze(auth_client, book)
    listed = auth_client.get("/api/portfolios/{}/hedge/records".format(book)).json()
    assert listed == []


def test_hedge_record_freezes_what_was_believed(auth_client, book):
    created = auth_client.post(
        "/api/portfolios/{}/hedge/records".format(book), json=_record_body()
    )
    assert created.status_code == 201
    record = created.json()
    assert record["state"] == "proposed"
    assert record["expected_cvar_reduction_low"] == 4200.0
    assert record["quote_snapshot"]["ask"] == 10.6
    assert record["estimator_version"] == "exposures-v1"
    assert record["executed_at"] is None

    listed = auth_client.get("/api/portfolios/{}/hedge/records".format(book)).json()
    assert [r["id"] for r in listed] == [record["id"]]


def test_hedge_needs_a_size(auth_client, book):
    response = auth_client.post(
        "/api/portfolios/{}/hedge/records".format(book),
        json=_record_body(quantity=None, notional=None),
    )
    assert response.status_code == 400


def test_lifecycle_advances_and_stamps_timestamps(auth_client, book):
    rid = auth_client.post(
        "/api/portfolios/{}/hedge/records".format(book), json=_record_body()
    ).json()["id"]
    url = "/api/portfolios/{}/hedge/records/{}".format(book, rid)

    accepted = auth_client.patch(url, json={"state": "accepted"}).json()
    assert accepted["state"] == "accepted" and accepted["executed_at"] is None

    executed = auth_client.patch(url, json={"state": "executed"}).json()
    assert executed["executed_at"] is not None and executed["closed_at"] is None

    closed = auth_client.patch(
        url, json={"state": "closed", "exit_value": 9000.0, "realised_book_pnl": -18_000.0}
    ).json()
    assert closed["closed_at"] is not None
    # Realised P&L derives from the two ends: 9000 exit - 4240 paid.
    assert closed["realised_hedge_pnl"] == pytest.approx(4760.0)


def test_illegal_transitions_are_refused(auth_client, book):
    rid = auth_client.post(
        "/api/portfolios/{}/hedge/records".format(book), json=_record_body()
    ).json()["id"]
    url = "/api/portfolios/{}/hedge/records/{}".format(book, rid)

    assert auth_client.patch(url, json={"state": "rolled"}).status_code == 400
    auth_client.patch(url, json={"state": "closed"})
    reopened = auth_client.patch(url, json={"state": "executed"})
    assert reopened.status_code == 400
    assert "cannot become" in reopened.json()["detail"]


def test_scorecard_reports_the_sample_before_the_verdict(auth_client, book):
    empty = auth_client.get("/api/portfolios/{}/hedge/scorecard".format(book)).json()
    assert empty["graded"] == 0 and "nothing to judge" in empty["note"]

    rid = auth_client.post(
        "/api/portfolios/{}/hedge/records".format(book), json=_record_body()
    ).json()["id"]
    url = "/api/portfolios/{}/hedge/records/{}".format(book, rid)
    auth_client.patch(url, json={"state": "executed"})
    auth_client.patch(
        url, json={"state": "closed", "exit_value": 9000.0, "realised_book_pnl": -18_000.0}
    )

    card = auth_client.get("/api/portfolios/{}/hedge/scorecard".format(book)).json()
    assert card["graded"] == 1
    assert card["realised_hedge_pnl"] == pytest.approx(4760.0)
    assert card["premium_paid"] == pytest.approx(4240.0)
    assert card["book_down_episodes"] == 1
    assert card["paid_when_the_book_fell"] == 1
    assert "too small a sample" in card["note"]


def test_another_user_cannot_read_or_touch_the_log(auth_client, book, other_client):
    rid = auth_client.post(
        "/api/portfolios/{}/hedge/records".format(book), json=_record_body()
    ).json()["id"]
    url = "/api/portfolios/{}/hedge/records/{}".format(book, rid)
    assert other_client.get("/api/portfolios/{}/hedge/records".format(book)).status_code == 404
    assert other_client.patch(url, json={"state": "accepted"}).status_code == 404
    assert other_client.delete(url).status_code == 404


def test_records_can_be_deleted(auth_client, book):
    rid = auth_client.post(
        "/api/portfolios/{}/hedge/records".format(book), json=_record_body()
    ).json()["id"]
    url = "/api/portfolios/{}/hedge/records/{}".format(book, rid)
    assert auth_client.delete(url).status_code == 204
    assert auth_client.get("/api/portfolios/{}/hedge/records".format(book)).json() == []


# --------------------------------------------------------------------------- #
# Narrative layer — step 8. The model explains; it never decides.
# --------------------------------------------------------------------------- #
class _Block:
    type = "tool_use"

    def __init__(self, payload):
        self.input = payload


class _Message:
    def __init__(self, payload):
        self.content = [_Block(payload)]


class _FakeClient:
    """Returns a canned narrative and records what it was asked."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Message(self.payload)


ANALYSIS = {
    "portfolio": {"name": "Book"},
    "value": 100_000.0,
    "benchmark": "SPY",
    "target": {"cvar_unhedged": -22_142.0, "reduction_sought": 11_071.0},
    "shocks": {"windows": 530, "independent_windows": 25, "horizon_days": 21,
               "period": ["2024-06-03", "2026-08-13"], "notes": ["vol moves with the market"]},
    "rows": [
        {"kind": "short_etf", "underlying": "SPY", "notional": 211_787.0, "quantity": None,
         "meets_target": False, "protection_bps": 140.0, "protection_bps_ci95": [53.0, 230.0],
         "cost_bps": 3.0, "cost_per_unit_protection": 0.02, "upside_loss": {"+10%": -21_179.0}},
    ],
    "excluded": [],
    "verdict": {"action": "de_risk_by_selling", "reason": "no candidate reaches the target"},
    "warnings": [],
}


def test_brief_carries_every_number_from_the_engine():
    brief = narrative.build_brief(ANALYSIS)
    assert "22,142" in brief and "11,071" in brief
    assert "530 overlapping windows, only 25 independent" in brief
    assert "short_etf" in brief
    assert "ENGINE VERDICT: de_risk_by_selling" in brief


def test_the_model_cannot_reverse_the_verdict():
    """The whole point of the redesign: a story may not sell you protection."""
    rogue = {
        "headline": "Buy the index hedge.",
        "recommended_action": "hedge",             # engine said sell
        "candidate_kind": "short_etf",
        "why_this_candidate": "It is cheap.",
        "what_you_give_up": "Some upside.",
        "what_stays_unprotected": "Single-name risk.",
        "sample_caution": "25 independent windows only.",
        "limits_of_this_analysis": [],
    }
    out = narrative.validate(rogue, ANALYSIS, narrative.build_brief(ANALYSIS))
    assert out["recommended_action"] == "de_risk_by_selling"
    assert any("overridden" in f for f in out["contradicted_engine"])
    # Naming a pick while the verdict is to sell reads as a recommendation.
    assert out["candidate_kind"] is None


def test_invented_instruments_are_dropped():
    payload = {
        "headline": "Use a calendar spread.",
        "recommended_action": "de_risk_by_selling",
        "candidate_kind": "calendar_spread",       # never priced
        "why_this_candidate": "Cheap vega.",
        "what_you_give_up": "Nothing.",
        "what_stays_unprotected": "Everything.",
        "sample_caution": "Small sample.",
    }
    out = narrative.validate(payload, ANALYSIS, narrative.build_brief(ANALYSIS))
    assert out["candidate_kind"] is None
    assert out["why_this_candidate"] is None
    assert any("never priced" in f for f in out["contradicted_engine"])


def test_invented_numbers_are_flagged():
    payload = {
        "headline": "The tail is $22,142 and vol is running at 38.4 percent.",
        "recommended_action": "de_risk_by_selling",
        "what_you_give_up": "Nothing.",
        "what_stays_unprotected": "Single-name risk.",
        "sample_caution": "Only 25 independent windows.",
    }
    out = narrative.validate(payload, ANALYSIS, narrative.build_brief(ANALYSIS))
    flags = " ".join(out["contradicted_engine"])
    assert "38.4" in flags                      # not in the brief
    assert "22142" not in flags                 # this one is real


def test_a_clean_narrative_passes_untouched():
    payload = {
        "headline": "Hedging does not pay here; reduce the position instead.",
        "recommended_action": "de_risk_by_selling",
        "what_you_give_up": "Upside, if you sell.",
        "what_stays_unprotected": "The single-name concentration.",
        "sample_caution": "Only 25 independent windows, so treat the range as the answer.",
        "limits_of_this_analysis": ["Earnings dates are not modelled."],
    }
    out = narrative.validate(payload, ANALYSIS, narrative.build_brief(ANALYSIS))
    assert out["contradicted_engine"] == []
    assert out["recommended_action"] == "de_risk_by_selling"


def test_run_forces_a_structured_call(monkeypatch):
    monkeypatch.setattr(narrative, "availability", lambda: {"enabled": True, "reason": ""})
    client = _FakeClient({
        "headline": "Sell instead.",
        "recommended_action": "de_risk_by_selling",
        "what_you_give_up": "-",
        "what_stays_unprotected": "-",
        "sample_caution": "-",
    })
    brief = narrative.build_brief(ANALYSIS)
    out = narrative.run(brief, ANALYSIS, client=client)
    call = client.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "hedge_narrative"}
    assert brief in call["messages"][0]["content"]
    assert out["contradicted_engine"] == []


def test_narrate_endpoint_is_off_without_a_key(auth_client, book, offline_options, monkeypatch):
    monkeypatch.setattr(
        narrative, "availability",
        lambda: {"enabled": False, "reason": "MFT_ANTHROPIC_API_KEY is not set"},
    )
    status = auth_client.get("/api/portfolios/{}/hedge/narrate/status".format(book)).json()
    assert status["enabled"] is False

    response = auth_client.post("/api/portfolios/{}/hedge/narrate".format(book), json={})
    assert response.status_code == 503
    assert "MFT_ANTHROPIC_API_KEY" in response.json()["detail"]


def test_narrate_endpoint_explains_a_real_analysis(auth_client, book, offline_options, monkeypatch):
    """The enabled path: the brief must come from this process's own analysis."""
    from backend.routers import hedge as hedge_router

    seen = {}

    def fake_run(brief, analysis, client=None):
        seen["brief"] = brief
        seen["verdict"] = analysis["verdict"]["action"]
        return narrative.validate(
            {
                "headline": "Hedging is not worth it here.",
                # Deliberately disagrees, to prove the endpoint corrects it.
                "recommended_action": "hedge",
                "candidate_kind": "protective_put",
                "why_this_candidate": "Cheapest.",
                "what_you_give_up": "Premium.",
                "what_stays_unprotected": "Single-name risk.",
                "sample_caution": "Few independent windows.",
                "limits_of_this_analysis": ["Earnings not modelled."],
            },
            analysis,
            brief,
        )

    monkeypatch.setattr(narrative, "availability", lambda: {"enabled": True, "model": "test"})
    monkeypatch.setattr(hedge_router.narrative, "run", fake_run)

    body = auth_client.post(
        "/api/portfolios/{}/hedge/narrate".format(book),
        json={"horizon_days": 21, "target_reduction_fraction": 1.0},
    ).json()

    # The analysis is genuine engine output, not anything a caller supplied.
    assert body["analysis"]["verdict"]["action"] == "de_risk_by_selling"
    assert "ENGINE VERDICT: de_risk_by_selling" in body["brief"]
    assert seen["verdict"] == "de_risk_by_selling"
    assert "CANDIDATES (engine-ranked, best first):" in seen["brief"]

    # And the model's disagreement was corrected, not published.
    told = body["narrative"]
    assert told["recommended_action"] == "de_risk_by_selling"
    assert told["candidate_kind"] is None
    assert any("overridden" in f for f in told["contradicted_engine"])
