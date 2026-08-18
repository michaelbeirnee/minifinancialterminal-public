"""Expandable Research Workbench context packet."""
from __future__ import annotations

import pytest

from backend.core.models import MFTObject
from backend.research import context


def _obj(results, provider="test", warnings=None):
    return MFTObject(results=results, provider=provider, warnings=warnings or [])


def _runner(path, **params):
    payloads = {
        "/equity/profile": {
            "symbol": "ACME", "name": "Acme Systems", "sector": "Technology",
            "industry": "Software",
        },
        "/overview/brief": {
            "regime": "RISK-ON",
            "signals": [{"label": "Regime", "value": "RISK-ON", "tone": "pos"}],
        },
        "/thesis/sector_rotation": [{
            "symbol": "XLK", "sector": "Technology",
            "relative_three_month": 0.08,
        }],
        "/equity/price/performance": [
            {"symbol": "ACME", "three_month": 0.20},
            {"symbol": "SPY", "three_month": 0.05},
            {"symbol": "XLK", "three_month": 0.12},
        ],
        "/equity/fundamental/metrics": [{
            "symbol": "ACME", "revenue_growth": 0.15, "earnings_growth": 0.20,
            "operating_margin": 0.18, "free_cash_flow": 1_000_000,
            "forward_pe": 30.0,
        }],
        "/equity/estimates/consensus": [{
            "symbol": "ACME", "recommendation": "buy", "analyst_count": 12,
        }],
    }
    return _obj(payloads[path])


def test_context_keeps_lanes_separate_and_builds_alignment():
    packet = context.build_context("acme", runner=_runner)

    assert packet["schema"] == "research_context.v1"
    assert packet["subject"] == {
        "symbol": "ACME", "name": "Acme Systems", "sector": "Technology",
        "industry": "Software", "benchmark": "SPY", "sector_etf": "XLK",
    }
    assert packet["top_down"]["state"] == "constructive"
    assert packet["top_down"]["relative_return"] == pytest.approx(0.15)
    assert packet["bottom_up"]["state"] == "constructive"
    assert packet["assessment"]["alignment"]["key"] == "aligned_constructive"
    assert packet["assessment"]["coverage"] == {
        "successful": 6, "total": 6, "ratio": 1.0,
    }
    assert {source["lane"] for source in packet["sources"]} == {
        "top_down", "bottom_up",
    }
    assert packet["exposure_bridge"]["status"] == "needs_exposure_proof"
    assert "real yields and valuation duration" in packet["exposure_bridge"]["driver_prompts"]


def test_context_degrades_one_lane_without_losing_the_packet():
    def partial(path, **params):
        if path in ("/equity/fundamental/metrics", "/equity/estimates/consensus"):
            raise RuntimeError("vendor unavailable")
        return _runner(path, **params)

    packet = context.build_context("ACME", runner=partial)

    assert packet["top_down"]["state"] == "constructive"
    assert packet["bottom_up"]["state"] == "unknown"
    assert packet["assessment"]["alignment"]["key"] == "incomplete"
    assert packet["assessment"]["coverage"]["successful"] == 4
    assert len(packet["warnings"]) == 2
    assert all("vendor unavailable" in warning for warning in packet["warnings"])


def test_sector_relative_read_falls_back_to_the_performance_packet():
    def no_rotation(path, **params):
        if path == "/thesis/sector_rotation":
            raise RuntimeError("sector scan unavailable")
        return _runner(path, **params)

    packet = context.build_context("ACME", runner=no_rotation)

    assert packet["top_down"]["sector_relative"] == pytest.approx(0.07)
    assert any("sector scan unavailable" in warning for warning in packet["warnings"])


def test_financials_use_their_sector_specific_framework():
    def financial_runner(path, **params):
        if path == "/equity/profile":
            return _obj({"symbol": "BANK", "sector": "Financial Services"})
        if path == "/thesis/sector_rotation":
            return _obj([])
        if path == "/equity/price/performance":
            return _obj([])
        if path == "/overview/brief":
            return _obj({"regime": "MIXED", "signals": []})
        if path == "/equity/fundamental/metrics":
            return _obj([{"symbol": "BANK"}])
        return _obj([{"symbol": "BANK"}])

    packet = context.build_context("BANK", runner=financial_runner)

    assert packet["subject"]["sector_etf"] == "XLF"
    assert "ROTCE and tangible book" in packet["bottom_up"]["framework"]["focus"]
    assert "P/TBV versus sustainable ROTCE" in packet["bottom_up"]["framework"]["valuation"]


def test_context_validates_subject_and_horizon():
    with pytest.raises(ValueError, match="must be different"):
        context.build_context("SPY", benchmark="SPY", runner=_runner)
    with pytest.raises(ValueError, match="horizon"):
        context.build_context("ACME", horizon="five_year", runner=_runner)


def test_research_context_is_a_platform_command(auth_client, monkeypatch):
    from backend.extensions import research

    monkeypatch.setattr(
        research.research_context,
        "build_context",
        lambda symbol, benchmark, horizon: {
            "schema": "research_context.v1", "subject": {"symbol": symbol},
            "warnings": [], "settings": {"benchmark": benchmark, "horizon": horizon},
        },
    )
    response = auth_client.get(
        "/api/v1/research/context?symbol=ACME&benchmark=QQQ&horizon=one_year"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["results"]["subject"]["symbol"] == "ACME"
    assert body["results"]["settings"] == {"benchmark": "QQQ", "horizon": "one_year"}
    assert body["provider"] == "mft"
