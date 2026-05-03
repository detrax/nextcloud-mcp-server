"""Webhook management API endpoints.

Provides REST API endpoints for managing webhook registrations with Nextcloud.
These endpoints are used by the Nextcloud PHP app (Astrolabe) to:
- List installed Nextcloud apps
- Create, list, and delete webhook registrations

All endpoints require OAuth bearer token authentication via UnifiedTokenVerifier.
"""

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from nextcloud_mcp_server.api.management import (
    _sanitize_error_for_client,
    extract_bearer_token,
    validate_token_and_get_user,
)
from nextcloud_mcp_server.auth.webhook_routes import webhook_auth_pair
from nextcloud_mcp_server.client.webhooks import WebhooksClient

from ..http import nextcloud_httpx_client

logger = logging.getLogger(__name__)


async def get_installed_apps(request: Request) -> JSONResponse:
    """GET /api/v1/apps - Get list of installed Nextcloud apps.

    Returns a list of installed app IDs for filtering webhook presets.

    Requires OAuth bearer token for authentication.
    """
    try:
        # Validate OAuth token and extract user
        user_id, validated = await validate_token_and_get_user(request)
    except Exception as e:
        logger.warning("Unauthorized access to /api/v1/apps: %s", e)
        return JSONResponse(
            {
                "error": "Unauthorized",
                "message": _sanitize_error_for_client(e, "get_installed_apps"),
            },
            status_code=401,
        )

    try:
        # Get Bearer token from request — forwarded to Nextcloud so the
        # capabilities response includes per-user / per-app entries (anonymous
        # capabilities omits notes, tables, forms etc.).
        token = extract_bearer_token(request)
        if not token:
            raise ValueError("Missing Authorization header")

        # Get Nextcloud host from OAuth context
        oauth_ctx = request.app.state.oauth_context
        nextcloud_host = oauth_ctx.get("config", {}).get("nextcloud_host", "")

        if not nextcloud_host:
            raise ValueError("Nextcloud host not configured")

        # Use OCS v2 capabilities. The legacy /ocs/v1.php/cloud/apps endpoint is
        # admin-only AND @PasswordConfirmationRequired — neither is satisfiable
        # via an OAuth bearer token, so it always 401s. Capabilities has no such
        # gates and returns a map keyed by app id for every enabled app that
        # implements OCSCapabilities, which is sufficient to populate the
        # webhook presets UI.
        async with nextcloud_httpx_client(
            base_url=nextcloud_host,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        ) as client:
            response = await client.get(
                "/ocs/v2.php/cloud/capabilities",
                params={"format": "json"},
                headers={"OCS-APIRequest": "true", "Accept": "application/json"},
            )

            if response.status_code != 200:
                raise ValueError(f"OCS API returned status {response.status_code}")

            data = response.json()
            capabilities = data.get("ocs", {}).get("data", {}).get("capabilities", {})
            apps = sorted(capabilities.keys())

            return JSONResponse({"apps": apps})

    except Exception as e:
        logger.error("Error getting installed apps for user %s: %s", user_id, e)
        return JSONResponse(
            {
                "error": "Internal error",
                "message": _sanitize_error_for_client(e, "get_installed_apps"),
            },
            status_code=500,
        )


async def list_webhooks(request: Request) -> JSONResponse:
    """GET /api/v1/webhooks - List all registered webhooks.

    Returns list of webhook registrations for the authenticated user.

    Requires OAuth bearer token for authentication.
    """
    try:
        # Validate OAuth token and extract user
        user_id, validated = await validate_token_and_get_user(request)
    except Exception as e:
        logger.warning("Unauthorized access to /api/v1/webhooks: %s", e)
        return JSONResponse(
            {
                "error": "Unauthorized",
                "message": _sanitize_error_for_client(e, "list_webhooks"),
            },
            status_code=401,
        )

    try:
        # Get Bearer token from request
        token = extract_bearer_token(request)
        if not token:
            raise ValueError("Missing Authorization header")

        # Get Nextcloud host from OAuth context
        oauth_ctx = request.app.state.oauth_context
        nextcloud_host = oauth_ctx.get("config", {}).get("nextcloud_host", "")

        if not nextcloud_host:
            raise ValueError("Nextcloud host not configured")

        # Create authenticated HTTP client
        async with nextcloud_httpx_client(
            base_url=nextcloud_host,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        ) as client:
            # Use WebhooksClient to list webhooks
            webhooks_client = WebhooksClient(client, user_id)
            webhooks = await webhooks_client.list_webhooks()

            return JSONResponse({"webhooks": webhooks})

    except Exception as e:
        logger.error("Error listing webhooks for user %s: %s", user_id, e)
        return JSONResponse(
            {
                "error": "Internal error",
                "message": _sanitize_error_for_client(e, "list_webhooks"),
            },
            status_code=500,
        )


async def create_webhook(request: Request) -> JSONResponse:
    """POST /api/v1/webhooks - Create a new webhook registration.

    Request body:
    {
        "event": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
        "uri": "http://mcp:8000/webhooks/nextcloud",
        "eventFilter": {"event.node.path": "/^\\/.*\\/files\\/Notes\\//"}
    }

    Returns the created webhook data including the webhook ID.

    Requires OAuth bearer token for authentication.
    """
    try:
        # Validate OAuth token and extract user
        user_id, validated = await validate_token_and_get_user(request)
    except Exception as e:
        logger.warning("Unauthorized access to /api/v1/webhooks: %s", e)
        return JSONResponse(
            {
                "error": "Unauthorized",
                "message": _sanitize_error_for_client(e, "create_webhook"),
            },
            status_code=401,
        )

    try:
        # Parse request body
        body = await request.json()
        event = body.get("event")
        uri = body.get("uri")
        # Accept both camelCase (eventFilter) and snake_case (event_filter)
        event_filter = body.get("eventFilter") or body.get("event_filter")

        if not event or not uri:
            return JSONResponse(
                {
                    "error": "Bad request",
                    "message": "Missing required fields: event, uri",
                },
                status_code=400,
            )

        # Get Bearer token from request
        token = extract_bearer_token(request)
        if not token:
            raise ValueError("Missing Authorization header")

        # Get Nextcloud host from OAuth context
        oauth_ctx = request.app.state.oauth_context
        nextcloud_host = oauth_ctx.get("config", {}).get("nextcloud_host", "")

        if not nextcloud_host:
            raise ValueError("Nextcloud host not configured")

        # Create authenticated HTTP client
        async with nextcloud_httpx_client(
            base_url=nextcloud_host,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        ) as client:
            # Use WebhooksClient to create webhook. Inject auth headers when
            # WEBHOOK_SECRET is configured so deliveries are authenticated.
            webhooks_client = WebhooksClient(client, user_id)
            auth_method, auth_data = webhook_auth_pair()
            webhook_data = await webhooks_client.create_webhook(
                event=event,
                uri=uri,
                event_filter=event_filter,
                auth_method=auth_method,
                auth_data=auth_data,
            )

            return JSONResponse({"webhook": webhook_data})

    except Exception as e:
        logger.error("Error creating webhook for user %s: %s", user_id, e)
        return JSONResponse(
            {
                "error": "Internal error",
                "message": _sanitize_error_for_client(e, "create_webhook"),
            },
            status_code=500,
        )


async def delete_webhook(request: Request) -> JSONResponse:
    """DELETE /api/v1/webhooks/{webhook_id} - Delete a webhook registration.

    Returns success/failure status.

    Requires OAuth bearer token for authentication.
    """
    try:
        # Validate OAuth token and extract user
        user_id, validated = await validate_token_and_get_user(request)
    except Exception as e:
        logger.warning("Unauthorized access to /api/v1/webhooks: %s", e)
        return JSONResponse(
            {
                "error": "Unauthorized",
                "message": _sanitize_error_for_client(e, "delete_webhook"),
            },
            status_code=401,
        )

    try:
        # Get webhook_id from path parameter
        webhook_id = request.path_params.get("webhook_id")
        if not webhook_id:
            return JSONResponse(
                {"error": "Bad request", "message": "Missing webhook_id"},
                status_code=400,
            )

        try:
            webhook_id = int(webhook_id)
        except ValueError:
            return JSONResponse(
                {"error": "Bad request", "message": "Invalid webhook_id"},
                status_code=400,
            )

        # Get Bearer token from request
        token = extract_bearer_token(request)
        if not token:
            raise ValueError("Missing Authorization header")

        # Get Nextcloud host from OAuth context
        oauth_ctx = request.app.state.oauth_context
        nextcloud_host = oauth_ctx.get("config", {}).get("nextcloud_host", "")

        if not nextcloud_host:
            raise ValueError("Nextcloud host not configured")

        # Create authenticated HTTP client
        async with nextcloud_httpx_client(
            base_url=nextcloud_host,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        ) as client:
            # Use WebhooksClient to delete webhook
            webhooks_client = WebhooksClient(client, user_id)
            await webhooks_client.delete_webhook(webhook_id=webhook_id)

            return JSONResponse({"success": True, "message": "Webhook deleted"})

    except Exception as e:
        logger.error("Error deleting webhook for user %s: %s", user_id, e)
        return JSONResponse(
            {
                "error": "Internal error",
                "message": _sanitize_error_for_client(e, "delete_webhook"),
            },
            status_code=500,
        )
