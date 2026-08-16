def test_register_and_login(client):
    r = client.post(
        "/api/auth/register",
        json={"username": "alice", "email": "alice@example.com", "password": "secret123"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["username"] == "alice"

    # Duplicate registration rejected.
    r2 = client.post(
        "/api/auth/register",
        json={"username": "alice", "email": "alice@example.com", "password": "secret123"},
    )
    assert r2.status_code == 400

    r3 = client.post("/api/auth/login", data={"username": "alice", "password": "secret123"})
    assert r3.status_code == 200
    assert "access_token" in r3.json()


def test_login_bad_password(client):
    client.post(
        "/api/auth/register",
        json={"username": "bob", "email": "bob@example.com", "password": "secret123"},
    )
    r = client.post("/api/auth/login", data={"username": "bob", "password": "wrong"})
    assert r.status_code == 401


def test_protected_requires_token(client):
    assert client.get("/api/data/history/AAPL").status_code == 401
    assert client.get("/api/auth/me").status_code == 401


def test_me(auth_client):
    r = auth_client.get("/api/auth/me")
    assert r.status_code == 200
    assert "username" in r.json()
