"""Tests for slack_log.auth — the OIDC guard.

Full OAuth round-trips need a live authentik, so these tests cover the parts
that must hold without one: env-driven on/off switch, and the AuthMiddleware
gate (anonymous → 302 to login, /healthz + /auth/* stay public).
"""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from slack_log import auth, indexer, server


def test_auth_config_from_env_off_when_unset(monkeypatch):
    for k in ("OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_DISCOVERY_URL"):
        monkeypatch.delenv(k, raising=False)
    assert auth.auth_config_from_env() is None


def test_auth_config_from_env_on_when_all_set(monkeypatch):
    monkeypatch.setenv("OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OIDC_DISCOVERY_URL", "https://sso.example/.well-known/openid-configuration")
    cfg = auth.auth_config_from_env()
    assert cfg is not None
    assert cfg["client_id"] == "cid"
    assert cfg["session_secret"]  # falls back to a generated value, never empty


def test_auth_config_partial_is_off(monkeypatch):
    monkeypatch.setenv("OIDC_CLIENT_ID", "cid")
    monkeypatch.delenv("OIDC_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("OIDC_DISCOVERY_URL", "https://sso.example/x")
    assert auth.auth_config_from_env() is None


def _app_with_auth() -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/search")
    def search():
        return {"hits": []}

    auth.install_auth(
        app,
        client_id="cid",
        client_secret="secret",
        discovery_url="https://sso.example/.well-known/openid-configuration",
        session_secret="test-session-secret",
        cookie_secure=False,
    )
    return app


def test_anonymous_request_redirects_to_login():
    client = TestClient(_app_with_auth())
    r = client.get("/search?q=foo", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("/auth/login")
    # original path preserved so login can bounce back
    assert "next=" in loc and "foo" in loc


def test_healthz_stays_public_under_auth():
    """k8s probes hit /healthz unauthenticated — it must never 302."""
    client = TestClient(_app_with_auth())
    r = client.get("/healthz", follow_redirects=False)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_auth_prefix_is_public():
    """The /auth/* prefix must bypass the guard — otherwise login can't run
    and you'd get an infinite redirect. Hitting a nonexistent /auth/ path
    should 404 (reached routing) rather than 302 (bounced by the guard)."""
    client = TestClient(_app_with_auth())
    r = client.get("/auth/does-not-exist", follow_redirects=False)
    assert r.status_code == 404


def test_server_create_app_no_auth_without_env(tmp_path, monkeypatch):
    """create_app must stay unauthenticated when OIDC env is absent."""
    for k in ("OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_DISCOVERY_URL"):
        monkeypatch.delenv(k, raising=False)
    data = tmp_path / "data"
    (data / "channels").mkdir(parents=True)
    (data / "users.json").write_text("{}")
    (data / "channels.json").write_text("{}")
    db = tmp_path / "search.db"
    indexer.build_index(data, db)
    html = tmp_path / "html"
    html.mkdir()
    app = server.create_app(db_path=db, html_root=html, data_root=data)
    client = TestClient(app)
    # No auth → /search reachable directly, no redirect to login.
    r = client.get("/search", follow_redirects=False)
    assert r.status_code == 200
