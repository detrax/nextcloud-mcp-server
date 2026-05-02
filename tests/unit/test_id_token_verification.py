"""Unit tests for OIDC ID token verification (issue #626 finding 1).

The OAuth callback handlers used to call
`jwt.decode(id_token, options={"verify_signature": False})` and trust the
result. They now go through `verify_id_token`, which checks signature
against JWKS and validates issuer / audience / exp / nonce per OIDC core
spec §3.1.3.7.
"""

import json
import time
from base64 import urlsafe_b64encode
from unittest.mock import patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from nextcloud_mcp_server.auth.token_utils import (
    IdTokenVerificationError,
    verify_id_token,
)

pytestmark = pytest.mark.unit


# Generated once per process — RSA keypair generation is slow.
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.TraditionalOpenSSL,
    encryption_algorithm=serialization.NoEncryption(),
)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_PRIVATE_PEM = _OTHER_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.TraditionalOpenSSL,
    encryption_algorithm=serialization.NoEncryption(),
)

ISSUER = "https://idp.example.com"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URI = f"{ISSUER}/jwks"


def _b64u_uint(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _build_jwks() -> dict:
    pub = _KEY.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "kid": "test-key-1",
                "alg": "RS256",
                "n": _b64u_uint(pub.n),
                "e": _b64u_uint(pub.e),
            }
        ]
    }


def _sign(
    claims: dict, *, kid: str = "test-key-1", key_pem: bytes = _PRIVATE_PEM
) -> str:
    return jwt.encode(claims, key_pem, algorithm="RS256", headers={"kid": kid})


def _idp_handler(request: httpx.Request) -> httpx.Response:
    if str(request.url) == DISCOVERY_URL:
        return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URI})
    if str(request.url) == JWKS_URI:
        return httpx.Response(
            200,
            content=json.dumps(_build_jwks()).encode(),
            headers={"content-type": "application/json"},
        )
    return httpx.Response(404)


@pytest.fixture
def mock_idp():
    """Patch nextcloud_httpx_client used inside token_utils.verify_id_token."""
    transport = httpx.MockTransport(_idp_handler)

    def fake_client(**kwargs):
        kwargs["transport"] = transport
        return httpx.AsyncClient(**kwargs)

    with patch(
        "nextcloud_mcp_server.auth.token_utils.nextcloud_httpx_client",
        side_effect=fake_client,
    ):
        yield


async def test_verify_id_token_accepts_valid_token(mock_idp):
    now = int(time.time())
    token = _sign(
        {
            "iss": ISSUER,
            "aud": "test-client",
            "sub": "alice",
            "iat": now,
            "exp": now + 60,
        }
    )
    payload = await verify_id_token(
        token, discovery_url=DISCOVERY_URL, expected_audience="test-client"
    )
    assert payload["sub"] == "alice"


async def test_verify_id_token_rejects_wrong_audience(mock_idp):
    now = int(time.time())
    token = _sign(
        {
            "iss": ISSUER,
            "aud": "other-client",
            "sub": "alice",
            "iat": now,
            "exp": now + 60,
        }
    )
    with pytest.raises(IdTokenVerificationError):
        await verify_id_token(
            token, discovery_url=DISCOVERY_URL, expected_audience="test-client"
        )


async def test_verify_id_token_rejects_expired_token(mock_idp):
    now = int(time.time())
    token = _sign(
        {
            "iss": ISSUER,
            "aud": "test-client",
            "sub": "alice",
            "iat": now - 120,
            "exp": now - 60,
        }
    )
    with pytest.raises(IdTokenVerificationError):
        await verify_id_token(
            token, discovery_url=DISCOVERY_URL, expected_audience="test-client"
        )


async def test_verify_id_token_rejects_wrong_issuer(mock_idp):
    now = int(time.time())
    token = _sign(
        {
            "iss": "https://evil.example.com",
            "aud": "test-client",
            "sub": "alice",
            "iat": now,
            "exp": now + 60,
        }
    )
    with pytest.raises(IdTokenVerificationError):
        await verify_id_token(
            token, discovery_url=DISCOVERY_URL, expected_audience="test-client"
        )


async def test_verify_id_token_rejects_wrong_signature(mock_idp):
    """Token signed with a different key but matching kid header must fail."""
    now = int(time.time())
    forged = _sign(
        {
            "iss": ISSUER,
            "aud": "test-client",
            "sub": "alice",
            "iat": now,
            "exp": now + 60,
        },
        key_pem=_OTHER_PRIVATE_PEM,
    )
    with pytest.raises(IdTokenVerificationError):
        await verify_id_token(
            forged, discovery_url=DISCOVERY_URL, expected_audience="test-client"
        )


async def test_verify_id_token_rejects_unknown_kid(mock_idp):
    now = int(time.time())
    token = _sign(
        {
            "iss": ISSUER,
            "aud": "test-client",
            "sub": "alice",
            "iat": now,
            "exp": now + 60,
        },
        kid="not-in-jwks",
    )
    with pytest.raises(IdTokenVerificationError, match="No JWKS key matches"):
        await verify_id_token(
            token, discovery_url=DISCOVERY_URL, expected_audience="test-client"
        )


async def test_verify_id_token_nonce_mismatch_rejected(mock_idp):
    now = int(time.time())
    token = _sign(
        {
            "iss": ISSUER,
            "aud": "test-client",
            "sub": "alice",
            "iat": now,
            "exp": now + 60,
            "nonce": "actual",
        }
    )
    with pytest.raises(IdTokenVerificationError, match="nonce"):
        await verify_id_token(
            token,
            discovery_url=DISCOVERY_URL,
            expected_audience="test-client",
            expected_nonce="expected",
        )


async def test_verify_id_token_missing_token_rejected():
    with pytest.raises(IdTokenVerificationError, match="missing"):
        await verify_id_token(
            "", discovery_url=DISCOVERY_URL, expected_audience="test-client"
        )
