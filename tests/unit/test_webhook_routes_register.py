"""Unit tests for ``_register_preset_webhooks``.

The helper threads ``webhook_auth_pair()`` into each ``create_webhook``
call, so it is the integration point between the secret-resolution logic
and the OCS client. These tests verify the wiring without standing up a
full Starlette app.
"""

import pytest

from nextcloud_mcp_server.auth import webhook_routes
from nextcloud_mcp_server.auth.webhook_routes import _register_preset_webhooks
from nextcloud_mcp_server.client.webhooks import WebhooksClient
from nextcloud_mcp_server.config import Settings
from nextcloud_mcp_server.server.webhook_presets import get_preset

pytestmark = pytest.mark.unit


def _patch_secret(monkeypatch, secret: str | None) -> None:
    monkeypatch.setattr(
        webhook_routes,
        "get_settings",
        lambda: Settings(webhook_secret=secret),
    )


def _make_webhooks_client(mocker, ids: list[int]):
    """Mock WebhooksClient.create_webhook to return one fake webhook per id."""
    client = mocker.AsyncMock(spec=WebhooksClient)
    client.create_webhook.side_effect = [{"id": i} for i in ids]
    return client


async def test_register_threads_bearer_auth_when_secret_set(monkeypatch, mocker):
    _patch_secret(monkeypatch, "supersecret")
    preset = get_preset("notes_sync")
    assert preset is not None
    client = _make_webhooks_client(mocker, ids=[101, 102, 103])

    registered = await _register_preset_webhooks(
        client, preset, "https://mcp.example.com/webhooks/nextcloud"
    )

    assert registered == [101, 102, 103]
    assert client.create_webhook.await_count == len(preset["events"])

    expected_auth = {"Authorization": "Bearer supersecret"}
    for call, event_config in zip(
        client.create_webhook.await_args_list, preset["events"]
    ):
        kwargs = call.kwargs
        assert kwargs["event"] == event_config["event"]
        assert kwargs["uri"] == "https://mcp.example.com/webhooks/nextcloud"
        assert kwargs["auth_method"] == "header"
        assert kwargs["auth_data"] == expected_auth
        # notes_sync uses path filters; ensure they round-trip through the helper
        assert kwargs["event_filter"] == event_config["filter"]


async def test_register_uses_none_auth_when_secret_unset(monkeypatch, mocker):
    _patch_secret(monkeypatch, None)
    preset = get_preset("notes_sync")
    assert preset is not None
    client = _make_webhooks_client(mocker, ids=[1, 2, 3])

    await _register_preset_webhooks(
        client, preset, "https://mcp.example.com/webhooks/nextcloud"
    )

    for call in client.create_webhook.await_args_list:
        assert call.kwargs["auth_method"] == "none"
        assert call.kwargs["auth_data"] is None


async def test_register_returns_ids_in_call_order(monkeypatch, mocker):
    _patch_secret(monkeypatch, None)
    preset = get_preset("notes_sync")
    assert preset is not None
    client = _make_webhooks_client(mocker, ids=[42, 43, 44])

    ids = await _register_preset_webhooks(client, preset, "https://example.com/wh")

    assert ids == [42, 43, 44]
