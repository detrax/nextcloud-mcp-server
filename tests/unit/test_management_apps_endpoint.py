"""
Unit tests for the Management API /api/v1/apps endpoint.

These tests cover the regression where /api/v1/apps proxied to
/ocs/v1.php/cloud/apps — an admin-only and @PasswordConfirmationRequired
endpoint that always 401s for OAuth bearer tokens. The handler now uses
/ocs/v2.php/cloud/capabilities, which accepts the bearer and returns an
authenticated capability map keyed by app id.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api.webhooks import get_installed_apps

pytestmark = pytest.mark.unit


def _build_test_app() -> Starlette:
    app = Starlette(routes=[Route("/api/v1/apps", get_installed_apps, methods=["GET"])])
    app.state.oauth_context = {"config": {"nextcloud_host": "http://nc.test"}}
    return app


def _patch_token_validation(mocker, user_id: str = "admin") -> None:
    mocker.patch(
        "nextcloud_mcp_server.api.webhooks.validate_token_and_get_user",
        new=AsyncMock(return_value=(user_id, {"sub": user_id})),
    )


def _patch_outbound_client(mocker, response: MagicMock) -> AsyncMock:
    """Patch the outbound httpx client; return the mocked .get() AsyncMock."""
    mock_get = AsyncMock(return_value=response)
    mock_client = AsyncMock()
    mock_client.get = mock_get
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    # Must return False (or None) so async-with does NOT suppress exceptions
    # raised inside the block — default AsyncMock() resolves to a truthy
    # MagicMock and would silently swallow ValueError.
    mock_client.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_client)
    mocker.patch(
        "nextcloud_mcp_server.api.webhooks.nextcloud_httpx_client", mock_factory
    )
    # Return both so tests can assert on factory kwargs and call args
    mock_factory.attach_mock(mock_get, "get")
    return mock_factory


def _capabilities_response(capabilities: dict[str, dict]) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"ocs": {"data": {"capabilities": capabilities}}}
    return response


async def test_returns_sorted_capability_keys(mocker):
    """Happy path: handler hits OCS v2 capabilities and returns sorted app keys."""
    _patch_token_validation(mocker)
    response = _capabilities_response(
        {
            "notes": {"api_version": "1.4"},
            "files": {"bigfilechunking": True},
            "core": {"webdav-root": "remote.php/webdav"},
            "tables": {"foo": "bar"},
        }
    )
    factory = _patch_outbound_client(mocker, response)

    app = _build_test_app()
    client = TestClient(app)
    http_response = client.get(
        "/api/v1/apps", headers={"Authorization": "Bearer test-token"}
    )

    assert http_response.status_code == 200
    assert http_response.json() == {"apps": ["core", "files", "notes", "tables"]}

    # Regression check: outbound URL is OCS v2 capabilities, NOT v1 cloud/apps.
    # /ocs/v1.php/cloud/apps was admin-only + @PasswordConfirmationRequired
    # which always 401s for OAuth bearer tokens — that was the bug.
    factory.get.assert_awaited_once()
    called_path = factory.get.call_args.args[0]
    assert called_path == "/ocs/v2.php/cloud/capabilities"
    assert "/cloud/apps" not in called_path

    # Bearer token forwarded so authenticated capabilities are returned
    factory_call_kwargs = factory.call_args.kwargs
    assert factory_call_kwargs["headers"] == {"Authorization": "Bearer test-token"}


async def test_empty_capabilities_returns_empty_list(mocker):
    """Empty capabilities map → empty apps list, not an error."""
    _patch_token_validation(mocker)
    response = _capabilities_response({})
    _patch_outbound_client(mocker, response)

    app = _build_test_app()
    client = TestClient(app)
    http_response = client.get(
        "/api/v1/apps", headers={"Authorization": "Bearer test-token"}
    )

    assert http_response.status_code == 200
    assert http_response.json() == {"apps": []}


async def test_ocs_error_returns_500_with_sanitized_message(mocker):
    """Non-200 from Nextcloud OCS surfaces as a generic 500 to the caller."""
    _patch_token_validation(mocker)
    error_response = MagicMock()
    error_response.status_code = 503
    _patch_outbound_client(mocker, error_response)

    app = _build_test_app()
    client = TestClient(app)
    http_response = client.get(
        "/api/v1/apps", headers={"Authorization": "Bearer test-token"}
    )

    assert http_response.status_code == 500
    body = http_response.json()
    assert body["error"] == "Internal error"
    # _sanitize_error_for_client returns a generic message; specific status
    # code must NOT leak to the client
    assert "503" not in body["message"]


async def test_missing_nextcloud_host_returns_500(mocker):
    """Misconfigured oauth_context (no nextcloud_host) surfaces as 500."""
    _patch_token_validation(mocker)
    response = _capabilities_response({})
    _patch_outbound_client(mocker, response)

    app = Starlette(routes=[Route("/api/v1/apps", get_installed_apps, methods=["GET"])])
    app.state.oauth_context = {"config": {}}  # no nextcloud_host

    client = TestClient(app)
    http_response = client.get(
        "/api/v1/apps", headers={"Authorization": "Bearer test-token"}
    )

    assert http_response.status_code == 500


async def test_missing_authorization_returns_500(mocker):
    """When token validation passes but Authorization header is absent, the
    handler can't construct the outbound bearer header — returns 500 with a
    sanitized message."""
    _patch_token_validation(mocker)
    response = _capabilities_response({})
    _patch_outbound_client(mocker, response)

    app = _build_test_app()
    client = TestClient(app)
    # No Authorization header
    http_response = client.get("/api/v1/apps")

    assert http_response.status_code == 500
