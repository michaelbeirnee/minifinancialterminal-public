"""Deterministic tests for the additional thesis-generation funnels."""
from __future__ import annotations

import pandas as pd
import pytest

from backend.core.models import Result
from backend.extensions import thesis_candidates as candidates
from backend.thesis import sources


def _capture_memory(monkeypatch):
    captured = {}

    def record_events(**kwargs):
        captured.update(kwargs)
        return len(kwargs["rows"])

    from backend.thesis import memory
    monkeypatch.setattr(memory, "record_events", record_events)
    return captured


def test_value_screen_normalises_provider_fields_and_records(monkeypatch):
    frame = pd.DataFrame([{
        "symbol": "abc",
        "longName": "ABC Holdings",
        "regularMarketPrice": {"raw": 42.5},
        "marketCap": 25_000_000_000,
        "trailingPE": 12.0,
        "forwardPE": 10.5,
        "pegRatio": 0.7,
        # Yahoo has emitted growth both as a fraction and as a percent.
        "earningsGrowth": 35.0,
        "revenueGrowth": 0.18,
    }])
    seen = {}

    def screen(name, limit):
        seen.update(name=name, limit=limit)
        return frame

    monkeypatch.setattr(candidates.yahoo, "predefined_screen", screen)
    memory = _capture_memory(monkeypatch)

    result = candidates.undervalued_large_caps(limit=200)
    row = result.data[0]

    assert seen == {"name": "undervalued_large_caps", "limit": 50}
    assert row["symbol"] == "ABC" and row["issuer"] == "ABC Holdings"
    assert row["last_price"] == 42.5 and row["market_cap"] == 25_000_000_000
    assert row["eps_growth"] == 0.35 and row["revenue_growth"] == 0.18
    assert row["family"] == "large_cap_value"
    assert memory["family"] == sources.UNDERVALUED_LARGE_CAPS
    assert memory["rows"][0]["known_on"] == result.extra["as_of"]


def _fake_screen(monkeypatch, frame, seen=None):
    """Stand in for the custom screen, recording how it was called."""
    def screen(filters, limit, sort_field, sort_asc=False):
        if seen is not None:
            seen.update(filters=filters, limit=limit, sort_field=sort_field,
                        sort_asc=sort_asc)
        return frame

    monkeypatch.setattr(candidates.yahoo, "equity_screen", screen)


def _fake_profiles(monkeypatch, profiles):
    """Stand in for the company profile the screen response cannot supply."""
    monkeypatch.setattr(candidates.yahoo, "info",
                        lambda symbol: dict(profiles.get(symbol, {})))


def test_high_growth_uses_explicit_cross_sector_gates_and_records(monkeypatch):
    frame = pd.DataFrame([{
        "symbol": "grow",
        "shortName": "Grow Co",
        "regularMarketPrice": 75.0,
        "marketCap": 12_000_000_000,
        "trailingPE": 45.0,
        "forwardPE": 35.0,
    }])
    seen = {}
    _fake_screen(monkeypatch, frame, seen)
    # The screen is gated on growth but answers with a quote payload that does
    # not contain it, so the numbers on the card come from the profile.
    _fake_profiles(monkeypatch, {"GROW": {
        "revenueGrowth": 0.40, "earningsGrowth": 0.35, "sector": "Technology",
    }})
    memory = _capture_memory(monkeypatch)

    result = candidates.high_growth(
        min_revenue_growth_pct=30,
        min_eps_growth_pct=25,
        min_market_cap_bn=5,
        limit=80,
    )
    row = result.data[0]

    assert seen["limit"] == 50
    assert seen["sort_field"] == "quarterlyrevenuegrowth.quarterly"
    assert ["gte", "quarterlyrevenuegrowth.quarterly", 30.0] in seen["filters"]
    assert ["gte", "epsgrowth.lasttwelvemonths", 25.0] in seen["filters"]
    assert ["gte", "intradaymarketcap", 5_000_000_000.0] in seen["filters"]
    assert ["is-in", "exchange", "NMS", "NYQ"] in seen["filters"]
    assert row["symbol"] == "GROW" and row["family"] == "high_growth"
    assert row["revenue_growth"] == pytest.approx(0.40)
    assert row["eps_growth"] == pytest.approx(0.35)
    assert row["sector"] == "Technology"
    assert "direction" not in row
    assert memory["family"] == sources.HIGH_GROWTH
    assert result.extra["gate"]["min_market_cap_bn"] == 5.0
    assert "not a recommendation" in result.warnings[0]


def test_growth_score_survives_a_screen_that_answers_with_nothing(monkeypatch):
    """The regression that started this: a card whose one number is missing.

    Yahoo's screener never echoes the fields it was filtered on. When the
    profile cannot supply them either, the growth terms are zero and the score
    must collapse to something visibly weak rather than to an inverted copy of
    the provider's own ranking.
    """
    frame = pd.DataFrame([
        {"symbol": "one", "shortName": "One"},
        {"symbol": "two", "shortName": "Two"},
    ])
    _fake_screen(monkeypatch, frame)
    _fake_profiles(monkeypatch, {})
    _capture_memory(monkeypatch)

    rows = candidates.high_growth(limit=2).data

    assert [row["symbol"] for row in rows] == ["ONE", "TWO"]
    assert all(row["revenue_growth"] is None for row in rows)
    assert all(row["score"] < 0.02 for row in rows), rows


def test_screen_rows_are_hydrated_only_where_the_screen_fell_short(monkeypatch):
    """A profile request per symbol is not free, so it is not made twice."""
    frame = pd.DataFrame([{
        "symbol": "full", "longName": "Full Co", "trailingPE": 11.0,
        "marketCap": 3_000_000_000, "regularMarketPrice": 20.0,
    }])
    _fake_screen(monkeypatch, frame)
    asked = []

    def info(symbol):
        asked.append(symbol)
        return {"revenueGrowth": 0.1, "earningsGrowth": 0.2, "trailingPegRatio": 1.4,
                "sector": "Industrials"}

    monkeypatch.setattr(candidates.yahoo, "info", info)
    _capture_memory(monkeypatch)

    row = candidates.high_growth(limit=1).data[0]

    assert asked == ["FULL"]           # once, not once per missing field
    assert row["pe_ratio"] == 11.0     # the screen's own answer is not overwritten
    assert row["peg_ratio"] == 1.4     # and the gaps are filled


def test_a_dead_profile_leaves_the_row_thin_rather_than_wrong(monkeypatch):
    frame = pd.DataFrame([{"symbol": "dead", "shortName": "Dead Co",
                           "marketCap": 4_000_000_000}])
    _fake_screen(monkeypatch, frame)

    def explode(symbol):
        raise RuntimeError("Yahoo said no")

    monkeypatch.setattr(candidates.yahoo, "info", explode)
    _capture_memory(monkeypatch)

    row = candidates.high_growth(limit=1).data[0]

    assert row["symbol"] == "DEAD"
    assert row["market_cap"] == 4_000_000_000
    assert row["revenue_growth"] is None


def test_one_issuer_gets_one_row(monkeypatch):
    """Preferred series and share classes are listings, not separate candidates.

    They arrive with the same issuer and the same fundamentals, and each one
    would become an anomaly card the model reads as independent corroboration.
    """
    frame = pd.DataFrame([
        {"symbol": "NLY", "longName": "Annaly Capital Management, Inc.",
         "marketCap": 17_000_000_000},
        {"symbol": "NLY-PF", "longName": "Annaly Capital Management, Inc."},
        {"symbol": "NLY-PG", "longName": "annaly capital management, inc."},
        {"symbol": "OTHER", "longName": "Other Corp"},
    ])
    _fake_screen(monkeypatch, frame)
    _fake_profiles(monkeypatch, {})
    _capture_memory(monkeypatch)

    rows = candidates.high_growth(limit=10).data

    assert [row["symbol"] for row in rows] == ["NLY", "OTHER"]


def test_a_missing_issuer_name_does_not_become_the_string_nan(monkeypatch):
    """A DataFrame column one row lacks arrives as NaN, which is not ``""``."""
    frame = pd.DataFrame([
        {"symbol": "named", "longName": "Named Co"},
        {"symbol": "blank"},
    ])
    _fake_screen(monkeypatch, frame)
    _fake_profiles(monkeypatch, {})
    _capture_memory(monkeypatch)

    issuers = {row["symbol"]: row["issuer"] for row in candidates.high_growth(limit=5).data}

    assert issuers["NAMED"] == "Named Co"
    assert issuers["BLANK"] == "BLANK"  # falls back to the symbol, not to "nan"


def test_crowded_shorts_gates_on_liquidity_and_computes_its_own_float_share(monkeypatch):
    """The reported percentage contradicts Yahoo's own share counts; counts win."""
    frame = pd.DataFrame([{
        "symbol": "squeeze", "shortName": "Squeeze Inc",
        "regularMarketPrice": 12.0, "marketCap": 900_000_000,
        "averageDailyVolume3Month": 900_000,
    }])
    seen = {}
    _fake_screen(monkeypatch, frame, seen)
    _fake_profiles(monkeypatch, {"SQUEEZE": {
        # 301% of float is not a fact about the world; the counts say 27.5%.
        "shortPercentOfFloat": 3.0143,
        "sharesShort": 2_750_000, "floatShares": 10_000_000,
        "shortRatio": 12.0, "sector": "Consumer Cyclical",
    }})
    memory = _capture_memory(monkeypatch)

    row = candidates.crowded_shorts(limit=10).data[0]

    assert ["gte", "short_percentage_of_float.value", 15.0] in seen["filters"]
    assert ["gte", "avgdailyvol3m", 500_000.0] in seen["filters"]
    assert row["short_percent"] == pytest.approx(0.275)
    assert row["days_to_cover"] == 12.0
    assert row["family"] == "hard_to_cover_short"   # 12 days is not a quick exit
    assert "direction" not in row  # triage, not the scanner, owns the posture
    assert memory["family"] == sources.CROWDED_SHORTS


def test_an_impossible_short_percentage_is_dropped_not_published(monkeypatch):
    frame = pd.DataFrame([{"symbol": "wild", "shortName": "Wild Co"}])
    _fake_screen(monkeypatch, frame)
    _fake_profiles(monkeypatch, {"WILD": {"shortPercentOfFloat": 3.0143}})
    _capture_memory(monkeypatch)

    row = candidates.crowded_shorts(limit=1).data[0]

    assert row["short_percent"] is None
    assert row["score"] < 0.02


def test_free_cash_flow_yield_refuses_incomparable_units(monkeypatch):
    """Three rows, one real yield.

    A US listing may report its accounts in another currency, and free cash
    flow cannot exceed operating cash flow. Both produce a confident number
    rather than an error, and both must sink rather than top a cash screen.
    """
    frame = pd.DataFrame([
        {"symbol": "usd", "longName": "Dollar Co"},
        {"symbol": "ars", "longName": "Peso Co"},
        {"symbol": "odd", "longName": "Incoherent Co"},
    ])
    _fake_screen(monkeypatch, frame)
    _fake_profiles(monkeypatch, {
        "USD": {"freeCashflow": 1e9, "operatingCashflow": 2e9,
                "marketCap": 1e10, "currency": "USD", "financialCurrency": "USD"},
        "ARS": {"freeCashflow": 1.2e12, "operatingCashflow": 1.1e13,
                "marketCap": 2e10, "currency": "USD", "financialCurrency": "ARS"},
        "ODD": {"freeCashflow": 3.4e10, "operatingCashflow": -3.5e8,
                "marketCap": 1.2e10, "currency": "USD", "financialCurrency": "USD"},
    })
    _capture_memory(monkeypatch)

    rows = {row["symbol"]: row for row in candidates.cash_generative(limit=5).data}

    assert rows["USD"]["fcf_yield"] == pytest.approx(0.10)
    assert rows["ARS"]["fcf_yield"] is None
    assert rows["ODD"]["fcf_yield"] is None
    # The one row whose arithmetic holds is the one that ranks.
    assert candidates.cash_generative(limit=5).data[0]["symbol"] == "USD"


def test_momentum_applies_its_proximity_gate_after_the_screen(monkeypatch):
    """Yahoo gates on the annual change but not on distance from the high."""
    frame = pd.DataFrame([
        {"symbol": "near", "shortName": "Near High", "fiftyTwoWeekChangePercent": 120.0,
         "fiftyTwoWeekHighChangePercent": -0.03, "twoHundredDayAverageChangePercent": 0.4},
        {"symbol": "faded", "shortName": "Faded", "fiftyTwoWeekChangePercent": 300.0,
         "fiftyTwoWeekHighChangePercent": -0.45},
    ])
    _fake_screen(monkeypatch, frame)
    _fake_profiles(monkeypatch, {})
    _capture_memory(monkeypatch)

    rows = candidates.momentum_leaders(max_high_distance_pct=15, limit=10).data

    assert [row["symbol"] for row in rows] == ["NEAR"]
    # The annual change arrives in percent while every distance is a fraction.
    assert rows[0]["one_year_change"] == pytest.approx(1.20)
    assert rows[0]["family"] == "extended_uptrend"


def test_the_signal_log_keeps_whatever_a_funnel_measured(monkeypatch):
    """The payload was an allowlist, which drops a new source's whole point."""
    frame = pd.DataFrame([{"symbol": "q", "longName": "Quality Co",
                           "marketCap": 9e9}])
    _fake_screen(monkeypatch, frame)
    _fake_profiles(monkeypatch, {"Q": {
        "returnOnEquity": 0.31, "grossMargins": 0.62, "operatingMargins": 0.28,
        "debtToEquity": 22.5, "sector": "Technology",
    }})
    memory = _capture_memory(monkeypatch)

    row = candidates.quality_compounders(limit=1).data[0]
    payload = memory["rows"][0]["payload"]

    assert row["family"] == "capital_efficient"      # debt/equity 0.225x
    assert row["debt_to_equity"] == pytest.approx(0.225)
    assert payload["return_on_equity"] == pytest.approx(0.31)
    assert payload["gross_margin"] == pytest.approx(0.62)
    assert payload["sector"] == "Technology"
    # Structural keys stay in their own columns rather than the payload.
    assert "symbol" not in payload and "score" not in payload


def test_price_dislocation_reuses_screener_and_records_as_of(monkeypatch):
    raw = [{
        "symbol": "DROP",
        "name": "Drop Corp",
        "one_month": -0.23,
        "three_month": -0.31,
        "market_cap": 8_000_000_000,
        "rsi14": 24.0,
        "ma50_dist": -0.16,
        "ma200_dist": -0.28,
    }]
    called = {}

    def run_screen(**kwargs):
        called.update(kwargs)
        return Result(raw, provider="yahoo", warnings=["upstream"],
                      extra={"as_of": "2026-08-14", "benchmark": "SPY"})

    from backend.extensions import screener
    monkeypatch.setattr(screener, "screener_run", run_screen)
    memory = _capture_memory(monkeypatch)

    result = candidates.price_dislocations(
        index="sp500", min_drop_pct=15, mcap_min=5, limit=8
    )
    row = result.data[0]

    assert called["direction"] == "down" and called["ascending"] is True
    assert called["timeframe"] == "one_month" and called["min_move"] == 15
    assert row["issuer"] == "Drop Corp" and row["score"] == 0.23
    assert memory["family"] == sources.PRICE_DISLOCATIONS
    assert memory["rows"][0]["known_on"] == "2026-08-14"
    assert result.warnings[0] == "upstream"


def test_sector_rotation_ranks_relative_leaders_and_laggards(monkeypatch):
    rows = [
        {"group": "Technology", "symbol": "XLK", "one_month": 0.04,
         "three_month": 0.12, "ytd": 0.18, "one_year": 0.24},
        {"group": "Utilities", "symbol": "XLU", "one_month": -0.01,
         "three_month": -0.02, "ytd": 0.01, "one_year": 0.03},
    ]
    from backend.extensions import equity
    monkeypatch.setattr(
        equity, "compare_groups",
        lambda **kwargs: Result(rows, provider="yahoo", warnings=["one stale row"]),
    )
    monkeypatch.setattr(
        candidates.yahoo, "history", lambda *args, **kwargs: pd.DataFrame({"close": [1.0]})
    )
    monkeypatch.setattr(candidates, "pct_change_table", lambda prices: {
        "one_month": 0.01, "three_month": 0.02, "ytd": 0.04, "one_year": 0.06,
    })
    memory = _capture_memory(monkeypatch)

    result = candidates.sector_rotation(min_relative_pct=2.0, limit=11)

    assert [row["symbol"] for row in result.data] == ["XLK", "XLU"]
    assert result.data[0]["family"] == "relative_leader"
    assert result.data[0]["relative_three_month"] == pytest.approx(0.10)
    assert result.data[1]["family"] == "relative_laggard"
    assert result.data[1]["relative_three_month"] == pytest.approx(-0.04)
    assert result.extra["benchmark"] == "SPY"
    assert result.warnings[0] == "one stale row"
    assert "cap-weighted" in result.warnings[-1]
    assert memory["family"] == sources.SECTOR_ROTATION
    assert memory["rows"][0]["payload"]["sector"] == "Technology"


def test_estimate_revisions_gates_on_movement_not_on_level(monkeypatch):
    """The one funnel that screens a change rather than a level.

    Yahoo has no revision filter, so the universe is screened on size and the
    gate is applied here — which also means a name whose estimates cannot be
    read has to be dropped rather than scored as though it were quiet.
    """
    frame = pd.DataFrame([
        {"symbol": "mover", "longName": "Mover Inc", "marketCap": 2e10},
        {"symbol": "quiet", "longName": "Quiet Corp", "marketCap": 3e10},
        {"symbol": "thin", "longName": "Thinly Covered", "marketCap": 4e10},
        {"symbol": "dark", "longName": "No Coverage", "marketCap": 5e10},
    ])
    _fake_screen(monkeypatch, frame)
    _fake_profiles(monkeypatch, {"MOVER": {"sector": "Technology"}})

    tables = {
        "MOVER": {
            "eps_revisions": {"upLast30days": 18.0, "downLast30days": 2.0},
            "eps_trend": {"current": 12.0, "30daysAgo": 11.0, "90daysAgo": 10.0},
            "earnings": {"numberOfAnalysts": 20.0},
        },
        # Estimates barely moved: below both gates.
        "QUIET": {
            "eps_revisions": {"upLast30days": 1.0, "downLast30days": 0.0},
            "eps_trend": {"current": 5.0, "30daysAgo": 5.0, "90daysAgo": 4.99},
            "earnings": {"numberOfAnalysts": 25.0},
        },
        # Moving hard, but on two desks — which is what min_analysts is for.
        "THIN": {
            "eps_revisions": {"upLast30days": 3.0, "downLast30days": 0.0},
            "eps_trend": {"current": 8.0, "30daysAgo": 7.0, "90daysAgo": 6.0},
            "earnings": {"numberOfAnalysts": 2.0},
        },
    }

    def estimates(symbol, kind):
        rows = tables.get(symbol, {}).get(kind)
        if rows is None:
            raise RuntimeError("no estimates for {}".format(symbol))
        return pd.DataFrame([rows], index=["+1y"])

    monkeypatch.setattr(candidates.yahoo, "estimates", estimates)
    memory = _capture_memory(monkeypatch)

    rows = candidates.estimate_revisions(limit=10).data

    assert [row["symbol"] for row in rows] == ["MOVER"]
    row = rows[0]
    assert row["up_30d"] == 18 and row["down_30d"] == 2   # tallies, not floats
    assert row["net_revisions"] == 16
    assert row["analyst_count"] == 20
    assert row["revision_breadth"] == pytest.approx(0.8)
    assert row["eps_drift_90d"] == pytest.approx(0.2)
    assert row["family"] == "revisions_up"
    assert memory["family"] == sources.ESTIMATE_REVISIONS


def test_revisions_that_disagree_with_the_consensus_are_marked_mixed(monkeypatch):
    """Desks moving one way while the number moves the other is the live case."""
    _fake_screen(monkeypatch, pd.DataFrame([
        {"symbol": "split", "longName": "Split Signal", "marketCap": 2e10}]))
    _fake_profiles(monkeypatch, {})

    tables = {
        "eps_revisions": {"upLast30days": 12.0, "downLast30days": 0.0},
        "eps_trend": {"current": 9.0, "30daysAgo": 9.5, "90daysAgo": 10.0},
        "earnings": {"numberOfAnalysts": 15.0},
    }
    monkeypatch.setattr(candidates.yahoo, "estimates",
                        lambda symbol, kind: pd.DataFrame([tables[kind]], index=["+1y"]))
    _capture_memory(monkeypatch)

    row = candidates.estimate_revisions(limit=5).data[0]

    assert row["net_revisions"] == 12          # every desk revised up
    assert row["eps_drift_90d"] == pytest.approx(-0.1)  # the number went down
    assert row["family"] == "revisions_mixed"


def test_new_categories_are_registered_with_distinct_prompts():
    from backend.thesis import triage

    expected = {
        sources.UNDERVALUED_LARGE_CAPS,
        sources.UNDERVALUED_GROWTH,
        sources.HIGH_GROWTH,
        sources.QUALITY_COMPOUNDERS,
        sources.CASH_GENERATIVE,
        sources.MARGIN_EXPANSION,
        sources.BALANCE_SHEET_STRESS,
        sources.MOMENTUM_LEADERS,
        sources.DIVIDEND_GROWERS,
        sources.ESTIMATE_REVISIONS,
        sources.CROWDED_SHORTS,
        sources.PRICE_DISLOCATIONS,
        sources.SECTOR_ROTATION,
    }
    assert expected <= set(sources.names())
    prompts = [triage.system_prompt(sources.get(name)) for name in expected]
    assert len(set(prompts)) == len(expected)


def test_direction_is_normalised_in_triage_and_deep_dive():
    from backend.thesis import deepdive, triage

    triaged = triage.validate({"candidates": [{
        "symbol": "abc", "promote": True, "confidence": "medium",
        "direction": "SHORT", "reason": "crowding plus a testable catalyst",
    }]}, ["ABC"])
    assert triaged["candidates"][0]["direction"] == "short"

    dossier = deepdive.validate_dossier({"direction": "sideways", "legs": []})
    assert dossier["direction"] == "neutral"


def test_drafted_thesis_keeps_category_and_short_direction(auth_client, monkeypatch):
    from backend.thesis import deepdive, triage

    monkeypatch.setattr(triage, "availability",
                        lambda: {"enabled": True, "reason": None})
    monkeypatch.setattr(deepdive, "run", lambda candidate: {
        "proceed": True,
        "confidence": "medium",
        "direction": "short",
        "claim": "Margins fall below the current run rate",
        "summary": "Verified enough for a review draft.",
        "legs": [],
    })

    response = auth_client.post(
        "/api/theses/deepdive?create_draft=true",
        json={
            "symbol": "SHORT",
            "direction": "short",
            "idea_source": sources.CROWDED_SHORTS,
            "legs": [],
        },
    )
    assert response.status_code == 200
    thesis = auth_client.get(
        "/api/theses/{}".format(response.json()["draft_thesis_id"])
    ).json()
    assert thesis["direction"] == "short"
    assert thesis["source"] == sources.CROWDED_SHORTS

    spoofed = auth_client.post(
        "/api/theses/deepdive?create_draft=true",
        json={"symbol": "SAFE", "idea_source": "made_up_category", "legs": []},
    ).json()
    fallback = auth_client.get(
        "/api/theses/{}".format(spoofed["draft_thesis_id"])
    ).json()
    assert fallback["source"] == "deep_dive"


def test_installing_a_falsifier_still_logs_the_candidate_that_produced_it(
        auth_client, monkeypatch):
    """A deep dive that installs a check must still reach the graded log.

    The loop that builds each ``ThesisCheck`` once bound it to ``candidate``,
    which is also the name of this endpoint's request body — so from the first
    installed falsifier onward the memory write received an ORM object instead
    of the triage candidate. ``record_deepdive`` catches and logs its own
    failures, so nothing surfaced: the most expensive artifact the engine
    produces simply stopped being recorded, and only when it succeeded.
    """
    from backend.thesis import deepdive, memory, triage

    monkeypatch.setattr(triage, "availability",
                        lambda: {"enabled": True, "reason": None})
    monkeypatch.setattr(deepdive, "run", lambda candidate: {
        "proceed": True,
        "confidence": "medium",
        "direction": "long",
        "claim": "The order book converts to revenue by the next report",
        "summary": "Verified enough for a review draft.",
        "legs": [{
            "claim": "Backlog is growing",
            "evidence": [],
            "falsifiers": [{
                "name": "Price breaks the thesis floor",
                "path": "/equity/price/quote",
                "params": {"symbol": "AAPL"},
                "field": "last_price",
                "comparator": "lt",
                "threshold": 1.0,
            }],
        }],
    })

    recorded = {}
    real_record = memory.record_deepdive

    def record(user_id, model, symbol, candidate, dossier, draft_thesis_id):
        recorded.update(symbol=symbol, candidate=candidate,
                        draft_thesis_id=draft_thesis_id)
        return real_record(user_id, model, symbol, candidate, dossier,
                           draft_thesis_id)

    monkeypatch.setattr(memory, "record_deepdive", record)

    body = {"symbol": "AAPL", "direction": "long",
            "idea_source": sources.HIGH_GROWTH, "legs": []}
    response = auth_client.post("/api/theses/deepdive?create_draft=true", json=body)

    assert response.status_code == 200
    assert response.json()["checks_installed"] == 1
    # The dict the caller sent, not whatever the falsifier loop was holding.
    assert isinstance(recorded["candidate"], dict)
    assert recorded["candidate"]["symbol"] == "AAPL"
    assert recorded["draft_thesis_id"] == response.json()["draft_thesis_id"]
