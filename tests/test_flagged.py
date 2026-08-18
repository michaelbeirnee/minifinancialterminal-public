"""Change detection: what moved between a filer's last two filings.

The detectors are pure functions over a fact table or a parsed document pair,
so most of this runs on hand-built inputs and asserts the arithmetic — the DSO
that should trip the receivables flag, the buyback that should not, the
threshold percentage that must not be read as an exposure. The document tests
run the parser over small HTML in the shape EDGAR actually serves: table cells
becoming ``|``, the table of contents carrying the same "Item 1A" as the
section, an audit firm tagged inline.

The live tests at the bottom follow the rest of the suite and hit SEC and Yahoo.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.core.errors import EmptyDataError
from backend.core.registry import execute
from backend.flagged import CATALOGUE, get, names
from backend.flagged import detectors as dt
from backend.flagged import documents as docs
from backend.flagged import facts as fx
from backend.flagged import market


# --------------------------------------------------------------------------- #
# Fact-table fixtures
# --------------------------------------------------------------------------- #
def _fact(concept, end, val, filed, form="10-K", start=None, unit="USD",
          accn=None, taxonomy="us-gaap"):
    end_ts = pd.Timestamp(end)
    start_ts = pd.Timestamp(start) if start else pd.NaT
    return {
        "taxonomy": taxonomy, "concept": concept, "label": concept, "unit": unit,
        "start": start_ts, "end": end_ts, "val": float(val),
        "accn": accn or "{}-{}".format(form, end), "fy": end_ts.year, "fp": "FY",
        "form": form, "filed": pd.Timestamp(filed), "frame": None,
        "days": (end_ts - start_ts).days if start else float("nan"),
    }


def _table(rows):
    return pd.DataFrame(rows)


def _two_years(revenue, receivables=None, deferred=None, buybacks=None, diluted=None):
    """A filer's last two annual reports as a fact table."""
    rows = []
    for year, rev in zip((2024, 2025), revenue):
        end = "{}-12-31".format(year)
        filed = "{}-02-20".format(year + 1)
        rows.append(_fact("Revenues", end, rev, filed, start="{}-01-01".format(year)))
        if receivables:
            rows.append(_fact("AccountsReceivableNetCurrent", end,
                              receivables[year - 2024], filed))
        if deferred:
            rows.append(_fact("ContractWithCustomerLiabilityCurrent", end,
                              deferred[year - 2024], filed))
        if buybacks:
            rows.append(_fact("PaymentsForRepurchaseOfCommonStock", end,
                              buybacks[year - 2024], filed, start="{}-01-01".format(year)))
        if diluted:
            rows.append(_fact("WeightedAverageNumberOfDilutedSharesOutstanding", end,
                              diluted[year - 2024], filed, start="{}-01-01".format(year),
                              unit="shares"))
    return _table(rows)


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #
def test_every_flag_type_states_how_it_lies():
    """The artifact note is the whole point of the catalogue, not decoration."""
    for flag in CATALOGUE:
        assert len(flag.artifact) > 80, flag.name
        assert flag.read_from in ("document", "xbrl", "index", "estimates", "holdings", "cluster")


def test_the_rating_flag_is_the_only_one_without_a_filing_behind_it():
    vendor_fed = [f.name for f in CATALOGUE if f.read_from == "estimates"]
    assert vendor_fed == ["rating_shift"]
    assert not get("rating_shift").directional


def test_flag_names_are_unique_and_looked_up_case_insensitively():
    assert len(set(names())) == len(names())
    assert get("Auditor_Change").name == "auditor_change"


# --------------------------------------------------------------------------- #
# Fact plumbing
# --------------------------------------------------------------------------- #
def test_the_newest_filing_wins_a_restated_period():
    """A prior year restated by the current 10-K is the version to compare to."""
    facts = _table([
        _fact("Revenues", "2024-12-31", 100, "2025-02-20", start="2024-01-01"),
        _fact("Revenues", "2024-12-31", 105, "2026-02-20", start="2024-01-01"),  # restated
        _fact("Revenues", "2025-12-31", 120, "2026-02-20", start="2025-01-01"),
    ])
    series = fx.concept_series(facts, ("Revenues",), "annual")
    assert series["val"].tolist() == [105.0, 120.0]


def test_a_stale_synonym_does_not_beat_the_tag_that_reaches_the_newest_period():
    facts = _table([
        _fact("SalesRevenueNet", "2017-12-31", 50, "2018-02-20", start="2017-01-01"),
        _fact("Revenues", "2024-12-31", 100, "2025-02-20", start="2024-01-01"),
        _fact("Revenues", "2025-12-31", 120, "2026-02-20", start="2025-01-01"),
    ])
    series = fx.concept_series(facts, ("SalesRevenueNet", "Revenues"), "annual")
    assert series["concept"].iloc[-1] == "Revenues"
    assert len(series) == 2


def test_year_over_year_pairs_on_the_gap_not_the_position():
    """A filer that skipped a year is not compared to two years ago."""
    facts = _table([
        _fact("Revenues", "2023-12-31", 90, "2024-02-20", start="2023-01-01"),
        _fact("Revenues", "2025-12-31", 120, "2026-02-20", start="2025-01-01"),
    ])
    assert fx.year_over_year(fx.concept_series(facts, ("Revenues",))) is None


def test_growth_refuses_a_base_that_cannot_carry_a_percentage():
    assert fx.growth(10.0, 0.0) is None
    assert fx.growth(10.0, -5.0) is None
    assert fx.growth(12.0, 10.0) == pytest.approx(0.2)


def test_first_appearance_is_dated_by_filing_not_by_period():
    """A concept backfilled onto comparative years is first known when filed."""
    facts = _table([
        _fact("GoodwillImpairmentLoss", "2023-12-31", 5, "2026-02-20", start="2023-01-01",
              accn="new"),
        _fact("GoodwillImpairmentLoss", "2025-12-31", 40, "2026-02-20", start="2025-01-01",
              accn="new"),
        _fact("Revenues", "2024-12-31", 100, "2025-02-20", start="2024-01-01", accn="old"),
        _fact("Revenues", "2025-12-31", 120, "2026-02-20", start="2025-01-01", accn="new"),
    ])
    first = fx.first_appearances(facts).set_index("concept")
    assert str(first.loc["GoodwillImpairmentLoss", "first_filed"])[:10] == "2026-02-20"
    assert first.loc["GoodwillImpairmentLoss", "first_accn"] == "new"


def test_silence_is_measured_against_the_previous_filing_of_the_same_form():
    """A 10-Q tags less than a 10-K; that is not a migration."""
    facts = _table([
        _fact("A", "2024-12-31", 1, "2025-02-20", form="10-K", accn="k1"),
        _fact("B", "2024-12-31", 1, "2025-02-20", form="10-K", accn="k1"),
        _fact("C", "2024-12-31", 1, "2025-02-20", form="10-K", accn="k1"),
        _fact("A", "2025-03-31", 1, "2025-05-01", form="10-Q", accn="q1"),
        _fact("A", "2025-06-30", 1, "2025-08-01", form="10-Q", accn="q2"),
        _fact("D", "2025-06-30", 1, "2025-08-01", form="10-Q", accn="q2"),
    ])
    # Against the 10-K, q2 "silenced" B and C. Against the previous 10-Q it
    # silenced nothing — and nothing is the right answer.
    assert fx.silenced_in(facts, "q2") == set()


# --------------------------------------------------------------------------- #
# XBRL detectors
# --------------------------------------------------------------------------- #
def test_receivables_outrunning_sales_fires_on_dso_drift():
    # Revenue +10%, receivables +50%: DSO 36.5 -> 49.8 days.
    facts = _two_years(revenue=(1000, 1100), receivables=(100, 150))
    flags = dt.receivables_flags(facts, "TST")
    assert len(flags) == 1
    row = flags[0]
    assert row["flag"] == "receivables_outrunning_sales"
    assert row["known_on"] == "2026-02-20"          # the filing date, not the period end
    assert row["dso_change_days"] == pytest.approx(13.3, abs=0.1)
    assert row["receivables_growth"] == pytest.approx(0.5)


def test_receivables_growing_with_sales_is_not_a_flag():
    facts = _two_years(revenue=(1000, 1300), receivables=(100, 128))
    assert dt.receivables_flags(facts, "TST") == []


def test_a_small_dso_move_is_invoice_timing_not_a_flag():
    # DSO 36.5 -> 39.5: three days, under the five-day floor.
    facts = _two_years(revenue=(1000, 1000), receivables=(100, 108))
    assert dt.receivables_flags(facts, "TST") == []


def test_deferred_revenue_divergence_reports_both_directions():
    behind = _two_years(revenue=(1000, 1300), deferred=(200, 190))
    ahead = _two_years(revenue=(1000, 1050), deferred=(200, 300))
    a, b = dt.deferred_revenue_flags(behind, "TST"), dt.deferred_revenue_flags(ahead, "TST")
    assert "recognised revenue outrunning" in a[0]["summary"]
    assert "deferred balance outrunning" in b[0]["summary"]
    assert a[0]["divergence"] < 0 < b[0]["divergence"]


def test_a_tag_migration_mid_pair_is_not_a_deferred_collapse():
    """DeferredRevenueCurrent one year, the ASC 606 tag the next: not comparable."""
    rows = [
        _fact("Revenues", "2024-12-31", 1000, "2025-02-20", start="2024-01-01"),
        _fact("Revenues", "2025-12-31", 1050, "2026-02-20", start="2025-01-01"),
        _fact("DeferredRevenueCurrent", "2024-12-31", 300, "2025-02-20"),
        _fact("ContractWithCustomerLiabilityCurrent", "2025-12-31", 310, "2026-02-20"),
    ]
    assert dt.deferred_revenue_flags(_table(rows), "TST") == []


def test_buyback_gap_fires_when_the_count_rises_despite_the_spending():
    facts = _two_years(revenue=(1000, 1000), buybacks=(500, 800),
                       diluted=(1_000_000, 1_030_000))
    flags = dt.buyback_flags(facts, "TST")
    assert len(flags) == 1
    assert flags[0]["share_count_change_pct"] == pytest.approx(0.03)
    assert flags[0]["repurchase_payments"] == 800.0
    assert flags[0]["cumulative_periods"] == 2


def test_a_buyback_that_shrank_the_count_is_doing_its_job():
    facts = _two_years(revenue=(1000, 1000), buybacks=(500, 800),
                       diluted=(1_000_000, 970_000))
    assert dt.buyback_flags(facts, "TST") == []


def test_new_concept_names_the_watched_ones_and_counts_the_rest():
    rows = [
        _fact("Revenues", "2024-12-31", 100, "2025-02-20", start="2024-01-01", accn="old"),
        _fact("Assets", "2024-12-31", 100, "2025-02-20", accn="old"),
        _fact("Revenues", "2025-12-31", 120, "2026-02-20", start="2025-01-01", accn="new"),
        _fact("Assets", "2025-12-31", 130, "2026-02-20", accn="new"),
        _fact("GoodwillImpairmentLoss", "2025-12-31", 40, "2026-02-20",
              start="2025-01-01", accn="new"),
        _fact("SomeDetailTag", "2025-12-31", 1, "2026-02-20", accn="new"),
    ]
    flags = dt.new_concept_flags(_table(rows), "TST")
    assert len(flags) == 1
    row = flags[0]
    assert row["known_on"] == "2026-02-20"
    assert [w["means"] for w in row["watched"]] == ["first goodwill impairment"]
    assert row["unwatched_count"] == 1
    assert row["paired_with_silence"] is False


def test_a_lone_unwatched_concept_is_not_a_row():
    rows = [
        _fact("Revenues", "2024-12-31", 100, "2025-02-20", start="2024-01-01", accn="old"),
        _fact("Revenues", "2025-12-31", 120, "2026-02-20", start="2025-01-01", accn="new"),
        _fact("SomeDetailTag", "2025-12-31", 1, "2026-02-20", accn="new"),
    ]
    assert dt.new_concept_flags(_table(rows), "TST") == []


def test_a_first_filing_tags_everything_for_the_first_time_and_says_nothing():
    rows = [_fact("Revenues", "2025-12-31", 120, "2026-02-20", start="2025-01-01", accn="only")]
    assert dt.new_concept_flags(_table(rows), "TST") == []


# --------------------------------------------------------------------------- #
# Ratings
# --------------------------------------------------------------------------- #
def _actions(rows):
    frame = pd.DataFrame(rows, columns=["GradeDate", "Firm", "ToGrade", "FromGrade", "Action"])
    frame["GradeDate"] = pd.to_datetime(frame["GradeDate"])
    return frame.set_index("GradeDate")


def _mix(now, then):
    keys = ("strongBuy", "buy", "hold", "sell", "strongSell")
    return pd.DataFrame([
        {"period": "0m", **dict(zip(keys, now))},
        {"period": "-1m", **dict(zip(keys, now))},
        {"period": "-2m", **dict(zip(keys, then))},
        {"period": "-3m", **dict(zip(keys, then))},
    ])


TODAY = pd.Timestamp("2026-08-17")


def test_a_one_sided_cluster_of_downgrades_is_a_shift():
    actions = _actions([
        ("2026-08-01", "A", "Hold", "Buy", "down"),
        ("2026-08-02", "B", "Hold", "Buy", "down"),
        ("2026-08-03", "C", "Sell", "Hold", "down"),
        ("2026-08-04", "D", "Buy", "Buy", "main"),
    ])
    flags = dt.rating_flags(actions, _mix((2, 5, 5, 0, 0), (2, 5, 5, 0, 0)), "TST",
                            today=TODAY)
    assert len(flags) == 1
    row = flags[0]
    assert row["direction"] == "down"
    assert row["downgrades"] == 3 and row["upgrades"] == 0
    assert row["known_on"] == "2026-08-03"           # last action, not today
    assert row["covering_desks"] == 12               # from the mix, not the action count
    assert row["read_via"] == "actions"


def test_maintains_are_not_rating_changes():
    actions = _actions([
        ("2026-08-0{}".format(i), "F{}".format(i), "Buy", "Buy", "main") for i in range(1, 8)
    ])
    assert dt.rating_flags(actions, _mix((2, 5, 5, 0, 0), (2, 5, 5, 0, 0)), "TST",
                           today=TODAY) == []


def test_a_desk_that_cut_twice_changed_its_mind_once():
    actions = _actions([
        ("2026-08-01", "A", "Hold", "Buy", "down"),
        ("2026-08-05", "A", "Sell", "Hold", "down"),
        ("2026-08-06", "B", "Hold", "Buy", "down"),
    ])
    # Two distinct desks, below the three-action floor.
    assert dt.rating_flags(actions, _mix((2, 5, 5, 0, 0), (2, 5, 5, 0, 0)), "TST",
                           today=TODAY) == []


def test_three_downgrades_at_a_forty_desk_name_are_not_a_shift():
    actions = _actions([
        ("2026-08-01", "A", "Hold", "Buy", "down"),
        ("2026-08-02", "B", "Hold", "Buy", "down"),
        ("2026-08-03", "C", "Sell", "Hold", "down"),
    ])
    wide = _mix((10, 20, 10, 0, 0), (10, 20, 10, 0, 0))
    assert dt.rating_flags(actions, wide, "TST", today=TODAY) == []


def test_a_mix_drift_fires_without_dated_actions_and_anchors_to_the_month():
    # 12 desks: four Buys became Holds over three months. Mean 4.17 -> 3.83.
    flags = dt.rating_flags(None, _mix((2, 4, 6, 0, 0), (2, 8, 2, 0, 0)), "TST",
                            today=TODAY)
    assert len(flags) == 1
    row = flags[0]
    assert row["read_via"] == "mix"
    assert row["direction"] == "down"
    assert row["known_on"] == "2026-08-01"
    assert row["mix_drift"] == pytest.approx(-0.333, abs=0.01)


def test_actions_and_mix_disagreeing_on_direction_is_not_a_flag():
    actions = _actions([
        ("2026-08-01", "A", "Buy", "Hold", "up"),
        ("2026-08-02", "B", "Buy", "Hold", "up"),
        ("2026-08-03", "C", "Buy", "Hold", "up"),
    ])
    assert dt.rating_flags(actions, _mix((2, 4, 6, 0, 0), (2, 8, 2, 0, 0)), "TST",
                           today=TODAY) == []


# --------------------------------------------------------------------------- #
# Documents: paragraphs and the section
# --------------------------------------------------------------------------- #
TEN_K = """
<html><body>
<p>Apple Inc. | 2025 Form 10-K | 1</p>
<table><tr><td>Item 1A.</td><td>Risk Factors</td><td>5</td></tr>
<tr><td>Item 1B.</td><td>Unresolved Staff Comments</td><td>19</td></tr></table>
<p>Item 1. Business</p>
<p>The Company designs, manufactures and markets smartphones and as described in Item 1A of this report the business is subject to risks.</p>
<p>Item 1A. Risk Factors</p>
<p>Macroeconomic and Industry Risks</p>
<p>The Company depends on component and product manufacturing and logistical services provided by outsourcing partners, many of which are located outside of the U.S. and any disruption could harm results.</p>
<p>The Company faces substantial competition in every market it serves and its competitors may imitate the features of its products and services and price them aggressively.</p>
<p>Item 1B. Unresolved Staff Comments</p>
<p>None.</p>
<p>Item 8. Financial Statements</p>
<p>The Company's third-party cellular network carriers accounted for 34 % and 38 % of total trade receivables as of September 27, 2025 and September 28, 2024, respectively.</p>
<p>As of September 27, 2025, the Company had one customer that represented 10% or more of total trade receivables, which accounted for 12 %.</p>
<p>Revenue from customers headquartered outside of the United States accounted for 31% of total revenue.</p>
<p>/s/ <ix:nonNumeric name="dei:AuditorName" contextRef="c-1">Ernst &amp; Young LLP</ix:nonNumeric></p>
<p>PCAOB Firm ID No. 000<ix:nonNumeric name="dei:AuditorFirmId" contextRef="c-1">42</ix:nonNumeric>.</p>
</body></html>
"""


def test_the_section_is_the_body_not_the_table_of_contents_or_a_cross_reference():
    lines = docs.paragraph_text(TEN_K)
    body = docs.section(lines, "10-K")
    text = " ".join(body)
    assert "outsourcing partners" in text and "substantial competition" in text
    assert "designs, manufactures" not in text          # the cross-reference in Item 1
    assert "trade receivables" not in text              # Item 8


def test_running_headers_are_furniture_not_headings():
    lines = docs.paragraph_text(TEN_K)
    assert not any("Form 10-K | 1" in line for line in lines)


def test_paragraphs_carry_the_heading_above_them():
    paras = docs.paragraphs(docs.section(docs.paragraph_text(TEN_K), "10-K"))
    assert len(paras) == 2
    assert all(p["heading"] == "Macroeconomic and Industry Risks" for p in paras)


def test_an_edited_paragraph_is_the_same_paragraph():
    old = [{"heading": "", "text": "Any breaches in our security measures or those of our third-party data center hosting facilities, cloud computing platform providers could harm our business."}]
    new = [{"heading": "", "text": "Any breaches in our security measures or those of our third-party data center providers, cloud computing platform providers could harm our business."}]
    diff = docs.match_paragraphs(new, old)
    assert diff["added"] == [] and diff["removed"] == []


def test_a_reworded_paragraph_about_the_same_things_is_the_same_paragraph():
    """Bigrams miss this one; content words catch it."""
    old = [{"heading": "", "text": "efforts by hackers or sophisticated groups, such as criminal organizations, state-sponsored organizations or nation-states, to launch coordinated attacks"}]
    new = [{"heading": "", "text": "efforts by threat actors, including criminal organizations, state-sponsored actors and nation-states, to launch coordinated and sustained attacks"}]
    diff = docs.match_paragraphs(new, old)
    assert diff["added"] == [] and diff["removed"] == []


def test_a_genuinely_new_paragraph_is_reported_with_its_nearest_miss():
    old = [{"heading": "", "text": "We rely on third-party data center hosting facilities and cloud computing platform providers located in the United States and other countries."}]
    new = [
        {"heading": "", "text": "We rely on third-party data center hosting facilities and cloud computing platform providers located in the United States and other countries."},
        {"heading": "Groq", "text": "We have entered into an intellectual property license arrangement with Groq that requires us to make substantial payments regardless of the outcome of the arrangement."},
    ]
    diff = docs.match_paragraphs(new, old)
    assert len(diff["added"]) == 1
    assert diff["added"][0]["heading"] == "Groq"
    assert diff["added"][0]["best_match"] < docs.SAME_PARAGRAPH
    assert diff["removed"] == []


def test_a_balanced_diff_is_called_a_rewrite_and_scored_down():
    old = [{"heading": "", "text": "old paragraph number {} about a completely different subject entirely with many words".format(i)} for i in range(6)]
    new = [{"heading": "", "text": "fresh text {} concerning some unrelated other topic altogether with plenty of words".format(i)} for i in range(6)]
    newer = {"symbol": "TST", "form": "10-K", "filing_date": "2026-02-20", "url": "u1"}
    older = {"symbol": "TST", "form": "10-K", "filing_date": "2025-02-20", "url": "u0"}
    flags = dt.risk_factor_flags({"risk_factors": new}, {"risk_factors": old}, newer, older)
    assert {f["flag"] for f in flags} == {"risk_factor_added", "risk_factor_removed"}
    assert all(f["rewrite_suspected"] for f in flags)
    assert all(f["score"] < 0.35 for f in flags)


def test_one_unreadable_section_means_no_diff_not_a_fake_one():
    newer = {"symbol": "TST", "form": "10-K", "filing_date": "2026-02-20", "url": "u1"}
    older = {"symbol": "TST", "form": "10-K", "filing_date": "2025-02-20", "url": "u0"}
    assert dt.risk_factor_flags({"risk_factors": []}, {"risk_factors": [{"heading": "", "text": "x " * 20}]},
                                newer, older) == []


# --------------------------------------------------------------------------- #
# Documents: concentration and the auditor
# --------------------------------------------------------------------------- #
def test_concentration_reads_this_years_figure_and_skips_the_threshold():
    from backend.providers import supplychain

    flat = supplychain._plain_text(TEN_K)
    stmts = {s["key"]: s for s in docs.concentration_statements(flat, None, None, 2025)}
    carriers = stmts["customer|accounts receivable|unspecified"]
    assert carriers["exposure_pct"] == 34.0            # not 38, the comparative
    one = stmts["customer|accounts receivable|one"]
    assert one["exposure_pct"] == 12.0                 # not 10, the threshold
    assert "customer|revenue|unspecified" not in stmts  # geography is not concentration


def test_a_comparative_sentence_is_last_years_disclosure():
    text = "Two direct customers accounted for 17 % and 16 % of our accounts receivable balance as of January 26, 2025."
    assert docs.concentration_statements(text, None, None, period_year=2026) == []
    assert len(docs.concentration_statements(text, None, None, period_year=2025)) == 1


def test_a_negated_statement_carries_the_threshold_not_an_exposure():
    text = "No customer accounted for more than 10% of net sales in fiscal 2025."
    (stmt,) = docs.concentration_statements(text, None, None, 2025)
    assert stmt["negated"] is True
    assert stmt["exposure_pct"] is None and stmt["threshold_pct"] == 10.0
    assert stmt["key"] == "customer|net sales|none"


def test_concentration_diff_separates_appeared_vanished_and_held():
    old = docs.concentration_statements(
        "One customer accounted for 15% of revenue. Two vendors accounted for 40% of purchases.", None, None, 2024)
    new = docs.concentration_statements(
        "One customer accounted for 22% of revenue. Three customers accounted for 25%, 18% and 13% of accounts receivable.", None, None, 2025)
    diff = docs.diff_concentration(new, old)
    assert [s["key"] for s in diff["appeared"]] == ["customer|accounts receivable|three"]
    assert [s["key"] for s in diff["vanished"]] == ["supplier|purchases|two"]
    assert diff["held"][0]["change_pct_points"] == pytest.approx(7.0)


def test_the_auditor_is_read_from_the_inline_xbrl_tag():
    from backend.providers import supplychain

    reading = docs.auditor_in(TEN_K, supplychain._plain_text(TEN_K))
    assert reading == {
        "auditor_firm_id": "42", "auditor": "Ernst & Young LLP",
        "auditor_location": None, "auditor_source": "inline XBRL (dei:AuditorName)",
    }
    assert docs.auditor_key(reading) == "pcaob:42"


def test_a_renamed_firm_with_the_same_pcaob_id_is_the_same_auditor():
    a = {"auditor_firm_id": "42", "auditor": "Ernst & Young LLP"}
    b = {"auditor_firm_id": "42", "auditor": "EY LLP"}
    assert docs.auditor_key(a) == docs.auditor_key(b)


def test_without_an_id_the_name_is_compared_on_letters_alone():
    a = {"auditor_firm_id": None, "auditor": "Ernst & Young LLP"}
    b = {"auditor_firm_id": None, "auditor": "ERNST &YOUNG, LLP"}
    assert docs.auditor_key(a) == docs.auditor_key(b)


def test_an_8k_item_401_dates_the_auditor_change_and_wins_over_the_cover_page():
    newer = {"symbol": "TST", "form": "10-K", "filing_date": "2026-02-20", "url": "u1"}
    older = {"symbol": "TST", "form": "10-K", "filing_date": "2025-02-20", "url": "u0"}
    new_read = {"auditor": {"auditor_firm_id": "238", "auditor": "PricewaterhouseCoopers LLP"}}
    old_read = {"auditor": {"auditor_firm_id": "42", "auditor": "Ernst & Young LLP"}}
    eightk = pd.DataFrame([{"filing_date": pd.Timestamp("2025-06-03"), "url": "u-8k", "items": "4.01"}])
    (flag,) = dt.auditor_flags(new_read, old_read, newer, older, eightk)
    assert flag["known_on"] == "2025-06-03"
    assert flag["filing_url"] == "u-8k"
    assert flag["prior_auditor"] == "Ernst & Young LLP"
    # Cover pages alone still fire, dated to the annual filing.
    (cover_only,) = dt.auditor_flags(new_read, old_read, newer, older, None)
    assert cover_only["known_on"] == "2026-02-20"
    assert cover_only["score"] < flag["score"]


def test_the_same_auditor_both_years_is_not_a_flag():
    newer = {"symbol": "TST", "form": "10-K", "filing_date": "2026-02-20", "url": "u1"}
    older = {"symbol": "TST", "form": "10-K", "filing_date": "2025-02-20", "url": "u0"}
    same = {"auditor": {"auditor_firm_id": "42", "auditor": "Ernst & Young LLP"}}
    assert dt.auditor_flags(same, same, newer, older, None) == []


# --------------------------------------------------------------------------- #
# Market screens: the join and the ranking
# --------------------------------------------------------------------------- #
def _frame_rows(rows):
    return pd.DataFrame(rows, columns=["accn", "cik", "entityName", "end", "val"])


def test_the_market_join_refuses_a_balance_that_does_not_end_with_the_flow(monkeypatch):
    """A June year-end's December balance against its June revenue is not DSO."""
    revenue_now = _frame_rows([("a1", "0000000001", "June Co", "2025-06-30", 1000),
                               ("a2", "0000000002", "Dec Co", "2025-12-31", 1000)])
    revenue_then = _frame_rows([("b1", "0000000001", "June Co", "2024-06-30", 900),
                                ("b2", "0000000002", "Dec Co", "2024-12-31", 900)])
    ar_now = _frame_rows([("a1", "0000000001", "June Co", "2025-12-31", 900),   # a 10-Q balance
                          ("a2", "0000000002", "Dec Co", "2025-12-31", 200)])
    ar_then = _frame_rows([("b1", "0000000001", "June Co", "2024-12-31", 100),
                           ("b2", "0000000002", "Dec Co", "2024-12-31", 100)])
    calls = iter([revenue_now, revenue_then, ar_now, ar_then])
    monkeypatch.setattr(market, "_revenue_frame", lambda period: next(calls))
    monkeypatch.setattr(market, "_frame", lambda tag, period, unit="USD": next(calls))
    monkeypatch.setattr(market, "_symbol_map", lambda: {"0000000001": "JUN", "0000000002": "DEC"})
    monkeypatch.setattr(market, "_filed_dates", lambda rows, workers=6: [r.__setitem__("known_on", "2026-02-20") for r in rows])
    rows, meta = market.receivables_screen(year=2025, min_revenue=0)
    assert [r["symbol"] for r in rows] == ["DEC"]
    assert meta["misaligned_periods_dropped"] == 1


def test_ranking_is_capped_then_by_size(monkeypatch):
    """The most pathological denominator does not lead the list."""
    rows = [
        {"cik": "1", "dso_change_days": 400.0, "revenue": 60e6, "accession_number": "x"},
        {"cik": "2", "dso_change_days": 45.0, "revenue": 5e9, "accession_number": "y"},
        {"cik": "3", "dso_change_days": 12.0, "revenue": 9e9, "accession_number": "z"},
    ]
    monkeypatch.setattr(market, "_symbol_map", lambda: {"1": "TINY", "2": "BIG", "3": "HUGE"})
    monkeypatch.setattr(market, "_filed_dates", lambda rows, workers=6: [r.__setitem__("known_on", "2026-02-20") for r in rows])
    ranked, meta = market._finish(rows, "receivables_outrunning_sales", 10,
                                  "dso_change_days", market.DSO_CAP_DAYS, "revenue", universe=100)
    # Both TINY and BIG are past the cap; BIG has more business behind it.
    assert [r["symbol"] for r in ranked] == ["BIG", "TINY", "HUGE"]
    assert ranked[0]["market_percentile"] == 100.0
    assert meta["universe"] == 100


def test_the_catalogue_command_filters_by_source():
    result = execute("/flagged/catalogue", read_from="document")
    assert {r["flag"] for r in result.results} == {
        "risk_factor_added", "risk_factor_removed",
        "concentration_appeared", "concentration_vanished"}
    with pytest.raises(EmptyDataError):
        execute("/flagged/catalogue", read_from="carrier pigeon")


def test_scan_rejects_an_unknown_flag_type():
    with pytest.raises(ValueError, match="Unknown flag type"):
        execute("/flagged/scan", symbol="AAPL", kinds="vibes")


def test_the_market_screen_is_registered_as_an_idea_source():
    from backend.thesis import sources

    src = sources.resolve(sources.get("flagged_market"))
    assert src.command == "/flagged/market"
    assert src.family_namespace == "flagged"        # same log as the per-symbol scan
    assert src.resolve_params({"screen": "buybacks", "limit": "999"}) == {
        "screen": "buybacks", "year": None, "limit": 40}


# --------------------------------------------------------------------------- #
# Live
# --------------------------------------------------------------------------- #
def test_live_the_fact_table_carries_filing_dates():
    table = fx.fact_table("AAPL")
    assert {"concept", "filed", "accn", "form", "end", "val"} <= set(table.columns)
    assert table["filed"].notna().all()
    filings = fx.latest_filings(table, limit=2)
    assert filings[0]["filed"] > filings[1]["filed"]


def test_live_two_annual_reports_are_read_and_the_auditor_is_tagged():
    from backend.providers import sec

    newer, older = docs.annual_pair("AAPL")
    assert newer["filing_date"] > older["filing_date"]
    read = docs.read(newer["url"], newer["form"], sec.cik_for("AAPL"), 2025)
    assert read["auditor"]["auditor_firm_id"] == "42"
    assert len(read["risk_factors"]) > 40
    assert any(s["role"] == "customer" for s in read["concentration"])


def test_live_the_market_screen_ranks_the_whole_market():
    result = execute("/flagged/market", screen="receivables", limit=10)
    assert result.extra["universe"] > 500
    assert len(result.results) == 10
    assert result.results[0]["market_percentile"] >= result.results[-1]["market_percentile"]
    assert all(r["known_on"] for r in result.results)


def test_live_the_rest_endpoint_serves_a_scan(auth_client):
    r = auth_client.get("/api/v1/flagged/scan",
                        params={"symbol": "NVDA", "kinds": "concentration_appeared,concentration_vanished,new_accounting_concept"})
    assert r.status_code in (200, 404)     # a quiet filer is a 404, by platform convention
    if r.status_code == 200:
        body = r.json()
        assert body["provider"] in ("sec", "yahoo")
        assert all("known_on" in row for row in body["results"])
