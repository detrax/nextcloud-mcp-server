"""Unit tests for the MCP elicitation helpers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.auth.elicitation import (
    ASTROLABE_SETTINGS_PATH,
    _astrolabe_settings_url,
    present_provisioning_required,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clear_nc_env(monkeypatch):
    """Strip the NC URL env vars by default; tests opt back in."""
    monkeypatch.delenv("NEXTCLOUD_PUBLIC_ISSUER_URL", raising=False)
    monkeypatch.delenv("NEXTCLOUD_HOST", raising=False)


def test_astrolabe_settings_url_prefers_public_issuer(monkeypatch):
    """Public issuer wins over host so the link is browser-reachable in Docker."""
    monkeypatch.setenv("NEXTCLOUD_PUBLIC_ISSUER_URL", "https://nc.example.com")
    monkeypatch.setenv("NEXTCLOUD_HOST", "http://internal:8080")

    assert (
        _astrolabe_settings_url() == f"https://nc.example.com{ASTROLABE_SETTINGS_PATH}"
    )


def test_astrolabe_settings_url_strips_trailing_slash_from_public_issuer(monkeypatch):
    """Trailing slash on NEXTCLOUD_PUBLIC_ISSUER_URL is normalized."""
    monkeypatch.setenv("NEXTCLOUD_PUBLIC_ISSUER_URL", "https://nc.example.com/")

    assert (
        _astrolabe_settings_url() == f"https://nc.example.com{ASTROLABE_SETTINGS_PATH}"
    )


def test_astrolabe_settings_url_falls_back_to_host(monkeypatch):
    """When only NEXTCLOUD_HOST is set, use it (and strip a trailing slash)."""
    monkeypatch.setenv("NEXTCLOUD_HOST", "https://only-host.example.com/")

    assert (
        _astrolabe_settings_url()
        == f"https://only-host.example.com{ASTROLABE_SETTINGS_PATH}"
    )


def test_astrolabe_settings_url_returns_none_when_unset():
    """No NC URL configured → None (caller renders the tool-only message)."""
    assert _astrolabe_settings_url() is None


async def test_present_provisioning_required_elicits_with_url(monkeypatch):
    """When NC URL is set and the client supports elicitation, send the URL."""
    monkeypatch.setenv("NEXTCLOUD_PUBLIC_ISSUER_URL", "https://nc.example.com")

    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=SimpleNamespace(action="accept", data=None))

    result = await present_provisioning_required(ctx)

    assert result == "accepted"
    ctx.elicit.assert_awaited_once()
    sent_message = ctx.elicit.await_args.kwargs["message"]
    assert "https://nc.example.com/index.php/apps/astrolabe/settings" in sent_message
    assert "nc_auth_provision_access" in sent_message


async def test_present_provisioning_required_without_url(monkeypatch):
    """When neither NC URL is set, fall back to the tool-only message."""
    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=SimpleNamespace(action="accept", data=None))

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

    result = await present_provisioning_required(ctx)  # type: ignore[arg-type]

    assert result == "message_only"


async def test_present_provisioning_required_handles_not_implemented():
    """SDK clients that don't support elicitation raise NotImplementedError."""
    ctx = MagicMock()
    ctx.elicit = AsyncMock(side_effect=NotImplementedError("client lacks elicit"))

    result = await present_provisioning_required(ctx)

    assert result == "message_only"


async def test_present_provisioning_required_handles_unexpected_error():
    """Any other elicit failure (e.g. transport) is fail-open to message_only."""
    ctx = MagicMock()
    ctx.elicit = AsyncMock(side_effect=RuntimeError("transport boom"))

    result = await present_provisioning_required(ctx)

    assert result == "message_only"


async def test_present_provisioning_required_decline_returns_declined():
    """User chose 'decline' on the prompt → propagate that to the caller."""
    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=SimpleNamespace(action="decline", data=None))

    result = await present_provisioning_required(ctx)

    assert result == "declined"


async def test_present_provisioning_required_cancel_returns_cancelled():
    """User chose 'cancel' on the prompt → propagate that to the caller."""
    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=SimpleNamespace(action="cancel", data=None))

    result = await present_provisioning_required(ctx)

    assert result == "cancelled"
