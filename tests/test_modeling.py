"""DCF arithmetic and the modeling endpoints.

The maths tests use assumptions chosen so the right answer can be worked out on
paper — a valuation engine that is only ever checked against itself is not
checked.
"""
import pytest

from backend.valuation import dcf

# 100 of revenue, all of it operating profit, no tax, no capital intensity.
# Free cash flow is therefore exactly 100 a year, and every number below can be
# derived by hand.
PLAIN = dict(
    revenue_base=100.0, shares_diluted=10.0, years=1,
    revenue_growth=0.0, operating_margin=1.0, tax_rate=0.0,
    depreciation_pct_revenue=0.0, capex_pct_revenue=0.0, nwc_pct_revenue_change=0.0,
    discount_rate=0.10, terminal_method="perpetuity", terminal_growth=0.0,
    net_debt=0.0, mid_year=False,
)


def test_the_arithmetic_is_the_arithmetic():
    result = dcf.value(dcf.Assumptions(**PLAIN))
    assert result.projections[0].free_cash_flow == pytest.approx(100.0)
    assert result.pv_explicit == pytest.approx(100 / 1.1)              # 90.909…
    assert result.terminal_value == pytest.approx(1000.0)              # 100 / 0.10
    assert result.pv_terminal == pytest.approx(1000 / 1.1)             # 909.09…
    assert result.enterprise_value == pytest.approx(1000.0)
    assert result.value_per_share == pytest.approx(100.0)              # 1000 / 10 shares


def test_net_debt_comes_out_of_the_equity_not_the_enterprise():
    result = dcf.value(dcf.Assumptions(**dict(PLAIN, net_debt=200.0)))
    assert result.enterprise_value == pytest.approx(1000.0)
    assert result.equity_value == pytest.approx(800.0)
    assert result.value_per_share == pytest.approx(80.0)


def test_mid_year_discounting_moves_the_flows_not_the_terminal_value():
    """Cash arrives across the year; the terminal stock still sits at year end."""
    result = dcf.value(dcf.Assumptions(**dict(PLAIN, mid_year=True)))
    assert result.projections[0].discount_factor == pytest.approx(1 / 1.1 ** 0.5)
    assert result.pv_terminal == pytest.approx(1000 / 1.1)   # unchanged by mid-year


def test_working_capital_is_charged_on_growth_not_on_revenue():
    """The mistake that quietly bleeds a stable company dry."""
    flat = dcf.value(dcf.Assumptions(**dict(PLAIN, nwc_pct_revenue_change=0.5)))
    assert flat.projections[0].nwc_change == pytest.approx(0.0)   # no growth, no build

    growing = dcf.value(dcf.Assumptions(
        **dict(PLAIN, revenue_growth=0.10, nwc_pct_revenue_change=0.5)))
    assert growing.projections[0].revenue == pytest.approx(110.0)
    assert growing.projections[0].nwc_change == pytest.approx(5.0)   # half of the +10


def test_tax_and_capital_intensity_land_where_they_should():
    result = dcf.value(dcf.Assumptions(**dict(
        PLAIN, tax_rate=0.25, depreciation_pct_revenue=0.10, capex_pct_revenue=0.15)))
    p = result.projections[0]
    assert p.nopat == pytest.approx(75.0)                # 100 EBIT less 25% tax
    assert p.depreciation == pytest.approx(10.0)
    assert p.capex == pytest.approx(15.0)
    assert p.free_cash_flow == pytest.approx(70.0)       # 75 + 10 − 15


def test_exit_multiple_values_the_terminal_year_on_ebitda():
    result = dcf.value(dcf.Assumptions(**dict(
        PLAIN, depreciation_pct_revenue=0.20, terminal_method="exit_multiple",
        exit_multiple=8.0)))
    # EBITDA = 100 EBIT + 20 depreciation; x 8 = 960
    assert result.terminal_value == pytest.approx(960.0)


def test_growth_at_or_above_the_discount_rate_is_refused():
    with pytest.raises(dcf.ModelError, match="converge"):
        dcf.value(dcf.Assumptions(**dict(PLAIN, terminal_growth=0.10)))
    with pytest.raises(dcf.ModelError, match="converge"):
        dcf.value(dcf.Assumptions(**dict(PLAIN, terminal_growth=0.12)))
    # Comfortably apart is fine.
    assert dcf.value(dcf.Assumptions(**dict(PLAIN, terminal_growth=0.03))).value_per_share > 0


def test_a_single_rate_covers_every_year_and_a_short_list_is_held_flat():
    one = dcf.value(dcf.Assumptions(**dict(PLAIN, years=3, revenue_growth=0.10)))
    listed = dcf.value(dcf.Assumptions(**dict(PLAIN, years=3, revenue_growth=[0.10])))
    assert [p.revenue for p in one.projections] == pytest.approx(
        [p.revenue for p in listed.projections])
    assert one.projections[2].revenue == pytest.approx(100 * 1.1 ** 3)


def test_per_year_assumptions_are_applied_year_by_year():
    result = dcf.value(dcf.Assumptions(**dict(
        PLAIN, years=3, revenue_growth=[0.20, 0.10, 0.0])))
    assert [round(p.revenue, 4) for p in result.projections] == [120.0, 132.0, 132.0]


def test_wacc_is_the_weighted_average_when_no_rate_is_given():
    a = dcf.Assumptions(revenue_base=100, shares_diluted=10, discount_rate=None,
                        equity_weight=0.6, cost_of_equity=0.10, cost_of_debt=0.05,
                        tax_rate=0.25)
    # 0.6 x 10% + 0.4 x 5% x (1 - 25%) = 6% + 1.5%
    assert a.wacc() == pytest.approx(0.075)


def test_a_stated_rate_overrides_the_weights():
    a = dcf.Assumptions(revenue_base=100, shares_diluted=10, discount_rate=0.08,
                        equity_weight=0.1, cost_of_equity=0.99)
    assert a.wacc() == pytest.approx(0.08)


def test_the_terminal_share_is_reported_and_warned_about():
    """A model that is 90% perpetuity should say so on its own face."""
    result = dcf.value(dcf.Assumptions(**PLAIN))
    assert result.terminal_share == pytest.approx(0.9, abs=0.01)
    assert any("terminal value" in w for w in result.warnings)


def test_net_debt_is_read_at_one_balance_sheet_date(monkeypatch):
    """A filer that stops tagging a line must not have an older year silently
    substituted: netting this year's debt against last year's investments
    produces a figure that appears on no filing. NVIDIA stopped tagging
    short-term investments after FY2025, which is how this was found.
    """
    from backend.valuation import seed as seeding

    periods = ["2026-01-25", "2025-01-26"]
    rows = [
        {"line_item": "revenue", "2026-01-25": 1000.0, "2025-01-26": 800.0},
        {"line_item": "weighted_average_shares_diluted", "2026-01-25": 10.0},
        {"line_item": "total_debt", "2026-01-25": 90.0, "2025-01-26": 80.0},
        {"line_item": "cash_and_equivalents", "2026-01-25": 40.0, "2025-01-26": 30.0},
        # Present in the older year only — the stale-value trap.
        {"line_item": "short_term_investments", "2026-01-25": None, "2025-01-26": 500.0},
    ]
    monkeypatch.setattr(seeding.statements_provider, "statements",
                        lambda *a, **k: (rows, {"periods": periods,
                                                "provider_by_statement": {}}))
    monkeypatch.setattr(seeding.yahoo, "info", lambda *a, **k: {})

    assumptions, evidence = seeding.seed("TEST")
    # 90 debt - 40 cash - nothing, NOT 90 - 40 - 500 from the year before.
    assert assumptions["net_debt"] == pytest.approx(50.0)
    assert any("short-term investments" in n for n in evidence["notes"])


def test_a_build_out_carried_into_the_perpetuity_is_flagged():
    """Capex far above D&A in the terminal year is a capital programme, not a
    steady state — and it is what makes a company mid-investment-cycle value at
    a fraction of its price. The model has to say so rather than just print a
    low number.
    """
    building = dict(PLAIN, depreciation_pct_revenue=0.08, capex_pct_revenue=0.25)
    result = dcf.value(dcf.Assumptions(**building))
    assert any("build-out" in w for w in result.warnings)

    # Capex in line with depreciation is an ordinary steady state; stay quiet.
    steady = dict(PLAIN, depreciation_pct_revenue=0.08, capex_pct_revenue=0.09)
    assert not any("build-out" in w for w in dcf.value(dcf.Assumptions(**steady)).warnings)


def test_impossible_inputs_are_rejected_not_absorbed():
    for bad in ({"revenue_base": 0.0}, {"shares_diluted": 0.0},
                {"years": 0}, {"years": 50}, {"discount_rate": -0.01}):
        with pytest.raises(dcf.ModelError):
            dcf.value(dcf.Assumptions(**dict(PLAIN, **bad)))


def test_sensitivity_grid_blanks_the_corners_it_cannot_value():
    a = dcf.Assumptions(**PLAIN)
    grid = dcf.sensitivity(a, [0.08, 0.10], [0.0, 0.09])
    assert grid["terminal_axis"] == "terminal_growth"
    assert len(grid["grid"]) == 2 and len(grid["grid"][0]) == 2
    assert grid["grid"][0][0] is not None          # 8% against a flat terminal: fine
    assert grid["grid"][0][1] is None              # 9% growth on an 8% rate: cannot
    # A higher discount rate is worth less, all else equal.
    assert grid["grid"][1][0] < grid["grid"][0][0]


def test_sensitivity_varies_the_multiple_when_that_is_the_method():
    a = dcf.Assumptions(**dict(PLAIN, terminal_method="exit_multiple", exit_multiple=10.0))
    grid = dcf.sensitivity(a, [0.10], [8.0, 12.0])
    assert grid["terminal_axis"] == "exit_multiple"
    assert grid["grid"][0][1] > grid["grid"][0][0]


# --------------------------------------------------------------------------- #
# Live: seeding and the endpoints
# --------------------------------------------------------------------------- #
def test_seed_endpoint_fills_every_assumption(auth_client):
    r = auth_client.get("/api/modeling/seed?symbol=AAPL")
    assert r.status_code == 200
    body = r.json()
    a = body["assumptions"]
    assert a["revenue_base"] > 0 and a["shares_diluted"] > 0
    assert len(a["revenue_growth"]) == a["years"]
    assert 0 <= a["tax_rate"] <= 0.5
    assert a["capex_pct_revenue"] > 0            # normalised away from the filed sign
    assert body["valuation"]["value_per_share"] > 0
    assert body["valuation"]["sensitivity"]["grid"]
    assert body["evidence"]["history"]["revenue"]


def test_value_endpoint_runs_arbitrary_assumptions(auth_client):
    r = auth_client.post("/api/modeling/value", json={
        "symbol": "AAPL", "assumptions": PLAIN, "sensitivity": False})
    assert r.status_code == 200
    assert r.json()["value_per_share"] == pytest.approx(100.0)


def test_value_endpoint_reports_an_impossible_model_as_a_bad_request(auth_client):
    r = auth_client.post("/api/modeling/value", json={
        "symbol": "AAPL", "assumptions": dict(PLAIN, terminal_growth=0.099)})
    assert r.status_code == 400
    assert "converge" in r.json()["detail"]


def test_models_are_saved_listed_rerun_and_deleted(auth_client):
    created = auth_client.post("/api/modeling/models", json={
        "name": "Plain vanilla", "symbol": "AAPL", "assumptions": PLAIN, "note": "test"})
    assert created.status_code == 201
    model_id = created.json()["id"]
    assert created.json()["value_per_share"] == pytest.approx(100.0)

    listed = auth_client.get("/api/modeling/models")
    assert any(m["id"] == model_id for m in listed.json())

    # The same name twice is a conflict, not a silent second copy.
    assert auth_client.post("/api/modeling/models", json={
        "name": "Plain vanilla", "symbol": "AAPL", "assumptions": PLAIN}).status_code == 409

    updated = auth_client.put("/api/modeling/models/{}".format(model_id), json={
        "assumptions": dict(PLAIN, net_debt=500.0)})
    assert updated.json()["value_per_share"] == pytest.approx(50.0)

    rerun = auth_client.get("/api/modeling/models/{}/rerun".format(model_id))
    assert rerun.status_code == 200
    assert rerun.json()["now"]["value_per_share"] == pytest.approx(50.0)
    assert rerun.json()["saved"]["value_per_share"] == pytest.approx(50.0)

    assert auth_client.delete("/api/modeling/models/{}".format(model_id)).status_code == 204
    assert auth_client.get("/api/modeling/models/{}".format(model_id)).status_code == 404


def test_renaming_onto_a_taken_name_is_a_conflict_not_a_crash(auth_client):
    """The database enforces one name per user; the API has to say so in HTTP.

    The Save button sends the name on every update, so typing a name that is
    already in use is an ordinary thing to do, not an edge case.
    """
    first = auth_client.post("/api/modeling/models", json={
        "name": "Rename A", "symbol": "AAPL", "assumptions": PLAIN})
    second = auth_client.post("/api/modeling/models", json={
        "name": "Rename B", "symbol": "AAPL", "assumptions": PLAIN})
    one, two = first.json()["id"], second.json()["id"]

    clash = auth_client.put("/api/modeling/models/{}".format(two), json={"name": "Rename A"})
    assert clash.status_code == 409
    # ...and the model is untouched rather than half-written.
    assert auth_client.get("/api/modeling/models/{}".format(two)).json()["name"] == "Rename B"

    # Re-saving a model under its own name is not a clash with itself.
    same = auth_client.put("/api/modeling/models/{}".format(two),
                           json={"name": "Rename B", "assumptions": PLAIN})
    assert same.status_code == 200

    for model_id in (one, two):
        auth_client.delete("/api/modeling/models/{}".format(model_id))


def test_one_account_cannot_read_anothers_models(client, auth_client):
    """Every query filters on user_id; this is the test that says so."""
    import uuid

    mine = auth_client.post("/api/modeling/models", json={
        "name": "Private model", "symbol": "AAPL", "assumptions": PLAIN})
    model_id = mine.json()["id"]

    other = "user_{}".format(uuid.uuid4().hex[:8])
    client.post("/api/auth/register", json={
        "username": other, "email": "{}@example.com".format(other), "password": "secret123"})
    token = client.post("/api/auth/login", data={
        "username": other, "password": "secret123"}).json()["access_token"]
    client.headers.update({"Authorization": "Bearer {}".format(token)})

    assert client.get("/api/modeling/models/{}".format(model_id)).status_code == 404
    assert client.delete("/api/modeling/models/{}".format(model_id)).status_code == 404
    assert not client.get("/api/modeling/models").json()

    auth_client.delete("/api/modeling/models/{}".format(model_id))


def test_seeding_a_non_filer_is_a_clean_error(auth_client):
    r = auth_client.get("/api/modeling/seed?symbol=SPY")
    assert r.status_code >= 400
    assert "detail" in r.json()
