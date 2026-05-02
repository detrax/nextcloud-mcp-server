"""Token utility functions for extracting user identity from MCP access tokens.

Extracted from server/oauth_tools.py to break circular import dependencies
between server/ and auth/ layers.
"""

import logging
from typing import Any

import jwt
from jwt import PyJWKSet
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import Context

from ..http import nextcloud_httpx_client

logger = logging.getLogger(__name__)


class IdTokenVerificationError(Exception):
    """Raised when an OIDC ID token fails signature or claim verification."""


async def verify_id_token(
    id_token: str,
    *,
    discovery_url: str,
    expected_audience: str,
    expected_nonce: str | None = None,
) -> dict[str, Any]:
    """Verify an OIDC ID token's signature and standard claims.

    Implements the verification steps required by OIDC core spec section
    3.1.3.7 (ID Token Validation) for the authorization-code flow:
      - Signature against JWKS (RS256)
      - Issuer matches the OP that issued the token
      - Audience contains the expected client_id
      - Token is not expired (`exp`)
      - `iat` is well-formed (PyJWT default)
      - `nonce` matches when one was included in the auth request

    Replaces the prior `jwt.decode(id_token, options={"verify_signature": False})`
    pattern (issue #626 finding 1) on the OAuth callback paths.

    Args:
        id_token: Raw ID token (JWT) string.
        discovery_url: OIDC `.well-known/openid-configuration` URL of the IdP.
        expected_audience: The MCP-server-side OAuth client_id used for this
            authorization request.
        expected_nonce: When the auth request included a nonce, the same value
            so it can be checked here. None disables the nonce check (callers
            that didn't bind a nonce in the auth request).

    Returns:
        Decoded, verified ID-token claims.

    Raises:
        IdTokenVerificationError: On any verification failure.
    """
    if not id_token:
        raise IdTokenVerificationError("ID token missing from token response")

    try:
        async with nextcloud_httpx_client() as http_client:
            discovery_response = await http_client.get(discovery_url)
            discovery_response.raise_for_status()
            discovery = discovery_response.json()

            issuer = discovery.get("issuer")
            jwks_uri = discovery.get("jwks_uri")
            if not issuer or not jwks_uri:
                raise IdTokenVerificationError(
                    "OIDC discovery response missing issuer or jwks_uri"
                )

            jwks_response = await http_client.get(jwks_uri)
            jwks_response.raise_for_status()
            jwks_data = jwks_response.json()
    except IdTokenVerificationError:
        raise
    except Exception as e:
        raise IdTokenVerificationError(
            f"Failed to fetch OIDC discovery / JWKS: {e}"
        ) from e

    try:
        jwks = PyJWKSet.from_dict(jwks_data)
        unverified_header = jwt.get_unverified_header(id_token)
        kid = unverified_header.get("kid")
        if not kid:
            raise IdTokenVerificationError("ID token header missing 'kid'")
        try:
            signing_key = jwks[kid]
        except KeyError as e:
            raise IdTokenVerificationError(
                f"No JWKS key matches ID token kid {kid!r}"
            ) from e

        payload: dict[str, Any] = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=expected_audience,
            issuer=issuer,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
                "require": ["sub", "iss", "aud", "exp", "iat"],
            },
        )
    except IdTokenVerificationError:
        raise
    except jwt.PyJWTError as e:
        raise IdTokenVerificationError(f"ID token verification failed: {e}") from e
    except Exception as e:
        raise IdTokenVerificationError(
            f"Unexpected error verifying ID token: {e}"
        ) from e

    if expected_nonce is not None and payload.get("nonce") != expected_nonce:
        raise IdTokenVerificationError("ID token nonce does not match request nonce")

    return payload


async def extract_user_id_from_token(ctx: Context) -> str:
    """Extract user_id from the verified MCP access token.

    Reads the `sub` claim from `AccessToken.resource`, which is populated by
    `UnifiedTokenVerifier` after JWT signature verification (or token
    introspection for opaque tokens). We never re-decode the raw token here:
    the verifier has already validated the signature and extracted the
    identity claim.

    Args:
        ctx: MCP context with access token (unused — kept for the public API)

    Returns:
        user_id from the verified token, or "default_user" when no token is
        present (e.g. BasicAuth mode where this should not be called).
    """
    access_token: AccessToken | None = get_access_token()

    if not access_token:
        logger.warning("No access token found via get_access_token()")
        return "default_user"

    user_id = access_token.resource
    if not user_id:
        logger.error(
            "Access token has no resource (sub) claim — verifier should have rejected it"
        )
        return "default_user"

    return user_id
