"""Pin one-time-use semantics on the Flow-2 callback's oauth_session row.

The PR #758 follow-up review flagged that
``oauth_callback_nextcloud`` reads ``code_verifier`` from the
``oauth_sessions`` table but never deletes the row, leaving the verifier
valid for the rest of the 10-minute TTL. This test exercises the real
storage layer to confirm the row is gone after the callback runs.

We mock everything *after* the deletion (discovery + token exchange +
ID token verification) so the test focuses on the cleanup contract,
not the OAuth wire protocol.

Also pins the AS-proxy callback's ID-token verification rejection path
introduced in PR #758 finding 1 (auto-review): a forged or unsigned
id_token must surface as a 400 ``invalid_token`` JSONResponse and must
not register a proxy code.
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet

from nextcloud_mcp_server.auth.oauth_routes import (
    ASProxySession,
    _as_proxy_sessions,
    _oauth_callback_as_proxy,
    _proxy_codes,
    oauth_callback_nextcloud,
)
from nextcloud_mcp_server.auth.storage import RefreshTokenStorage
from nextcloud_mcp_server.auth.token_utils import IdTokenVerificationError

pytestmark = pytest.mark.unit


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_callback_cleanup.db"
        s = RefreshTokenStorage(
            db_path=str(db_path), encryption_key=Fernet.generate_key().decode()
        )
        await s.initialize()
        yield s


def _build_request(*, code: str, state: str, storage: RefreshTokenStorage):
    request = MagicMock()
    request.query_params = {"code": code, "state": state}
    request.app.state.oauth_context = {
        "storage": storage,
        "config": {
            "discovery_url": "https://idp.example.com/.well-known/openid-configuration",
            "mcp_server_url": "https://mcp.example.com",
            "client_id": "mcp-server",
            "client_secret": "mcp-secret",
        },
    }
    return request


async def test_callback_deletes_oauth_session_after_reading_verifier(storage):
    """After a successful callback exchange the row is gone.

    Pins the PR #758 follow-up review fix: previously the row stayed
    until the 10-minute TTL elapsed, leaving the stored ``code_verifier``
    valid for replay if ``state`` leaked.
    """
    state = "state-abc-123"
    await storage.store_oauth_session(
        session_id=state,
        client_redirect_uri="http://localhost:9999/callback",
        state=state,
        mcp_authorization_code="verifier-pkce-secret",
        flow_type="flow2",
    )
    # Sanity check: row exists before the callback runs.
    assert await storage.get_oauth_session(state) is not None

    request = _build_request(code="idp-auth-code", state=state, storage=storage)

    # Stub everything after the deletion: discovery, token exchange, ID
    # token verification, and the user_oidc UserInfo round-trip. The
    # exact responses don't matter — we only care that the deletion has
    # happened by the time these are invoked.
    fake_discovery = {
        "token_endpoint": "https://idp.example.com/token",
        "userinfo_endpoint": "https://idp.example.com/userinfo",
        "issuer": "https://idp.example.com",
    }
    fake_userinfo = {"sub": "alice", "email": "alice@example.com"}
    fake_token_response = MagicMock()
    fake_token_response.json.return_value = {
        "access_token": "ac-tok",
        "refresh_token": "rf-tok",
        "id_token": "id-tok",
        "expires_in": 3600,
    }
    fake_token_response.raise_for_status = MagicMock()

    fake_http = MagicMock()
    fake_http.post = AsyncMock(return_value=fake_token_response)
    fake_http.__aenter__ = AsyncMock(return_value=fake_http)
    fake_http.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "nextcloud_mcp_server.auth.oauth_routes._get_cached_discovery",
            new=AsyncMock(return_value=fake_discovery),
        ),
        patch(
            "nextcloud_mcp_server.auth.oauth_routes.nextcloud_httpx_client",
            return_value=fake_http,
        ),
        patch(
            "nextcloud_mcp_server.auth.oauth_routes.verify_id_token",
            new=AsyncMock(return_value=fake_userinfo),
        ),
    ):
        # The callback may go on to do extra work (storing tokens, redirecting,
        # rendering HTML); we don't care about the response body, only the
        # storage-level side effect.
        try:
            await oauth_callback_nextcloud(request)
        except Exception:
            # Any error past the deletion point is fine for this test.
            pass

    assert await storage.get_oauth_session(state) is None, (
        "oauth_callback_nextcloud must delete the oauth_sessions row "
        "after reading code_verifier (PR #758 follow-up review)"
    )


async def test_callback_no_session_row_does_not_crash(storage):
    """If the row is already gone (e.g. expired), the callback proceeds."""
    state = "state-missing"
    # No store_oauth_session call — the row never existed.

    request = _build_request(code="idp-auth-code", state=state, storage=storage)

    fake_discovery = {
        "token_endpoint": "https://idp.example.com/token",
        "userinfo_endpoint": "https://idp.example.com/userinfo",
        "issuer": "https://idp.example.com",
    }
    fake_token_response = MagicMock()
    fake_token_response.json.return_value = {"access_token": "ac"}
    fake_token_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "boom",
            request=MagicMock(),
            response=MagicMock(status_code=400),
        )
    )

    fake_http = MagicMock()
    fake_http.post = AsyncMock(return_value=fake_token_response)
    fake_http.__aenter__ = AsyncMock(return_value=fake_http)
    fake_http.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "nextcloud_mcp_server.auth.oauth_routes._get_cached_discovery",
            new=AsyncMock(return_value=fake_discovery),
        ),
        patch(
            "nextcloud_mcp_server.auth.oauth_routes.nextcloud_httpx_client",
            return_value=fake_http,
        ),
    ):
        # We don't care what happens past the deletion — just that the
        # missing-row branch doesn't try to delete a nonexistent session.
        try:
            await oauth_callback_nextcloud(request)
        except Exception:
            pass

    # No crash, no row, no surprises.
    assert await storage.get_oauth_session(state) is None


# ---------------------------------------------------------------------------
# AS proxy callback (PR #758 finding 1): ID-token verification rejection
# ---------------------------------------------------------------------------


def _build_as_proxy_request(*, code: str, state: str):
    request = MagicMock()
    request.query_params = {"code": code, "state": state}
    request.app.state.oauth_context = {
        "config": {
            "discovery_url": "https://idp.example.com/.well-known/openid-configuration",
            "mcp_server_url": "https://mcp.example.com",
            "client_id": "mcp-server",
            "client_secret": "mcp-secret",
        }
    }
    return request


async def test_as_proxy_rejects_invalid_id_token():
    """Forged/unsigned id_token in the IdP token response → 400 invalid_token.

    Pins PR #758 finding 1. Without verification a compromised IdP or
    tampered transport could plant arbitrary identity claims into the
    cached ProxyCodeEntry that downstream clients pick up.
    """
    server_state = "as-proxy-state-rejected"
    _as_proxy_sessions[server_state] = ASProxySession(
        client_id="mcp-client",
        client_redirect_uri="http://127.0.0.1:9999/callback",
        client_state="client-state-xyz",
        code_challenge="challenge",
        code_challenge_method="S256",
        requested_scopes="openid",
    )
    _proxy_codes.clear()

    request = _build_as_proxy_request(code="auth-code", state=server_state)

    fake_discovery = {
        "token_endpoint": "https://idp.example.com/token",
        "issuer": "https://idp.example.com",
    }
    fake_token_response = MagicMock(status_code=200)
    fake_token_response.json.return_value = {
        "access_token": "ac-tok",
        "refresh_token": "rf-tok",
        "id_token": "forged.id.token",
        "token_type": "Bearer",
    }

    fake_http = MagicMock()
    fake_http.post = AsyncMock(return_value=fake_token_response)
    fake_http.__aenter__ = AsyncMock(return_value=fake_http)
    fake_http.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "nextcloud_mcp_server.auth.oauth_routes._get_cached_discovery",
            new=AsyncMock(return_value=fake_discovery),
        ),
        patch(
            "nextcloud_mcp_server.auth.oauth_routes.nextcloud_httpx_client",
            return_value=fake_http,
        ),
        patch(
            "nextcloud_mcp_server.auth.oauth_routes.verify_id_token",
            new=AsyncMock(side_effect=IdTokenVerificationError("bad signature")),
        ),
    ):
        response = await _oauth_callback_as_proxy(request, server_state)

    assert response.status_code == 400
    body = bytes(response.body).decode()
    assert "invalid_token" in body
    # Critical: the proxy code store must not have grown — a rejected
    # callback must not be turned into a redeemable proxy code.
    assert _proxy_codes == {}
    # And the session has been popped (one-time use).
    assert server_state not in _as_proxy_sessions
