"""User accounts, sessions and saved actions.

Only the watchlist-quote and alert-evaluation tests need network; the rest
exercise the database and the ownership rules.
"""
import uuid

import pytest

from backend.database import schema_overview


def _register(client, password="secret123", **extra):
    username = "u_{}".format(uuid.uuid4().hex[:10])
    body = {"username": username, "email": "{}@example.com".format(username),
            "password": password}
    body.update(extra)
    resp = client.post("/api/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    token = client.post(
        "/api/auth/login", data={"username": username, "password": password}
    ).json()["access_token"]
    return username, token


@pytest.fixture()
def other_client(client):
    """A second signed-in user, for the isolation tests."""
    from fastapi.testclient import TestClient

    second = TestClient(client.app)
    _, token = _register(second)
    second.headers.update({"Authorization": "Bearer {}".format(token)})
    return second


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def test_schema_contains_every_table():
    tables = {t["table"] for t in schema_overview()}
    assert tables == {
        "users", "user_sessions", "user_settings", "saved_commands", "saved_results",
        "command_runs", "watchlists", "watchlist_items", "alerts", "calendar_events",
        "backtest_runs", "portfolios", "positions", "transactions",
        "theses", "thesis_evidence", "thesis_checks",
        "signal_events", "signal_runs", "triage_records", "deepdive_records",
        "hedge_records", "valuation_models", "research_feature_snapshots",
        "production_signal_vintages", "production_runs", "production_orders",
        "production_position_snapshots", "raw_observations",
    }


def test_existing_database_is_migrated_in_place(tmp_path):
    """A database written before these columns existed must keep working.

    ``create_all`` only creates missing *tables*, so without the column sync an
    older ``terminal.db`` would fail on every query of ``users``.
    """
    import sqlite3

    from sqlalchemy import create_engine, inspect, text

    from backend import database

    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username VARCHAR(64) NOT NULL UNIQUE,
            email VARCHAR(255) NOT NULL UNIQUE,
            hashed_password VARCHAR(255) NOT NULL,
            created_at DATETIME
        );
        INSERT INTO users (username, email, hashed_password)
        VALUES ('legacy_user', 'legacy@example.com', 'x');
        """
    )
    legacy.commit()
    legacy.close()

    engine = create_engine("sqlite:///{}".format(path), future=True)
    original = database.engine
    database.engine = engine  # point init_db at the legacy file
    try:
        database.init_db()
        columns = {c["name"] for c in inspect(engine).get_columns("users")}
        assert {"full_name", "is_active", "is_admin", "last_login_at", "login_count"} <= columns

        with engine.connect() as conn:
            # The pre-existing row survives, and the new NOT NULL columns are filled.
            row = conn.execute(
                text("SELECT username, is_active, login_count FROM users")
            ).one()
            assert row.username == "legacy_user"
            assert row.login_count == 0
            # Tables that did not exist before were created outright.
            assert "watchlists" in inspect(engine).get_table_names()
    finally:
        database.engine = original
        engine.dispose()


def test_database_endpoint_reports_the_schema(auth_client):
    body = auth_client.get("/api/system/database").json()
    assert body["dialect"] in ("sqlite", "postgresql")
    users = next(t for t in body["tables"] if t["table"] == "users")
    assert {"username", "email", "hashed_password", "last_login_at"} <= {
        c["name"] for c in users["columns"]
    }


# --------------------------------------------------------------------------- #
# Accounts & sessions
# --------------------------------------------------------------------------- #
def test_register_stores_profile_fields(client):
    username, token = _register(client, full_name="Ada Lovelace")
    client.headers.update({"Authorization": "Bearer {}".format(token)})
    me = client.get("/api/auth/me").json()
    assert me["username"] == username
    assert me["full_name"] == "Ada Lovelace"
    assert me["is_active"] is True
    assert me["login_count"] == 1


def test_login_records_a_session(auth_client):
    sessions = auth_client.get("/api/auth/sessions").json()
    assert len(sessions) == 1
    assert sessions[0]["is_active"] is True
    assert sessions[0]["revoked_at"] is None
    assert sessions[0]["jti"]


def test_logout_revokes_the_token(client):
    _, token = _register(client)
    client.headers.update({"Authorization": "Bearer {}".format(token)})
    assert client.get("/api/auth/me").status_code == 200

    assert client.post("/api/auth/logout").json()["revoked"] is True
    # The JWT is still cryptographically valid, but its session is gone.
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401
    assert "revoked" in resp.json()["detail"].lower()


def test_password_change_revokes_every_session(client):
    _, token = _register(client)
    client.headers.update({"Authorization": "Bearer {}".format(token)})
    body = client.post(
        "/api/auth/password",
        json={"current_password": "secret123", "new_password": "newsecret456"},
    ).json()
    assert body["updated"] is True
    assert body["sessions_revoked"] >= 1
    assert client.get("/api/auth/me").status_code == 401


def test_password_change_rejects_a_wrong_current_password(auth_client):
    resp = auth_client.post(
        "/api/auth/password",
        json={"current_password": "not-it", "new_password": "newsecret456"},
    )
    assert resp.status_code == 400


def test_profile_update(auth_client):
    resp = auth_client.patch("/api/auth/me", json={"full_name": "Grace Hopper"})
    assert resp.status_code == 200
    assert resp.json()["full_name"] == "Grace Hopper"


def test_revoking_another_users_session_is_a_404(auth_client, other_client):
    victim = other_client.get("/api/auth/sessions").json()[0]
    assert auth_client.delete("/api/auth/sessions/{}".format(victim["id"])).status_code == 404
    assert other_client.get("/api/auth/me").status_code == 200


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def test_settings_upsert_and_delete(auth_client):
    auth_client.put("/api/user/settings", json={"key": "theme", "value": "amber"})
    auth_client.put("/api/user/settings",
                    json={"key": "home_symbols", "value": ["AAPL", "SPY"]})
    # Same key again updates rather than duplicating.
    auth_client.put("/api/user/settings", json={"key": "theme", "value": "green"})

    settings = {s["key"]: s["value"] for s in auth_client.get("/api/user/settings").json()}
    assert settings == {"theme": "green", "home_symbols": ["AAPL", "SPY"]}

    assert auth_client.delete("/api/user/settings/theme").status_code == 204
    assert auth_client.delete("/api/user/settings/theme").status_code == 404


def test_settings_are_per_user(auth_client, other_client):
    auth_client.put("/api/user/settings", json={"key": "theme", "value": "amber"})
    assert other_client.get("/api/user/settings").json() == []


# --------------------------------------------------------------------------- #
# Saved commands
# --------------------------------------------------------------------------- #
def test_saved_command_lifecycle(auth_client):
    created = auth_client.post(
        "/api/user/saved",
        json={"name": "Apple daily", "command_path": "/equity/price/historical",
              "parameters": {"symbol": "AAPL", "start_date": "2024-01-01"},
              "description": "AAPL since 2024", "is_favorite": True},
    )
    assert created.status_code == 201, created.text
    saved_id = created.json()["id"]
    assert created.json()["parameters"]["symbol"] == "AAPL"

    listed = auth_client.get("/api/user/saved?favorites_only=true").json()
    assert [s["id"] for s in listed] == [saved_id]

    patched = auth_client.patch(
        "/api/user/saved/{}".format(saved_id), json={"name": "AAPL bars", "is_favorite": False}
    ).json()
    assert patched["name"] == "AAPL bars" and patched["is_favorite"] is False

    assert auth_client.delete("/api/user/saved/{}".format(saved_id)).status_code == 204
    assert auth_client.get("/api/user/saved").json() == []


def test_saved_command_validates_against_the_registry(auth_client):
    unknown_path = auth_client.post(
        "/api/user/saved", json={"name": "bad", "command_path": "/nope/nope"}
    )
    assert unknown_path.status_code == 400

    bad_param = auth_client.post(
        "/api/user/saved",
        json={"name": "bad2", "command_path": "/equity/price/quote",
              "parameters": {"not_a_param": 1}},
    )
    assert bad_param.status_code == 400
    assert "not_a_param" in bad_param.json()["detail"]


def test_saved_command_names_are_unique_per_user(auth_client, other_client):
    body = {"name": "dupe", "command_path": "/equity/price/quote",
            "parameters": {"symbol": "AAPL"}}
    assert auth_client.post("/api/user/saved", json=body).status_code == 201
    assert auth_client.post("/api/user/saved", json=body).status_code == 400
    # A different account may reuse the name.
    assert other_client.post("/api/user/saved", json=body).status_code == 201


def test_saved_command_of_another_user_is_invisible(auth_client, other_client):
    saved_id = auth_client.post(
        "/api/user/saved",
        json={"name": "mine", "command_path": "/equity/price/quote",
              "parameters": {"symbol": "AAPL"}},
    ).json()["id"]
    assert other_client.get("/api/user/saved").json() == []
    assert other_client.patch("/api/user/saved/{}".format(saved_id),
                              json={"name": "stolen"}).status_code == 404
    assert other_client.delete("/api/user/saved/{}".format(saved_id)).status_code == 404


def test_running_a_saved_command_returns_data_and_bumps_counters(auth_client):
    saved_id = auth_client.post(
        "/api/user/saved",
        json={"name": "curve", "command_path": "/fixedincome/government/yield_curve"},
    ).json()["id"]

    body = auth_client.post("/api/user/saved/{}/run".format(saved_id)).json()
    assert body["saved_command"]["name"] == "curve"
    assert len(body["results"]) > 5

    saved = auth_client.get("/api/user/saved").json()[0]
    assert saved["run_count"] == 1
    assert saved["last_run_at"] is not None


# --------------------------------------------------------------------------- #
# Saved results (data snapshots)
# --------------------------------------------------------------------------- #
def test_saved_result_lifecycle(auth_client):
    rows = [{"date": "2026-01-0{}".format(i + 1), "close": 100.0 + i} for i in range(3)]
    created = auth_client.post(
        "/api/user/results",
        json={"name": "AAPL snapshot", "command_path": "/equity/price/historical",
              "parameters": {"symbol": "AAPL"}, "results": rows, "provider": "yahoo"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["row_count"] == 3 and body["truncated"] is False
    assert "results" not in body  # the list schema never ships payloads

    listed = auth_client.get("/api/user/results").json()
    assert [r["name"] for r in listed] == ["AAPL snapshot"]

    full = auth_client.get("/api/user/results/{}".format(body["id"])).json()
    assert full["results"] == rows

    assert auth_client.delete("/api/user/results/{}".format(body["id"])).status_code == 204
    assert auth_client.get("/api/user/results").json() == []


def test_saved_result_truncates_at_the_row_cap(auth_client):
    rows = [{"i": i} for i in range(5200)]
    body = auth_client.post(
        "/api/user/results",
        json={"name": "big", "command_path": "/x", "results": rows},
    ).json()
    assert body["truncated"] is True
    assert body["row_count"] == 5000
    full = auth_client.get("/api/user/results/{}".format(body["id"])).json()
    assert len(full["results"]) == 5000


def test_saved_result_rejects_oversized_payloads(auth_client):
    rows = [{"blob": "x" * 5000} for _ in range(500)]  # ~2.5 MB
    resp = auth_client.post(
        "/api/user/results", json={"name": "huge", "command_path": "/x", "results": rows}
    )
    assert resp.status_code == 413
    assert "CSV" in resp.json()["detail"]


def test_saved_results_are_per_user(auth_client, other_client):
    rid = auth_client.post(
        "/api/user/results",
        json={"name": "mine", "command_path": "/x", "results": [{"a": 1}]},
    ).json()["id"]
    assert other_client.get("/api/user/results").json() == []
    assert other_client.get("/api/user/results/{}".format(rid)).status_code == 404
    assert other_client.delete("/api/user/results/{}".format(rid)).status_code == 404


def test_stats_counts_saved_results(auth_client):
    auth_client.post("/api/user/results",
                     json={"name": "s", "command_path": "/x", "results": [{"a": 1}]})
    assert auth_client.get("/api/user/stats").json()["saved_results"] == 1


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
def test_platform_calls_are_recorded_in_history(auth_client):
    assert auth_client.get("/api/user/history").json() == []

    auth_client.get("/api/v1/equity/price/quote?symbol=AAPL")
    history = auth_client.get("/api/user/history").json()
    assert len(history) == 1
    entry = history[0]
    assert entry["command_path"] == "/equity/price/quote"
    assert entry["parameters"] == {"symbol": "AAPL"}
    assert entry["status"] == "ok"
    assert entry["provider"] == "yahoo"
    assert entry["duration_ms"] >= 0
    assert entry["row_count"] == 1


def test_failed_calls_are_recorded_with_the_error(auth_client):
    resp = auth_client.get("/api/v1/equity/price/quote?symbol=AAPL&provider=bloomberg")
    assert resp.status_code == 400

    entry = auth_client.get("/api/user/history?status=error").json()[0]
    assert entry["status"] == "error"
    assert "bloomberg" in entry["error"]


def test_history_is_per_user_and_clearable(auth_client, other_client):
    auth_client.get("/api/v1/equity/price/quote?symbol=MSFT")
    assert len(auth_client.get("/api/user/history").json()) == 1
    assert other_client.get("/api/user/history").json() == []

    assert auth_client.delete("/api/user/history").json()["deleted"] == 1
    assert auth_client.get("/api/user/history").json() == []


def test_stats_summarises_the_account(auth_client):
    auth_client.post("/api/user/saved",
                     json={"name": "s", "command_path": "/equity/price/quote"})
    auth_client.get("/api/v1/equity/price/quote?symbol=AAPL")

    stats = auth_client.get("/api/user/stats").json()
    assert stats["saved_commands"] == 1
    assert stats["command_runs"] == 1
    assert stats["login_count"] == 1
    assert stats["most_used"][0]["command_path"] == "/equity/price/quote"


# --------------------------------------------------------------------------- #
# Watchlists
# --------------------------------------------------------------------------- #
def test_watchlist_lifecycle(auth_client):
    created = auth_client.post(
        "/api/user/watchlists",
        json={"name": "Mega cap", "symbols": ["aapl", "msft", "aapl"], "is_default": True},
    )
    assert created.status_code == 201, created.text
    watchlist = created.json()
    # Symbols are normalised and de-duplicated on the way in.
    assert [i["symbol"] for i in watchlist["items"]] == ["AAPL", "MSFT"]
    assert watchlist["is_default"] is True

    wid = watchlist["id"]
    added = auth_client.post("/api/user/watchlists/{}/items".format(wid),
                             json={"symbol": "nvda", "note": "watch earnings"})
    assert added.status_code == 201
    assert added.json()["symbol"] == "NVDA"

    # Same symbol twice is rejected.
    assert auth_client.post("/api/user/watchlists/{}/items".format(wid),
                            json={"symbol": "NVDA"}).status_code == 400

    assert auth_client.delete("/api/user/watchlists/{}/items/nvda".format(wid)).status_code == 204
    assert auth_client.delete("/api/user/watchlists/{}/items/nvda".format(wid)).status_code == 404

    assert len(auth_client.get("/api/user/watchlists/{}".format(wid)).json()["items"]) == 2
    assert auth_client.delete("/api/user/watchlists/{}".format(wid)).status_code == 204
    assert auth_client.get("/api/user/watchlists").json() == []


def test_only_one_watchlist_is_default(auth_client):
    auth_client.post("/api/user/watchlists", json={"name": "first", "is_default": True})
    auth_client.post("/api/user/watchlists", json={"name": "second", "is_default": True})
    defaults = [w for w in auth_client.get("/api/user/watchlists").json() if w["is_default"]]
    assert [w["name"] for w in defaults] == ["second"]


def test_watchlist_of_another_user_is_invisible(auth_client, other_client):
    wid = auth_client.post("/api/user/watchlists", json={"name": "private"}).json()["id"]
    assert other_client.get("/api/user/watchlists/{}".format(wid)).status_code == 404
    assert other_client.delete("/api/user/watchlists/{}".format(wid)).status_code == 404


def test_deleting_a_watchlist_removes_its_items(auth_client):
    from backend.database import SessionLocal
    from backend.models import WatchlistItem

    wid = auth_client.post(
        "/api/user/watchlists", json={"name": "temp", "symbols": ["AAPL", "MSFT"]}
    ).json()["id"]
    auth_client.delete("/api/user/watchlists/{}".format(wid))

    with SessionLocal() as db:
        orphans = db.query(WatchlistItem).filter(WatchlistItem.watchlist_id == wid).count()
    assert orphans == 0


def test_watchlist_quotes_resolve_live_prices(auth_client):
    wid = auth_client.post(
        "/api/user/watchlists", json={"name": "quotes", "symbols": ["AAPL", "MSFT"]}
    ).json()["id"]
    body = auth_client.get("/api/user/watchlists/{}/quotes".format(wid)).json()
    assert {r["symbol"] for r in body["results"]} == {"AAPL", "MSFT"}
    assert all(r["last_price"] > 0 for r in body["results"])


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
def test_alert_lifecycle(auth_client):
    created = auth_client.post(
        "/api/user/alerts",
        json={"symbol": "aapl", "condition": "price_above", "threshold": 1.0,
              "note": "any move"},
    )
    assert created.status_code == 201, created.text
    alert = created.json()
    assert alert["symbol"] == "AAPL" and alert["trigger_count"] == 0

    patched = auth_client.patch("/api/user/alerts/{}".format(alert["id"]),
                                json={"is_active": False}).json()
    assert patched["is_active"] is False
    assert auth_client.get("/api/user/alerts?active_only=true").json() == []

    assert auth_client.delete("/api/user/alerts/{}".format(alert["id"])).status_code == 204


def test_alert_rejects_an_unknown_condition(auth_client):
    resp = auth_client.post(
        "/api/user/alerts",
        json={"symbol": "AAPL", "condition": "vibes", "threshold": 1.0},
    )
    assert resp.status_code == 422


def test_alert_evaluation_marks_triggers(auth_client):
    # A threshold of $1 is certain to be exceeded, so this must trigger.
    auth_client.post("/api/user/alerts",
                     json={"symbol": "AAPL", "condition": "price_above", "threshold": 1.0})
    # ...and one that cannot.
    auth_client.post("/api/user/alerts",
                     json={"symbol": "AAPL", "condition": "price_below", "threshold": 1.0})

    body = auth_client.post("/api/user/alerts/evaluate").json()
    assert body["checked"] == 2
    assert len(body["triggered"]) == 1
    assert body["triggered"][0]["condition"] == "price_above"

    alerts = {a["condition"]: a for a in auth_client.get("/api/user/alerts").json()}
    assert alerts["price_above"]["trigger_count"] == 1
    assert alerts["price_above"]["last_value"] > 1
    assert alerts["price_below"]["trigger_count"] == 0
    assert alerts["price_below"]["last_checked_at"] is not None


def test_alerts_are_per_user(auth_client, other_client):
    auth_client.post("/api/user/alerts",
                     json={"symbol": "AAPL", "condition": "price_above", "threshold": 1.0})
    assert other_client.get("/api/user/alerts").json() == []
    assert other_client.post("/api/user/alerts/evaluate").json()["checked"] == 0


# --------------------------------------------------------------------------- #
# Everything needs a token
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/user/settings"),
        ("get", "/api/user/saved"),
        ("get", "/api/user/history"),
        ("get", "/api/user/stats"),
        ("get", "/api/user/watchlists"),
        ("get", "/api/user/alerts"),
        ("get", "/api/auth/sessions"),
        ("get", "/api/system/database"),
    ],
)
def test_user_endpoints_require_authentication(client, method, path):
    assert getattr(client, method)(path).status_code == 401
