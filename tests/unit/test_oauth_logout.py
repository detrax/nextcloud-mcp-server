"""Unit tests for OAuth logout (issue #626 finding 4) and the
SessionAuthBackend (finding 2).

These cover the new server-side session lifecycle:
  - logout deletes refresh token + browser session
  - logout calls IdP revocation_endpoint when available
  - logout still succeeds when IdP/storage errors
  - SessionAuthBackend resolves random session_id -> user_id, fails
    closed when the session is unknown / expired / has no refresh token
"""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet
from starlette.requests import HTTPConnection

from nextcloud_mcp_server.auth.browser_oauth_routes import (
    _revoke_refresh_token_at_idp,
    oauth_logout,
)
from nextcloud_mcp_server.auth.session_backend import SessionAuthBackend
from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# storage fixture (real SQLite backend; lighter than mocking every call)
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_logout.db"
        s = RefreshTokenStorage(
            db_path=str(db_path), encryption_key=Fernet.generate_key().decode()
        )
        await s.initialize()
        yield s


def _build_request(*, cookie: str | None, oauth_context: dict | None):
    """Build a minimal Starlette-style request stub for oauth_logout."""
    request = MagicMock()
    request.query_params = {}
    request.cookies = {"mcp_session": cookie} if cookie else {}
    request.app.state.oauth_context = oauth_context
    return request


# ---------------------------------------------------------------------------
# oauth_logout
# ---------------------------------------------------------------------------


async def test_logout_deletes_refresh_token_and_session(storage):
    """Happy path: logout removes the refresh token and the browser session."""
    await storage.create_browser_session(session_id="sid-1", user_id="alice")
    await storage.store_refresh_token(
        user_id="alice", refresh_token="rt-abc", flow_type="browser"
    )

    request = _build_request(
        cookie="sid-1",
        oauth_context={"storage": storage, "discovery_url": None},
    )

    with patch(
        "nextcloud_mcp_server.auth.browser_oauth_routes._revoke_refresh_token_at_idp",
        new=AsyncMock(),
    ):
        response = await oauth_logout(request)

    assert response.status_code == 302
    assert await storage.get_refresh_token("alice") is None
    assert await storage.get_browser_session_user("sid-1") is None


async def test_logout_calls_revocation_when_refresh_token_present(storage):
    """The IdP revocation helper is called with the stored refresh token."""
    await storage.create_browser_session(session_id="sid-2", user_id="bob")
    await storage.store_refresh_token(
        user_id="bob", refresh_token="rt-xyz", flow_type="browser"
    )

    revoke = AsyncMock()
    request = _build_request(
        cookie="sid-2",
        oauth_context={"storage": storage, "discovery_url": "http://idp/.well-known"},
    )

    with patch(
        "nextcloud_mcp_server.auth.browser_oauth_routes._revoke_refresh_token_at_idp",
        new=revoke,
    ):
        await oauth_logout(request)

    revoke.assert_awaited_once()
    args = revoke.await_args.args
    # Second arg is the refresh token string
    assert args[1] == "rt-xyz"


async def test_logout_no_session_cookie_returns_302(storage):
    """Without a cookie, logout still 302s and doesn't touch storage."""
    request = _build_request(
        cookie=None, oauth_context={"storage": storage, "discovery_url": None}
    )
    response = await oauth_logout(request)
    assert response.status_code == 302


async def test_logout_swallows_storage_errors(storage):
    """Logout is best-effort — a storage failure must not 500 the response."""
    await storage.create_browser_session(session_id="sid-3", user_id="carol")
    broken_storage = MagicMock()
    broken_storage.get_browser_session_user = AsyncMock(
        side_effect=RuntimeError("db down")
    )
    broken_storage.delete_browser_session = AsyncMock()

    request = _build_request(
        cookie="sid-3",
        oauth_context={"storage": broken_storage, "discovery_url": None},
    )
    response = await oauth_logout(request)
    assert response.status_code == 302  # logout still succeeds


async def test_logout_handles_session_with_no_refresh_token(storage):
    """Cookie + session row exist but refresh token already gone — logout is idempotent."""
    await storage.create_browser_session(session_id="sid-4", user_id="dave")

    revoke = AsyncMock()
    request = _build_request(
        cookie="sid-4",
        oauth_context={"storage": storage, "discovery_url": None},
    )
    with patch(
        "nextcloud_mcp_server.auth.browser_oauth_routes._revoke_refresh_token_at_idp",
        new=revoke,
    ):
        await oauth_logout(request)

    # Revoke not called — no token to revoke
    revoke.assert_not_called()
    # Browser session still cleared
    assert await storage.get_browser_session_user("sid-4") is None


# ---------------------------------------------------------------------------
# _revoke_refresh_token_at_idp
# ---------------------------------------------------------------------------


def _httpx_handler(routes: dict[str, httpx.Response]):
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))

    return handler


async def test_revoke_helper_posts_to_revocation_endpoint():
    discovery_url = "http://idp.example/.well-known"
    revocation_url = "http://idp.example/revoke"

    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == discovery_url:
            return httpx.Response(
                200,
                content=json.dumps({"revocation_endpoint": revocation_url}).encode(),
                headers={"content-type": "application/json"},
            )
        if str(request.url) == revocation_url:
            received.append(request)
            return httpx.Response(200)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    def fake_client(**kwargs):
        kwargs["transport"] = transport
        return httpx.AsyncClient(**kwargs)

    with patch(
        "nextcloud_mcp_server.auth.browser_oauth_routes.nextcloud_httpx_client",
        side_effect=fake_client,
    ):
        await _revoke_refresh_token_at_idp(
            {
                "discovery_url": discovery_url,
                "client_id": "test-client",
                "client_secret": "test-secret",
            },
            "rt-secret",
        )

    assert len(received) == 1
    body = received[0].content.decode()
    assert "token=rt-secret" in body
    assert "token_type_hint=refresh_token" in body


async def test_revoke_helper_skips_when_no_revocation_endpoint():
    """IdPs without a revocation_endpoint advertised: helper must no-op silently."""
    discovery_url = "http://idp.example/.well-known"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == discovery_url:
            return httpx.Response(200, json={})  # no revocation_endpoint
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    def fake_client(**kwargs):
        kwargs["transport"] = transport
        return httpx.AsyncClient(**kwargs)

    with patch(
        "nextcloud_mcp_server.auth.browser_oauth_routes.nextcloud_httpx_client",
        side_effect=fake_client,
    ):
        # Returns None and does not raise
        result = await _revoke_refresh_token_at_idp(
            {
                "discovery_url": discovery_url,
                "client_id": "x",
                "client_secret": "y",
            },
            "rt",
        )
    assert result is None


async def test_revoke_helper_silent_on_idp_error():
    """If the IdP 500s, the helper must not raise — caller treats it as best-effort."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    transport = httpx.MockTransport(handler)

    def fake_client(**kwargs):
        kwargs["transport"] = transport
        return httpx.AsyncClient(**kwargs)

    with patch(
        "nextcloud_mcp_server.auth.browser_oauth_routes.nextcloud_httpx_client",
        side_effect=fake_client,
    ):
        result = await _revoke_refresh_token_at_idp(
            {
                "discovery_url": "http://x/.well-known",
                "client_id": "x",
                "client_secret": "y",
            },
            "rt",
        )
    assert result is None


# ---------------------------------------------------------------------------
# SessionAuthBackend
# ---------------------------------------------------------------------------


def _build_conn(*, cookie: str | None, oauth_context: dict | None):
    conn = MagicMock(spec=HTTPConnection)
    conn.cookies = {"mcp_session": cookie} if cookie else {}
    conn.url = SimpleNamespace(path="/app")
    conn.app = MagicMock()
    conn.app.state.oauth_context = oauth_context
    return conn


async def test_session_backend_authenticates_known_session_with_token(storage):
    await storage.create_browser_session(session_id="sid-A", user_id="alice")
    await storage.store_refresh_token(
        user_id="alice", refresh_token="rt", flow_type="browser"
    )

    backend = SessionAuthBackend(oauth_enabled=True)
    conn = _build_conn(cookie="sid-A", oauth_context={"storage": storage})

    result = await backend.authenticate(conn)
    assert result is not None
    creds, user = result
    assert "authenticated" in creds.scopes
    assert user.username == "alice"


async def test_session_backend_rejects_unknown_session(storage):
    backend = SessionAuthBackend(oauth_enabled=True)
    conn = _build_conn(cookie="not-a-real-sid", oauth_context={"storage": storage})
    assert await backend.authenticate(conn) is None


async def test_session_backend_rejects_session_without_refresh_token(storage):
    """Defense-in-depth: session row exists but user has no refresh token."""
    await storage.create_browser_session(session_id="sid-B", user_id="bob")
    # Note: NO refresh token stored for bob

    backend = SessionAuthBackend(oauth_enabled=True)
    conn = _build_conn(cookie="sid-B", oauth_context={"storage": storage})
    assert await backend.authenticate(conn) is None


async def test_session_backend_rejects_when_no_cookie(storage):
    backend = SessionAuthBackend(oauth_enabled=True)
    conn = _build_conn(cookie=None, oauth_context={"storage": storage})
    assert await backend.authenticate(conn) is None


async def test_session_backend_basicauth_mode_short_circuits(monkeypatch, storage):
    """In BasicAuth mode (oauth_enabled=False) the backend never touches storage."""
    monkeypatch.setenv("NEXTCLOUD_USERNAME", "admin-user")
    backend = SessionAuthBackend(oauth_enabled=False)
    conn = _build_conn(cookie=None, oauth_context=None)
    result = await backend.authenticate(conn)
    assert result is not None
    _, user = result
    assert user.username == "admin-user"
