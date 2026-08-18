"""Shared-end-market read-through: the normaliser, the inflection, the find.

``find`` is pure over members already read, so the cluster logic — cohort,
confirmers, exposure, the lag test, the anchor date — is exercised on
hand-built members. The live test at the bottom reads a real cluster.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.core.errors import EmptyDataError
from backend.core.registry import execute
from backend.flagged import readthrough as rt


# --------------------------------------------------------------------------- #
# Lines
# --------------------------------------------------------------------------- #
def test_filers_agree_on_a_line_however_they_wrote_it():
    assert rt.canon("Greater China") == rt.canon("China") == rt.canon("PRC") == "china"
    assert rt.canon("United States") == rt.canon("North America") == rt.canon("Americas") == "north_america"
    assert rt.canon("Europe and Israel") == rt.canon("EMEA") == "europe"
    assert rt.canon("Data Center") == rt.canon("Datacenter") == "data_center"


def test_residuals_are_not_end_markets_and_unknown_lines_keep_their_name():
    assert rt.canon("Other") is None and rt.canon("Rest of World") is None
    assert rt.canon("Total") is None
    assert rt.canon("Professional Visualization") == "professional_visualization"


def _series(values, start="2025-03-31"):
    """Quarterly series ending each calendar quarter, oldest first."""
    stamps = pd.date_range(end=pd.Timestamp("2026-06-30"), periods=len(values), freq="QE")
    return {s: float(v) for s, v in zip(stamps, values)}


def test_inflection_is_the_change_in_year_over_year_growth():
    # 100 a quarter a year ago, then 110 last quarter (+10%) and 150 this quarter (+50%).
    s = _series([100, 100, 100, 100, 100, 110, 150])
    got = rt.inflection(s)
    assert got["growth"] == pytest.approx(0.5)
    assert got["prior_growth"] == pytest.approx(0.1)
    assert got["inflection"] == pytest.approx(0.4)
    assert got["quarter"] == "2026-06-30"


def test_a_series_with_a_hole_still_matches_on_dates():
    s = _series([100, 100, 100, 100, 100, 110, 150])
    del s[pd.Timestamp("2025-12-31")]           # a quarter the 10-Q did not disaggregate
    got = rt.inflection(s)
    assert got["growth"] == pytest.approx(0.5)  # this quarter vs a year ago is unaffected


# --------------------------------------------------------------------------- #
# The find
# --------------------------------------------------------------------------- #
def _member(symbol, lines, drift, filed="2026-08-04", quarter="2026-06-30"):
    """A member with ``lines`` as {key: (share, inflection)}."""
    return {
        "symbol": symbol, "filed": filed, "latest": quarter,
        "lines": {k: {"dimension": "geographic", "label": k, "share": sh, "inflection": inf,
                      "growth": inf, "prior_growth": 0.0, "quarter": quarter,
                      "series": {pd.Timestamp(quarter): 1.0}}
                  for k, (sh, inf) in lines.items()},
        "consensus": {"drift_90d": drift, "net_revisions_30d": 0, "eps_fy1": 1.0},
    }


CONFIRMERS = [
    _member("A", {"china": (0.3, -0.25)}, drift=-0.06, filed="2026-07-30"),
    _member("B", {"china": (0.4, -0.30)}, drift=-0.05, filed="2026-08-04"),
    _member("C", {"china": (0.2, -0.15)}, drift=-0.04, filed="2026-08-06"),
]


def test_three_peers_decelerating_and_a_flat_exposed_member_is_a_read_through():
    lag = _member("L", {"china": (0.35, None)}, drift=0.003, filed="2026-05-01", quarter="2026-03-31")
    flags, clusters = rt.find(CONFIRMERS + [lag])
    assert len(flags) == 1
    f = flags[0]
    assert f["symbol"] == "L" and f["flag"] == "read_through" and f["direction"] == "down"
    assert f["own_status"] == "not yet reported"
    assert f["known_on"] == "2026-08-06"          # the third confirmer's filing (Jul 30, Aug 4, Aug 6)
    assert [c["symbol"] for c in f["confirmers"]] == ["A", "B", "C"]
    assert f["exposure"] == 0.35
    assert clusters[0]["verdict"] == "common inflection"


def test_a_member_whose_consensus_already_moved_is_not_lagging():
    moved = _member("M", {"china": (0.35, None)}, drift=-0.05, filed="2026-05-01", quarter="2026-03-31")
    flags, _ = rt.find(CONFIRMERS + [moved])
    assert flags == []


def test_a_member_that_reported_and_diverged_is_an_exception_not_a_laggard():
    diverged = _member("D", {"china": (0.35, +0.20)}, drift=0.0)
    flags, _ = rt.find(CONFIRMERS + [diverged])
    assert flags == []


def test_a_member_that_reported_and_agrees_carries_its_own_line_as_evidence():
    agrees = _member("G", {"china": (0.35, -0.12)}, drift=0.0)   # under MIN_INFLECTION? no: 0.12 >= 0.10 -> confirmer
    flags, clusters = rt.find(CONFIRMERS + [agrees])
    # G's inflection clears the bar, so it is a confirmer, not a laggard.
    assert "G" in clusters[0]["confirmers"] and flags == []
    weak = _member("W", {"china": (0.35, -0.05)}, drift=0.0)     # agrees in sign, too small to confirm
    flags, _ = rt.find(CONFIRMERS + [weak])
    assert flags[0]["symbol"] == "W" and flags[0]["own_status"] == "reported, own line agrees"


def test_immaterial_exposure_is_not_a_read_through():
    tiny = _member("T", {"china": (0.04, None)}, drift=0.0, quarter="2026-03-31", filed="2026-05-01")
    assert rt.find(CONFIRMERS + [tiny])[0] == []


def test_two_confirmers_are_a_coincidence():
    lag = _member("L", {"china": (0.35, None)}, drift=0.0, quarter="2026-03-31", filed="2026-05-01")
    flags, clusters = rt.find(CONFIRMERS[:2] + [lag])
    assert flags == [] and clusters[0]["verdict"] == "no common inflection"


def test_confirmers_must_be_half_the_reporters():
    dissent = [_member("X{}".format(i), {"china": (0.3, 0.0)}, drift=0.0) for i in range(4)]
    lag = _member("L", {"china": (0.35, None)}, drift=0.0, quarter="2026-03-31", filed="2026-05-01")
    flags, clusters = rt.find(CONFIRMERS + dissent + [lag])
    assert flags == [] and clusters[0]["verdict"] == "no common inflection"


def test_a_stale_confirmer_is_not_in_the_cohort():
    old = _member("O", {"china": (0.3, -0.5)}, drift=-0.1, quarter="2026-03-31", filed="2026-05-01")
    lag = _member("L", {"china": (0.35, None)}, drift=0.0, quarter="2026-03-31", filed="2026-05-01")
    flags, clusters = rt.find(CONFIRMERS[:2] + [old, lag])
    assert clusters[0]["reported_this_quarter"] == ["A", "B"]
    assert flags == []


def test_no_coverage_cannot_lag():
    lag = _member("N", {"china": (0.35, None)}, drift=None, quarter="2026-03-31", filed="2026-05-01")
    assert rt.find(CONFIRMERS + [lag])[0] == []


def test_the_command_needs_a_hub_and_the_source_is_registered():
    from backend.thesis import sources

    src = sources.resolve(sources.get("read_through"))
    assert src.command == "/flagged/read_through" and src.family_namespace == "flagged"
    with pytest.raises(Exception):
        execute("/flagged/read_through", symbol="")


def test_read_through_is_not_part_of_all_in_the_scan():
    from backend.extensions import flagged as ext

    assert "read_through" not in ext._wanted("all")
    assert "read_through" in ext._wanted("read_through,rating_shift")


# --------------------------------------------------------------------------- #
# Live
# --------------------------------------------------------------------------- #
def test_live_a_semicap_cluster_shares_geographies():
    members = rt.read_members(["AMAT", "LRCX", "KLAC"])
    lines = [set(m["lines"]) for m in members if m.get("lines")]
    assert len(lines) == 3
    assert {"china", "taiwan", "korea", "japan"} <= (lines[0] & lines[1] & lines[2])
    assert all(m["filed"] for m in members)
    flags, clusters = rt.find(members, min_agreeing=2)
    assert any(c["verdict"] for c in clusters)
