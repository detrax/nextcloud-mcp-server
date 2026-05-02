"""Token utility functions for extracting user identity from MCP access tokens.

Extracted from server/oauth_tools.py to break circular import dependencies
between server/ and auth/ layers.
"""

import logging
import time
from typing import Any

import jwt
from jwt import PyJWKSet
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import Context
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from ..http import nextcloud_httpx_client

logger = logging.getLogger(__name__)


# OIDC discovery + JWKS caches keyed by URL → (expires_at, data). Mirrors the
# pattern in oauth_routes._get_cached_discovery so that ID-token verification
# during the OAuth callback doesn't make two extra round-trips per login (PR
# #758 finding 4). 5-minute TTL matches oauth_routes.
_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_OIDC_CACHE_TTL = 300


class IdTokenVerificationError(Exception):
    """Raised when an OIDC ID token fails signature or claim verification."""


async def _get_cached(
    cache: dict[str, tuple[float, dict[str, Any]]], url: str
) -> dict[str, Any]:
    """Return cached JSON response for *url* or fetch + cache on miss/expiry."""
    now = time.time()
    entry = cache.get(url)
    if entry is not None:
        expires_at, data = entry
        if now < expires_at:
            return data
    async with nextcloud_httpx_client() as http_client:
        response = await http_client.get(url)
        response.raise_for_status()
        data = response.json()
    cache[url] = (now + _OIDC_CACHE_TTL, data)
    return data


async def get_oidc_discovery(discovery_url: str) -> dict[str, Any]:
    """Return the cached OIDC discovery document for *discovery_url*.

    Shares the 5-minute discovery cache used by `verify_id_token`, so a
    callback that does discovery → token-exchange → ID-token verification
    reuses one HTTP round-trip instead of three. Public alias for `_get_cached`
    against `_discovery_cache` (PR #758 nits 5 & 6).
    """
    return await _get_cached(_discovery_cache, discovery_url)


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
        discovery = await _get_cached(_discovery_cache, discovery_url)

        issuer = discovery.get("issuer")
        jwks_uri = discovery.get("jwks_uri")
        if not issuer or not jwks_uri:
            raise IdTokenVerificationError(
                "OIDC discovery response missing issuer or jwks_uri"
            )

        jwks_data = await _get_cached(_jwks_cache, jwks_uri)
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
        except KeyError:
            # Cache miss may indicate IdP key rotation. Refresh JWKS once
            # before giving up, per OIDC core §10.1.1: when an unrecognised
            # `kid` arrives the relying party should refetch the JWKS rather
            # than waiting for cache TTL to elapse.
            _jwks_cache.pop(jwks_uri, None)
            try:
                jwks_data = await _get_cached(_jwks_cache, jwks_uri)
                jwks = PyJWKSet.from_dict(jwks_data)
                signing_key = jwks[kid]
            except KeyError as e:
                raise IdTokenVerificationError(
                    f"No JWKS key matches ID token kid {kid!r}"
                ) from e
            except Exception as e:
                raise IdTokenVerificationError(
                    f"Failed to refresh JWKS after kid miss: {e}"
                ) from e

        # PyJWT verifies the JWT with the algorithm declared in its header,
        # cross-checked against this allowlist (so an attacker can't downgrade
        # to ``none`` or HMAC). The allowlist covers the OIDC algorithms
        # most cloud IdPs ship by default:
        #   - RS256: Nextcloud user_oidc, Keycloak default, Auth0, Google.
        #   - PS256: Azure AD on newer keys.
        #   - ES256: some Keycloak realms, AWS Cognito user pools.
        # Symmetric (HSxxx) and ``none`` are intentionally absent.
        payload: dict[str, Any] = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256", "PS256", "ES256"],
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


async def extract_user_id_from_token(_ctx: Context) -> str:
    """Extract user_id from the verified MCP access token.

    Reads the `sub` claim from `AccessToken.resource`, which is populated by
    `UnifiedTokenVerifier` after JWT signature verification (or token
    introspection for opaque tokens). We never re-decode the raw token here:
    the verifier has already validated the signature and extracted the
    identity claim.

    Args:
        _ctx: MCP context with access token. Intentionally unused — kept on
            the public signature so call sites can pass the FastMCP Context
            they already hold without rewriting; identity is read from the
            verifier-populated AccessToken via get_access_token().

    Returns:
        user_id from the verified token, or ``"default_user"`` when no
        access token is present at all (BasicAuth mode — there is no
        OAuth identity to extract, so the sentinel is returned and the
        caller's BasicAuth branch handles it).

    Raises:
        McpError: An access token was present but had no ``sub`` claim
            (``access_token.resource`` empty). Failing closed prevents a
            malformed IdP token from silently bucketing every request
            under the ``"default_user"`` key in SQLite, which would risk
            cross-tenant data exposure (PR #758 follow-up review).
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
        raise McpError(
            ErrorData(
                code=-1,
                message="Cannot determine user identity from access token",
            )
        )

    return user_id
