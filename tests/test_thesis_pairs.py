"""Deterministic tests for pair dislocation along disclosed links.

Nothing here touches the network. The three inputs the funnel is built on —
the concentration sentences the miner recovers, the peer group, and daily
closes — are supplied as fixtures, so what is being tested is the reasoning
across them: which pairs are admitted, which are cointegrated, and which have
left their history.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.core.errors import EmptyDataError
from backend.extensions import thesis_pairs as funnel
from backend.thesis import pairs, sources

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
N_DAYS = 750
INDEX = pd.bdate_range("2023-01-02", periods=N_DAYS)


def _walk(seed: int, drift: float = 0.0, vol: float = 0.012, start: float = 100.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(rng.normal(drift, vol, N_DAYS)))


def _tied_to(base: np.ndarray, seed: int, beta: float = 1.2, alpha: float = 0.5,
             phi: float = 0.9, noise: float = 0.01) -> np.ndarray:
    """A series cointegrated with ``base``: log-linear plus mean-reverting noise."""
    rng = np.random.default_rng(seed)
    error = np.zeros(N_DAYS)
    for t in range(1, N_DAYS):
        error[t] = phi * error[t - 1] + rng.normal(0, noise)
    return np.exp(alpha + beta * np.log(base) + error)


def _shocked(series: np.ndarray, last_days: int = 63, to: float = 0.8) -> np.ndarray:
    """The same series with its final window walked down to ``to`` of itself."""
    out = series.copy()
    out[-last_days:] = out[-last_days:] * np.linspace(1.0, to, last_days)
    return out


BASE = _walk(1)
TIED = _tied_to(BASE, 2)
SHOCKED = _shocked(TIED)
UNRELATED = _walk(3, start=50.0)


def _series(values: np.ndarray, days: int = N_DAYS) -> pd.Series:
    return pd.Series(values[-days:], index=INDEX[-days:])


def _edge(symbol, company, relationship, pct, basis="net sales", quote="…",
          disclosed_by="counterparty", pct_of=None):
    return {"relationship": relationship, "symbol": symbol, "company": company,
            "exposure_pct": pct, "exposure_basis": basis, "pct_of": pct_of or symbol,
            "disclosed_by": disclosed_by, "quote": quote, "form": "10-K",
            "filing_date": "2026-02-19", "filing_url": "https://sec.gov/x",
            "cik": "1", "disclosures": 2}


def _peer(symbol, company, sources_, why="same industry"):
    return {"symbol": symbol, "company": company, "sources": list(sources_),
            "why": why, "mentions": 1 if "filings" in sources_ else 0,
            "form": "10-K" if "filings" in sources_ else None,
            "filed": "2026-01-30" if "filings" in sources_ else None,
            "filing_url": "https://sec.gov/p" if "filings" in sources_ else None,
            "score": 1.0, "agreement": len(sources_)}


def _fake_links(monkeypatch, counterparties=None, own=None, peers=None):
    """The three legs of the pair universe, keyed by anchor."""
    def counterparties_fn(symbol, years=4, max_candidates=40, limit=15):
        rows = (counterparties or {}).get(symbol)
        if not rows:
            raise EmptyDataError("nobody names {}".format(symbol))
        return pd.DataFrame(rows)

    def own_fn(symbol, limit=15):
        rows = (own or {}).get(symbol)
        if not rows:
            raise EmptyDataError("{} names nobody".format(symbol))
        return pd.DataFrame(rows)

    def peers_fn(symbol, limit=12, years=3):
        rows = (peers or {}).get(symbol)
        if rows is None:
            raise EmptyDataError("nothing comparable to {}".format(symbol))
        return rows[:limit], {"subject": {"symbol": symbol}}

    monkeypatch.setattr(pairs.supplychain, "counterparties", counterparties_fn)
    monkeypatch.setattr(pairs.supplychain, "subject_disclosures", own_fn)
    monkeypatch.setattr(pairs.peer_source, "peer_group", peers_fn)


def _fake_prices(monkeypatch, closes):
    def history(symbol, start=None, end=None, interval="1d", **_kwargs):
        if symbol not in closes:
            raise RuntimeError("no history for {}".format(symbol))
        return pd.DataFrame({"close": closes[symbol]})

    monkeypatch.setattr(pairs.yahoo, "history", history)


def _fake_profiles(monkeypatch, profiles=None):
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
# The relationship
# --------------------------------------------------------------------------- #
def test_relationship_reads_an_intact_pair_as_intact():
    stats = pairs.relationship(_series(TIED), _series(BASE))

    assert stats["state"] == "intact"
    assert stats["p_value_history"] < 0.05 and stats["p_value_full"] < 0.05
    assert stats["hedge_ratio"] == pytest.approx(1.2, abs=0.05)
    assert abs(stats["z_now"]) < 2.0
    assert stats["half_life_days"] is not None and stats["half_life_days"] < 63
    assert stats["history_days"] == N_DAYS - 63 and stats["recent_days"] == 63


def test_relationship_flags_a_pair_that_left_its_history_out_of_sample():
    stats = pairs.relationship(_series(SHOCKED), _series(BASE))

    # The fit is on the history alone, so the shock cannot pull the line towards
    # itself: the hedge ratio is the same one the intact pair produced.
    assert stats["hedge_ratio"] == pytest.approx(
        pairs.relationship(_series(TIED), _series(BASE))["hedge_ratio"])
    assert stats["z_now"] < -2.0
    assert stats["days_outside"] > 30
    # A 20% walk against a spread with a ~2% sigma is enough to break the
    # whole-window test, so it reads as broken rather than dislocated.
    assert stats["p_value_history"] < 0.05 and stats["p_value_full"] > 0.10
    assert stats["state"] == "broken"
    # The shocked leg fell roughly 20% more than the base did over the window.
    assert stats["recent_move_a"] - stats["recent_move_b"] == pytest.approx(-0.2, abs=0.08)


def test_relationship_reads_a_moderate_stretch_as_dislocated():
    stretched = _shocked(TIED, last_days=10, to=0.93)
    stats = pairs.relationship(_series(stretched), _series(BASE))

    assert stats["z_now"] <= -2.0
    assert stats["p_value_full"] <= 0.10
    assert stats["state"] == "dislocated"


def test_relationship_refuses_a_pair_with_too_little_shared_history():
    assert pairs.relationship(_series(TIED, 200), _series(BASE, 200), min_obs=250) is None
    # Enough rows in total, but the recent window would leave no history to fit.
    assert pairs.relationship(_series(TIED, 260), _series(BASE, 260),
                              min_obs=250, recent_days=250) is None


def test_unrelated_walks_do_not_cointegrate():
    stats = pairs.relationship(_series(UNRELATED), _series(BASE))
    assert stats["p_value_history"] > 0.10


def test_half_life_is_none_for_a_series_that_does_not_revert():
    reverting = np.zeros(400)
    rng = np.random.default_rng(5)
    for t in range(1, 400):
        reverting[t] = 0.8 * reverting[t - 1] + rng.normal(0, 1)
    assert 1 < pairs.half_life(pd.Series(reverting)) < 10
    assert pairs.half_life(pd.Series(np.cumsum(np.ones(400)))) is None
    assert pairs.half_life(pd.Series(np.zeros(5))) is None


def test_score_rewards_distance_fit_link_and_reversion():
    strong = pairs.score_row(-3.0, 0.001, 8.0, 0.9, 63)
    weak_fit = pairs.score_row(-3.0, 0.60, 8.0, 0.9, 63)
    weak_link = pairs.score_row(-3.0, 0.001, 8.0, 0.2, 63)
    slow = pairs.score_row(-3.0, 0.001, 200.0, 0.9, 63)
    wandering = pairs.score_row(-3.0, 0.001, None, 0.9, 63)
    nearer = pairs.score_row(-1.0, 0.001, 8.0, 0.9, 63)

    assert strong > weak_fit and strong > weak_link and strong > nearer
    assert strong > slow > wandering > 0
    # Sign-neutral: a rich anchor and a cheap anchor are the same distance.
    assert pairs.score_row(3.0, 0.001, 8.0, 0.9, 63) == strong
    # A pair the fit found moving against each other is not the relationship
    # a disclosed link implies, and is halved.
    assert pairs.score_row(-3.0, 0.001, 8.0, 0.9, 63, hedge_ratio=-0.9) == pytest.approx(strong / 2)
    assert pairs.score_row(-3.0, 0.001, 8.0, 0.9, 63, hedge_ratio=1.1) == strong


def test_legs_names_rich_cheap_mover_and_lagging():
    sides = pairs.legs("HUB", "SUP", {"z_now": -2.5, "recent_move_a": -0.18,
                                       "recent_move_b": 0.03})
    # Negative z: the anchor sits below the line the other predicts — cheap.
    assert sides["cheap_leg"] == "HUB" and sides["rich_leg"] == "SUP"
    # The anchor moved 18%, the other 3%: the other has not repriced.
    assert sides["mover"] == "HUB" and sides["lagging"] == "SUP"
    assert sides["mover_move"] == -0.18 and sides["lagging_move"] == 0.03


# --------------------------------------------------------------------------- #
# The pair universe
# --------------------------------------------------------------------------- #
def test_linked_pairs_reads_all_three_legs_and_keeps_the_best_edge(monkeypatch):
    _fake_links(
        monkeypatch,
        counterparties={"HUB": [_edge("SUP", "Supplier Inc", "supplier", 27.0),
                                _edge("DIST", "Distributor Corp", "customer", 12.0,
                                      basis="purchases")]},
        own={"HUB": [_edge("SUP", "Supplier Inc", "supplier", 8.0, basis="purchases",
                           disclosed_by="subject", pct_of="HUB")]},
        peers={"HUB": [_peer("RIVAL", "Rival plc", ["filings", "classification"],
                             "named as competition in 1 filing, same industry"),
                       _peer("SUP", "Supplier Inc", ["classification", "registration"]),
                       _peer("LOOSE", "Loose Ltd", ["registration"], "same SIC code")]},
    )

    edges, report = pairs.linked_pairs("HUB")
    by_symbol = {edge["symbol"]: edge for edge in edges}

    # SUP arrives three times; the 27% counterparty disclosure wins.
    assert by_symbol["SUP"]["relationship"] == "supplier"
    assert by_symbol["SUP"]["exposure_pct"] == 27.0
    assert by_symbol["SUP"]["disclosed_by"] == "counterparty"
    assert by_symbol["DIST"]["relationship"] == "customer"
    assert by_symbol["RIVAL"]["relationship"] == "shared_segment"
    assert by_symbol["RIVAL"]["peer_evidence"] == "filings"
    assert by_symbol["RIVAL"]["strength"] == 1.0
    # A single-classification comparable is below the default evidence bar.
    assert "LOOSE" not in by_symbol
    assert report["dropped"]["peer_evidence"] == 1
    assert report["legs"]["peers"]["rows"] == 3
    # Strongest link first.
    assert edges[0]["symbol"] in ("SUP", "RIVAL")


def test_linked_pairs_gates_on_exposure_and_relationship_kind(monkeypatch):
    _fake_links(
        monkeypatch,
        counterparties={"HUB": [_edge("SUP", "Supplier Inc", "supplier", 27.0),
                                _edge("SMALL", "Small Co", "supplier", 4.0)]},
        peers={"HUB": [_peer("RIVAL", "Rival plc", ["filings"])]},
    )

    edges, report = pairs.linked_pairs("HUB", min_exposure_pct=10.0,
                                       relationships=("supplier",))
    assert [edge["symbol"] for edge in edges] == ["SUP"]
    assert report["dropped"]["below_floor"] == 1
    # The peer leg was never read: no shared_segment asked for.
    assert "peers" not in report["legs"]

    edges, _ = pairs.linked_pairs("HUB", relationships=("shared_segment",),
                                  peer_evidence="filings")
    assert [edge["symbol"] for edge in edges] == ["RIVAL"]


def test_linked_pairs_survives_a_leg_that_fails(monkeypatch):
    _fake_links(monkeypatch, peers={"HUB": [_peer("RIVAL", "Rival plc", ["filings"])]})

    edges, report = pairs.linked_pairs("HUB")
    assert [edge["symbol"] for edge in edges] == ["RIVAL"]
    # Both filing legs came back empty — an answer, flagged as such, not a failure.
    assert report["legs"]["counterparty_filings"]["error"]
    assert report["legs"]["counterparty_filings"]["empty"] is True
    assert report["legs"]["own_filing"]["empty"] is True


def test_linked_pairs_tells_a_failed_leg_from_an_empty_one(monkeypatch):
    _fake_links(monkeypatch, peers={"HUB": [_peer("RIVAL", "Rival plc", ["filings"])]})

    def broken(symbol, years=4, max_candidates=40, limit=15):
        raise RuntimeError("EDGAR full-text search returned 503")

    monkeypatch.setattr(pairs.supplychain, "counterparties", broken)

    _edges, report = pairs.linked_pairs("HUB")
    assert report["legs"]["counterparty_filings"]["empty"] is False
    assert "503" in report["legs"]["counterparty_filings"]["error"]


def test_pair_dislocation_warns_on_a_failed_leg_but_not_an_empty_one(monkeypatch):
    _wire(monkeypatch)
    result = funnel.pair_dislocation(symbols="HUB")
    # The own-filing leg named nobody: an answer, not a warning.
    assert not any("own_filing" in w for w in result.warnings)

    def broken(symbol, limit=15):
        raise RuntimeError("EDGAR returned 503")

    monkeypatch.setattr(pairs.supplychain, "subject_disclosures", broken)
    result = funnel.pair_dislocation(symbols="HUB")
    assert any("own_filing — EDGAR returned 503" in w for w in result.warnings)


def test_peer_evidence_labels():
    assert pairs.peer_evidence_of(["filings"]) == "filings"
    assert pairs.peer_evidence_of(["classification", "registration"]) == "agree"
    assert pairs.peer_evidence_of(["classification"]) == "any"


# --------------------------------------------------------------------------- #
# The funnel, end to end
# --------------------------------------------------------------------------- #
def _wire(monkeypatch, closes=None, peers=None):
    """HUB with a supplier that has walked away from it, a customer that has
    not, and a competitor whose prices are unrelated."""
    _fake_links(
        monkeypatch,
        counterparties={"HUB": [
            _edge("SUP", "Supplier Inc", "supplier", 27.0,
                  quote="Sales to Hub accounted for 27% of our net sales."),
            _edge("DIST", "Distributor Corp", "customer", 12.0, basis="purchases",
                  quote="Hub products were 12% of our purchases."),
        ]},
        peers=peers if peers is not None else {
            "HUB": [_peer("RIVAL", "Rival plc", ["filings", "classification"],
                          "named as competition in 1 filing, same industry")]},
    )
    _fake_prices(monkeypatch, closes or {
        "HUB": _series(BASE), "SUP": _series(SHOCKED),
        "DIST": _series(_tied_to(BASE, 9, beta=0.8, alpha=1.0)),
        "RIVAL": _series(UNRELATED),
    })
    _fake_profiles(monkeypatch, {
        "SUP": {"longName": "Supplier Inc", "marketCap": 4e9, "sector": "Technology"},
        "HUB": {"longName": "Hub Corp", "marketCap": 900e9, "sector": "Technology"},
    })
    return _capture_memory(monkeypatch)


def test_pair_dislocation_flags_the_broken_supplier_pair_and_nothing_else(monkeypatch):
    recorded = _wire(monkeypatch)

    result = funnel.pair_dislocation(symbols="HUB")
    rows = result.data

    assert [(row["symbol"], row["pair_with"]) for row in rows] == [("HUB", "SUP")]
    row = rows[0]
    # SUP fell 20% further than HUB did: SUP is the mover, HUB the leg that has
    # not repriced against it — and the candidate.
    assert row["mover"] == "SUP" and row["mover_move"] < row["recent_move"]
    assert row["family"] == "broken_pair" and row["state"] == "broken"
    assert row["relationship"] == "supplier" and row["exposure_pct"] == 27.0
    assert row["z_now"] < -2.0 or row["z_now"] > 2.0
    assert row["evidence"].startswith("Sales to Hub")
    assert row["issuer"] == "Hub Corp" and row["pair_with_issuer"] == "Supplier Inc"
    assert row["score"] > 0 and row["action"] == "investigate"

    extra = result.extra
    assert extra["pairs_drawn"] == 3 and extra["pairs_tested"] == 3
    # The customer pair is cointegrated and intact; the competitor pair never
    # cointegrated at all — both are counted, neither is emitted.
    assert extra["pairs_intact"] == 1 and extra["pairs_never_cointegrated"] == 1
    assert extra["pairs_flagged"] == 1
    assert extra["expected_false_cointegrations"] == pytest.approx(0.3)
    assert extra["anchors_used"] == ["HUB"]

    assert recorded["family"] == sources.PAIR_DISLOCATION
    assert recorded["rows"][0]["symbol"] == "HUB"
    assert recorded["rows"][0]["family"] == "broken_pair"
    assert recorded["parameters"]["z_threshold"] == 2.0


def test_pair_dislocation_can_show_the_whole_tested_set(monkeypatch):
    _wire(monkeypatch)

    rows = funnel.pair_dislocation(symbols="HUB", include_intact=True).data
    families = sorted(row["family"] for row in rows)

    assert families == ["broken_pair", "intact_pair"]
    # Never-cointegrated pairs stay out even then: no relationship, no reading.
    assert all(row["pair_with"] != "RIVAL" and row["symbol"] != "RIVAL" for row in rows)


def test_pair_dislocation_cards_carry_the_link_and_the_spread(monkeypatch):
    _wire(monkeypatch)
    row = funnel.pair_dislocation(symbols="HUB").data[0]

    lines = sources.get(sources.PAIR_DISLOCATION).detail(row)

    assert lines[0].startswith("  pair: HUB vs SUP · supplier (27.0% of SUP net sales)")
    assert "σ now" in lines[1] and "broken" in lines[1]
    assert "Engle-Granger p" in lines[2] and "SUP moved" in lines[2]
    assert lines[3] == '  linked by: "Sales to Hub accounted for 27% of our net sales."'


def test_pair_dislocation_is_empty_when_no_pair_left_its_history(monkeypatch):
    _wire(monkeypatch, closes={
        "HUB": _series(BASE), "SUP": _series(TIED),
        "DIST": _series(_tied_to(BASE, 9, beta=0.8, alpha=1.0)),
        "RIVAL": _series(UNRELATED),
    })

    with pytest.raises(EmptyDataError) as caught:
        funnel.pair_dislocation(symbols="HUB")
    assert "3 pair(s) tested, none dislocated" in str(caught.value)
    assert "1 never cointegrated" in str(caught.value)


def test_pair_dislocation_is_empty_when_nothing_links_the_anchor(monkeypatch):
    _fake_links(monkeypatch)
    _fake_prices(monkeypatch, {})
    _fake_profiles(monkeypatch)
    _capture_memory(monkeypatch)

    with pytest.raises(EmptyDataError) as caught:
        funnel.pair_dislocation(symbols="HUB")
    assert "No pair to test" in str(caught.value)


def test_pair_dislocation_never_truncates_named_anchors_silently(monkeypatch):
    _wire(monkeypatch)

    result = funnel.pair_dislocation(symbols="HUB,AAA,BBB", max_anchors=1)

    assert result.extra["anchors_used"] == ["HUB"]
    assert any("AAA, BBB not scanned" in w for w in result.warnings)


def test_pair_dislocation_restricts_relationship_kinds(monkeypatch):
    _wire(monkeypatch)

    result = funnel.pair_dislocation(symbols="HUB", relationships="shared_segment",
                                     include_intact=True, max_p_value=1.0)
    # Only the competitor pair was drawn; with the p ceiling lifted it is shown.
    assert result.extra["pairs_drawn"] == 1
    assert {row["relationship"] for row in result.data} == {"shared_segment"}

    with pytest.raises(ValueError):
        funnel.pair_dislocation(symbols="HUB", relationships="nonsense")
    with pytest.raises(ValueError):
        funnel.pair_dislocation(symbols="HUB", peer_evidence="hearsay")


def test_pair_dislocation_reports_pairs_it_could_not_price(monkeypatch):
    _wire(monkeypatch, closes={"HUB": _series(BASE), "SUP": _series(SHOCKED)})

    result = funnel.pair_dislocation(symbols="HUB")

    assert result.extra["pairs_untestable"] == 2
    assert result.extra["pairs_tested"] == 1
    assert any("price history: DIST" in w for w in result.warnings)


def test_pair_dislocation_is_registered_on_the_thesis_menu():
    from backend.core.registry import REGISTRY

    assert "/thesis/pair_dislocation" in REGISTRY
    source = sources.get(sources.PAIR_DISLOCATION)
    assert source.command == "/thesis/pair_dislocation"
    assert source.universe == sources.STOCK_UNIVERSE
    assert "concentration" in source.skip_enrichments
