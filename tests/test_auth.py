"""Tests for slack_log.web.auth — the OIDC guard.

Full OAuth round-trips need a live authentik, so these tests cover the parts
that must hold without one: env-driven on/off switch, and the AuthMiddleware
gate (anonymous → 302 to login, /healthz + /auth/* stay public).
"""


from fastapi import FastAPI
from fastapi.testclient import TestClient

from slack_log.pipeline import index
from slack_log.web import auth
from slack_log.web.app import create_app
from slack_log.store import JsonlStore


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


def test_auth_routes_not_shadowed_by_static_mount(tmp_path, monkeypatch):
    """Regression: StaticFiles mount('/') is a catch-all. install_auth must run
    BEFORE the mount, or /auth/login gets shadowed and 404s — breaking login."""
    monkeypatch.setenv("OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OIDC_DISCOVERY_URL", "https://sso.example/.well-known/openid-configuration")
    data = tmp_path / "data"
    (data / "channels").mkdir(parents=True)
    (data / "users.json").write_text("{}")
    (data / "channels.json").write_text("{}")
    db = tmp_path / "search.db"
    index.build_index(data, db)
    app = create_app(JsonlStore(data_root=data, db_path=db))
    client = TestClient(app, raise_server_exceptions=False)
    # /auth/login must reach the auth handler. If StaticFiles shadowed it the
    # response is 404; reaching the handler yields 302 (or 500 when the fake
    # discovery URL is unreachable) — anything but 404.
    r = client.get("/auth/login", follow_redirects=False)
    assert r.status_code != 404, "auth route shadowed by static mount"


def _capture_access(client_call):
    """Run a request and return access-log messages from the slack_log.access
    logger (propagate=False, so attach a handler directly)."""
    import logging
    logger = logging.getLogger("slack_log.access")
    records: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    h = _Grab()
    logger.addHandler(h)
    try:
        client_call()
    finally:
        logger.removeHandler(h)
    return records


def test_access_log_records_path_and_user():
    """AccessLogMiddleware emits an access line with path + user fields.
    Stream target (stdout) is enforced in code: StreamHandler(sys.stdout)."""
    client = TestClient(_app_with_auth())
    msgs = _capture_access(lambda: client.get("/search?q=foo", follow_redirects=False))
    line = next((m for m in msgs if "access path=/search" in m), None)
    assert line is not None, "no access log line emitted"
    assert "status=" in line and "user_name=" in line


def test_access_log_skips_healthz():
    """/healthz is probe noise — must not produce an access line."""
    client = TestClient(_app_with_auth())
    msgs = _capture_access(lambda: client.get("/healthz"))
    assert not any("access path=/healthz" in m for m in msgs)


def test_access_logger_targets_stdout():
    """12-factor: access log must write to stdout, not stderr."""
    import logging
    import sys
    logger = logging.getLogger("slack_log.access")
    streams = [getattr(h, "stream", None) for h in logger.handlers]
    assert sys.stdout in streams, "access logger must have a stdout StreamHandler"
    assert sys.stderr not in streams, "access logger must not write to stderr"


def test_server_create_app_no_auth_without_env(tmp_path, monkeypatch):
    """create_app must stay unauthenticated when OIDC env is absent."""
    for k in ("OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_DISCOVERY_URL"):
        monkeypatch.delenv(k, raising=False)
    data = tmp_path / "data"
    (data / "channels").mkdir(parents=True)
    (data / "users.json").write_text("{}")
    (data / "channels.json").write_text("{}")
    db = tmp_path / "search.db"
    index.build_index(data, db)
    app = create_app(JsonlStore(data_root=data, db_path=db))
    client = TestClient(app)
    # No auth → /search reachable directly, no redirect to login.
    r = client.get("/search", follow_redirects=False)
    assert r.status_code == 200
