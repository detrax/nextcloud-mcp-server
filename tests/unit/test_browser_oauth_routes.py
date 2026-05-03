"""Unit tests for ``browser_oauth_routes`` helpers.

Pins the round-6 review fix that ``_should_use_secure_cookies`` must not
trust ``bool(settings.cookie_secure)`` — Dynaconf normally coerces but
tests / direct ``settings.set`` calls can leave the raw string in place,
and ``bool("false")`` is ``True``.
"""

import pytest

from nextcloud_mcp_server.auth import browser_oauth_routes

pytestmark = pytest.mark.unit


def _fake_settings(*, cookie_secure, mcp_server_url=""):
    return type(
        "S",
        (),
        {
            "cookie_secure": cookie_secure,
            "nextcloud_mcp_server_url": mcp_server_url,
        },
    )()


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        (False, False),
        ("true", True),
        ("false", False),
        ("True", True),
        ("FALSE", False),
        ("0", False),
        ("1", True),
        ("no", False),
        ("yes", True),
        ("off", False),
        ("on", True),
        ("", False),
    ],
)
def test_should_use_secure_cookies_string_coercion(monkeypatch, value, expected):
    monkeypatch.setattr(
        browser_oauth_routes,
        "get_settings",
        lambda: _fake_settings(cookie_secure=value),
    )
    assert browser_oauth_routes._should_use_secure_cookies() is expected


def test_should_use_secure_cookies_falls_back_to_https_scheme(monkeypatch):
    monkeypatch.setattr(
        browser_oauth_routes,
        "get_settings",
        lambda: _fake_settings(
            cookie_secure=None, mcp_server_url="https://mcp.example.com"
        ),
    )
    assert browser_oauth_routes._should_use_secure_cookies() is True


def test_should_use_secure_cookies_falls_back_to_http_scheme(monkeypatch):
    monkeypatch.setattr(
        browser_oauth_routes,
        "get_settings",
        lambda: _fake_settings(
            cookie_secure=None, mcp_server_url="http://localhost:8000"
        ),
    )
    assert browser_oauth_routes._should_use_secure_cookies() is False
