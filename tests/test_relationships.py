"""Supply-chain relationship mining.

The reading rules are tested against paragraphs written out here rather than
against live filings: what matters is that a sentence stating a concentration
is picked up, pointed the right way and given the right number, and that a
sentence merely containing a company name and a percentage is not. Those are
properties of the parser, and a filing that happens to be re-worded next year
should not be able to turn one of them red.

The network-backed tests at the bottom follow the rest of the suite and hit
EDGAR live.
"""
import pytest

from backend.providers import supplychain as sc

APPLE = ["Apple Inc", "Apple"]


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #
def test_aliases_strip_legal_form():
    assert sc.aliases_for("Qorvo, Inc.") == ["Qorvo, Inc", "Qorvo"]
    assert sc.aliases_for("NVIDIA Corporation") == ["NVIDIA Corporation", "NVIDIA"]
    # Nothing to strip: one alias, not a duplicate pair.
    assert sc.aliases_for("Fabrinet") == ["Fabrinet"]


def test_aliases_keep_short_names_whole():
    """"Ford Motor Co" must not shrink past "Ford Motor" into nothing."""
    assert sc.aliases_for("Ford Motor Company") == ["Ford Motor Company", "Ford Motor"]


def test_register_names_expand_into_prose():
    # No filing writes "NVIDIA CORP", so searching EDGAR for it finds nothing.
    assert sc._expand_register_name("NVIDIA CORP") == "NVIDIA Corporation"
    assert sc._expand_register_name("FORD MOTOR CO") == "FORD MOTOR Company"
    assert sc._expand_register_name("QUALCOMM INC/DE") == "QUALCOMM Inc"


# --------------------------------------------------------------------------- #
# Reading a disclosure
# --------------------------------------------------------------------------- #
def test_reads_a_supplier_disclosure():
    found = sc.disclosures_in(
        "Direct sales to Apple Inc. accounted for 27.7% of our net sales for "
        "the year ended December 31, 2023.", APPLE)
    assert len(found) == 1
    assert found[0]["exposure_pct"] == 27.7
    assert found[0]["relationship"] == "supplier"
    assert found[0]["exposure_basis"] == "net sales"


def test_reseller_is_a_customer_not_a_supplier():
    """Money flows the other way when the filer is moving the subject's goods."""
    found = sc.disclosures_in(
        "For example, sales of Apple Inc. products and services comprised "
        "approximately 12%, 12%, and 11% of our total revenue.", APPLE)
    assert found[0]["relationship"] == "customer"
    assert found[0]["exposure_pct"] == 12.0


def test_buying_from_the_subject_is_a_customer():
    found = sc.disclosures_in(
        "Tesla, Inc. accounted for approximately 87% of our energy storage "
        "system purchases.", ["Tesla, Inc", "Tesla"])
    assert found[0]["relationship"] == "customer"
    assert found[0]["exposure_pct"] == 87.0


def test_reads_the_current_year_out_of_a_table_row():
    """Concentration tables run newest-first and carry no wording of their own."""
    found = sc.disclosures_in(
        "Sales to the following customer accounted for 10% or more of net "
        "revenue: | Apple, Inc. | 11 % | 17 % | 19 %", APPLE)
    assert found[0]["exposure_pct"] == 11.0


def test_ignores_commentary_that_merely_mentions_the_company():
    """A name and a number in one sentence is not a relationship."""
    assert sc.disclosures_in(
        "NVIDIA noted in their Q3 result that its GPU installed base is fully "
        "utilized as demand for AI infrastructure continues to exceed "
        "expectations, with compute growing 56% year-on-year across customer "
        "segments.", ["NVIDIA Corporation", "NVIDIA"]) == []


def test_ignores_equity_plan_boilerplate():
    """"Purchase" in a 10-K is far more often about shares than about goods."""
    assert sc.disclosures_in(
        "Employees who participate in the NVIDIA Corporation Amended and "
        "Restated 2012 Employee Stock Purchase Plan may purchase shares at 85% "
        "of fair market value.", ["NVIDIA Corporation", "NVIDIA"]) == []


def test_a_lowercase_match_is_a_verb_not_a_company():
    """The guard that keeps "we target 30%" off Target Corporation's map."""
    assert sc.disclosures_in(
        "We target a 30% gross margin across our customer base and revenue "
        "lines.", ["Target Corporation", "Target"]) == []


def test_table_cells_do_not_split_the_name_off_its_number():
    text = sc._plain_text(
        "<table><tr><td>Apple, Inc.</td><td>11 %</td><td>17 %</td></tr></table>")
    assert "|" in text                      # cells are marked, so numbers cannot glue
    assert sc._SENTENCE.split(text) == [text]   # …but the row stays one sentence


def test_percentages_far_from_the_name_are_not_attributed_to_it():
    filler = "Our results depend on many factors described elsewhere. " * 6
    assert sc.disclosures_in(
        "Apple Inc. is one of several parties we work with. " + filler +
        "Revenue from our largest customer accounted for 44% of our revenues.",
        APPLE) == []


# --------------------------------------------------------------------------- #
# Which side a filer ends up on
# --------------------------------------------------------------------------- #
def test_side_is_a_vote_not_the_biggest_number():
    """A distributor states its position twice; one stray label must not win."""
    found = [
        {"relationship": "supplier", "exposure_pct": 21.0},
        {"relationship": "customer", "exposure_pct": 12.0},
        {"relationship": "customer", "exposure_pct": 10.0},
    ]
    assert sc._side_of(found) == "customer"
    assert sc._side_of([{"relationship": "supplier", "exposure_pct": 5.0}]) == "supplier"


# --------------------------------------------------------------------------- #
# Live: EDGAR and the assembled graph
# --------------------------------------------------------------------------- #
def test_counterparties_finds_apples_disclosed_suppliers():
    df = sc.counterparties("AAPL")
    assert not df.empty
    tickers = set(df["symbol"].dropna())
    # Cirrus Logic and Qorvo have named Apple in every 10-K for years.
    assert tickers & {"CRUS", "QRVO", "SWKS", "AVGO"}
    assert (df["exposure_pct"] > 0).all()
    assert (df["exposure_pct"] <= 100).all()
    assert df["quote"].str.len().min() > 20
    assert df["filing_url"].str.startswith("https://www.sec.gov/").all()


def test_graph_endpoint(auth_client):
    r = auth_client.get("/api/v1/equity/relationships/graph?symbol=AAPL")
    assert r.status_code == 200
    body = r.json()
    rows = body["results"]
    assert rows
    assert {row["relationship"] for row in rows} <= {"supplier", "customer", "peer"}
    assert body["extra"]["subject"]["symbol"] == "AAPL"
    assert body["extra"]["counts"]["supplier"] > 0
    # One row per company, and never the subject itself.
    symbols = [row["symbol"] for row in rows]
    assert len(symbols) == len(set(symbols))
    assert "AAPL" not in symbols
    # Every disclosed row can be checked against the filing it came from.
    for row in rows:
        if row["exposure_pct"] is not None:
            assert row["quote"] and row["filing_url"]
            assert row["pct_of"]


def test_graph_survives_a_company_nobody_discloses(auth_client):
    """The comparables leg still answers when both filing legs come back empty."""
    r = auth_client.get("/api/v1/equity/relationships/graph?symbol=KO")
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        body = r.json()
        assert body["results"]
        assert set(body["extra"]["sources"]) == {
            "counterparty_filings", "own_filing", "peers"}


def test_unknown_symbol_is_a_clean_error(auth_client):
    r = auth_client.get("/api/v1/equity/relationships/graph?symbol=ZZZZNOTREAL")
    assert r.status_code >= 400
    assert "detail" in r.json()
