"""OIDC authentication against authentik — login / callback / logout + a guard.

Auth is **opt-in**: it activates only when OIDC_CLIENT_ID / OIDC_CLIENT_SECRET /
OIDC_DISCOVERY_URL are all set in the environment (production deployment).
Local dev and pytest run with no auth so nothing external is required.

The slack-log archive contains real names and internal discussion, so the
production deployment must never be reachable without a login.
"""

from __future__ import annotations

import os
import secrets
from urllib.parse import quote, urlparse

from authlib.integrations.starlette_client import OAuth
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

# /healthz must stay open — k8s liveness/readiness probes hit it unauthenticated.
PUBLIC_PATHS = frozenset({"/healthz"})
PUBLIC_PREFIXES = ("/auth/",)


def _setup_oauth(client_id: str, client_secret: str, discovery_url: str) -> OAuth:
    oauth = OAuth()
    oauth.register(
        name="authentik",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url=discovery_url,  # lazy — fetched on first use, not now
        client_kwargs={
            "scope": "openid profile email",
            "headers": {"Accept": "application/json"},
        },
    )
    return oauth


async def _login(request: Request) -> Response:
    oauth: OAuth = request.app.state.oauth
    redirect_uri = str(request.url_for("auth_callback"))
    request.session["next"] = request.query_params.get("next", "/")
    return await oauth.authentik.authorize_redirect(request, redirect_uri)


async def _callback(request: Request) -> Response:
    from authlib.integrations.base_client.errors import MismatchingStateError, OAuthError

    oauth: OAuth = request.app.state.oauth
    try:
        token = await oauth.authentik.authorize_access_token(request)
    except (MismatchingStateError, OAuthError):
        # Stale state (logged out mid-flow, session expired) — restart login.
        return RedirectResponse(url="/auth/login", status_code=302)

    userinfo = token.get("userinfo", {})
    request.session["user"] = {
        "sub": userinfo.get("sub", ""),
        "name": userinfo.get("name"),
        "email": userinfo.get("email"),
    }
    id_token = token.get("id_token")
    if id_token:
        request.session["id_token"] = id_token

    next_path = request.session.pop("next", "/")
    parsed = urlparse(next_path)
    if parsed.scheme or parsed.netloc:  # reject open-redirect
        next_path = "/"
    return RedirectResponse(url=next_path, status_code=302)


async def _logout(request: Request) -> Response:
    if request.method != "POST":
        return Response(status_code=405)

    id_token = request.session.get("id_token")
    request.session.clear()

    if id_token:
        try:
            oauth: OAuth = request.app.state.oauth
            metadata = await oauth.authentik.load_server_metadata()
            end_session = metadata.get("end_session_endpoint")
            if end_session:
                return RedirectResponse(
                    url=f"{end_session}?id_token_hint={id_token}"
                    f"&post_logout_redirect_uri={request.base_url}",
                    status_code=302,
                )
        except Exception:
            pass
    return RedirectResponse(url="/", status_code=302)


class AuthMiddleware(BaseHTTPMiddleware):
    """Reject any request without a logged-in session, except public paths."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)
        if not request.session.get("user"):
            full = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(
                url=f"/auth/login?next={quote(full, safe='')}", status_code=302
            )
        return await call_next(request)


def auth_config_from_env() -> dict | None:
    """OIDC config from env, or None when auth should stay off (dev / pytest)."""
    cid = os.environ.get("OIDC_CLIENT_ID")
    secret = os.environ.get("OIDC_CLIENT_SECRET")
    discovery = os.environ.get("OIDC_DISCOVERY_URL")
    if not (cid and secret and discovery):
        return None
    return {
        "client_id": cid,
        "client_secret": secret,
        "discovery_url": discovery,
        # A missing SESSION_SECRET_KEY would silently invalidate cookies on every
        # restart; generate a process-stable fallback but warn-worthy in prod.
        "session_secret": os.environ.get("SESSION_SECRET_KEY") or secrets.token_hex(32),
        "cookie_secure": os.environ.get("SESSION_COOKIE_SECURE", "true").lower() == "true",
    }


def install_auth(
    app,
    *,
    client_id: str,
    client_secret: str,
    discovery_url: str,
    session_secret: str,
    cookie_secure: bool = True,
) -> None:
    """Wire OIDC auth onto a FastAPI/Starlette app: oauth + routes + middleware."""
    app.state.oauth = _setup_oauth(client_id, client_secret, discovery_url)
    app.add_route("/auth/login", _login, name="auth_login")
    app.add_route("/auth/callback", _callback, name="auth_callback")
    app.add_route("/auth/logout", _logout, methods=["POST"], name="auth_logout")
    # Middleware runs outer-to-inner in reverse add order: add AuthMiddleware
    # first (inner) so SessionMiddleware (added last, outer) populates the
    # session before the guard reads it.
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        SessionMiddleware, secret_key=session_secret, https_only=cookie_secure
    )
