"""MCP elicitation helpers for Login Flow v2.

Provides a unified way to present login URLs to users, using MCP elicitation
when the client supports it, or falling back to returning the URL in a message.
"""

import logging

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from nextcloud_mcp_server.config import get_settings

logger = logging.getLogger(__name__)

# Path of the Astrolabe Nextcloud app's settings UI. The full URL is
# reconstructed at elicitation time from settings.nextcloud_public_issuer_url
# / settings.nextcloud_host so the user gets a browser-reachable link without
# needing a separate config knob. If the Astrolabe app is not installed this
# path will 404, and the user falls back to the nc_auth_provision_access tool
# path mentioned in the same message.
ASTROLABE_SETTINGS_PATH = "/index.php/apps/astrolabe/settings"


class LoginFlowConfirmation(BaseModel):
    """Schema for Login Flow v2 confirmation elicitation."""

    acknowledged: bool = Field(
        default=False,
        description="Check this box after completing login at the provided URL",
    )


class ProvisioningRequiredConfirmation(BaseModel):
    """Schema for the 'app password not provisioned' elicitation."""

    acknowledged: bool = Field(
        default=False,
        description="Check this box after enabling Nextcloud access",
    )


def _astrolabe_settings_url() -> str | None:
    """Construct the Astrolabe settings page URL from settings.

    Prefers ``nextcloud_public_issuer_url`` (the browser-reachable public URL)
    over ``nextcloud_host`` (which may be an internal hostname in Docker
    deployments). Returns None if neither is set.
    """
    settings = get_settings()
    base = (
        settings.nextcloud_public_issuer_url or settings.nextcloud_host or ""
    ).strip()
    if not base:
        return None
    return f"{base.rstrip('/')}{ASTROLABE_SETTINGS_PATH}"


async def present_login_url(
    ctx: Context,
    login_url: str,
    message: str | None = None,
) -> str:
    """Present a login URL to the user via MCP elicitation or message.

    Tries MCP elicitation first (ctx.elicit) for interactive clients.
    Falls back to returning the URL as a plain message.

    Args:
        ctx: MCP context
        login_url: URL the user should open in their browser
        message: Optional custom message (defaults to standard Login Flow prompt)

    Returns:
        "accepted" if user acknowledged via elicitation,
        "declined" if user declined,
        "message_only" if elicitation not supported (URL returned in message)
    """
    if message is None:
        message = (
            f"Please log in to Nextcloud to grant access:\n\n"
            f"{login_url}\n\n"
            f"Open this URL in your browser, log in, and grant the requested permissions. "
            f"Then check the box below and click OK."
        )

    if not hasattr(ctx, "elicit"):
        logger.debug(
            "Elicitation not available (no elicit method), returning URL in message"
        )
        return "message_only"

    try:
        result = await ctx.elicit(
            message=message,
            schema=LoginFlowConfirmation,
        )

        if result.action == "accept":
            if hasattr(result, "data") and not result.data.acknowledged:  # type: ignore[union-attr]
                logger.warning(
                    "User accepted login flow without checking the acknowledged box — "
                    "login completion will be verified via polling"
                )
            logger.info("User acknowledged login flow completion")
            return "accepted"
        elif result.action == "decline":
            logger.info("User declined login flow")
            return "declined"
        else:
            logger.info("User cancelled login flow")
            return "cancelled"

    except NotImplementedError:
        # Elicitation not supported by this client/SDK - fall back to message
        logger.debug("Elicitation not available, returning URL in message")
        return "message_only"
    except Exception as e:
        logger.warning(
            "Elicitation failed unexpectedly (%s: %s), falling back to message",
            type(e).__name__,
            e,
        )
        return "message_only"


async def present_provisioning_required(ctx: Context) -> str:
    """Elicit a provisioning prompt when a tool is called without an app password.

    Used by the ``@require_scopes`` decorator (Login Flow v2 path) to give
    the user a clickable Astrolabe settings URL — or a fallback instruction
    to call the ``nc_auth_provision_access`` MCP tool — instead of just
    raising a plain ``ProvisioningRequiredError`` text message that an LLM
    has to translate.

    The Astrolabe settings URL is reconstructed from
    ``settings.nextcloud_public_issuer_url`` /
    ``settings.nextcloud_host``; if Astrolabe is not installed the link
    404s and the user falls back to the tool path suggested in the same
    message.

    Returns:
        Same string contract as :func:`present_login_url`:
        ``"accepted"`` / ``"declined"`` / ``"cancelled"`` / ``"message_only"``.
    """
    settings_url = _astrolabe_settings_url()

    if settings_url:
        message = (
            "Nextcloud access is not yet provisioned for this user.\n\n"
            f"Open this URL to enable it via the Astrolabe app:\n\n{settings_url}\n\n"
            "If the Astrolabe app is not installed, ask your MCP client to call "
            "the `nc_auth_provision_access` tool instead — it will return a "
            "Login Flow v2 URL you can open in your browser.\n\n"
            "Then check the box below and retry the original request."
        )
    else:
        message = (
            "Nextcloud access is not yet provisioned for this user.\n\n"
            "Ask your MCP client to call the `nc_auth_provision_access` tool — "
            "it will return a Login Flow v2 URL you can open in your browser to "
            "grant access.\n\n"
            "Then check the box below and retry the original request."
        )

    if not hasattr(ctx, "elicit"):
        logger.debug(
            "Elicitation not available on context — returning message_only "
            "(plain ProvisioningRequiredError will surface to the caller)"
        )
        return "message_only"

    try:
        result = await ctx.elicit(
            message=message,
            schema=ProvisioningRequiredConfirmation,
        )

        if result.action == "accept":
            logger.info("User acknowledged provisioning-required prompt")
            return "accepted"
        elif result.action == "decline":
            logger.info("User declined provisioning-required prompt")
            return "declined"
        else:
            logger.info("User cancelled provisioning-required prompt")
            return "cancelled"

    except NotImplementedError:
        logger.debug(
            "Elicitation not supported by client — falling back to plain error"
        )
        return "message_only"
    except Exception as e:
        logger.warning(
            "Provisioning elicitation failed unexpectedly (%s: %s), "
            "falling back to plain error",
            type(e).__name__,
            e,
        )
        return "message_only"
