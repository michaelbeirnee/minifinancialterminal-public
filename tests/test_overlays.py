"""Offline tests for the CBOE reference overlays.

Step 7 of docs/hedge-construction.md. Canned index histories: the reference
runs up with one deep crash, PROT clips the crash (real protection bought
with give-up), and the premium sellers must never be ranked as protection no
matter how well they score.
"""
import numpy as np
import pandas as pd
import pytest

from backend.portfolio import overlays

DATES = pd.bdate_range("2020-01-02", periods=600)


def _reference() -> pd.Series:
    """Steady drift with a 35% crash in the middle."""
    returns = np.full(len(DATES), 0.0006)
    returns[200:240] = -0.011  # the crash
    return pd.Series(100.0 * np.cumprod(1.0 + returns), index=DATES)


def _protected(reference: pd.Series, floor: float, drag: float) -> pd.Series:
    """Reference returns clipped at ``floor``, minus a daily premium ``drag``."""
    returns = reference.pct_change().fillna(0.0).clip(lower=floor) - drag
    return pd.Series(100.0 * np.cumprod(1.0 + returns).to_numpy(), index=reference.index)


REFERENCE = _reference()
PANEL = pd.DataFrame(
    {
        "^SP500TR": REFERENCE,
        # Cheap enough that the crash it avoided outweighed its premium — a
        # real outcome (protective puts beat the S&P through 2008), so the
        # give-up is legitimately negative here.
        "PROT_CHEAP": _protected(REFERENCE, -0.004, 0.00010),
        # Expensive enough to cost more than it saved over the window.
        "PROT_DEAR": _protected(REFERENCE, -0.004, 0.00090),
        "^BXM": _protected(REFERENCE, -1.0, 0.00020),
    }
)

CHEAP = overlays.Overlay("PROT_CHEAP", "Cheap protection", "clipped, low drag", True)
DEAR = overlays.Overlay("PROT_DEAR", "Dear protection", "clipped, high drag", True)
BUYWRITE = overlays.Overlay("^BXM", "Buy-write", "sells calls", False)


def test_protective_overlays_rank_by_cost_per_drawdown_removed():
    out = overlays.compare(PANEL, "^SP500TR", overlays=(DEAR, CHEAP))
    assert [r["symbol"] for r in out["overlays"]] == ["PROT_CHEAP", "PROT_DEAR"]
    assert out["protective_available"] is True

    cheap, dear = out["overlays"]
    for row in (cheap, dear):
        assert row["drawdown_removed"] > 0                            # the fall was shortened
        assert abs(row["max_drawdown"]) < abs(row["reference_max_drawdown"])
        assert row["downside_capture"] < 1.0                          # took less of the fall
        assert row["cagr_give_up_per_drawdown_removed"] == pytest.approx(
            row["cagr_give_up"] / row["drawdown_removed"], abs=1e-4
        )
    assert dear["cagr_give_up"] > 0                                   # dear protection is paid for
    assert cheap["cagr_give_up"] < dear["cagr_give_up"]
    assert cheap["period"]["sessions"] == len(DATES) - 1


def test_protection_that_paid_off_shows_a_negative_give_up():
    """Insurance can beat the underlying over a window containing its crash.

    That is a real outcome, not an error, so the give-up goes negative and
    the overlay ranks first rather than being clamped to zero.
    """
    out = overlays.compare(PANEL, "^SP500TR", overlays=(CHEAP,))
    row = out["overlays"][0]
    assert row["cagr_give_up"] < 0
    assert row["cagr"] > row["reference_cagr"]
    assert row["cagr_give_up_per_drawdown_removed"] < 0


def test_premium_sellers_are_never_ranked_as_protection():
    """Even scoring well, an overwrite strategy stays out of the hedge list."""
    out = overlays.compare(PANEL, "^SP500TR", overlays=(CHEAP, BUYWRITE))
    assert [r["symbol"] for r in out["overlays"]] == ["PROT_CHEAP"]
    assert [r["symbol"] for r in out["comparators"]] == ["^BXM"]
    assert out["comparators"][0]["protective"] is False


def test_missing_protective_history_is_reported_not_implied():
    """An empty overlay list must read as "could not measure", not "no good"."""
    panel = PANEL[["^SP500TR", "^BXM"]]
    out = overlays.compare(panel, "^SP500TR", overlays=(CHEAP, DEAR, BUYWRITE))
    assert out["protective_available"] is False
    assert out["overlays"] == []
    assert "NO PROTECTIVE OVERLAY COULD BE EVALUATED" in out["notes"][0]
    assert {s["symbol"] for s in out["skipped"]} == {"PROT_CHEAP", "PROT_DEAR"}
    assert all("no price history" in s["reason"] for s in out["skipped"])


def test_too_little_overlap_is_skipped_with_the_count():
    short = PANEL.copy()
    short.loc[short.index[100:], "PROT_CHEAP"] = np.nan
    out = overlays.compare(short, "^SP500TR", overlays=(CHEAP,))
    assert out["overlays"] == []
    assert "100 overlapping sessions" in out["skipped"][0]["reason"]


def test_a_missing_reference_is_an_error_not_a_guess():
    with pytest.raises(ValueError):
        overlays.compare(PANEL, "^NOPE", overlays=(CHEAP,))


def test_shipped_registry_marks_only_puts_and_collars_protective():
    protective = {o.symbol for o in overlays.OVERLAYS if o.protective}
    sellers = {o.symbol for o in overlays.OVERLAYS if not o.protective}
    assert protective == {"^PPUT", "^CLL"}
    assert sellers == {"^BXM", "^PUT"}
