"""Portfolios: cost-basis accounting, the blotter, and portfolio-level analytics.

The accounting tests are pure arithmetic and run offline. The valuation,
performance, risk and factor tests need network, as elsewhere in this suite.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from backend.models import Transaction
from backend.portfolio import analytics
from backend.portfolio.accounting import build_ledger

DAY_ONE = datetime(2023, 1, 3)


def txn(seq, side, symbol=None, quantity=0.0, price=1.0, fees=0.0, day=0):
    row = Transaction(
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        fees=fees,
        trade_date=DAY_ONE + timedelta(days=day),
    )
    row.id = seq
    return row


# --------------------------------------------------------------------------- #
# Cost basis
# --------------------------------------------------------------------------- #
def test_fifo_matches_oldest_lot_first():
    """Buy 10 @ 100, buy 10 @ 120, sell 15 @ 130."""
    ledger = build_ledger(
        [
            txn(1, "buy", "AAPL", 10, 100, day=0),
            txn(2, "buy", "AAPL", 10, 120, day=1),
            txn(3, "sell", "AAPL", 15, 130, day=2),
        ],
        "fifo",
    )
    holding = ledger.holdings["AAPL"]
    # The whole first lot (10 x 30) plus half the second (5 x 10).
    assert holding.realized_pnl == pytest.approx(350.0)
    assert holding.quantity == pytest.approx(5.0)
    assert holding.avg_cost == pytest.approx(120.0)  # only the newer lot survives


def test_average_cost_realises_against_one_basis():
    ledger = build_ledger(
        [
            txn(1, "buy", "AAPL", 10, 100, day=0),
            txn(2, "buy", "AAPL", 10, 120, day=1),
            txn(3, "sell", "AAPL", 15, 130, day=2),
        ],
        "average",
    )
    holding = ledger.holdings["AAPL"]
    assert holding.avg_cost == pytest.approx(110.0)
    assert holding.realized_pnl == pytest.approx(300.0)  # 15 x (130 - 110)
    assert holding.quantity == pytest.approx(5.0)


def test_fees_are_capitalised_into_the_basis():
    """A round trip at the same price loses exactly the commissions."""
    ledger = build_ledger(
        [
            txn(1, "buy", "F", 10, 100, fees=10, day=0),
            txn(2, "sell", "F", 10, 100, fees=10, day=1),
        ],
        "fifo",
    )
    assert ledger.holdings["F"].realized_pnl == pytest.approx(-20.0)
    assert ledger.cash == pytest.approx(-20.0)
    assert ledger.fees == pytest.approx(20.0)


def test_selling_more_than_held_flips_the_position_short():
    ledger = build_ledger(
        [txn(1, "buy", "X", 5, 100, day=0), txn(2, "sell", "X", 10, 110, day=1)], "fifo"
    )
    holding = ledger.holdings["X"]
    assert holding.realized_pnl == pytest.approx(50.0)  # the 5 long shares
    assert holding.quantity == pytest.approx(-5.0)  # the rest opened a short
    assert holding.avg_cost == pytest.approx(110.0)


def test_short_covered_at_a_lower_price_gains():
    ledger = build_ledger(
        [txn(1, "sell", "T", 10, 100, day=0), txn(2, "buy", "T", 10, 90, day=1)], "fifo"
    )
    assert ledger.holdings["T"].realized_pnl == pytest.approx(100.0)
    assert ledger.holdings["T"].quantity == 0.0
    assert ledger.cash == pytest.approx(100.0)


def test_cash_sides_move_cash_not_shares():
    ledger = build_ledger(
        [
            txn(1, "deposit", None, 10_000, day=0),
            txn(2, "buy", "D", 10, 50, day=1),
            txn(3, "dividend", "D", 25, day=5),
            txn(4, "fee", None, 3, day=6),
            txn(5, "withdraw", None, 1_000, day=7),
        ],
        "fifo",
    )
    assert ledger.holdings["D"].quantity == pytest.approx(10.0)  # a dividend buys nothing
    assert ledger.holdings["D"].dividends == pytest.approx(25.0)
    assert ledger.cash == pytest.approx(10_000 - 500 + 25 - 3 - 1_000)
    assert ledger.net_deposits == pytest.approx(9_000.0)


def test_closing_a_position_leaves_realised_pnl_visible():
    ledger = build_ledger(
        [txn(1, "buy", "C", 10, 10, day=0), txn(2, "sell", "C", 10, 12, day=1)], "fifo"
    )
    assert ledger.holdings["C"].quantity == 0.0
    assert ledger.holdings["C"].realized_pnl == pytest.approx(20.0)
    assert ledger.open_holdings == []


# --------------------------------------------------------------------------- #
# Return series
# --------------------------------------------------------------------------- #
def test_deposits_do_not_count_as_return():
    """Paying money in makes the portfolio bigger, not better."""
    index = pd.bdate_range("2023-01-03", periods=5)
    transactions = [
        txn(1, "deposit", None, 1_000, day=0),
        txn(2, "deposit", None, 5_000, day=2),
    ]
    frame, _ = analytics.value_series(transactions)
    frame = frame.reindex(index).ffill()
    assert frame["total_value"].iloc[-1] == pytest.approx(6_000.0)
    assert frame["return"].abs().max() == pytest.approx(0.0)


def test_xirr_recovers_a_known_rate():
    flows = [(datetime(2023, 1, 1), -1_000.0), (datetime(2024, 1, 1), 1_100.0)]
    assert analytics.xirr(flows) == pytest.approx(0.10, abs=1e-3)


def test_xirr_is_none_without_a_sign_change():
    assert analytics.xirr([(datetime(2023, 1, 1), -100.0), (datetime(2024, 1, 1), -50.0)]) is None


def test_concentration_reads_effective_positions():
    rows = [{"weight": 0.5}, {"weight": 0.25}, {"weight": 0.25}]
    out = analytics.concentration(rows)
    assert out["herfindahl"] == pytest.approx(0.375)
    assert out["effective_positions"] == pytest.approx(2.67, abs=0.01)
    assert out["top_1"] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# API — CRUD and ownership
# --------------------------------------------------------------------------- #
@pytest.fixture()
def portfolio(auth_client):
    resp = auth_client.post(
        "/api/portfolios",
        json={"name": "Test book", "initial_cash": 100_000, "benchmark": "SPY"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_lists_and_deletes(auth_client, portfolio):
    listed = auth_client.get("/api/portfolios").json()
    assert any(p["id"] == portfolio["id"] for p in listed)
    assert portfolio["cash"] == pytest.approx(100_000.0)  # opening deposit booked

    assert auth_client.delete("/api/portfolios/{}".format(portfolio["id"])).status_code == 204
    assert auth_client.get("/api/portfolios/{}".format(portfolio["id"])).status_code == 404


def test_duplicate_name_is_rejected(auth_client, portfolio):
    resp = auth_client.post("/api/portfolios", json={"name": portfolio["name"]})
    assert resp.status_code == 400


def test_trading_updates_positions_and_cash(auth_client, portfolio):
    pid = portfolio["id"]
    for body in (
        {"side": "buy", "symbol": "aapl", "quantity": 100, "price": 150, "fees": 1},
        {"side": "buy", "symbol": "MSFT", "quantity": 50, "price": 300},
        {"side": "sell", "symbol": "AAPL", "quantity": 40, "price": 170},
    ):
        assert auth_client.post(
            "/api/portfolios/{}/transactions".format(pid), json=body
        ).status_code == 201, body

    detail = auth_client.get("/api/portfolios/{}".format(pid)).json()
    positions = {p["symbol"]: p for p in detail["positions"]}
    assert positions["AAPL"]["quantity"] == pytest.approx(60.0)
    assert positions["MSFT"]["quantity"] == pytest.approx(50.0)
    # Bought at 150 + 0.01/share of commission, sold 40 at 170.
    assert positions["AAPL"]["realized_pnl"] == pytest.approx(40 * (170 - 150.01))
    expected_cash = 100_000 - (100 * 150 + 1) - 50 * 300 + 40 * 170
    assert detail["portfolio"]["cash"] == pytest.approx(expected_cash)


def test_symbol_is_required_for_a_trade(auth_client, portfolio):
    resp = auth_client.post(
        "/api/portfolios/{}/transactions".format(portfolio["id"]),
        json={"side": "buy", "quantity": 10, "price": 5},
    )
    assert resp.status_code == 400


def test_correcting_a_transaction_restates_the_position(auth_client, portfolio):
    pid = portfolio["id"]
    created = auth_client.post(
        "/api/portfolios/{}/transactions".format(pid),
        json={"side": "buy", "symbol": "NVDA", "quantity": 10, "price": 400},
    ).json()

    auth_client.patch(
        "/api/portfolios/{}/transactions/{}".format(pid, created["id"]),
        json={"quantity": 25},
    )
    positions = auth_client.get("/api/portfolios/{}/positions".format(pid)).json()
    assert positions[0]["quantity"] == pytest.approx(25.0)

    auth_client.delete("/api/portfolios/{}/transactions/{}".format(pid, created["id"]))
    assert auth_client.get("/api/portfolios/{}/positions".format(pid)).json() == []


def test_changing_the_cost_basis_method_restates_history(auth_client, portfolio):
    pid = portfolio["id"]
    for body in (
        {"side": "buy", "symbol": "KO", "quantity": 10, "price": 100},
        {"side": "buy", "symbol": "KO", "quantity": 10, "price": 120},
        {"side": "sell", "symbol": "KO", "quantity": 15, "price": 130},
    ):
        auth_client.post("/api/portfolios/{}/transactions".format(pid), json=body)

    fifo = auth_client.get("/api/portfolios/{}/positions".format(pid)).json()[0]
    assert fifo["realized_pnl"] == pytest.approx(350.0)

    auth_client.patch("/api/portfolios/{}".format(pid), json={"cost_basis_method": "average"})
    average = auth_client.get("/api/portfolios/{}/positions".format(pid)).json()[0]
    assert average["realized_pnl"] == pytest.approx(300.0)


def test_another_user_cannot_touch_the_book(auth_client, portfolio, other_client):
    pid = portfolio["id"]
    assert other_client.get("/api/portfolios/{}".format(pid)).status_code == 404
    assert other_client.get("/api/portfolios/{}/positions".format(pid)).status_code == 404
    assert other_client.post(
        "/api/portfolios/{}/transactions".format(pid),
        json={"side": "buy", "symbol": "AAPL", "quantity": 1, "price": 1},
    ).status_code == 404
    assert other_client.delete("/api/portfolios/{}".format(pid)).status_code == 404


def test_analytics_need_transactions(auth_client):
    pid = auth_client.post("/api/portfolios", json={"name": "Empty book"}).json()["id"]
    for route in ("performance", "risk", "factors"):
        resp = auth_client.get("/api/portfolios/{}/{}".format(pid, route))
        assert resp.status_code == 400, route


@pytest.fixture()
def other_client(client):
    """A second signed-in user, for the isolation test."""
    import uuid

    from fastapi.testclient import TestClient

    second = TestClient(client.app)
    username = "p_{}".format(uuid.uuid4().hex[:10])
    second.post(
        "/api/auth/register",
        json={"username": username, "email": "{}@example.com".format(username),
              "password": "secret123"},
    )
    token = second.post(
        "/api/auth/login", data={"username": username, "password": "secret123"}
    ).json()["access_token"]
    second.headers.update({"Authorization": "Bearer {}".format(token)})
    return second


# --------------------------------------------------------------------------- #
# API — analytics (network)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def funded(auth_client):
    """A portfolio funded in January 2023, invested the next day."""
    pid = auth_client.post("/api/portfolios", json={"name": "Funded book"}).json()["id"]
    auth_client.post(
        "/api/portfolios/{}/transactions".format(pid),
        json={"side": "deposit", "quantity": 100_000, "trade_date": "2023-01-03T00:00:00"},
    )
    for symbol, quantity, price in (("AAPL", 100, 130.0), ("MSFT", 50, 240.0), ("KO", 200, 60.0)):
        auth_client.post(
            "/api/portfolios/{}/transactions".format(pid),
            json={
                "side": "buy", "symbol": symbol, "quantity": quantity, "price": price,
                "trade_date": "2023-01-04T00:00:00",
            },
        )
    return pid


def test_summary_marks_holdings_to_market(auth_client, funded):
    body = auth_client.get("/api/portfolios/{}/summary".format(funded)).json()
    assert {r["symbol"] for r in body["positions"]} == {"AAPL", "MSFT", "KO"}
    assert body["totals"]["total_value"] > 0
    assert body["totals"]["cost_basis"] == pytest.approx(100 * 130 + 50 * 240 + 200 * 60)
    assert sum(abs(r["weight"]) for r in body["positions"]) == pytest.approx(1.0, abs=1e-6)
    assert body["concentration"]["effective_positions"] >= 1


def test_performance_reports_a_curve_and_metrics(auth_client, funded):
    body = auth_client.get(
        "/api/portfolios/{}/performance?end=2024-01-01".format(funded)
    ).json()
    assert len(body["series"]) > 20
    assert body["metrics"]["observations"] > 200
    assert "sharpe" in body["metrics"] and "max_drawdown" in body["metrics"]
    # The opening deposit is a flow, never a return.
    assert abs(body["totals"]["time_weighted_return"]) < 2.0
    # Only one deposit, so time- and money-weighted returns should broadly agree.
    totals = body["totals"]
    assert totals["money_weighted_return_annual"] == pytest.approx(
        totals["time_weighted_return_annual"], abs=0.05
    )
    assert body["benchmark"]["symbol"] == "SPY"
    assert body["benchmark"]["beta"] is not None


def test_risk_puts_var_in_dollars(auth_client, funded):
    body = auth_client.get("/api/portfolios/{}/risk?end=2024-01-01".format(funded)).json()
    var = body["value_at_risk"]
    assert var["historical_pct"] < 0  # a loss
    assert var["historical_amount"] == pytest.approx(
        var["historical_pct"] * body["total_value"], rel=1e-3
    )
    assert body["volatility"]["annualised"] > 0
    contributions = body["risk_contribution"]
    assert {c["symbol"] for c in contributions} == {"AAPL", "MSFT", "KO"}
    # Contributions decompose total volatility, so they sum back to all of it.
    assert sum(c["pct_of_risk"] for c in contributions) == pytest.approx(1.0, abs=1e-4)


def test_factor_exposure_regresses_the_whole_book(auth_client, funded):
    body = auth_client.get("/api/portfolios/{}/factors?end=2024-01-01".format(funded)).json()
    assert set(body["factors"]) == {"MKT", "MOM", "LOWVOL"}
    assert "MKT" in body["exposure"]["betas"]
    assert 0.0 <= body["exposure"]["r_squared"] <= 1.0
    assert {h["symbol"] for h in body["holdings"]} == {"AAPL", "MSFT", "KO"}


def test_allocation_groups_by_sector(auth_client, funded):
    body = auth_client.get("/api/portfolios/{}/allocation".format(funded)).json()
    assert len(body["by_symbol"]) == 3
    assert sum(b["weight"] for b in body["by_sector"]) == pytest.approx(1.0, abs=1e-6)
    assert body["cash_weight"] is not None
