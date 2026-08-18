"""Deterministic tests for propagation along disclosed links.

Nothing here touches the network. The three inputs the funnel is built on — a
filer's segment table, the concentration sentences the miner recovers, and the
sell side's next-year consensus — are supplied as fixtures, so what is being
tested is the reasoning across them: which segment counts as a shock, which
edges carry a magnitude, and which counterparties have not been repriced.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.core.errors import EmptyDataError
from backend.core.models import Result
from backend.extensions import thesis_propagation as funnel
from backend.thesis import propagation, sources

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
#: Eight quarter ends, newest first — enough for two year-over-year comparisons.
PERIODS = ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30",
           "2025-06-30", "2025-03-31", "2024-12-31", "2024-09-30"]


def _segment_row(name, dimension, share, values, derived=False):
    row = {"dimension": dimension, "section": dimension, "segment": name,
           "derived": derived, "weight": "", "revenue_share": share}
    row.update(dict(zip(PERIODS, values)))
    return row


def _segment_filing(rows, periods=None):
    return rows, {"periods": list(periods or PERIODS), "symbol": "HUB",
                  "period": "quarter", "warnings": []}


def _fake_segments(monkeypatch, payload):
    monkeypatch.setattr(propagation.segments, "revenue_segments",
                        lambda symbol, period="annual", limit=8, dimension="all": payload)


def _estimate_tables(current, ago_90, up=0, down=0, analysts=6):
    """The three Yahoo estimate tables for one symbol, at the ``+1y`` horizon."""
    return {
        "eps_trend": pd.DataFrame(
            {"current": [current], "30daysAgo": [current], "90daysAgo": [ago_90]},
            index=["+1y"]),
        "eps_revisions": pd.DataFrame(
            {"upLast30days": [up], "downLast30days": [down]}, index=["+1y"]),
        "earnings": pd.DataFrame({"numberOfAnalysts": [analysts]}, index=["+1y"]),
    }


def _fake_estimates(monkeypatch, by_symbol):
    def estimates(symbol, kind="earnings"):
        if symbol not in by_symbol:
            raise RuntimeError("no coverage for {}".format(symbol))
        return by_symbol[symbol][kind]

    monkeypatch.setattr(propagation.yahoo, "estimates", estimates)


def _edge(symbol, company, relationship, pct, basis="net sales", quote="…"):
    return {"relationship": relationship, "symbol": symbol, "company": company,
            "exposure_pct": pct, "exposure_basis": basis, "pct_of": symbol,
            "disclosed_by": "counterparty", "quote": quote, "form": "10-K",
            "filing_date": "2026-02-19", "filing_url": "https://sec.gov/x",
            "cik": "1", "disclosures": 2}


def _fake_counterparties(monkeypatch, by_hub):
    def counterparties(symbol, years=4, max_candidates=40, limit=15):
        if symbol not in by_hub:
            raise EmptyDataError("nobody names {}".format(symbol))
        return pd.DataFrame(by_hub[symbol])

    monkeypatch.setattr(propagation.supplychain, "counterparties", counterparties)


def _fake_market(monkeypatch, moves=None, profiles=None):
    """Prices and profiles, through the real helpers the funnel calls."""
    from backend.extensions import equity

    def performance(symbol, provider=None):
        wanted = [s.strip().upper() for s in str(symbol).split(",")]
        return Result([{"symbol": s, **(moves or {}).get(s, {})} for s in wanted])

    monkeypatch.setattr(equity, "price_performance", performance)
    monkeypatch.setattr(funnel.yahoo, "info",
                        lambda symbol: dict((profiles or {}).get(symbol, {})))


def _capture_memory(monkeypatch):
    captured = {}

    def record_events(**kwargs):
        captured.update(kwargs)
        return len(kwargs["rows"])

    from backend.thesis import memory
    monkeypatch.setattr(memory, "record_events", record_events)
    return captured


# --------------------------------------------------------------------------- #
# Segment trend
# --------------------------------------------------------------------------- #
def test_segment_trend_picks_the_largest_contracting_segment(monkeypatch):
    _fake_segments(monkeypatch, _segment_filing([
        # Two quarters of year-over-year decline: -10% after -2%.
        _segment_row("Data Center", "business", 0.55,
                     [90, 98, 105, 104, 100, 100, 100, 100]),
        _segment_row("Gaming", "business", 0.30,
                     [130, 125, 120, 115, 100, 100, 100, 100]),
        _segment_row("Total disclosed", "business", 0.85,
                     [220, 223, 225, 219, 200, 200, 200, 200], derived=True),
    ]))

    trend = propagation.segment_trend("HUB")

    assert trend["segment"] == "Data Center"
    assert trend["trend"] == "contracting" and trend["direction"] == "down"
    assert trend["yoy_latest"] == pytest.approx(-0.10)
    assert trend["yoy_prior"] == pytest.approx(-0.02)
    # A ten-point fall against the twenty-point full-scale swing.
    assert trend["magnitude"] == pytest.approx(0.5)
    assert trend["share"] == 0.55 and trend["period"] == "2026-06-30"


def test_segment_trend_ignores_an_immaterial_segment(monkeypatch):
    _fake_segments(monkeypatch, _segment_filing([
        # Collapsing, and 4% of revenue: arithmetic, not a hub shock.
        _segment_row("Legacy", "business", 0.04, [10, 20, 30, 40, 100, 100, 100, 100]),
        _segment_row("Core", "business", 0.90, [110, 108, 106, 104, 100, 100, 100, 100]),
    ]))

    assert propagation.segment_trend("HUB") is None


def test_segment_trend_reads_one_axis_and_prefers_reportable_segments(monkeypatch):
    _fake_segments(monkeypatch, _segment_filing([
        # Growth of 12% down to 2%: decelerating, on the axis management reports.
        _segment_row("Networking", "business", 0.40,
                     [102, 112, 103, 102, 100, 100, 100, 100]),
        # A bigger product line falling harder, on a second axis describing the
        # same revenue. Mixing the two would double-count it.
        _segment_row("Switches", "product", 0.70, [70, 80, 90, 95, 100, 100, 100, 100]),
    ]))

    trend = propagation.segment_trend("HUB")

    assert trend["segment"] == "Networking" and trend["dimension"] == "business"
    assert trend["trend"] == "decelerating"


def test_segment_trend_needs_a_positive_base_and_enough_history(monkeypatch):
    short = PERIODS[:4]
    _fake_segments(monkeypatch, _segment_filing(
        [_segment_row("Core", "business", 0.9, [90, 95, 100, 100] + [None] * 4)], short))

    assert propagation.segment_trend("HUB") is None


# --------------------------------------------------------------------------- #
# Consensus and reflection
# --------------------------------------------------------------------------- #
def test_consensus_move_reads_the_out_year_and_counts_desks(monkeypatch):
    _fake_estimates(monkeypatch, {
        "HUB": _estimate_tables(current=4.0, ago_90=5.0, up=1, down=13, analysts=30)})

    move = propagation.consensus_move("HUB")

    assert move["eps_drift_90d"] == pytest.approx(-0.20)
    assert move["net_revisions"] == -12 and move["analyst_count"] == 30


def test_reflection_separates_uncovered_from_unmoved():
    assert propagation.reflection({"analyst_count": 8, "eps_drift_90d": 0.001,
                                   "net_revisions": 0}) == "unreflected"
    assert propagation.reflection({"analyst_count": 8, "eps_drift_90d": -0.09,
                                   "net_revisions": -1}) == "reflected"
    # Nobody covers it, so there is no estimate that failed to move — a
    # different claim entirely, and the one this must never round down.
    assert propagation.reflection({"analyst_count": None,
                                   "eps_drift_90d": None}) == "uncovered"


def test_score_discounts_a_link_the_market_has_already_priced():
    exposure = propagation.exposure_term(27.0, "net sales")
    live = propagation.score_row(exposure, 1.0, "unreflected")
    priced = propagation.score_row(exposure, 1.0, "reflected")
    quiet = propagation.score_row(exposure, 0.0, "unreflected")

    assert live > priced > 0
    # A receivables disclosure is credit exposure, not a demand channel.
    assert propagation.exposure_term(27.0, "accounts receivable") < exposure
    # No shock still orders rows by exposure rather than collapsing them.
    assert 0 < quiet < live


# --------------------------------------------------------------------------- #
# Edges
# --------------------------------------------------------------------------- #
def test_disclosed_edges_keeps_only_quantified_listed_counterparties(monkeypatch):
    _fake_counterparties(monkeypatch, {"HUB": [
        _edge("SUP", "Supplier Inc", "supplier", 27.0),
        _edge("DIST", "Distributor Inc", "customer", 41.0, basis="purchases"),
        _edge("THIN", "Barely Inc", "supplier", 4.0),
        _edge(None, "Private Partner LLC", "supplier", 55.0),
    ]})

    edges, dropped = propagation.disclosed_edges("HUB", min_exposure_pct=10.0)

    assert [e["symbol"] for e in edges] == ["SUP", "DIST"]
    # Direction is read from the hub's point of view: a supplier sells to it.
    assert edges[0]["link"] == "demand" and edges[1]["link"] == "supply"
    assert dropped == {"below_floor": 1, "unlisted": 1}


# --------------------------------------------------------------------------- #
# The funnel
# --------------------------------------------------------------------------- #
def _wire_one_hub(monkeypatch):
    _fake_segments(monkeypatch, _segment_filing([
        _segment_row("Data Center", "business", 0.55,
                     [90, 98, 105, 104, 100, 100, 100, 100])]))
    _fake_estimates(monkeypatch, {
        "HUB": _estimate_tables(current=4.0, ago_90=5.0, up=0, down=12, analysts=30),
        # Nobody has moved this one: no drift, no revisions, five analysts.
        "SUP": _estimate_tables(current=2.0, ago_90=2.0, analysts=5),
        # Already cut by a sixth: the link is priced.
        "DIST": _estimate_tables(current=1.0, ago_90=1.2, down=4, analysts=9),
        # "TINY" is absent, which is what no coverage looks like.
    })
    _fake_counterparties(monkeypatch, {"HUB": [
        _edge("SUP", "Supplier Inc", "supplier", 27.0,
              quote="Sales to Hub Corporation accounted for 27% of our net sales."),
        _edge("DIST", "Distributor Inc", "customer", 41.0, basis="purchases"),
        _edge("TINY", "Uncovered Inc", "supplier", 18.0),
    ]})
    _fake_market(
        monkeypatch,
        moves={"HUB": {"three_month": -0.31, "one_year": -0.4},
               "SUP": {"three_month": -0.02, "one_year": 0.1},
               "DIST": {"three_month": -0.18, "one_year": -0.2},
               "TINY": {"three_month": 0.01, "one_year": 0.0}},
        profiles={"HUB": {"longName": "Hub Corporation", "marketCap": 2e12},
                  "SUP": {"marketCap": 3e9, "sector": "Technology"},
                  "DIST": {"marketCap": 8e9, "sector": "Technology"},
                  "TINY": {"marketCap": 6e8, "sector": "Industrials"}})


def test_link_propagation_ranks_the_unrepriced_counterparty_first(monkeypatch):
    _wire_one_hub(monkeypatch)
    recorded = _capture_memory(monkeypatch)

    result = funnel.link_propagation(hubs="HUB")
    rows = {row["symbol"]: row for row in result.data}

    assert result.data[0]["symbol"] == "SUP"
    assert rows["SUP"]["family"] == "unreflected_exposure"
    assert rows["DIST"]["family"] == "reflected_exposure"
    assert rows["TINY"]["family"] == "uncovered_exposure"
    assert rows["SUP"]["score"] > rows["DIST"]["score"]

    # The transmission channel, its size, and the sentence it was read from.
    assert rows["SUP"]["exposure_pct"] == 27.0 and rows["SUP"]["link"] == "demand"
    assert "27% of our net sales" in rows["SUP"]["quote"]
    assert rows["SUP"]["filing_url"] == "https://sec.gov/x"

    # What moved at the far end, and that it moved on the business rather than
    # only on expectations.
    walked = result.extra["hubs_walked"][0]
    assert walked["hub"] == "HUB" and walked["issuer"] == "Hub Corporation"
    assert set(walked["channels"]) == {"consensus", "segment", "price"}
    assert walked["direction"] == "down" and walked["conflicting"] is False
    assert rows["SUP"]["hub_segment"] == "Data Center"
    assert rows["SUP"]["hub_segment_yoy"] == pytest.approx(-0.10)
    assert rows["SUP"]["hub_eps_drift_90d"] == pytest.approx(-0.20)

    assert recorded["family"] == sources.LINK_PROPAGATION
    assert recorded["rows"][0]["known_on"] == result.extra["as_of"]


def test_link_propagation_cards_quote_the_filing(monkeypatch):
    _wire_one_hub(monkeypatch)
    _capture_memory(monkeypatch)

    row = funnel.link_propagation(hubs="HUB").data[0]
    card = "\n".join(sources.get(sources.LINK_PROPAGATION).detail(row))

    assert "27.0% of net sales comes from HUB" in card
    assert "Data Center" in card and "consensus" in card
    assert "Sales to Hub Corporation accounted for 27% of our net sales." in card


def test_link_propagation_drops_a_counterparty_below_the_size_floor(monkeypatch):
    _wire_one_hub(monkeypatch)
    _capture_memory(monkeypatch)

    result = funnel.link_propagation(hubs="HUB", min_market_cap_bn=1.0)

    assert "TINY" not in {row["symbol"] for row in result.data}
    assert result.extra["dropped_below_market_cap"] == 1


def test_link_propagation_keeps_one_row_per_counterparty_across_hubs(monkeypatch):
    _wire_one_hub(monkeypatch)
    _capture_memory(monkeypatch)
    # The same supplier discloses against a second hub, more weakly.
    _fake_counterparties(monkeypatch, {
        "HUB": [_edge("SUP", "Supplier Inc", "supplier", 27.0)],
        "HUB2": [_edge("SUP", "Supplier Inc", "supplier", 12.0)],
    })
    _fake_estimates(monkeypatch, {
        "HUB": _estimate_tables(current=4.0, ago_90=5.0, down=12, analysts=30),
        "HUB2": _estimate_tables(current=3.0, ago_90=3.3, down=5, analysts=20),
        "SUP": _estimate_tables(current=2.0, ago_90=2.0, analysts=5),
    })
    _fake_market(
        monkeypatch,
        moves={s: {"three_month": 0.0, "one_year": 0.0}
               for s in ("HUB", "HUB2", "SUP")},
        profiles={"SUP": {"marketCap": 3e9}})

    result = funnel.link_propagation(hubs="HUB,HUB2", max_hubs=2)

    assert [row["symbol"] for row in result.data] == ["SUP"]
    assert result.data[0]["hub"] == "HUB"
    assert result.data[0]["also_exposed_to"] == "HUB2"
    assert len(result.extra["hubs_walked"]) == 2


def test_link_propagation_says_which_hubs_it_could_not_use(monkeypatch):
    _wire_one_hub(monkeypatch)
    _capture_memory(monkeypatch)
    _fake_estimates(monkeypatch, {
        "HUB": _estimate_tables(current=4.0, ago_90=5.0, down=12, analysts=30),
        # Consensus flat, and no filing names it — nothing to propagate.
        "QUIET": _estimate_tables(current=2.0, ago_90=2.0, analysts=9),
        "SUP": _estimate_tables(current=2.0, ago_90=2.0, analysts=5),
    })

    result = funnel.link_propagation(hubs="HUB,QUIET", max_hubs=2, read_segments=False)
    skipped = {row["hub"]: row["reason"] for row in result.extra["hubs_skipped"]}

    assert "QUIET" in skipped and "nothing material moved" in skipped["QUIET"]
    assert [row["hub"] for row in result.extra["hubs_walked"]] == ["HUB"]


def test_link_propagation_never_truncates_named_hubs_silently(monkeypatch):
    _wire_one_hub(monkeypatch)
    _capture_memory(monkeypatch)

    result = funnel.link_propagation(hubs="HUB,HUB2,HUB3", max_hubs=1)

    assert any("HUB2, HUB3" in warning for warning in result.warnings)


def test_link_propagation_is_empty_when_nothing_propagates(monkeypatch):
    _wire_one_hub(monkeypatch)
    _capture_memory(monkeypatch)

    with pytest.raises(EmptyDataError):
        funnel.link_propagation(hubs="HUB", min_exposure_pct=90.0)
