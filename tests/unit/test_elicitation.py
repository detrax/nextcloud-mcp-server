"""Unit tests for the MCP elicitation helpers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nextcloud_mcp_server.auth.elicitation import (
    ASTROLABE_SETTINGS_PATH,
    _astrolabe_settings_url,
    present_provisioning_required,
)

pytestmark = pytest.mark.unit


def _fake_settings(
    public_issuer_url: str | None = None, host: str | None = None
) -> SimpleNamespace:
    """Build a Settings-shaped object exposing only the fields elicitation reads."""
    return SimpleNamespace(
        nextcloud_public_issuer_url=public_issuer_url,
        nextcloud_host=host,
    )


def test_astrolabe_settings_url_prefers_public_issuer():
    """Public issuer wins over host so the link is browser-reachable in Docker."""
    fake = _fake_settings(
        public_issuer_url="https://nc.example.com", host="http://internal:8080"
    )
    with patch("nextcloud_mcp_server.auth.elicitation.get_settings", return_value=fake):
        assert (
            _astrolabe_settings_url()
            == f"https://nc.example.com{ASTROLABE_SETTINGS_PATH}"
        )


def test_astrolabe_settings_url_strips_trailing_slash_from_public_issuer():
    """Trailing slash on nextcloud_public_issuer_url is normalized."""
    fake = _fake_settings(public_issuer_url="https://nc.example.com/")
    with patch("nextcloud_mcp_server.auth.elicitation.get_settings", return_value=fake):
        assert (
            _astrolabe_settings_url()
            == f"https://nc.example.com{ASTROLABE_SETTINGS_PATH}"
        )


def test_astrolabe_settings_url_falls_back_to_host():
    """When only nextcloud_host is set, use it (and strip a trailing slash)."""
    fake = _fake_settings(host="https://only-host.example.com/")
    with patch("nextcloud_mcp_server.auth.elicitation.get_settings", return_value=fake):
        assert (
            _astrolabe_settings_url()
            == f"https://only-host.example.com{ASTROLABE_SETTINGS_PATH}"
        )


def test_astrolabe_settings_url_returns_none_when_unset():
    """No NC URL configured → None (caller renders the tool-only message)."""
    fake = _fake_settings()
    with patch("nextcloud_mcp_server.auth.elicitation.get_settings", return_value=fake):
        assert _astrolabe_settings_url() is None


async def test_present_provisioning_required_elicits_with_url():
    """When NC URL is set and the client supports elicitation, send the URL."""
    fake = _fake_settings(public_issuer_url="https://nc.example.com")
    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=SimpleNamespace(action="accept", data=None))

    with patch("nextcloud_mcp_server.auth.elicitation.get_settings", return_value=fake):
        result = await present_provisioning_required(ctx)

    assert result == "accepted"
    ctx.elicit.assert_awaited_once()
    sent_message = ctx.elicit.await_args.kwargs["message"]
    assert "https://nc.example.com/index.php/apps/astrolabe/settings" in sent_message
    assert "nc_auth_provision_access" in sent_message


async def test_present_provisioning_required_without_url():
    """When neither NC URL is set, fall back to the tool-only message."""
    fake = _fake_settings()
    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=SimpleNamespace(action="accept", data=None))

    with patch("nextcloud_mcp_server.auth.elicitation.get_settings", return_value=fake):
        result = await present_provisioning_required(ctx)

    assert result == "accepted"
    sent_message = ctx.elicit.await_args.kwargs["message"]
    assert "astrolabe" not in sent_message.lower()
    assert "nc_auth_provision_access" in sent_message


async def test_present_provisioning_required_no_elicit_method():
    """Contexts that don't expose ctx.elicit fall back to message_only."""

    class _NoElicit:
        pass

    ctx = _NoElicit()

    fake = _fake_settings()
    with patch("nextcloud_mcp_server.auth.elicitation.get_settings", return_value=fake):
        result = await present_provisioning_required(ctx)  # type: ignore[arg-type]

    assert result == "message_only"


async def test_present_provisioning_required_handles_not_implemented():
    """SDK clients that don't support elicitation raise NotImplementedError."""
    fake = _fake_settings()
    ctx = MagicMock()
    ctx.elicit = AsyncMock(side_effect=NotImplementedError("client lacks elicit"))

    with patch("nextcloud_mcp_server.auth.elicitation.get_settings", return_value=fake):
        result = await present_provisioning_required(ctx)

    assert result == "message_only"


async def test_present_provisioning_required_handles_unexpected_error():
    """Any other elicit failure (e.g. transport) is fail-open to message_only."""
    fake = _fake_settings()
    ctx = MagicMock()
    ctx.elicit = AsyncMock(side_effect=RuntimeError("transport boom"))

    with patch("nextcloud_mcp_server.auth.elicitation.get_settings", return_value=fake):
        result = await present_provisioning_required(ctx)

    assert result == "message_only"


async def test_present_provisioning_required_decline_returns_declined():
    """User chose 'decline' on the prompt → propagate that to the caller."""
    fake = _fake_settings()
    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=SimpleNamespace(action="decline", data=None))

    with patch("nextcloud_mcp_server.auth.elicitation.get_settings", return_value=fake):
        result = await present_provisioning_required(ctx)

    assert result == "declined"


async def test_present_provisioning_required_cancel_returns_cancelled():
    """User chose 'cancel' on the prompt → propagate that to the caller."""
    fake = _fake_settings()
    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=SimpleNamespace(action="cancel", data=None))

    with patch("nextcloud_mcp_server.auth.elicitation.get_settings", return_value=fake):
        result = await present_provisioning_required(ctx)

    assert result == "cancelled"
