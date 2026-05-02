"""Unit tests for @require_scopes with stored app passwords (Login Flow v2).

Tests the third enforcement mode in scope_authorization.py that checks
application-level scopes stored alongside app passwords.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.server.fastmcp import Context

from nextcloud_mcp_server.auth.scope_authorization import (
    ProvisioningRequiredError,
    _get_stored_scopes,
    _scope_cache,
    require_scopes,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clear_scope_cache():
    """Clear scope cache before each test."""
    _scope_cache.clear()
    yield
    _scope_cache.clear()


async def test_get_stored_scopes_with_scopes():
    """Test getting specific scopes from storage."""
    mock_storage = AsyncMock()
    mock_storage.get_app_password_with_scopes.return_value = {
        "app_password": "xxxxx",
        "scopes": ["notes.read", "calendar.read"],
        "username": "alice",
        "created_at": 1000,
        "updated_at": 1000,
    }

    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_shared_storage",
        return_value=mock_storage,
    ):
        result = await _get_stored_scopes("alice")

    assert result == ["notes.read", "calendar.read"]


async def test_get_stored_scopes_null_scopes():
    """Test that NULL scopes returns 'all'."""
    mock_storage = AsyncMock()
    mock_storage.get_app_password_with_scopes.return_value = {
        "app_password": "xxxxx",
        "scopes": None,
        "username": "bob",
        "created_at": 1000,
        "updated_at": 1000,
    }

    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_shared_storage",
        return_value=mock_storage,
    ):
        result = await _get_stored_scopes("bob")

    assert result == "all"


async def test_get_stored_scopes_no_password():
    """Test that missing app password returns None."""
    mock_storage = AsyncMock()
    mock_storage.get_app_password_with_scopes.return_value = None

    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_shared_storage",
        return_value=mock_storage,
    ):
        result = await _get_stored_scopes("nobody")

    assert result is None


async def test_get_stored_scopes_storage_error():
    """Test that storage errors propagate to the caller."""
    mock_storage = AsyncMock()
    mock_storage.get_app_password_with_scopes.side_effect = RuntimeError("DB error")

    with (
        patch(
            "nextcloud_mcp_server.auth.scope_authorization.get_shared_storage",
            return_value=mock_storage,
        ),
        pytest.raises(RuntimeError, match="DB error"),
    ):
        await _get_stored_scopes("alice")


def _make_login_flow_ctx() -> MagicMock:
    """Build a minimal Context shaped like the Login-Flow-v2 / OAuth case.

    request_context.access_token must be non-None to pass the BasicAuth-mode
    short-circuit in require_scopes; the token's actual scopes don't matter
    because the Login-Flow-v2 branch checks stored scopes instead.
    """
    ctx = MagicMock()
    ctx.request_context = SimpleNamespace(
        access_token=SimpleNamespace(scopes=[], token="opaque")
    )
    ctx.elicit = AsyncMock(return_value=SimpleNamespace(action="accept", data=None))
    return ctx


async def test_decorator_elicits_before_raising_when_app_password_missing():
    """When no app password is stored, the decorator must elicit a clickable
    Astrolabe / Login-Flow-v2 prompt to the client *before* raising
    ProvisioningRequiredError.

    Why: an LLM-only error message ("call nc_auth_provision_access") is
    unfriendly to humans whose MCP client supports elicitation. See
    cbcoutinho/nextcloud-mcp-server#752.
    """
    ctx = _make_login_flow_ctx()

    @require_scopes("notes.read")
    async def fake_tool_missing_pwd(ctx: Context):  # noqa: ARG001
        return "ok"

    fake_settings = SimpleNamespace(enable_login_flow=True)
    elicit_mock = AsyncMock(return_value="accepted")

    with (
        patch(
            "nextcloud_mcp_server.auth.scope_authorization.get_settings",
            return_value=fake_settings,
        ),
        patch(
            "nextcloud_mcp_server.auth.scope_authorization._get_stored_scopes",
            return_value=None,
        ),
        patch(
            "nextcloud_mcp_server.auth.token_utils.extract_user_id_from_token",
            return_value="alice",
        ),
        patch(
            "nextcloud_mcp_server.auth.elicitation.present_provisioning_required",
            elicit_mock,
        ),
        pytest.raises(ProvisioningRequiredError),
    ):
        await fake_tool_missing_pwd(ctx=ctx)

    elicit_mock.assert_awaited_once_with(ctx)


async def test_decorator_does_not_elicit_when_scopes_only_partially_missing():
    """When the user *has* an app password but is missing some requested
    scopes, the decorator raises InsufficientScopeError (step-up auth),
    not ProvisioningRequiredError — and must not elicit the
    provisioning-required prompt, because the user is already provisioned.
    """
    from nextcloud_mcp_server.auth.scope_authorization import (
        InsufficientScopeError,
    )

    ctx = _make_login_flow_ctx()

    @require_scopes("notes.write")
    async def fake_tool_missing_scope(ctx: Context):  # noqa: ARG001
        return "ok"

    fake_settings = SimpleNamespace(enable_login_flow=True)
    elicit_mock = AsyncMock()

    with (
        patch(
            "nextcloud_mcp_server.auth.scope_authorization.get_settings",
            return_value=fake_settings,
        ),
        patch(
            "nextcloud_mcp_server.auth.scope_authorization._get_stored_scopes",
            return_value=["notes.read"],  # has read, lacks write
        ),
        patch(
            "nextcloud_mcp_server.auth.token_utils.extract_user_id_from_token",
            return_value="alice",
        ),
        patch(
            "nextcloud_mcp_server.auth.elicitation.present_provisioning_required",
            elicit_mock,
        ),
        pytest.raises(InsufficientScopeError),
    ):
        await fake_tool_missing_scope(ctx=ctx)

    elicit_mock.assert_not_awaited()
