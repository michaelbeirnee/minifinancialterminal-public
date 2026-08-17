"""Deterministic tests for the additional thesis-generation funnels."""
from __future__ import annotations

import pandas as pd

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


def test_crowded_short_screen_is_direction_neutral_context(monkeypatch):
    monkeypatch.setattr(candidates.yahoo, "predefined_screen", lambda name, limit: pd.DataFrame([{
        "symbol": "SQUEEZE",
        "shortName": "Squeeze Inc",
        "shortPercentOfFloat": 0.275,
        "averageDailyVolume3Month": 900_000,
    }]))
    memory = _capture_memory(monkeypatch)

    row = candidates.crowded_shorts(limit=10).data[0]

    assert row["short_percent"] == 0.275
    assert row["score"] == 0.275
    assert row["family"] == "high_short_interest"
    assert "direction" not in row  # triage, not the scanner, owns the posture
    assert memory["family"] == sources.CROWDED_SHORTS


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


def test_new_categories_are_registered_with_distinct_prompts():
    from backend.thesis import triage

    expected = {
        sources.UNDERVALUED_LARGE_CAPS,
        sources.UNDERVALUED_GROWTH,
        sources.CROWDED_SHORTS,
        sources.PRICE_DISLOCATIONS,
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
