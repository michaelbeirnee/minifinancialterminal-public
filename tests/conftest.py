"""Shared pytest fixtures.

Market data is pulled live from Yahoo Finance, so these tests require network
access. They run against a throwaway SQLite DB and cache dir so they never touch
a developer's real database.
"""
import os
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.environ["MFT_DATABASE_URL"] = f"sqlite:///{_db_path}"
os.environ["MFT_CACHE_DIR"] = tempfile.mkdtemp()

import pytest
from fastapi.testclient import TestClient

from backend.database import init_db
from backend.main import app


@pytest.fixture(scope="session", autouse=True)
def _setup_db():
    init_db()
    yield
    try:
        os.close(_db_fd)
        os.unlink(_db_path)
    except OSError:
        pass


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def auth_client(client):
    """A TestClient with a registered+logged-in user's bearer token set."""
    import uuid

    username = f"user_{uuid.uuid4().hex[:8]}"
    client.post(
        "/api/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": "secret123"},
    )
    tok = client.post(
        "/api/auth/login", data={"username": username, "password": "secret123"}
    ).json()["access_token"]
    client.headers.update({"Authorization": f"Bearer {tok}"})
    return client
