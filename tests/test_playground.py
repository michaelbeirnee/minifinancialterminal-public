"""The Python playground: the kernel process, the manager, the endpoints.

These tests spawn the real kernel subprocess (that *is* the unit — the pipe
protocol, the fd shuffle, the namespace) but never touch the network: code
under test uses stdlib, numpy and pandas only. One module-scoped kernel keeps
the spawn cost to a single import of the backend.
"""
from __future__ import annotations

import uuid

import pytest

from backend.config import settings
from backend.playground.manager import Kernel, manager


@pytest.fixture(scope="module")
def kernel():
    k = Kernel()
    yield k
    k.kill()


def run(k, code, timeout=90.0):
    return k.run(code, timeout=timeout)


# --------------------------------------------------------------------------- #
# The kernel
# --------------------------------------------------------------------------- #
def test_state_persists_between_runs(kernel):
    r1 = run(kernel, "x = 21")
    assert r1["ok"] and "x" in r1["variables"]
    r2 = run(kernel, "x * 2")
    assert r2["ok"]
    assert r2["outputs"] == [{"type": "repr", "text": "42"}]


def test_stdout_is_captured_not_streamed_into_the_pipe(kernel):
    r = run(kernel, "print('hello'); print('world')")
    assert r["ok"]
    assert r["outputs"][0] == {"type": "stdout", "text": "hello\nworld\n"}


def test_traceback_starts_at_user_code(kernel):
    r = run(kernel, "def f():\n    return 1/0\nf()")
    assert not r["ok"]
    err = r["outputs"][-1]
    assert err["type"] == "error"
    assert 'File "<playground>"' in err["text"]
    assert "ZeroDivisionError" in err["text"]
    assert "kernel.py" not in err["text"]  # our frames are trimmed


def test_kernel_survives_errors(kernel):
    run(kernel, "boom = 1")
    run(kernel, "raise ValueError('x')")
    r = run(kernel, "boom")
    assert r["ok"] and r["outputs"][0]["text"] == "1"


def test_dataframe_tail_expression_renders_as_table(kernel):
    r = run(kernel, "pd.DataFrame({'a': [1, 2], 'b': [3.5, float('nan')]})")
    assert r["ok"]
    (t,) = r["outputs"]
    assert t["type"] == "table"
    assert t["columns"] == ["a", "b"]
    assert t["rows"] == [[1, 3.5], [2, None]]  # NaN -> null, ints stay ints


def test_meaningful_index_is_kept_range_index_dropped(kernel):
    r = run(kernel, "pd.Series([1, 2], index=['x', 'y'], name='v')")
    (t,) = r["outputs"]
    assert t["columns"] == ["index", "v"]
    r2 = run(kernel, "pd.DataFrame({'v': [1]})")
    assert r2["outputs"][0]["columns"] == ["v"]


def test_big_table_is_truncated_with_a_note(kernel):
    r = run(kernel, "show(pd.DataFrame({'n': range(500)}))")
    (t,) = r["outputs"]
    assert len(t["rows"]) == 200
    assert "500" in t["note"]


def test_chart_from_dataframe_and_arrays(kernel):
    r = run(kernel, "chart(pd.DataFrame({'a': [1, 2, 3], 'b': [3, 2, 1]}), title='t')")
    (c,) = r["outputs"]
    assert c["type"] == "chart" and c["title"] == "t"
    assert [s["label"] for s in c["series"]] == ["a", "b"]
    assert c["x"] == [0, 1, 2]
    r2 = run(kernel, "chart([1, 4, 9], x=[1, 2, 3], labels=['sq'])")
    (c2,) = r2["outputs"]
    assert c2["series"][0] == {"label": "sq", "data": [1, 4, 9]} and c2["x"] == [1, 2, 3]


def test_mft_and_sklearn_are_available(kernel):
    r = run(kernel, "import sklearn\nprint(type(mft).__name__, hasattr(mft, 'equity'))")
    assert r["ok"], r["outputs"]
    assert "True" in r["outputs"][0]["text"]


def test_namespace_variables_hide_the_preloaded_names(kernel):
    r = run(kernel, "y = 1")
    assert "y" in r["variables"]
    for builtin_name in ("mft", "np", "pd", "show", "chart", "live_ticks"):
        assert builtin_name not in r["variables"]


def test_timeout_kills_and_restart_loses_state():
    k = Kernel()
    try:
        assert k.run("slow = 'state'", timeout=90)["ok"]
        r = k.run("while True: pass", timeout=2)
        assert not r["ok"] and r.get("restarted")
        assert "Timed out" in r["outputs"][0]["text"]
        assert not k.alive()
        r2 = k.run("'slow' in dir()", timeout=90)
        assert r2["fresh"] is True
        assert r2["outputs"][0]["text"] == "False"
    finally:
        k.kill()


# --------------------------------------------------------------------------- #
# The manager
# --------------------------------------------------------------------------- #
def test_manager_scopes_kernels_per_user():
    try:
        manager.run(9001, "secret = 'alpha'")
        r = manager.run(9002, "'secret' in dir()")
        assert r["outputs"][0]["text"] == "False"
        r2 = manager.run(9001, "secret")
        assert r2["outputs"][0]["text"] == "'alpha'"
    finally:
        manager.reset(9001)
        manager.reset(9002)


def test_manager_reset():
    try:
        manager.run(9003, "z = 1")
        assert manager.reset(9003) is True
        assert manager.status(9003) is None
        assert manager.reset(9003) is False
    finally:
        manager.reset(9003)


# --------------------------------------------------------------------------- #
# The endpoints
# --------------------------------------------------------------------------- #
def _register(client):
    username = "pg_{}".format(uuid.uuid4().hex[:10])
    client.post("/api/auth/register", json={
        "username": username, "email": "{}@example.com".format(username),
        "password": "secret123"})
    tok = client.post("/api/auth/login",
                      data={"username": username, "password": "secret123"}).json()["access_token"]
    return {"Authorization": "Bearer {}".format(tok)}


def test_endpoints_require_a_token(client):
    assert client.post("/api/playground/run", json={"code": "1"}).status_code == 401
    assert client.get("/api/playground/status").status_code == 401
    assert client.post("/api/playground/reset").status_code == 401


def test_status_reports_packages_and_enablement(auth_client):
    body = auth_client.get("/api/playground/status").json()
    assert body["enabled"] is True  # tests run with MFT_DEBUG default on
    assert body["packages"]["pandas"]
    assert body["packages"]["scikit-learn"]


def test_run_and_reset_roundtrip(auth_client):
    r = auth_client.post("/api/playground/run", json={"code": "v = 7\nv"}).json()
    assert r["ok"] and r["outputs"][-1]["text"] == "7"
    r2 = auth_client.post("/api/playground/run", json={"code": "v + 1"}).json()
    assert r2["outputs"][0]["text"] == "8"
    assert auth_client.post("/api/playground/reset").json()["killed"] is True
    r3 = auth_client.post("/api/playground/run", json={"code": "'v' in dir()"}).json()
    assert r3["outputs"][0]["text"] == "False"
    auth_client.post("/api/playground/reset")


def test_run_rejects_empty_code(auth_client):
    assert auth_client.post("/api/playground/run", json={"code": "   "}).status_code == 422


def test_disabled_deployment_refuses_runs(auth_client, monkeypatch):
    monkeypatch.setattr(settings, "playground_enabled", False)
    r = auth_client.post("/api/playground/run", json={"code": "1"})
    assert r.status_code == 403
    assert "MFT_PLAYGROUND_ENABLED" in r.json()["detail"]
    # status still answers, so the UI can explain the off state
    assert auth_client.get("/api/playground/status").json()["enabled"] is False


def test_enablement_follows_debug_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "playground_enabled", None)
    monkeypatch.setattr(settings, "debug", False)
    assert settings.playground_on is False
    monkeypatch.setattr(settings, "playground_enabled", True)
    assert settings.playground_on is True
