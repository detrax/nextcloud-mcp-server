"""Unit tests for ``browser_oauth_routes`` helpers.

Pins the round-6 review fix that ``_should_use_secure_cookies`` must not
trust ``bool(settings.cookie_secure)`` — Dynaconf normally coerces but
tests / direct ``settings.set`` calls can leave the raw string in place,
and ``bool("false")`` is ``True``.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet

from nextcloud_mcp_server.auth import browser_oauth_routes, token_utils
from nextcloud_mcp_server.auth.browser_oauth_routes import oauth_login_callback
from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

pytestmark = pytest.mark.unit


def _fake_settings(*, cookie_secure, mcp_server_url=""):
    return type(
        "S",
        (),
        {
            "cookie_secure": cookie_secure,
            "nextcloud_mcp_server_url": mcp_server_url,
        },
    )()


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        (False, False),
        ("true", True),
        ("false", False),
        ("True", True),
        ("FALSE", False),
        ("0", False),
        ("1", True),
        ("no", False),
        ("yes", True),
        ("off", False),
        ("on", True),
        ("", False),
    ],
)
def test_should_use_secure_cookies_string_coercion(monkeypatch, value, expected):
    monkeypatch.setattr(
        browser_oauth_routes,
        "get_settings",
        lambda: _fake_settings(cookie_secure=value),
    )
    assert browser_oauth_routes._should_use_secure_cookies() is expected


def test_should_use_secure_cookies_falls_back_to_https_scheme(monkeypatch):
    monkeypatch.setattr(
        browser_oauth_routes,
        "get_settings",
        lambda: _fake_settings(
            cookie_secure=None, mcp_server_url="https://mcp.example.com"
        ),
    )
    assert browser_oauth_routes._should_use_secure_cookies() is True


def test_should_use_secure_cookies_falls_back_to_http_scheme(monkeypatch):
    monkeypatch.setattr(
        browser_oauth_routes,
        "get_settings",
        lambda: _fake_settings(
            cookie_secure=None, mcp_server_url="http://localhost:8000"
        ),
    )
    assert browser_oauth_routes._should_use_secure_cookies() is False


# ---------------------------------------------------------------------------
# oauth_login_callback: missing refresh_token must NOT create a session
# ---------------------------------------------------------------------------
#
# Pins PR #758 round-7 medium 1: when the IdP returns no refresh_token,
# ``SessionAuthBackend`` would silently reject every subsequent request
# (because ``get_refresh_token`` returns None), bouncing the user back to
# ``/oauth/login`` in a loop. The callback now bails with a 400 error page
# *before* any browser_sessions row or Set-Cookie header is created.


@pytest.fixture
def _clear_oidc_caches():
    token_utils._discovery_cache.clear()
    token_utils._jwks_cache.clear()
    token_utils._fetch_locks.clear()
    yield
    token_utils._discovery_cache.clear()
    token_utils._jwks_cache.clear()
    token_utils._fetch_locks.clear()


@pytest.fixture
async def _no_refresh_storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        s = RefreshTokenStorage(
            db_path=str(Path(tmpdir) / "norefresh.db"),
            encryption_key=Fernet.generate_key().decode(),
        )
        await s.initialize()
        yield s


async def test_callback_rejects_token_response_without_refresh_token(
    _clear_oidc_caches, _no_refresh_storage
):
    storage = _no_refresh_storage
    state = "state-norefresh"

    await storage.store_oauth_session(
        session_id=state,
        client_id="browser-ui",
        client_redirect_uri="/app",
        state=state,
        code_challenge="cc",
        code_challenge_method="S256",
        mcp_authorization_code="cv",
        flow_type="browser",
        ttl_seconds=600,
    )

    discovery = {
        "issuer": "http://idp.example",
        "token_endpoint": "http://idp.example/token",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                content=json.dumps(discovery).encode(),
                headers={"content-type": "application/json"},
            )
        if str(request.url) == "http://idp.example/token":
            # Successful token exchange but no refresh_token (e.g. IdP
            # config without offline_access).
            return httpx.Response(
                200,
                content=json.dumps(
                    {
                        "access_token": "at",
                        "id_token": "id-token-stub",
                        "token_type": "Bearer",
                    }
                ).encode(),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    def fake_client(**kwargs):
        kwargs["transport"] = transport
        return httpx.AsyncClient(**kwargs)

    request = MagicMock()
    request.query_params = {"code": "abc", "state": state}
    request.cookies = {}
    request.app.state.oauth_context = {
        "storage": storage,
        "oauth_client": None,
        "config": {
            "discovery_url": "http://idp.example/.well-known/openid-configuration",
            "client_id": "test",
            "client_secret": "secret",
            "mcp_server_url": "http://localhost",
        },
    }
    request.url_for = MagicMock(return_value="/oauth/login")

    fake_userinfo = {"sub": "alice", "preferred_username": "alice"}

    with (
        patch(
            "nextcloud_mcp_server.auth.browser_oauth_routes.nextcloud_httpx_client",
            side_effect=fake_client,
        ),
        patch(
            "nextcloud_mcp_server.auth.token_utils.nextcloud_httpx_client",
            side_effect=fake_client,
        ),
        patch(
            "nextcloud_mcp_server.auth.browser_oauth_routes.verify_id_token",
            new=AsyncMock(return_value=fake_userinfo),
        ),
        patch(
            "nextcloud_mcp_server.auth.browser_oauth_routes._get_userinfo_endpoint",
            new=AsyncMock(return_value=None),
        ),
    ):
        response = await oauth_login_callback(request)

    assert response.status_code == 400
    body = response.body.decode()
    assert "Login Failed" in body
    assert "refresh token" in body.lower()

    # No browser session row may have been created.
    assert await storage.get_browser_session_user("ignored") is None
    # Nothing under the verified user_id either.
    assert await storage.get_refresh_token("alice") is None

    # No Set-Cookie header — the user must not walk away with an unusable
    # session cookie.
    set_cookie = response.headers.get("set-cookie", "")
    assert "mcp_session" not in set_cookie
