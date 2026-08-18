"""Institutional-flow inflections: the 13F data set read and the gate over it.

The provider is exercised against a synthetic archive in the exact shape SEC
serves — SUBMISSION, COVERPAGE and INFOTABLE as tab-separated members of a zip
— so the amendment resolution, the class filter and the identity-change label
are properties of this code rather than of whichever quarter happens to be
published. ``gate`` is pure and takes the numbers straight; those tests state
the thresholds in the language the module documents them in.

The live tests at the bottom hit SEC and Yahoo, like the rest of the suite.
"""
from __future__ import annotations

import io
import zipfile

import pandas as pd
import pytest

from backend.core.errors import EmptyDataError
from backend.core.registry import execute
from backend.flagged import flows
from backend.providers import thirteenf


# --------------------------------------------------------------------------- #
# A data set in a bottle
# --------------------------------------------------------------------------- #
def _tsv(rows, columns):
    return "\n".join(["\t".join(columns)] + ["\t".join(str(v) for v in r) for r in rows]) + "\n"


def _archive(submissions, coverpage, infotable) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SUBMISSION.tsv", _tsv(submissions,
                    ["ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT"]))
        zf.writestr("COVERPAGE.tsv", _tsv(coverpage,
                    ["ACCESSION_NUMBER", "ISAMENDMENT", "AMENDMENTTYPE", "FILINGMANAGER_NAME", "REPORTTYPE"]))
        zf.writestr("INFOTABLE.tsv", _tsv(infotable,
                    ["ACCESSION_NUMBER", "NAMEOFISSUER", "TITLEOFCLASS", "CUSIP", "VALUE",
                     "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL"]))
    return buf.getvalue()


PERIOD = "31-MAR-2026"

# Two filers. Filer A files an original then a RESTATEMENT (which must win);
# filer B files an original then a NEW HOLDINGS amendment (which must add).
# Filer C files a 13F-NT (no table). Filer A's original also lists a
# convertible note and a put option under the issuer, both to be dropped.
SUBS = [
    ("A-1", "10-MAY-2026", "13F-HR", "0000000001", PERIOD),
    ("A-2", "20-MAY-2026", "13F-HR/A", "0000000001", PERIOD),
    ("B-1", "12-MAY-2026", "13F-HR", "0000000002", PERIOD),
    ("B-2", "25-MAY-2026", "13F-HR/A", "0000000002", PERIOD),
    ("C-1", "13-MAY-2026", "13F-NT", "0000000003", PERIOD),
    ("A-0", "05-MAR-2026", "13F-HR", "0000000001", "31-DEC-2025"),   # a late prior-period filing
]
COVER = [
    ("A-1", "N", "", "Alpha Capital", "13F HOLDINGS REPORT"),
    ("A-2", "Y", "RESTATEMENT", "Alpha Capital", "13F HOLDINGS REPORT"),
    ("B-1", "N", "", "Vanguard Group Inc", "13F HOLDINGS REPORT"),
    ("B-2", "Y", "NEW HOLDINGS", "Vanguard Group Inc", "13F HOLDINGS REPORT"),
    ("C-1", "N", "", "Gamma Notice LLC", "13F NOTICE"),
    ("A-0", "N", "", "Alpha Capital", "13F HOLDINGS REPORT"),
]
INFO = [
    ("A-1", "WIDGET CO", "COM", "111111111", 1000000, 100000, "SH", ""),   # superseded
    ("A-1", "WIDGET CO", "NOTE 5% CVT", "111111AB1", 500000, 500000, "SH", ""),
    ("A-1", "WIDGET CO", "COM", "111111111", 200000, 20000, "SH", "Put"),
    ("A-2", "WIDGET CO", "COM", "111111111", 800000, 80000, "SH", ""),     # the restated position
    ("A-2", "GADGET INC", "CL A", "222222222", 300000, 30000, "SH", ""),
    ("B-1", "WIDGET CO", "COM", "111111111", 400000, 40000, "SH", ""),
    ("B-2", "GADGET INC", "CL A", "222222222", 100000, 10000, "SH", ""),   # added by amendment
    ("A-0", "WIDGET CO", "COM", "111111111", 9999999, 999999, "SH", ""),   # wrong period
]


@pytest.fixture()
def archive(monkeypatch):
    body = _archive(SUBS, COVER, INFO)
    monkeypatch.setattr(thirteenf, "fetch", lambda *a, **k: body)
    return thirteenf.positions.__wrapped__("http://example/13f.zip", "2026-03-31")


def test_a_restatement_replaces_and_a_new_holdings_amendment_adds(archive):
    pos = archive["positions"].astype({"filer_cik": str, "cusip": str})
    a_widget = pos[(pos.filer_cik == "0000000001") & (pos.cusip == "111111111")]
    assert a_widget["shares"].tolist() == [80000]           # not 100000, not both
    b_gadget = pos[(pos.filer_cik == "0000000002") & (pos.cusip == "222222222")]
    assert b_gadget["shares"].tolist() == [10000]           # arrived by amendment


def test_notes_puts_and_notices_are_not_positions(archive):
    pos = archive["positions"].astype({"cusip": str, "filer_cik": str})
    assert "111111AB1" not in set(pos.cusip)                # the convertible
    assert pos["shares"].sum() == 80000 + 30000 + 40000 + 10000   # the put's 20000 is gone
    assert "0000000003" not in set(pos.filer_cik)          # 13F-NT has no table


def test_only_the_asked_for_period_is_read(archive):
    pos = archive["positions"]
    assert pos["shares"].max() < 999999                     # the Dec-2025 filing stayed out
    assert archive["filings"] == 3                          # A-2 (superseding A-1), B-1, B-2


def test_index_managers_are_recognised_by_name(archive):
    filers = archive["filers"].set_index("filer_cik")
    assert bool(filers.loc["0000000002", "passive"]) is True
    assert bool(filers.loc["0000000001", "passive"]) is False


def test_the_window_period_is_the_end_of_its_first_month():
    from datetime import date

    assert thirteenf._period_for(date(2026, 3, 1)) == date(2026, 3, 31)
    assert thirteenf._period_for(date(2025, 12, 1)) == date(2025, 12, 31)
    assert thirteenf._period_for(date(2026, 6, 1)) == date(2026, 6, 30)


def test_the_deadline_is_forty_five_days_rolled_to_a_weekday():
    from datetime import date

    assert thirteenf.deadline_for(date(2026, 3, 31)) == date(2026, 5, 15)   # a Friday
    assert thirteenf.deadline_for(date(2025, 12, 31)) == date(2026, 2, 16)  # Feb 14 is a Saturday


# --------------------------------------------------------------------------- #
# The flow table
# --------------------------------------------------------------------------- #
def _positions(rows, names, passive=()):
    """A positions() result from ``(filer, cusip, shares)`` triples."""
    pos = pd.DataFrame(rows, columns=["filer_cik", "cusip", "shares"])
    pos["value"] = pos["shares"] * 10.0
    filers = pd.DataFrame({"filer_cik": sorted({r[0] for r in rows})})
    filers["filer"] = filers["filer_cik"].map(names)
    filers["filed"] = "2026-05-15"
    filers["passive"] = filers["filer_cik"].isin(passive)
    return {"period_end": "x", "positions": pos, "filers": filers,
            "issuer_names": {c: "ISSUER " + c for c in {r[1] for r in rows}}, "filings": len(filers)}


def test_net_change_is_over_common_filers_only(monkeypatch):
    """A filer crossing the threshold is not a trade."""
    now = _positions([("F1", "C1", 100), ("F2", "C1", 50), ("NEW", "C1", 1000)],
                     {"F1": "One", "F2": "Two", "NEW": "Newcomer"})
    then = _positions([("F1", "C1", 60), ("F2", "C1", 50), ("GONE", "C1", 800)],
                      {"F1": "One", "F2": "Two", "GONE": "Departed"})
    monkeypatch.setattr(thirteenf, "positions", lambda url, p: now if url == "now" else then)
    table = thirteenf.flows.__wrapped__("now", "2026-03-31", "then", "2025-12-31")
    row = table.set_index("cusip").loc["C1"]
    assert row["net_change"] == 40                    # F1's +40; the newcomer's 1000 is not flow
    assert row["entering_filer_shares"] == 1000
    assert row["departing_filer_shares"] == 800
    assert row["common_filers"] == 2
    assert row["top_buyers"][0]["filer"] == "One" and row["top_buyers"][0]["held_now"] == 100


def test_everyone_out_and_nothing_left_is_an_identity_change_not_a_sale(monkeypatch):
    holders = ["F{}".format(i) for i in range(6)]
    names = {h: h for h in holders}
    then = _positions([(h, "OLD", 100) for h in holders], names)
    now = _positions([(h, "NEW", 100) for h in holders], names)
    monkeypatch.setattr(thirteenf, "positions", lambda url, p: now if url == "now" else then)
    table = thirteenf.flows.__wrapped__("now", "x", "then", "y").set_index("cusip")
    assert bool(table.loc["OLD", "identity_change_suspected"]) is True
    assert bool(table.loc["NEW", "identity_change_suspected"]) is True


def test_one_holder_selling_out_is_a_sale(monkeypatch):
    then = _positions([("F1", "C1", 100), ("F1", "C2", 5)], {"F1": "Only"})
    now = _positions([("F1", "C2", 5)], {"F1": "Only"})
    monkeypatch.setattr(thirteenf, "positions", lambda url, p: now if url == "now" else then)
    table = thirteenf.flows.__wrapped__("now", "x", "then", "y").set_index("cusip")
    assert table.loc["C1", "net_change"] == -100
    assert bool(table.loc["C1", "identity_change_suspected"]) is False


def test_passive_share_is_index_managers_part_of_gross_flow(monkeypatch):
    then = _positions([("V", "C1", 100), ("A", "C1", 100)], {"V": "Vanguard", "A": "Active"}, passive=("V",))
    now = _positions([("V", "C1", 400), ("A", "C1", 0)], {"V": "Vanguard", "A": "Active"}, passive=("V",))
    monkeypatch.setattr(thirteenf, "positions", lambda url, p: now if url == "now" else then)
    table = thirteenf.flows.__wrapped__("now", "x", "then", "y").set_index("cusip")
    assert table.loc["C1", "gross_flow"] == 400                       # |+300| + |-100|
    assert table.loc["C1", "passive_share"] == pytest.approx(0.75)


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def _cand(**over):
    base = {
        "symbol": "TST", "cusip": "111111111", "issuer": "TEST CO", "known_on": "2026-05-15",
        "period_end": "2026-03-31", "prior_period_end": "2025-12-31",
        "net_change": -1_000_000, "shares_now": 4_000_000, "shares_prior": 5_000_000,
        "gross_flow": 1_200_000, "filers_now": 20, "filers_prior": 21,
        "positions_opened": 1, "positions_closed": 2,
        "entering_filer_shares": 0, "departing_filer_shares": 0,
        "top_sellers": [{"filer": "Seller LP", "change": -800_000, "held_now": 1_500_000,
                         "held_prior": 2_300_000, "passive": False, "filed": "2026-05-14"}],
        "top_buyers": [], "passive_share": 0.05,
        "adv": 100_000.0, "close": 20.0, "sessions": 61,
        "shares_outstanding": 30_000_000.0, "shares_outstanding_prior": 30_000_000.0,
        "domicile": "US-DE",
    }
    base.update(over)
    return base


def test_a_ten_day_exit_at_a_small_cap_is_a_flag_with_its_overhang():
    row = flows.gate(_cand())
    assert row is not None
    assert row["flag"] == "institutional_flow"
    assert row["family"] == "institutional_flow_distribution"
    assert row["days_of_volume"] == pytest.approx(10.0)
    assert row["overhang_days"] == pytest.approx(15.0)     # 1.5M still held / 100k a day
    assert row["market_cap"] == 600_000_000
    assert row["known_on"] == "2026-05-15"                 # the deadline, never the period end
    assert not row["issuance_suspected"] and not row["single_filer_suspect"]


def test_the_same_flow_at_a_large_cap_is_not_a_flag():
    assert flows.gate(_cand(shares_outstanding=300_000_000.0)) is None      # $6B


def test_below_five_days_of_volume_is_absorbed_by_the_tape():
    assert flows.gate(_cand(adv=250_000.0)) is None                          # 4 days


def test_a_new_listing_has_no_average_to_divide_by():
    assert flows.gate(_cand(sessions=30)) is None


def test_an_etf_is_asset_allocation_not_a_view():
    assert flows.gate(_cand(issuer="21shares XRP ETF")) is None


def test_issuance_is_labelled_when_the_company_sold_the_shares():
    # +3M net accumulation while shares outstanding rose 4M (13% of the company).
    row = flows.gate(_cand(net_change=3_000_000, top_sellers=[], top_buyers=[
        {"filer": "RA Capital", "change": 2_000_000, "held_now": 2_000_000, "held_prior": 0,
         "passive": False, "filed": "2026-05-15"}],
        shares_outstanding=34_000_000.0, shares_outstanding_prior=30_000_000.0))
    assert row["issuance_suspected"] is True
    assert row["score"] < 0.3
    assert "likely issuance" in row["summary"]


def test_routine_share_creep_is_not_issuance():
    row = flows.gate(_cand(net_change=3_000_000, top_sellers=[],
                           shares_outstanding=30_300_000.0, shares_outstanding_prior=30_000_000.0))
    assert row["issuance_suspected"] is False              # 1% growth is options and RSUs


def test_one_filer_claiming_a_third_of_the_company_is_suspect():
    row = flows.gate(_cand(top_sellers=[{"filer": "Tiny RIA", "change": -900_000,
                                         "held_now": 11_000_000, "held_prior": 11_900_000,
                                         "passive": False, "filed": "2026-05-15"}]))
    assert row["single_filer_suspect"] is True
    assert "check the filing" in row["summary"]


def test_a_foreign_domicile_is_labelled_and_damped():
    home = flows.gate(_cand())
    away = flows.gate(_cand(domicile="CA-ON"))
    assert away["foreign_domicile"] is True and home["foreign_domicile"] is False
    assert away["score"] == pytest.approx(home["score"] / 2)


def test_index_flow_damps_the_score_but_stays_on_the_row():
    passive = flows.gate(_cand(passive_share=0.8))
    active = flows.gate(_cand(passive_share=0.0))
    assert passive["score"] < active["score"]
    assert passive["passive_share"] == 0.8


def test_direction_filter_and_the_reconciled_symbol():
    assert flows.gate(_cand(), direction="accumulation") is None
    assert flows._reconcile("BRKB", {"BRK-B", "AAPL"}) == "BRK-B"
    assert flows._reconcile("AAPL", {"BRK-B", "AAPL"}) == "AAPL"
    assert flows._reconcile("XYZ", {"BRK-B", "AAPL"}) == "XYZ"


def test_the_flow_command_rejects_a_bad_direction():
    with pytest.raises(ValueError, match="direction"):
        execute("/flagged/flows", direction="sideways")


def test_the_flow_source_is_registered_under_the_flagged_namespace():
    from backend.thesis import sources

    src = sources.resolve(sources.get("institutional_flows"))
    assert src.command == "/flagged/flows"
    assert src.family_namespace == "flagged"
    assert src.resolve_params({"direction": "distribution", "limit": "99"}) == {
        "max_market_cap_bn": 2.0, "min_days_of_volume": 5.0,
        "direction": "distribution", "limit": 40}


# --------------------------------------------------------------------------- #
# Live
# --------------------------------------------------------------------------- #
def test_live_the_data_set_index_lists_windows_with_periods():
    ws = thirteenf.windows()
    assert len(ws) >= 2
    assert ws[0]["period_end"] > ws[1]["period_end"]
    assert ws[0]["deadline"] > ws[0]["period_end"]


def test_live_the_cusip_map_covers_a_small_cap_and_names_the_ticker():
    m = thirteenf.cusip_symbol_map()
    assert len(m) > 5000
    assert (m["cusip"].str.len() == 9).all()


def test_live_the_screen_returns_dated_rows_and_the_per_symbol_read_works():
    result = execute("/flagged/flows", limit=5)
    assert result.extra["crossed_gates"] >= 1
    for row in result.results:
        assert row["known_on"] == result.extra["known_on"]
        assert row["days_of_volume"] >= flows.MIN_DAYS_OF_VOLUME
        assert row["market_cap"] <= flows.MAX_MARKET_CAP
    one = execute("/flagged/flows", symbol=result.results[0]["symbol"])
    assert one.results[0]["symbol"] == result.results[0]["symbol"]
