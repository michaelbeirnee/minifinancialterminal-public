"""Revenue disaggregated out of the filings' own XBRL.

The reading rules are tested against contexts and facts written out here rather
than against live filings. What matters is that a fact split on one axis is
picked up and pointed at the right breakdown, that a cross-tab cell is not, and
that a filer tagging two levels of one axis ends up with the finer of them —
properties of the parser, which a filer re-cutting its segments next year should
not be able to turn red.

The network-backed tests at the bottom follow the rest of the suite and hit
EDGAR live.
"""
import pandas as pd
import pytest

from backend.core.errors import EmptyDataError
from backend.providers import segments as sg


# --------------------------------------------------------------------------- #
# Which breakdown a fact belongs to
# --------------------------------------------------------------------------- #
def test_axes_map_to_their_breakdown_whatever_the_prefix():
    # The same axis moved from us-gaap to srt in 2021; both must still read.
    assert sg._dimension_of("us-gaap:StatementBusinessSegmentsAxis") == "business"
    assert sg._dimension_of("srt:StatementGeographicalAxis") == "geographic"
    assert sg._dimension_of("us-gaap:StatementGeographicalAxis") == "geographic"
    assert sg._dimension_of("srt:ProductOrServiceAxis") == "product"
    assert sg._dimension_of("us-gaap:FairValueByFairValueHierarchyLevelAxis") is None


def test_geography_is_not_read_as_a_business_segment():
    """The catch-all "segment" rule must not claim a geographical axis."""
    assert sg._dimension_of("srt:StatementGeographicalSegmentAxis") == "geographic"


def test_a_single_axis_is_the_breakdown():
    assert sg._breakdown({"srt:ProductOrServiceAxis": "aapl:IPhoneMember"}) == (
        "product", "aapl:IPhoneMember")


def test_a_cross_tab_cell_is_not_a_breakdown():
    """Segment x geography counts the same revenue twice if both are kept."""
    assert sg._breakdown({
        "us-gaap:StatementBusinessSegmentsAxis": "xom:UpstreamMember",
        "srt:StatementGeographicalAxis": "country:US",
    }) is None


def test_an_operating_segment_qualifier_is_allowed_through():
    assert sg._breakdown({
        "srt:ConsolidationItemsAxis": "us-gaap:OperatingSegmentsMember",
        "us-gaap:StatementBusinessSegmentsAxis": "aapl:AmericasSegmentMember",
    }) == ("business", "aapl:AmericasSegmentMember")


def test_reconciling_items_and_eliminations_are_not_segments():
    for member in ("us-gaap:IntersegmentEliminationMember",
                   "us-gaap:MaterialReconcilingItemsMember"):
        assert sg._breakdown({
            "srt:ConsolidationItemsAxis": member,
            "us-gaap:StatementBusinessSegmentsAxis": "aapl:AmericasSegmentMember",
        }) is None


def test_a_qualifier_on_its_own_is_the_total_not_a_segment():
    assert sg._breakdown({"srt:ConsolidationItemsAxis": "us-gaap:OperatingSegmentsMember"}) is None


def test_an_unread_axis_disqualifies_the_fact():
    """A forecast is tagged like everything else and must not read as a segment."""
    assert sg._breakdown({
        "us-gaap:StatementBusinessSegmentsAxis": "aapl:AmericasSegmentMember",
        "srt:StatementScenarioAxis": "srt:ScenarioForecastMember",
    }) is None


# --------------------------------------------------------------------------- #
# Naming a member
# --------------------------------------------------------------------------- #
def test_the_filings_label_wins_and_loses_its_member_suffix():
    labels = {"aapl_IPhoneMember": "iPhone [Member]"}
    assert sg._member_label("aapl:IPhoneMember", labels) == "iPhone"


def test_capitalised_taxonomy_labels_are_made_readable():
    # country:US is "UNITED STATES" in the SEC's own taxonomy.
    assert sg._member_label("country:US", {"country_US": "UNITED STATES"}) == "United States"
    # One capitalised word is an acronym, not shouting.
    assert sg._member_label("nvda:EMEAMember", {"nvda_EMEAMember": "EMEA"}) == "EMEA"


def test_labels_arrive_xml_escaped():
    labels = {"nvda_ComputeAndNetworkingMember": "Compute &amp; Networking"}
    assert sg._member_label("nvda:ComputeAndNetworkingMember", labels) == "Compute & Networking"


def test_an_unlabelled_member_falls_back_to_its_name():
    assert sg._member_label("aapl:AmericasSegmentMember", {}) == "Americas"
    assert sg._member_label("msft:MorePersonalComputingMember", {}) == "More Personal Computing"


# --------------------------------------------------------------------------- #
# Two levels of one axis
# --------------------------------------------------------------------------- #
def test_a_member_that_is_the_sum_of_its_neighbours_gives_way_to_them():
    """Apple tags Products alongside the four hardware lines that make it up."""
    values = {"Products": 307003, "Services": 109158, "iPhone": 209586,
              "Mac": 33708, "iPad": 28023, "Wearables": 35686}
    kept, dropped = sg._drop_rollups(values, total=416161)
    assert dropped == ["Products"]
    assert sum(values[k] for k in kept) == 416161


def test_a_breakdown_that_adds_up_is_left_alone():
    """Coincidence is not evidence: 30 + 20 = 50 changes nothing here."""
    values = {"A": 50, "B": 30, "C": 20}
    kept, dropped = sg._drop_rollups(values, total=100)
    assert dropped == []
    assert set(kept) == {"A", "B", "C"}


def test_one_member_equalling_another_is_not_a_roll_up():
    assert not sg._subset_sums_to([50, 10], 50)      # a single equal sibling
    assert sg._subset_sums_to([40, 10, 7], 50)       # two of them, exactly
    assert not sg._subset_sums_to([40, 11, 7], 50)   # nothing lands on it


def test_the_finer_of_two_tagged_tables_is_the_one_kept():
    """Microsoft tags Product/Service on the income statement and eleven
    product lines in the revenue note, both against the same axis."""
    values = {"Product": 64696, "Service and Other": 267143,
              "Server products": 129425, "Microsoft 365": 101997, "Other lines": 99721}
    roles = {"Product": ["Role_StatementINCOMESTATEMENTS"],
             "Service and Other": ["Role_StatementINCOMESTATEMENTS"],
             "Server products": ["Role_DisclosureRevenue"],
             "Microsoft 365": ["Role_DisclosureRevenue"],
             "Other lines": ["Role_DisclosureRevenue"]}
    kept, dropped, role = sg._resolve(values, roles, total=331839)
    assert role == "Role_DisclosureRevenue"
    assert set(kept) == {"Server products", "Microsoft 365", "Other lines"}
    assert set(dropped) == {"Product", "Service and Other"}


def test_a_table_that_does_not_resolve_the_over_count_is_not_taken():
    """Better to report the over-count than to pick an arbitrary subset."""
    values = {"A": 80, "B": 70, "C": 60}
    roles = {"A": ["Role_Goodwill"], "B": ["Role_Goodwill"], "C": ["Role_Goodwill"]}
    kept, dropped, role = sg._resolve(values, roles, total=100)
    assert role is None
    assert dropped == []
    assert set(kept) == {"A", "B", "C"}


def test_a_group_that_adds_up_is_never_second_guessed():
    values = {"A": 60, "B": 40}
    kept, dropped, role = sg._resolve(values, {}, total=100)
    assert (set(kept), dropped, role) == ({"A", "B"}, [], None)


# --------------------------------------------------------------------------- #
# Restatements
# --------------------------------------------------------------------------- #
def _facts(rows):
    frame = pd.DataFrame(rows)
    frame["filed"] = pd.to_datetime(frame["filed"])
    return frame


def test_the_newest_filings_version_of_a_period_is_the_one_used():
    """A renamed segment would otherwise report the same year twice."""
    frame = _facts([
        {"dimension": "product", "start": "2024-07-01", "end": "2025-06-30",
         "member": "msft:GamingMember", "filed": "2025-07-30"},
        {"dimension": "product", "start": "2024-07-01", "end": "2025-06-30",
         "member": "msft:SearchMember", "filed": "2025-07-30"},
        {"dimension": "product", "start": "2024-07-01", "end": "2025-06-30",
         "member": "msft:XBOXMember", "filed": "2026-07-29"},
        {"dimension": "product", "start": "2024-07-01", "end": "2025-06-30",
         "member": "msft:SearchAdvertisingMember", "filed": "2026-07-29"},
    ])
    kept = sg._restated(frame)
    assert set(kept["member"]) == {"msft:XBOXMember", "msft:SearchAdvertisingMember"}


def test_a_passing_mention_does_not_supersede_a_whole_table():
    frame = _facts([
        {"dimension": "business", "start": "2024-01-01", "end": "2024-12-31",
         "member": "x:AMember", "filed": "2025-02-01"},
        {"dimension": "business", "start": "2024-01-01", "end": "2024-12-31",
         "member": "x:BMember", "filed": "2025-02-01"},
        {"dimension": "business", "start": "2024-01-01", "end": "2024-12-31",
         "member": "x:AMember", "filed": "2026-02-01"},
    ])
    assert set(sg._restated(frame)["member"]) == {"x:AMember", "x:BMember"}


def test_the_quarter_and_the_year_to_date_are_kept_apart():
    """Both spans end on the same day, and Q4 is later worked out from the YTD."""
    frame = _facts([
        {"dimension": "business", "start": "2025-04-01", "end": "2025-06-30",
         "member": "x:AMember", "filed": "2025-07-30"},
        {"dimension": "business", "start": "2025-01-01", "end": "2025-06-30",
         "member": "x:AMember", "filed": "2025-07-30"},
    ])
    assert len(sg._restated(frame)) == 2


def test_a_banks_taxable_equivalent_line_ranks_below_its_plain_one():
    """It stands in where a bank tags nothing else — it does not replace it."""
    assert (sg._RANK["RevenuesNetOfInterestExpense"]
            < sg._RANK["RevenuesNetOfInterestExpenseFullTaxEquivalentBasis"])


def test_the_preferred_concept_wins_a_tie_inside_one_filing():
    frame = pd.DataFrame([
        {"start": "2024-01-01", "end": "2024-12-31", "value": 90,
         "rank": sg._RANK["RevenuesNetOfInterestExpenseFullTaxEquivalentBasis"],
         "filed": pd.Timestamp("2025-02-01")},
        {"start": "2024-01-01", "end": "2024-12-31", "value": 89,
         "rank": sg._RANK["RevenuesNetOfInterestExpense"],
         "filed": pd.Timestamp("2025-02-01")},
    ])
    assert sg._period_series(frame, "annual").to_dict() == {pd.Timestamp("2024-12-31"): 89.0}


def test_a_restated_period_takes_the_newest_filings_number():
    frame = pd.DataFrame([
        {"start": "2024-01-01", "end": "2024-12-31", "value": 100, "rank": 0,
         "filed": pd.Timestamp("2025-02-01")},
        {"start": "2024-01-01", "end": "2024-12-31", "value": 110, "rank": 0,
         "filed": pd.Timestamp("2026-02-01")},
        # A quarter, on an annual basis: not this period's number at all.
        {"start": "2024-10-01", "end": "2024-12-31", "value": 30, "rank": 0,
         "filed": pd.Timestamp("2026-02-01")},
    ])
    series = sg._period_series(frame, "annual")
    assert series.to_dict() == {pd.Timestamp("2024-12-31"): 110.0}


# --------------------------------------------------------------------------- #
# Reading a filing's parts
# --------------------------------------------------------------------------- #
def test_the_instance_is_picked_out_of_the_filing_folder():
    names = ["aapl-20250927.htm", "aapl-20250927.xsd", "aapl-20250927_cal.xml",
             "aapl-20250927_lab.xml", "aapl-20250927_htm.xml", "FilingSummary.xml", "R2.xml"]
    assert sg._instance_name(names, "aapl-20250927.htm") == "aapl-20250927_htm.xml"
    # A filing old enough to predate inline XBRL ships its instance on its own.
    assert sg._instance_name(["abc-20140101.xml", "abc-20140101_lab.xml", "FilingSummary.xml"],
                             "abc-10k.htm") == "abc-20140101.xml"
    assert sg._instance_name(["FilingSummary.xml", "R2.xml"], "x.htm") is None


def test_metalinks_gives_labels_tables_and_where_a_member_is_presented():
    payload = {"instance": {"msft-20260630.htm": {
        "report": {"R4": {"role": "http://x/role/Role_DisclosureRevenue",
                          "shortName": "Revenue by Product (Detail)"}},
        "tag": {
            "msft_XBOXMember": {
                "presentation": ["http://x/role/Role_DisclosureRevenue"],
                "lang": {"en-us": {"role": {"label": "XBOX [Member]", "terseLabel": "Xbox"}}},
            },
        },
    }}}
    found = sg._from_metalinks(payload)
    assert found["labels"]["msft_XBOXMember"] == "Xbox"      # the terse label leads
    assert found["roles"]["msft_XBOXMember"] == ["Role_DisclosureRevenue"]
    assert found["tables"]["Role_DisclosureRevenue"] == "Revenue by Product (Detail)"


def test_role_uris_are_compared_without_their_taxonomy_date():
    """The same table has a new URI every year; the name on the end does not."""
    assert (sg._role_name("http://www.microsoft.com/20260630/taxonomy/role/Role_Revenue")
            == sg._role_name("http://www.microsoft.com/20250630/taxonomy/role/Role_Revenue"))


# --------------------------------------------------------------------------- #
# Saying what does not add up
# --------------------------------------------------------------------------- #
def test_a_breakdown_that_does_not_add_up_says_so_either_way():
    over, under = sg._coverage_warnings([
        {"section": "Reportable segments", "coverage": 1.33},
        {"section": "Geography", "coverage": 0.44},
    ])
    assert "133%" in over and "between segments" in over
    assert "44%" in under and "discloses no more" in under
    # A breakdown that adds up needs no explaining.
    assert sg._coverage_warnings([{"section": "Geography", "coverage": 1.0}]) == []


# --------------------------------------------------------------------------- #
# Live: EDGAR
# --------------------------------------------------------------------------- #
def test_apple_reports_all_three_breakdowns():
    rows, meta = sg.revenue_segments("AAPL", period="annual", limit=4)
    newest = meta["periods"][0]
    by_dimension = {d["dimension"]: d for d in meta["dimensions"]}
    assert set(by_dimension) == {"business", "geographic", "product"}

    segments_of = lambda dim: {r["segment"] for r in rows if r["dimension"] == dim}   # noqa: E731
    assert {"Americas", "Europe", "Greater China", "Japan"} <= segments_of("business")
    assert {"iPhone", "Mac", "iPad", "Services"} <= segments_of("product")

    # Apple tags Products alongside the hardware lines that make it up; the
    # finer split is what survives, so each breakdown adds up to revenue once.
    total = next(r for r in rows if r["dimension"] == "total")[newest]
    for dim, entry in by_dimension.items():
        disclosed = next(r for r in rows
                         if r["dimension"] == dim and r["weight"] == "subtotal")
        assert disclosed[newest] == pytest.approx(total, rel=0.02)
        assert entry["coverage"] == pytest.approx(1.0, abs=0.02)
    assert "Products" in meta["superseded"]

    # Every row is traceable to the filings it was read out of.
    assert all(f["url"].startswith("https://www.sec.gov/") for f in meta["filings"])
    assert meta["currency"] == "USD"


def test_quarterly_segments_include_the_fiscal_fourth_quarter():
    """Nobody files fiscal Q4 on its own; it is the year less the nine months."""
    rows, meta = sg.revenue_segments("AAPL", period="quarter", limit=6)
    assert len(meta["periods"]) >= 5
    americas = next(r for r in rows if r["segment"] == "Americas")
    reported = [p for p in meta["periods"] if americas[p] is not None]
    assert len(reported) >= 5
    # A quarter of Apple's Americas revenue, not a year of it.
    assert all(2e10 < americas[p] < 1e11 for p in reported)


def test_one_breakdown_can_be_asked_for_on_its_own():
    rows, meta = sg.revenue_segments("AAPL", period="annual", limit=2, dimension="geographic")
    assert {r["dimension"] for r in rows} == {"geographic", "total"}
    assert [d["dimension"] for d in meta["dimensions"]] == ["geographic"]


def test_a_company_that_files_nothing_says_so():
    with pytest.raises(EmptyDataError):
        sg.revenue_segments("SPY")


def test_revenue_segments_endpoint(auth_client):
    r = auth_client.get("/api/v1/equity/fundamental/revenue_segments?symbol=MSFT&limit=3")
    assert r.status_code == 200
    body = r.json()
    rows, extra = body["results"], body["extra"]
    assert rows and extra["symbol"] == "MSFT"
    assert body["provider"] == "sec"

    newest = extra["periods"][0]
    segments_of = {r["segment"] for r in rows if r["dimension"] == "business"}
    assert {"Intelligent Cloud", "More Personal Computing"} <= segments_of
    # Microsoft tags its product lines twice, at two levels of the same axis.
    products = [r for r in rows if r["dimension"] == "product" and r["weight"] == ""]
    total = next(r for r in rows if r["dimension"] == "total")[newest]
    assert sum(r[newest] for r in products if r[newest]) == pytest.approx(total, rel=0.02)
    assert all(0 <= r["revenue_share"] <= 1 for r in products)


def test_an_etf_has_no_segments_to_report(auth_client):
    r = auth_client.get("/api/v1/equity/fundamental/revenue_segments?symbol=QQQ")
    assert r.status_code == 404
