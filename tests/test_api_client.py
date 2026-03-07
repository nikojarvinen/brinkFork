"""Comprehensive tests for the Brink Home Cloud API client.

Tests cover OIDC PKCE authentication, token management, API methods,
HTML parsing, URL validation, and error handling.

This test module avoids importing through the integration's __init__.py
(which uses Python 3.12+ syntax) by pre-populating sys.modules for the
parent package, then importing const.py and brink_home_cloud.py directly.

Run standalone (without Home Assistant test deps):
    python -m pytest tests/test_api_client.py --noconftest \\
        -p no:socket -p no:homeassistant_custom_component -p no:homeassistant
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import importlib
import importlib.util
import os
import sys
import time
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
import yarl

# ---------------------------------------------------------------------------
# Polyfill: asyncio.timeout was added in Python 3.11. The production code
# uses it, so we provide a minimal backport for test runs on 3.10.
# ---------------------------------------------------------------------------
if not hasattr(asyncio, "timeout"):

    @contextlib.asynccontextmanager
    async def _timeout(delay: float | None):  # type: ignore[no-untyped-def]
        """Minimal polyfill for asyncio.timeout (no actual enforcement)."""
        yield

    asyncio.timeout = _timeout  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# Bootstrap: Load const.py and brink_home_cloud.py without triggering
# the package __init__.py (which requires Python 3.12+ and Home Assistant).
# ---------------------------------------------------------------------------
_INTEGRATION_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "custom_components", "brink_ventilation")
)


def _ensure_package(dotted: str) -> types.ModuleType:
    """Ensure a package exists in sys.modules (stub if needed)."""
    if dotted in sys.modules:
        return sys.modules[dotted]
    mod = types.ModuleType(dotted)
    mod.__path__ = []  # type: ignore[attr-defined]
    mod.__package__ = dotted
    sys.modules[dotted] = mod
    return mod


def _load_module_from_file(dotted_name: str, filepath: str) -> types.ModuleType:
    """Import a single .py file as *dotted_name* without executing parent __init__.py.

    If the module is already present in ``sys.modules`` (e.g. because conftest
    already triggered a normal import), reuse it to avoid creating a second
    copy of its classes — which would break ``isinstance`` / ``except`` checks
    across modules that imported the original copy.
    """
    if dotted_name in sys.modules:
        return sys.modules[dotted_name]
    spec = importlib.util.spec_from_file_location(dotted_name, filepath)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create spec for {filepath}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Pre-populate stubs so relative imports inside const.py / brink_home_cloud.py
# resolve to real modules without touching __init__.py.
_cc_pkg = _ensure_package("custom_components")
_bv_pkg = _ensure_package("custom_components.brink_ventilation")
_core_pkg = _ensure_package("custom_components.brink_ventilation.core")

# Load const.py first (brink_home_cloud.py imports from it).
_const_mod = _load_module_from_file(
    "custom_components.brink_ventilation.const",
    os.path.join(_INTEGRATION_DIR, "const.py"),
)

# Wire the const module into the package hierarchy so
# `patch("custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.CookieJar")`
# can traverse the dotted path correctly.
_bv_pkg.const = _const_mod  # type: ignore[attr-defined]

# Load brink_home_cloud.py
_cloud_mod = _load_module_from_file(
    "custom_components.brink_ventilation.core.brink_home_cloud",
    os.path.join(_INTEGRATION_DIR, "core", "brink_home_cloud.py"),
)

# Wire into package hierarchy for patch() resolution.
_cc_pkg.brink_ventilation = _bv_pkg  # type: ignore[attr-defined]
_bv_pkg.core = _core_pkg  # type: ignore[attr-defined]
_core_pkg.brink_home_cloud = _cloud_mod  # type: ignore[attr-defined]

# Pull the symbols we need from the loaded modules.
API_V1_URL: str = _const_mod.API_V1_URL  # type: ignore[attr-defined]
OIDC_AUTH_URL: str = _const_mod.OIDC_AUTH_URL  # type: ignore[attr-defined]
OIDC_CLIENT_ID: str = _const_mod.OIDC_CLIENT_ID  # type: ignore[attr-defined]
OIDC_REDIRECT_URI: str = _const_mod.OIDC_REDIRECT_URI  # type: ignore[attr-defined]
OIDC_SCOPE: str = _const_mod.OIDC_SCOPE  # type: ignore[attr-defined]
OIDC_TOKEN_URL: str = _const_mod.OIDC_TOKEN_URL  # type: ignore[attr-defined]
PARAM_NAME_MAP: dict[str, str] = _const_mod.PARAM_NAME_MAP  # type: ignore[attr-defined]
PARAM_VENTILATION_LEVEL: str = _const_mod.PARAM_VENTILATION_LEVEL  # type: ignore[attr-defined]
PARAM_OPERATING_MODE: str = _const_mod.PARAM_OPERATING_MODE  # type: ignore[attr-defined]
PARAM_FRESH_AIR_TEMP: str = _const_mod.PARAM_FRESH_AIR_TEMP  # type: ignore[attr-defined]

BrinkAuthError = _cloud_mod.BrinkAuthError  # type: ignore[attr-defined]
BrinkHomeCloud = _cloud_mod.BrinkHomeCloud  # type: ignore[attr-defined]
_InputFieldExtractor = _cloud_mod._InputFieldExtractor  # type: ignore[attr-defined]
_extract_parameters = _cloud_mod._extract_parameters  # type: ignore[attr-defined]
_is_trusted_url = _cloud_mod._is_trusted_url  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------
TEST_EMAIL = "user@example.com"
TEST_PASSWORD = "s3cret!"
SYSTEM_ID = 13090

# A realistic OIDC login page HTML fragment
LOGIN_PAGE_HTML = """
<html>
<body>
<form method="post">
    <input name="__RequestVerificationToken" type="hidden"
           value="CfDJ8CSRF_TOKEN_VALUE_HERE" />
    <input name="ReturnUrl" type="hidden"
           value="/idsrv/connect/authorize/callback?client_id=spa" />
    <input name="Username" type="text" />
    <input name="Password" type="password" />
    <button type="submit">Login</button>
</form>
</body>
</html>
"""

LOGIN_PAGE_HTML_NO_CSRF = """
<html>
<body>
<form method="post">
    <input name="ReturnUrl" type="hidden"
           value="/idsrv/connect/authorize/callback?client_id=spa" />
    <input name="Username" type="text" />
    <input name="Password" type="password" />
</form>
</body>
</html>
"""

# Minimal uidescription response for parsing tests
UIDESCRIPTION_RESPONSE: dict[str, Any] = {
    "root": {
        "navigationItems": [
            {
                "componentId": 13107,
                "name": "Flair 325",
                "parameterGroups": [
                    {
                        "parameters": [
                            {
                                "id": 100,
                                "name": "Lüftungsstufe",
                                "value": "2",
                                "valueId": 1001,
                                "valueState": 0,
                                "readWrite": True,
                                "controlType": "list",
                                "listItems": [
                                    {"value": "0", "text": "0"},
                                    {"value": "1", "text": "1"},
                                    {"value": "2", "text": "2"},
                                    {"value": "3", "text": "3"},
                                ],
                                "minValue": None,
                                "maxValue": None,
                                "unit": None,
                                "unitOfMeasure": None,
                                "componentId": 13107,
                            },
                            {
                                "id": 101,
                                "name": "Betriebsart",
                                "value": "1",
                                "valueId": 1002,
                                "valueState": 0,
                                "readWrite": True,
                                "controlType": "list",
                                "listItems": [],
                                "minValue": None,
                                "maxValue": None,
                                "unit": None,
                                "unitOfMeasure": None,
                                "componentId": 13107,
                            },
                        ]
                    }
                ],
                "navigationItems": [
                    {
                        "componentId": 13107,
                        "name": "Temperatures",
                        "parameterGroups": [
                            {
                                "parameters": [
                                    {
                                        "id": 200,
                                        "name": "Frischlufttemperatur",
                                        "value": "15.0",
                                        "valueId": 2001,
                                        "valueState": 0,
                                        "readWrite": False,
                                        "controlType": "value",
                                        "listItems": None,
                                        "minValue": -20,
                                        "maxValue": 50,
                                        "unit": "C",
                                        "unitOfMeasure": None,
                                        "componentId": 13107,
                                    }
                                ]
                            }
                        ],
                        "navigationItems": [],
                    }
                ],
            }
        ]
    }
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_response(
    *,
    status: int = 200,
    text: str = "",
    json_data: dict | list | None = None,
    url: str = "https://www.brink-home.com/idsrv/account/login",
    headers: dict[str, str] | None = None,
    history: list | None = None,
) -> AsyncMock:
    """Create a mock aiohttp.ClientResponse."""
    resp = AsyncMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.read = AsyncMock(return_value=b"")
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    else:
        resp.json = AsyncMock(return_value={})
    resp.url = yarl.URL(url)
    resp.headers = headers or {}
    resp.history = history or []
    resp.release = AsyncMock()
    resp.raise_for_status = MagicMock()
    return resp


def _make_mock_session() -> MagicMock:
    """Create a mock aiohttp.ClientSession."""
    session = MagicMock(spec=aiohttp.ClientSession)
    # Default every verb to return a 200
    for verb in ("get", "post", "put", "delete"):
        setattr(session, verb, AsyncMock(return_value=_make_mock_response()))
    return session


def _make_token_response(
    *,
    access_token: str = "test-access-token-abc",
    expires_in: int = 3600,
    refresh_token: str | None = None,
) -> dict[str, Any]:
    """Build a realistic OIDC token endpoint JSON response."""
    data: dict[str, Any] = {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "scope": OIDC_SCOPE,
    }
    if refresh_token:
        data["refresh_token"] = refresh_token
    return data


def _make_redirect_url(code: str, state: str) -> str:
    """Build the redirect URI that the OIDC server sends back with code + state."""
    return f"{OIDC_REDIRECT_URI}?code={code}&state={state}"


# ---------------------------------------------------------------------------
# Constructor & basic setup
# ---------------------------------------------------------------------------


class TestClientInit:
    """Tests for BrinkHomeCloud.__init__."""

    def test_client_init(self) -> None:
        """Client initializes with email, password, and session."""
        session = MagicMock(spec=aiohttp.ClientSession)
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        assert client._username == TEST_EMAIL
        assert client._password == TEST_PASSWORD
        assert client._session is session
        assert client._access_token is None
        assert client._token_expiry == 0.0
        assert client._refresh_token is None
        assert client._auth_fail_count == 0


# ---------------------------------------------------------------------------
# OIDC Authentication tests
# ---------------------------------------------------------------------------


class TestOIDCAuthentication:
    """Tests for the full OIDC PKCE login flow."""

    @pytest.mark.asyncio
    async def test_login_success(self) -> None:
        """Full OIDC flow: authorize page -> login form -> redirect with code -> token exchange."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        test_code = "auth-code-12345"
        test_state = "fake-state-abc"

        # Step 1: authorize GET -> returns login page HTML
        auth_page_resp = _make_mock_response(
            status=200,
            text=LOGIN_PAGE_HTML,
            url="https://www.brink-home.com/idsrv/account/login?returnUrl=%2F",
        )

        # Step 2: login POST -> 302 redirect with Location containing the code
        redirect_url = _make_redirect_url(test_code, test_state)
        login_redirect_resp = _make_mock_response(
            status=302,
            headers={"Location": redirect_url},
            url="https://www.brink-home.com/idsrv/account/login",
        )
        login_redirect_resp.text = AsyncMock(return_value="")

        # Step 3: token exchange POST -> access token
        token_data = _make_token_response(access_token="my-bearer-token")
        token_resp = _make_mock_response(
            status=200,
            json_data=token_data,
            url=OIDC_TOKEN_URL,
        )

        # The main session is used for token exchange (step 3).
        session.post = AsyncMock(return_value=token_resp)

        # Mock the internal OIDC session
        mock_oidc_session = MagicMock(spec=aiohttp.ClientSession)
        mock_oidc_session.__aenter__ = AsyncMock(return_value=mock_oidc_session)
        mock_oidc_session.__aexit__ = AsyncMock(return_value=False)
        mock_oidc_session.get = AsyncMock(return_value=auth_page_resp)
        mock_oidc_session.post = AsyncMock(return_value=login_redirect_resp)

        with (
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.ClientSession",
                return_value=mock_oidc_session,
            ),
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.CookieJar",
            ),
            patch.object(
                client,
                "_build_pkce_challenge",
                return_value=("verifier123", "challenge123", test_state, "nonce123"),
            ),
        ):
            await client.login()

        assert client._access_token == "my-bearer-token"
        assert client._token_expiry > 0

    @pytest.mark.asyncio
    async def test_login_invalid_credentials(self) -> None:
        """Login returns 200 with error text -> BrinkAuthError(is_credentials_error=True)."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, "wrong-password")

        auth_page_resp = _make_mock_response(
            status=200,
            text=LOGIN_PAGE_HTML,
            url="https://www.brink-home.com/idsrv/account/login",
        )

        invalid_resp = _make_mock_response(
            status=200,
            text='<html><body><div class="error">Invalid username or password</div></body></html>',
            url="https://www.brink-home.com/idsrv/account/login",
        )

        mock_oidc_session = MagicMock(spec=aiohttp.ClientSession)
        mock_oidc_session.__aenter__ = AsyncMock(return_value=mock_oidc_session)
        mock_oidc_session.__aexit__ = AsyncMock(return_value=False)
        mock_oidc_session.get = AsyncMock(return_value=auth_page_resp)
        mock_oidc_session.post = AsyncMock(return_value=invalid_resp)

        test_state = "test-state"
        with (
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.ClientSession",
                return_value=mock_oidc_session,
            ),
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.CookieJar",
            ),
            patch.object(
                client,
                "_build_pkce_challenge",
                return_value=("verifier", "challenge", test_state, "nonce"),
            ),
        ):
            with pytest.raises(BrinkAuthError) as exc_info:
                await client.login()
            assert exc_info.value.is_credentials_error is True

    @pytest.mark.asyncio
    async def test_login_network_error(self) -> None:
        """aiohttp.ClientError during OIDC authorize -> propagates as error."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        mock_oidc_session = MagicMock(spec=aiohttp.ClientSession)
        mock_oidc_session.__aenter__ = AsyncMock(return_value=mock_oidc_session)
        mock_oidc_session.__aexit__ = AsyncMock(return_value=False)
        mock_oidc_session.get = AsyncMock(side_effect=aiohttp.ClientError("Connection failed"))

        with (
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.ClientSession",
                return_value=mock_oidc_session,
            ),
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.CookieJar",
            ),
        ):
            with pytest.raises((BrinkAuthError, aiohttp.ClientError)):
                await client.login()

    @pytest.mark.asyncio
    async def test_login_csrf_missing(self) -> None:
        """No CSRF token in login page HTML -> BrinkAuthError."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        auth_page_resp = _make_mock_response(
            status=200,
            text=LOGIN_PAGE_HTML_NO_CSRF,
            url="https://www.brink-home.com/idsrv/account/login",
        )

        mock_oidc_session = MagicMock(spec=aiohttp.ClientSession)
        mock_oidc_session.__aenter__ = AsyncMock(return_value=mock_oidc_session)
        mock_oidc_session.__aexit__ = AsyncMock(return_value=False)
        mock_oidc_session.get = AsyncMock(return_value=auth_page_resp)

        with (
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.ClientSession",
                return_value=mock_oidc_session,
            ),
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.CookieJar",
            ),
        ):
            with pytest.raises(BrinkAuthError, match="CSRF token"):
                await client.login()

    @pytest.mark.asyncio
    async def test_login_unexpected_status(self) -> None:
        """Login POST returns unexpected HTTP status (500) -> BrinkAuthError."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        auth_page_resp = _make_mock_response(
            status=200,
            text=LOGIN_PAGE_HTML,
            url="https://www.brink-home.com/idsrv/account/login",
        )

        error_resp = _make_mock_response(
            status=500,
            text="Internal Server Error",
            url="https://www.brink-home.com/idsrv/account/login",
        )

        mock_oidc_session = MagicMock(spec=aiohttp.ClientSession)
        mock_oidc_session.__aenter__ = AsyncMock(return_value=mock_oidc_session)
        mock_oidc_session.__aexit__ = AsyncMock(return_value=False)
        mock_oidc_session.get = AsyncMock(return_value=auth_page_resp)
        mock_oidc_session.post = AsyncMock(return_value=error_resp)

        test_state = "test-state"
        with (
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.ClientSession",
                return_value=mock_oidc_session,
            ),
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.CookieJar",
            ),
            patch.object(
                client,
                "_build_pkce_challenge",
                return_value=("verifier", "challenge", test_state, "nonce"),
            ),
        ):
            with pytest.raises(BrinkAuthError, match="status 500"):
                await client.login()

    @pytest.mark.asyncio
    async def test_login_no_auth_code(self) -> None:
        """Redirect chain has no code parameter -> BrinkAuthError."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        auth_page_resp = _make_mock_response(
            status=200,
            text=LOGIN_PAGE_HTML,
            url="https://www.brink-home.com/idsrv/account/login",
        )

        # Redirect but with no ?code= in the Location
        redirect_resp = _make_mock_response(
            status=302,
            headers={"Location": "https://www.brink-home.com/app/?state=abc"},
            url="https://www.brink-home.com/idsrv/account/login",
        )
        redirect_resp.text = AsyncMock(return_value="")

        # The follow_redirects_for_code gets a 200 without code
        final_resp = _make_mock_response(
            status=200,
            text="<html>No code here</html>",
            url="https://www.brink-home.com/app/?state=abc",
        )

        mock_oidc_session = MagicMock(spec=aiohttp.ClientSession)
        mock_oidc_session.__aenter__ = AsyncMock(return_value=mock_oidc_session)
        mock_oidc_session.__aexit__ = AsyncMock(return_value=False)
        mock_oidc_session.get = AsyncMock(side_effect=[auth_page_resp, final_resp])
        mock_oidc_session.post = AsyncMock(return_value=redirect_resp)

        test_state = "test-state"
        with (
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.ClientSession",
                return_value=mock_oidc_session,
            ),
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.CookieJar",
            ),
            patch.object(
                client,
                "_build_pkce_challenge",
                return_value=("verifier", "challenge", test_state, "nonce"),
            ),
        ):
            with pytest.raises(BrinkAuthError, match="authorization code"):
                await client.login()

    @pytest.mark.asyncio
    async def test_login_token_exchange_failure(self) -> None:
        """Token endpoint returns non-200 -> BrinkAuthError."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        test_state = "test-state"
        test_code = "auth-code-xyz"

        auth_page_resp = _make_mock_response(
            status=200,
            text=LOGIN_PAGE_HTML,
            url="https://www.brink-home.com/idsrv/account/login",
        )

        redirect_url = _make_redirect_url(test_code, test_state)
        login_redirect_resp = _make_mock_response(
            status=302,
            headers={"Location": redirect_url},
            url="https://www.brink-home.com/idsrv/account/login",
        )
        login_redirect_resp.text = AsyncMock(return_value="")

        # Token exchange fails with 400
        token_error_resp = _make_mock_response(
            status=400,
            json_data={"error": "invalid_grant"},
            url=OIDC_TOKEN_URL,
        )

        mock_oidc_session = MagicMock(spec=aiohttp.ClientSession)
        mock_oidc_session.__aenter__ = AsyncMock(return_value=mock_oidc_session)
        mock_oidc_session.__aexit__ = AsyncMock(return_value=False)
        mock_oidc_session.get = AsyncMock(return_value=auth_page_resp)
        mock_oidc_session.post = AsyncMock(return_value=login_redirect_resp)

        # Token exchange uses the main session
        session.post = AsyncMock(return_value=token_error_resp)

        with (
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.ClientSession",
                return_value=mock_oidc_session,
            ),
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.CookieJar",
            ),
            patch.object(
                client,
                "_build_pkce_challenge",
                return_value=("verifier", "challenge", test_state, "nonce"),
            ),
        ):
            with pytest.raises(BrinkAuthError, match="token exchange failed"):
                await client.login()

    @pytest.mark.asyncio
    async def test_login_token_missing_access_token(self) -> None:
        """Token response lacks access_token field -> BrinkAuthError."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        test_state = "test-state"
        test_code = "auth-code-xyz"

        auth_page_resp = _make_mock_response(
            status=200,
            text=LOGIN_PAGE_HTML,
            url="https://www.brink-home.com/idsrv/account/login",
        )

        redirect_url = _make_redirect_url(test_code, test_state)
        login_redirect_resp = _make_mock_response(
            status=302,
            headers={"Location": redirect_url},
            url="https://www.brink-home.com/idsrv/account/login",
        )
        login_redirect_resp.text = AsyncMock(return_value="")

        # Token response missing access_token
        token_resp = _make_mock_response(
            status=200,
            json_data={"token_type": "Bearer", "expires_in": 3600},
            url=OIDC_TOKEN_URL,
        )

        mock_oidc_session = MagicMock(spec=aiohttp.ClientSession)
        mock_oidc_session.__aenter__ = AsyncMock(return_value=mock_oidc_session)
        mock_oidc_session.__aexit__ = AsyncMock(return_value=False)
        mock_oidc_session.get = AsyncMock(return_value=auth_page_resp)
        mock_oidc_session.post = AsyncMock(return_value=login_redirect_resp)

        session.post = AsyncMock(return_value=token_resp)

        with (
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.ClientSession",
                return_value=mock_oidc_session,
            ),
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.CookieJar",
            ),
            patch.object(
                client,
                "_build_pkce_challenge",
                return_value=("verifier", "challenge", test_state, "nonce"),
            ),
        ):
            with pytest.raises(BrinkAuthError, match="access token"):
                await client.login()

    @pytest.mark.asyncio
    async def test_login_authorize_non_200(self) -> None:
        """OIDC authorize endpoint returns non-200 -> BrinkAuthError."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        auth_error_resp = _make_mock_response(
            status=503,
            text="Service Unavailable",
            url="https://www.brink-home.com/idsrv/connect/authorize",
        )

        mock_oidc_session = MagicMock(spec=aiohttp.ClientSession)
        mock_oidc_session.__aenter__ = AsyncMock(return_value=mock_oidc_session)
        mock_oidc_session.__aexit__ = AsyncMock(return_value=False)
        mock_oidc_session.get = AsyncMock(return_value=auth_error_resp)

        with (
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.ClientSession",
                return_value=mock_oidc_session,
            ),
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.CookieJar",
            ),
        ):
            with pytest.raises(BrinkAuthError, match="status 503"):
                await client.login()

    @pytest.mark.asyncio
    async def test_login_untrusted_login_page_url(self) -> None:
        """Login page redirects to untrusted host -> BrinkAuthError."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        # Authorize returns 200 but the final URL is on an untrusted domain
        auth_page_resp = _make_mock_response(
            status=200,
            text=LOGIN_PAGE_HTML,
            url="https://evil.example.com/phishing/login",
        )

        mock_oidc_session = MagicMock(spec=aiohttp.ClientSession)
        mock_oidc_session.__aenter__ = AsyncMock(return_value=mock_oidc_session)
        mock_oidc_session.__aexit__ = AsyncMock(return_value=False)
        mock_oidc_session.get = AsyncMock(return_value=auth_page_resp)

        with (
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.ClientSession",
                return_value=mock_oidc_session,
            ),
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.CookieJar",
            ),
        ):
            with pytest.raises(BrinkAuthError, match="untrusted host"):
                await client.login()

    @pytest.mark.asyncio
    async def test_login_200_no_error_text(self) -> None:
        """Login returns 200 without error/invalid text -> BrinkAuthError without is_credentials_error."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        auth_page_resp = _make_mock_response(
            status=200,
            text=LOGIN_PAGE_HTML,
            url="https://www.brink-home.com/idsrv/account/login",
        )

        # 200 response with no "invalid" or "error" in body
        ok_resp = _make_mock_response(
            status=200,
            text="<html><body>Welcome, please wait...</body></html>",
            url="https://www.brink-home.com/idsrv/account/login",
        )

        mock_oidc_session = MagicMock(spec=aiohttp.ClientSession)
        mock_oidc_session.__aenter__ = AsyncMock(return_value=mock_oidc_session)
        mock_oidc_session.__aexit__ = AsyncMock(return_value=False)
        mock_oidc_session.get = AsyncMock(return_value=auth_page_resp)
        mock_oidc_session.post = AsyncMock(return_value=ok_resp)

        test_state = "test-state"
        with (
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.ClientSession",
                return_value=mock_oidc_session,
            ),
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.CookieJar",
            ),
            patch.object(
                client,
                "_build_pkce_challenge",
                return_value=("verifier", "challenge", test_state, "nonce"),
            ),
        ):
            with pytest.raises(BrinkAuthError) as exc_info:
                await client.login()
            # When body doesn't contain "invalid" or "error", is_credentials_error
            # is False (the generic 200-without-redirect path).
            assert exc_info.value.is_credentials_error is False

    @pytest.mark.asyncio
    async def test_login_with_refresh_token(self) -> None:
        """Token response includes refresh_token -> stored and verified."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        test_state = "test-state"
        test_code = "auth-code-xyz"

        auth_page_resp = _make_mock_response(
            status=200,
            text=LOGIN_PAGE_HTML,
            url="https://www.brink-home.com/idsrv/account/login",
        )

        redirect_url = _make_redirect_url(test_code, test_state)
        login_redirect_resp = _make_mock_response(
            status=302,
            headers={"Location": redirect_url},
            url="https://www.brink-home.com/idsrv/account/login",
        )
        login_redirect_resp.text = AsyncMock(return_value="")

        # Token response WITH refresh_token
        token_data = _make_token_response(
            access_token="access-1",
            refresh_token="refresh-1",
        )
        token_resp = _make_mock_response(
            status=200,
            json_data=token_data,
            url=OIDC_TOKEN_URL,
        )

        # Refresh verification call -> returns new tokens
        refresh_data = _make_token_response(
            access_token="access-2",
            refresh_token="refresh-2",
        )
        refresh_resp = _make_mock_response(
            status=200,
            json_data=refresh_data,
            url=OIDC_TOKEN_URL,
        )

        mock_oidc_session = MagicMock(spec=aiohttp.ClientSession)
        mock_oidc_session.__aenter__ = AsyncMock(return_value=mock_oidc_session)
        mock_oidc_session.__aexit__ = AsyncMock(return_value=False)
        mock_oidc_session.get = AsyncMock(return_value=auth_page_resp)
        mock_oidc_session.post = AsyncMock(return_value=login_redirect_resp)

        # Main session: first call = token exchange, second call = refresh verification
        session.post = AsyncMock(side_effect=[token_resp, refresh_resp])

        with (
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.ClientSession",
                return_value=mock_oidc_session,
            ),
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.CookieJar",
            ),
            patch.object(
                client,
                "_build_pkce_challenge",
                return_value=("verifier", "challenge", test_state, "nonce"),
            ),
        ):
            await client.login()

        # After refresh verification, we should have the refreshed token
        assert client._access_token == "access-2"
        assert client._refresh_token == "refresh-2"

    @pytest.mark.asyncio
    async def test_login_refresh_verification_fails_keeps_token(self) -> None:
        """Refresh token verification fails -> original tokens still usable."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        test_state = "test-state"
        test_code = "auth-code-xyz"

        auth_page_resp = _make_mock_response(
            status=200,
            text=LOGIN_PAGE_HTML,
            url="https://www.brink-home.com/idsrv/account/login",
        )

        redirect_url = _make_redirect_url(test_code, test_state)
        login_redirect_resp = _make_mock_response(
            status=302,
            headers={"Location": redirect_url},
            url="https://www.brink-home.com/idsrv/account/login",
        )
        login_redirect_resp.text = AsyncMock(return_value="")

        token_data = _make_token_response(
            access_token="access-orig",
            refresh_token="refresh-orig",
        )
        token_resp = _make_mock_response(
            status=200,
            json_data=token_data,
            url=OIDC_TOKEN_URL,
        )

        # Refresh verification fails
        refresh_fail_resp = _make_mock_response(
            status=400,
            json_data={"error": "invalid_grant"},
            url=OIDC_TOKEN_URL,
        )

        mock_oidc_session = MagicMock(spec=aiohttp.ClientSession)
        mock_oidc_session.__aenter__ = AsyncMock(return_value=mock_oidc_session)
        mock_oidc_session.__aexit__ = AsyncMock(return_value=False)
        mock_oidc_session.get = AsyncMock(return_value=auth_page_resp)
        mock_oidc_session.post = AsyncMock(return_value=login_redirect_resp)

        session.post = AsyncMock(side_effect=[token_resp, refresh_fail_resp])

        with (
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.ClientSession",
                return_value=mock_oidc_session,
            ),
            patch(
                "custom_components.brink_ventilation.core.brink_home_cloud.aiohttp.CookieJar",
            ),
            patch.object(
                client,
                "_build_pkce_challenge",
                return_value=("verifier", "challenge", test_state, "nonce"),
            ),
        ):
            await client.login()

        # Original access token should still be set
        assert client._access_token == "access-orig"
        # Refresh token is restored after verification failure
        assert client._refresh_token == "refresh-orig"


# ---------------------------------------------------------------------------
# PKCE generation
# ---------------------------------------------------------------------------


class TestPKCE:
    """Tests for PKCE code_verifier and code_challenge generation."""

    def test_pkce_verifier_length(self) -> None:
        """Code verifier produced by token_urlsafe(48) is 64 chars (base64url of 48 bytes)."""
        verifier = BrinkHomeCloud._generate_code_verifier()
        # token_urlsafe(48) produces a 64-character string
        assert len(verifier) == 64
        # Must be URL-safe characters only
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        assert set(verifier) <= allowed

    def test_pkce_challenge_format(self) -> None:
        """Challenge is base64url-encoded SHA256 of the verifier."""
        verifier = "test_verifier_string_for_pkce"
        challenge = BrinkHomeCloud._generate_code_challenge(verifier)

        # Manually compute expected challenge
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

        assert challenge == expected

    def test_pkce_challenge_no_padding(self) -> None:
        """PKCE challenge must not contain base64 padding characters."""
        verifier = BrinkHomeCloud._generate_code_verifier()
        challenge = BrinkHomeCloud._generate_code_challenge(verifier)
        assert "=" not in challenge

    def test_build_pkce_challenge_returns_four_values(self) -> None:
        """_build_pkce_challenge returns (verifier, challenge, state, nonce)."""
        session = MagicMock(spec=aiohttp.ClientSession)
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        verifier, challenge, state, nonce = client._build_pkce_challenge()

        assert len(verifier) == 64
        assert len(challenge) > 0
        assert len(state) > 0
        assert len(nonce) > 0
        # Verify challenge matches verifier
        assert challenge == BrinkHomeCloud._generate_code_challenge(verifier)


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------


class TestTokenManagement:
    """Tests for _ensure_token, backoff, and _bearer_headers."""

    @pytest.mark.asyncio
    async def test_ensure_token_fresh(self) -> None:
        """Token not expired -> no refresh, no login."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._access_token = "valid-token"
        client._token_expiry = time.monotonic() + 3600  # Expires in 1 hour

        with patch.object(client, "_oidc_login", new_callable=AsyncMock) as mock_login:
            await client._ensure_token()
            mock_login.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_token_expired_refreshes(self) -> None:
        """Token expired + refresh token available -> _refresh_access_token called."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._access_token = "expired-token"
        client._token_expiry = 0.0  # Already expired
        client._refresh_token = "valid-refresh-token"

        with patch.object(
            client, "_refresh_access_token", new_callable=AsyncMock
        ) as mock_refresh:
            # Make refresh succeed by setting a fresh token
            async def side_effect():
                client._access_token = "new-token"
                client._token_expiry = time.monotonic() + 3600

            mock_refresh.side_effect = side_effect
            await client._ensure_token()
            mock_refresh.assert_called_once()
            assert client._auth_fail_count == 0

    @pytest.mark.asyncio
    async def test_ensure_token_refresh_fails_full_login(self) -> None:
        """Refresh token fails -> falls back to full OIDC login."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._access_token = "expired-token"
        client._token_expiry = 0.0
        client._refresh_token = "bad-refresh-token"

        with (
            patch.object(
                client,
                "_refresh_access_token",
                new_callable=AsyncMock,
                side_effect=BrinkAuthError("Refresh failed"),
            ) as mock_refresh,
            patch.object(
                client, "_oidc_login", new_callable=AsyncMock
            ) as mock_login,
        ):
            async def login_side_effect():
                client._access_token = "new-token"
                client._token_expiry = time.monotonic() + 3600

            mock_login.side_effect = login_side_effect
            await client._ensure_token()
            mock_refresh.assert_called_once()
            mock_login.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_token_backoff(self) -> None:
        """Multiple OIDC failures -> increasing backoff cooldown."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._access_token = None
        client._token_expiry = 0.0

        with patch.object(
            client,
            "_oidc_login",
            new_callable=AsyncMock,
            side_effect=BrinkAuthError("Login failed"),
        ):
            # First failure
            with pytest.raises(BrinkAuthError):
                await client._ensure_token()
            assert client._auth_fail_count == 1
            assert client._auth_cooldown_until > 0

            # Second call should hit cooldown
            with pytest.raises(BrinkAuthError, match="cooldown"):
                await client._ensure_token()

    @pytest.mark.asyncio
    async def test_ensure_token_backoff_schedule(self) -> None:
        """Backoff schedule: 60s, 120s, 240s, 480s, 900s (cap)."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._access_token = None
        client._token_expiry = 0.0

        expected_backoffs = [60, 120, 240, 480, 900]

        with patch.object(
            client,
            "_oidc_login",
            new_callable=AsyncMock,
            side_effect=BrinkAuthError("Login failed"),
        ):
            for i, expected_backoff in enumerate(expected_backoffs):
                # Reset cooldown to allow the attempt
                client._auth_cooldown_until = 0.0
                before = time.monotonic()
                with pytest.raises(BrinkAuthError):
                    await client._ensure_token()
                assert client._auth_fail_count == i + 1
                # The cooldown should be approximately before + expected_backoff
                assert client._auth_cooldown_until >= before + expected_backoff - 1
                assert client._auth_cooldown_until <= before + expected_backoff + 1

    @pytest.mark.asyncio
    async def test_ensure_token_backoff_capped_at_5(self) -> None:
        """Fail count is capped at 5 (900s max backoff)."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._access_token = None
        client._token_expiry = 0.0

        with patch.object(
            client,
            "_oidc_login",
            new_callable=AsyncMock,
            side_effect=BrinkAuthError("Login failed"),
        ):
            # Fail 7 times
            for _ in range(7):
                client._auth_cooldown_until = 0.0
                with pytest.raises(BrinkAuthError):
                    await client._ensure_token()

            # Fail count capped at 5
            assert client._auth_fail_count == 5

    @pytest.mark.asyncio
    async def test_ensure_token_backoff_resets_on_success(self) -> None:
        """Successful login resets backoff counters."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._access_token = None
        client._token_expiry = 0.0
        client._auth_fail_count = 3
        client._auth_cooldown_until = 0.0  # Allow attempt

        with patch.object(client, "_oidc_login", new_callable=AsyncMock) as mock_login:
            async def login_side_effect():
                client._access_token = "fresh-token"
                client._token_expiry = time.monotonic() + 3600

            mock_login.side_effect = login_side_effect
            await client._ensure_token()
            assert client._auth_fail_count == 0
            assert client._auth_cooldown_until == 0.0

    def test_bearer_headers(self) -> None:
        """_bearer_headers returns correct Authorization: Bearer header."""
        session = MagicMock(spec=aiohttp.ClientSession)
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._access_token = "my-test-token"

        headers = client._bearer_headers()
        assert headers == {
            "Authorization": "Bearer my-test-token",
            "Accept": "application/json",
        }

    @pytest.mark.asyncio
    async def test_refresh_access_token_success(self) -> None:
        """_refresh_access_token updates token on success."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._refresh_token = "old-refresh"

        refresh_data = _make_token_response(
            access_token="refreshed-access",
            refresh_token="new-refresh",
        )
        refresh_resp = _make_mock_response(
            status=200,
            json_data=refresh_data,
            url=OIDC_TOKEN_URL,
        )

        session.post = AsyncMock(return_value=refresh_resp)
        await client._refresh_access_token()

        assert client._access_token == "refreshed-access"
        assert client._refresh_token == "new-refresh"
        assert client._token_expiry > time.monotonic()

    @pytest.mark.asyncio
    async def test_refresh_access_token_no_refresh_token(self) -> None:
        """_refresh_access_token with no refresh token -> BrinkAuthError."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._refresh_token = None

        with pytest.raises(BrinkAuthError, match="No refresh token"):
            await client._refresh_access_token()

    @pytest.mark.asyncio
    async def test_refresh_access_token_rejected(self) -> None:
        """Refresh token rejected (HTTP 400) -> clears refresh_token and raises."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._refresh_token = "expired-refresh"

        refresh_resp = _make_mock_response(
            status=400,
            json_data={"error": "invalid_grant"},
            url=OIDC_TOKEN_URL,
        )
        session.post = AsyncMock(return_value=refresh_resp)

        with pytest.raises(BrinkAuthError, match="Refresh token rejected"):
            await client._refresh_access_token()
        assert client._refresh_token is None

    @pytest.mark.asyncio
    async def test_refresh_access_token_network_error(self) -> None:
        """Network error during refresh -> clears refresh_token and raises."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._refresh_token = "some-refresh"

        session.post = AsyncMock(side_effect=aiohttp.ClientError("timeout"))

        with pytest.raises(BrinkAuthError, match="Refresh token request failed"):
            await client._refresh_access_token()
        assert client._refresh_token is None

    @pytest.mark.asyncio
    async def test_refresh_access_token_missing_access_token(self) -> None:
        """Refresh response missing access_token -> clears refresh and raises."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._refresh_token = "some-refresh"

        refresh_resp = _make_mock_response(
            status=200,
            json_data={"token_type": "Bearer", "expires_in": 3600},
            url=OIDC_TOKEN_URL,
        )
        session.post = AsyncMock(return_value=refresh_resp)

        with pytest.raises(BrinkAuthError, match="missing access_token"):
            await client._refresh_access_token()
        assert client._refresh_token is None

    @pytest.mark.asyncio
    async def test_refresh_access_token_invalid_json(self) -> None:
        """Refresh response not valid JSON -> clears refresh and raises."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._refresh_token = "some-refresh"

        refresh_resp = _make_mock_response(
            status=200,
            text="not json",
            url=OIDC_TOKEN_URL,
        )
        refresh_resp.json = AsyncMock(side_effect=ValueError("Invalid JSON"))
        session.post = AsyncMock(return_value=refresh_resp)

        with pytest.raises(BrinkAuthError, match="not valid JSON"):
            await client._refresh_access_token()
        assert client._refresh_token is None

    @pytest.mark.asyncio
    async def test_refresh_does_not_update_refresh_token_when_absent(self) -> None:
        """If the server does not rotate the refresh token, the old one is kept."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._refresh_token = "keep-this"

        refresh_data = _make_token_response(access_token="new-access")
        # No refresh_token in response
        refresh_resp = _make_mock_response(
            status=200,
            json_data=refresh_data,
            url=OIDC_TOKEN_URL,
        )
        session.post = AsyncMock(return_value=refresh_resp)

        await client._refresh_access_token()
        assert client._access_token == "new-access"
        # Old refresh token should remain
        assert client._refresh_token == "keep-this"


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


class TestURLValidation:
    """Tests for _is_trusted_url."""

    def test_is_trusted_url_brink_domain(self) -> None:
        """brink-home.com with HTTPS is accepted."""
        assert _is_trusted_url("https://www.brink-home.com/idsrv/login") is True
        assert _is_trusted_url("https://www.brink-home.com/portal/api/v1.1/") is True
        assert _is_trusted_url("https://www.brink-home.com/") is True

    def test_is_trusted_url_rejected(self) -> None:
        """Other domains and non-HTTPS are rejected."""
        assert _is_trusted_url("https://evil.example.com/") is False
        assert _is_trusted_url("http://www.brink-home.com/") is False
        assert _is_trusted_url("https://brink-home.com/") is False  # Missing www
        assert _is_trusted_url("ftp://www.brink-home.com/") is False
        assert _is_trusted_url("") is False
        assert _is_trusted_url("not a url at all") is False

    def test_is_trusted_url_port_443(self) -> None:
        """Explicit port 443 is accepted, other ports rejected."""
        assert _is_trusted_url("https://www.brink-home.com:443/path") is True
        assert _is_trusted_url("https://www.brink-home.com:8443/path") is False
        assert _is_trusted_url("https://www.brink-home.com:80/path") is False


# ---------------------------------------------------------------------------
# API methods
# ---------------------------------------------------------------------------


class TestAPIMethods:
    """Tests for get_systems, get_device_data, write_parameters."""

    @pytest.mark.asyncio
    async def test_get_systems_success(self) -> None:
        """get_systems returns parsed list of systems."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._access_token = "valid-token"
        client._token_expiry = time.monotonic() + 3600

        api_response = {
            "totalCount": 1,
            "items": [
                {
                    "systemShareId": 13090,
                    "systemName": "My Brink",
                    "serialNumber": "SN12345",
                    "gatewayState": "online",
                }
            ],
        }

        resp = _make_mock_response(status=200, json_data=api_response)
        session.get = AsyncMock(return_value=resp)

        systems = await client.get_systems()
        assert len(systems) == 1
        assert systems[0]["system_id"] == 13090
        assert systems[0]["name"] == "My Brink"
        assert systems[0]["serial_number"] == "SN12345"
        assert systems[0]["gateway_state"] == "online"

    @pytest.mark.asyncio
    async def test_get_systems_empty(self) -> None:
        """No systems -> empty list."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._access_token = "valid-token"
        client._token_expiry = time.monotonic() + 3600

        api_response = {"totalCount": 0, "items": []}

        resp = _make_mock_response(status=200, json_data=api_response)
        session.get = AsyncMock(return_value=resp)

        systems = await client.get_systems()
        assert systems == []

    @pytest.mark.asyncio
    async def test_get_systems_missing_system_share_id(self) -> None:
        """System items without systemShareId are skipped."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._access_token = "valid-token"
        client._token_expiry = time.monotonic() + 3600

        api_response = {
            "totalCount": 2,
            "items": [
                {"systemName": "Broken", "serialNumber": "X"},
                {"systemShareId": 999, "systemName": "OK"},
            ],
        }

        resp = _make_mock_response(status=200, json_data=api_response)
        session.get = AsyncMock(return_value=resp)

        systems = await client.get_systems()
        assert len(systems) == 1
        assert systems[0]["system_id"] == 999

    @pytest.mark.asyncio
    async def test_get_device_data_success(self) -> None:
        """get_device_data returns parsed uidescription parameters."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._access_token = "valid-token"
        client._token_expiry = time.monotonic() + 3600

        resp = _make_mock_response(status=200, json_data=UIDESCRIPTION_RESPONSE)
        session.get = AsyncMock(return_value=resp)

        data = await client.get_device_data(SYSTEM_ID)
        assert "components" in data
        assert len(data["components"]) == 1

        comp = data["components"][0]
        assert comp["component_id"] == 13107
        assert comp["name"] == "Flair 325"
        params = comp["parameters"]
        assert PARAM_VENTILATION_LEVEL in params
        assert params[PARAM_VENTILATION_LEVEL]["value"] == "2"
        assert params[PARAM_VENTILATION_LEVEL]["value_id"] == 1001
        assert PARAM_OPERATING_MODE in params
        assert PARAM_FRESH_AIR_TEMP in params

    @pytest.mark.asyncio
    async def test_get_device_data_invalid_system_id(self) -> None:
        """get_device_data with non-integer system_id -> ValueError."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        with pytest.raises(ValueError, match="system_id must be an integer"):
            await client.get_device_data("not-an-int")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_get_device_data_unknown_params(self) -> None:
        """Unknown parameter names are stored under unknown_{id} key."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._access_token = "valid-token"
        client._token_expiry = time.monotonic() + 3600

        response_with_unknown = {
            "root": {
                "navigationItems": [
                    {
                        "componentId": 99,
                        "name": "Test Component",
                        "parameterGroups": [
                            {
                                "parameters": [
                                    {
                                        "id": 9999,
                                        "name": "Unbekannter Parameter",
                                        "value": "42",
                                        "valueId": 8888,
                                        "valueState": 0,
                                        "readWrite": False,
                                    }
                                ]
                            }
                        ],
                        "navigationItems": [],
                    }
                ]
            }
        }

        resp = _make_mock_response(status=200, json_data=response_with_unknown)
        session.get = AsyncMock(return_value=resp)

        data = await client.get_device_data(SYSTEM_ID)
        comp = data["components"][0]
        # Unknown param should be stored as unknown_9999
        assert "unknown_9999" in comp["parameters"]
        assert comp["parameters"]["unknown_9999"]["value"] == "42"

    @pytest.mark.asyncio
    async def test_write_parameters_success(self) -> None:
        """write_parameters sends PUT with correct payload."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._access_token = "valid-token"
        client._token_expiry = time.monotonic() + 3600

        resp = _make_mock_response(status=200)
        session.put = AsyncMock(return_value=resp)

        await client.write_parameters(SYSTEM_ID, [(1001, "3"), (1002, "0")])

        session.put.assert_called_once()
        call_kwargs = session.put.call_args
        assert f"systems/{SYSTEM_ID}/parameter-values" in call_kwargs.args[0]
        payload = call_kwargs.kwargs["json"]
        assert len(payload["writeValues"]) == 2
        assert payload["writeValues"][0] == {
            "valueId": 1001,
            "value": "3",
            "state": 0,
        }
        assert payload["writeValues"][1] == {
            "valueId": 1002,
            "value": "0",
            "state": 0,
        }

    @pytest.mark.asyncio
    async def test_write_parameters_invalid_system_id(self) -> None:
        """write_parameters with non-integer system_id -> ValueError."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        with pytest.raises(ValueError, match="system_id must be an integer"):
            await client.write_parameters("not-an-int", [(1001, "3")])  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_write_parameters_invalid_value_id(self) -> None:
        """write_parameters with non-integer value_id -> ValueError."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._access_token = "valid-token"
        client._token_expiry = time.monotonic() + 3600

        with pytest.raises(ValueError, match="value_id must be an integer"):
            await client.write_parameters(SYSTEM_ID, [("bad", "3")])  # type: ignore[list-item]

    @pytest.mark.asyncio
    async def test_write_parameters_401_retry(self) -> None:
        """Write gets 401 -> re-login -> retry -> success."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._access_token = "stale-token"
        client._token_expiry = time.monotonic() + 3600

        # First call: 401, second call: 200
        resp_401 = _make_mock_response(status=401)
        resp_200 = _make_mock_response(status=200)
        session.put = AsyncMock(side_effect=[resp_401, resp_200])

        with patch.object(client, "_ensure_token", new_callable=AsyncMock) as mock_ensure:
            await client.write_parameters(SYSTEM_ID, [(1001, "2")])

        assert session.put.call_count == 2

    @pytest.mark.asyncio
    async def test_write_parameters_401_retry_fails(self) -> None:
        """Write gets 401 twice -> BrinkAuthError."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._access_token = "stale-token"
        client._token_expiry = time.monotonic() + 3600

        resp_401 = _make_mock_response(status=401)
        session.put = AsyncMock(return_value=resp_401)

        with patch.object(client, "_ensure_token", new_callable=AsyncMock):
            with pytest.raises(BrinkAuthError, match="persistent 401"):
                await client.write_parameters(SYSTEM_ID, [(1001, "2")])

        # Two attempts: initial + retry
        assert session.put.call_count == 2

    @pytest.mark.asyncio
    async def test_write_parameters_server_error(self) -> None:
        """Write gets 500 -> raises via raise_for_status."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._access_token = "valid-token"
        client._token_expiry = time.monotonic() + 3600

        resp_500 = _make_mock_response(status=500)
        resp_500.raise_for_status = MagicMock(
            side_effect=aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=500,
                message="Internal Server Error",
            )
        )
        session.put = AsyncMock(return_value=resp_500)

        with pytest.raises(aiohttp.ClientResponseError):
            await client.write_parameters(SYSTEM_ID, [(1001, "2")])


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------


class TestHTMLParsing:
    """Tests for _InputFieldExtractor and _extract_form_fields."""

    def test_input_field_extractor(self) -> None:
        """Extracts form fields from HTML with input elements."""
        html = """
        <form>
            <input name="__RequestVerificationToken" value="csrf-abc-123" />
            <input name="ReturnUrl" value="/callback?id=1" />
            <input name="Username" value="" />
            <input name="OtherField" value="ignored" />
        </form>
        """
        extractor = _InputFieldExtractor(
            {"__RequestVerificationToken", "ReturnUrl"}
        )
        extractor.feed(html)

        assert extractor.results["__requestverificationtoken"] == "csrf-abc-123"
        assert extractor.results["returnurl"] == "/callback?id=1"
        assert "username" not in extractor.results
        assert "otherfield" not in extractor.results

    def test_input_field_extractor_case_insensitive(self) -> None:
        """Field name matching is case-insensitive."""
        html = '<input NAME="__REQUESTVERIFICATIONTOKEN" VALUE="tok123" />'
        extractor = _InputFieldExtractor({"__RequestVerificationToken"})
        extractor.feed(html)
        assert extractor.results["__requestverificationtoken"] == "tok123"

    def test_input_field_extractor_no_value_attribute(self) -> None:
        """Input without value attribute is skipped."""
        html = '<input name="__RequestVerificationToken" />'
        extractor = _InputFieldExtractor({"__RequestVerificationToken"})
        extractor.feed(html)
        assert "__requestverificationtoken" not in extractor.results

    def test_input_field_extractor_non_input_tag(self) -> None:
        """Non-input tags are ignored even with matching name/value."""
        html = '<div name="__RequestVerificationToken" value="bad" />'
        extractor = _InputFieldExtractor({"__RequestVerificationToken"})
        extractor.feed(html)
        assert len(extractor.results) == 0

    def test_extract_form_fields_from_login_page(self) -> None:
        """_extract_form_fields on a realistic login page returns CSRF + ReturnUrl."""
        fields = BrinkHomeCloud._extract_form_fields(LOGIN_PAGE_HTML)
        assert fields["__requestverificationtoken"] == "CfDJ8CSRF_TOKEN_VALUE_HERE"
        assert "returnurl" in fields

    def test_extract_form_fields_empty_html(self) -> None:
        """Empty HTML -> no fields extracted."""
        fields = BrinkHomeCloud._extract_form_fields("")
        assert fields == {}


# ---------------------------------------------------------------------------
# uidescription parsing
# ---------------------------------------------------------------------------


class TestParseUidescription:
    """Tests for _parse_uidescription and _extract_parameters."""

    def test_parse_uidescription(self) -> None:
        """Parses nested parameter tree from uidescription response."""
        result = BrinkHomeCloud._parse_uidescription(UIDESCRIPTION_RESPONSE)

        assert "components" in result
        assert len(result["components"]) == 1

        comp = result["components"][0]
        assert comp["component_id"] == 13107
        assert comp["name"] == "Flair 325"

        params = comp["parameters"]
        # Top-level params
        assert PARAM_VENTILATION_LEVEL in params
        assert params[PARAM_VENTILATION_LEVEL]["value"] == "2"
        assert params[PARAM_VENTILATION_LEVEL]["numeric_id"] == 100

        assert PARAM_OPERATING_MODE in params

        # Nested child params
        assert PARAM_FRESH_AIR_TEMP in params
        assert params[PARAM_FRESH_AIR_TEMP]["value"] == "15.0"
        assert params[PARAM_FRESH_AIR_TEMP]["unit_of_measure"] == "C"

    def test_parse_uidescription_empty(self) -> None:
        """Empty root -> no components."""
        result = BrinkHomeCloud._parse_uidescription({"root": {}})
        assert result == {"components": []}

    def test_parse_uidescription_no_root(self) -> None:
        """Missing root key -> no components."""
        result = BrinkHomeCloud._parse_uidescription({})
        assert result == {"components": []}

    def test_parse_uidescription_nav_item_no_params(self) -> None:
        """Navigation item with no parameters is not included in components."""
        data = {
            "root": {
                "navigationItems": [
                    {
                        "componentId": 1,
                        "name": "Empty",
                        "parameterGroups": [],
                        "navigationItems": [],
                    }
                ]
            }
        }
        result = BrinkHomeCloud._parse_uidescription(data)
        assert result["components"] == []

    def test_extract_parameters_max_depth(self) -> None:
        """Recursion beyond depth 20 is stopped."""
        # Build a deeply nested structure
        inner: dict[str, Any] = {
            "parameterGroups": [
                {
                    "parameters": [
                        {
                            "id": 1,
                            "name": "Lüftungsstufe",
                            "value": "1",
                            "valueId": 1,
                        }
                    ]
                }
            ],
            "navigationItems": [],
        }
        current = inner
        for _ in range(25):
            current = {
                "parameterGroups": [],
                "navigationItems": [current],
            }

        parameters: dict[str, dict[str, Any]] = {}
        _extract_parameters(current, parameters)
        # The parameter at depth > 20 should NOT be extracted
        assert len(parameters) == 0

    def test_extract_parameters_unknown_no_id(self) -> None:
        """Parameter with unknown name and no id is skipped entirely."""
        nav_item = {
            "parameterGroups": [
                {
                    "parameters": [
                        {
                            "name": "TotallyUnknown",
                            "value": "99",
                            # No "id" key
                        }
                    ]
                }
            ],
            "navigationItems": [],
        }
        parameters: dict[str, dict[str, Any]] = {}
        _extract_parameters(nav_item, parameters)
        assert len(parameters) == 0

    def test_extract_parameters_unit_fallback(self) -> None:
        """unitOfMeasure is used when unit is None."""
        nav_item = {
            "parameterGroups": [
                {
                    "parameters": [
                        {
                            "id": 100,
                            "name": "Lüftungsstufe",
                            "value": "2",
                            "valueId": 1001,
                            "unit": None,
                            "unitOfMeasure": "m3/h",
                        }
                    ]
                }
            ],
            "navigationItems": [],
        }
        parameters: dict[str, dict[str, Any]] = {}
        _extract_parameters(nav_item, parameters)
        assert parameters[PARAM_VENTILATION_LEVEL]["unit_of_measure"] == "m3/h"

    def test_extract_parameters_all_fields(self) -> None:
        """All parameter fields are extracted into the output dict."""
        nav_item = {
            "parameterGroups": [
                {
                    "parameters": [
                        {
                            "id": 100,
                            "name": "Lüftungsstufe",
                            "value": "2",
                            "valueId": 1001,
                            "valueState": 0,
                            "readWrite": True,
                            "controlType": "list",
                            "listItems": [{"value": "0"}],
                            "minValue": 0,
                            "maxValue": 3,
                            "unit": "steps",
                            "componentId": 42,
                        }
                    ]
                }
            ],
            "navigationItems": [],
        }
        parameters: dict[str, dict[str, Any]] = {}
        _extract_parameters(nav_item, parameters)

        p = parameters[PARAM_VENTILATION_LEVEL]
        assert p["name"] == "Lüftungsstufe"
        assert p["value"] == "2"
        assert p["value_id"] == 1001
        assert p["value_state"] == 0
        assert p["read_write"] is True
        assert p["control_type"] == "list"
        assert p["list_items"] == [{"value": "0"}]
        assert p["min_value"] == 0
        assert p["max_value"] == 3
        assert p["unit_of_measure"] == "steps"
        assert p["component_id"] == 42
        assert p["numeric_id"] == 100


# ---------------------------------------------------------------------------
# extract_code_from_redirect
# ---------------------------------------------------------------------------


class TestExtractCodeFromRedirect:
    """Tests for _extract_code_from_redirect."""

    def test_extract_code_with_state_match(self) -> None:
        """Code is extracted when state matches."""
        url = "https://www.brink-home.com/app/?code=abc123&state=mystate"
        code = BrinkHomeCloud._extract_code_from_redirect(url, expected_state="mystate")
        assert code == "abc123"

    def test_extract_code_state_mismatch(self) -> None:
        """State mismatch returns None."""
        url = "https://www.brink-home.com/app/?code=abc123&state=wrong"
        code = BrinkHomeCloud._extract_code_from_redirect(url, expected_state="mystate")
        assert code is None

    def test_extract_code_no_state_check(self) -> None:
        """Without expected_state, code is extracted regardless."""
        url = "https://www.brink-home.com/app/?code=abc123&state=anything"
        code = BrinkHomeCloud._extract_code_from_redirect(url, expected_state=None)
        assert code == "abc123"

    def test_extract_code_no_code_param(self) -> None:
        """URL without code parameter returns None."""
        url = "https://www.brink-home.com/app/?state=mystate"
        code = BrinkHomeCloud._extract_code_from_redirect(url, expected_state="mystate")
        assert code is None

    def test_extract_code_from_fragment(self) -> None:
        """Code in URL fragment is extracted."""
        url = "https://www.brink-home.com/app/#code=fragcode"
        code = BrinkHomeCloud._extract_code_from_redirect(url)
        assert code == "fragcode"

    def test_extract_code_empty_url(self) -> None:
        """Empty URL returns None."""
        code = BrinkHomeCloud._extract_code_from_redirect("")
        assert code is None

    def test_extract_code_missing_state_param(self) -> None:
        """URL with code but missing state when state expected returns None."""
        url = "https://www.brink-home.com/app/?code=abc123"
        code = BrinkHomeCloud._extract_code_from_redirect(url, expected_state="mystate")
        assert code is None


# ---------------------------------------------------------------------------
# Follow redirects
# ---------------------------------------------------------------------------


class TestFollowRedirects:
    """Tests for _follow_redirects_for_code."""

    @pytest.mark.asyncio
    async def test_follow_redirects_relative_url(self) -> None:
        """Relative redirect URL is resolved against base URL."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        mock_oidc_session = AsyncMock(spec=aiohttp.ClientSession)

        # Response is a redirect whose Location contains code
        resp1 = _make_mock_response(
            status=302,
            headers={"Location": "/idsrv/connect/authorize/callback?code=mycode&state=st"},
            url="https://www.brink-home.com/idsrv/step1",
        )
        mock_oidc_session.get = AsyncMock(return_value=resp1)

        code = await client._follow_redirects_for_code(
            mock_oidc_session,
            "/idsrv/step1",
            "https://www.brink-home.com/idsrv/login",
            expected_state="st",
        )
        assert code == "mycode"

    @pytest.mark.asyncio
    async def test_follow_redirects_untrusted_url_stops(self) -> None:
        """Redirect to untrusted domain is refused (no HTTP request made)."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        mock_oidc_session = AsyncMock(spec=aiohttp.ClientSession)

        # URL on untrusted domain WITHOUT a code param -- the method should
        # refuse to follow the redirect and return None.
        code = await client._follow_redirects_for_code(
            mock_oidc_session,
            "https://evil.example.com/steal?state=abc",
            "https://www.brink-home.com/idsrv/login",
            expected_state="abc",
        )
        assert code is None
        # Should NOT have followed the redirect to the untrusted domain
        mock_oidc_session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_follow_redirects_untrusted_url_with_code_still_extracts(self) -> None:
        """If the untrusted URL already contains the code, it is extracted.

        The code is in the query string, so _extract_code_from_redirect finds
        it before the trust check. This is correct behaviour -- the code was
        already in the redirect Location header, no additional request needed.
        """
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        mock_oidc_session = AsyncMock(spec=aiohttp.ClientSession)

        code = await client._follow_redirects_for_code(
            mock_oidc_session,
            "https://evil.example.com/steal?code=stolen&state=abc",
            "https://www.brink-home.com/idsrv/login",
            expected_state="abc",
        )
        # Code is extracted from the URL itself (no HTTP request needed)
        assert code == "stolen"
        mock_oidc_session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_follow_redirects_network_error(self) -> None:
        """Network error during redirect chain returns None."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        mock_oidc_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_oidc_session.get = AsyncMock(
            side_effect=aiohttp.ClientError("Connection reset")
        )

        code = await client._follow_redirects_for_code(
            mock_oidc_session,
            "https://www.brink-home.com/idsrv/step1",
            "https://www.brink-home.com/idsrv/login",
        )
        assert code is None

    @pytest.mark.asyncio
    async def test_follow_redirects_code_in_final_200(self) -> None:
        """Code found in URL of a 200 response (not a redirect)."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        mock_oidc_session = AsyncMock(spec=aiohttp.ClientSession)

        # Responds with 200 but URL contains code
        resp = _make_mock_response(
            status=200,
            url="https://www.brink-home.com/app/?code=final_code&state=st",
        )
        mock_oidc_session.get = AsyncMock(return_value=resp)

        code = await client._follow_redirects_for_code(
            mock_oidc_session,
            "https://www.brink-home.com/idsrv/callback",
            "https://www.brink-home.com/idsrv/login",
            expected_state="st",
        )
        assert code == "final_code"

    @pytest.mark.asyncio
    async def test_follow_redirects_empty_location(self) -> None:
        """Redirect with empty Location header stops the chain."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        mock_oidc_session = AsyncMock(spec=aiohttp.ClientSession)

        resp = _make_mock_response(
            status=302,
            headers={"Location": ""},
            url="https://www.brink-home.com/idsrv/step1",
        )
        mock_oidc_session.get = AsyncMock(return_value=resp)

        code = await client._follow_redirects_for_code(
            mock_oidc_session,
            "https://www.brink-home.com/idsrv/step1",
            "https://www.brink-home.com/idsrv/login",
        )
        assert code is None


# ---------------------------------------------------------------------------
# Submit login credentials (ReturnUrl handling)
# ---------------------------------------------------------------------------


class TestSubmitLoginReturnUrl:
    """Tests for ReturnUrl validation in _submit_login_credentials."""

    @pytest.mark.asyncio
    async def test_return_url_relative_path_is_included(self) -> None:
        """Relative ReturnUrl (starts with /) is included in form data."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        mock_oidc_session = AsyncMock(spec=aiohttp.ClientSession)

        test_state = "mystate"
        test_code = "testcode"
        redirect_url = _make_redirect_url(test_code, test_state)

        redirect_resp = _make_mock_response(
            status=302,
            headers={"Location": redirect_url},
        )
        redirect_resp.text = AsyncMock(return_value="")
        mock_oidc_session.post = AsyncMock(return_value=redirect_resp)

        code = await client._submit_login_credentials(
            mock_oidc_session,
            "https://www.brink-home.com/idsrv/login",
            "csrf-token",
            "/idsrv/connect/authorize/callback?client_id=spa",
            test_state,
        )
        assert code == test_code

        # Check form data included ReturnUrl
        call_kwargs = mock_oidc_session.post.call_args
        form_data = call_kwargs.kwargs.get("data", {})
        assert form_data.get("ReturnUrl") == "/idsrv/connect/authorize/callback?client_id=spa"

    @pytest.mark.asyncio
    async def test_return_url_untrusted_is_excluded(self) -> None:
        """Untrusted absolute ReturnUrl is excluded from form data."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        mock_oidc_session = AsyncMock(spec=aiohttp.ClientSession)

        test_state = "mystate"
        test_code = "testcode"
        redirect_url = _make_redirect_url(test_code, test_state)

        redirect_resp = _make_mock_response(
            status=302,
            headers={"Location": redirect_url},
        )
        redirect_resp.text = AsyncMock(return_value="")
        mock_oidc_session.post = AsyncMock(return_value=redirect_resp)

        code = await client._submit_login_credentials(
            mock_oidc_session,
            "https://www.brink-home.com/idsrv/login",
            "csrf-token",
            "https://evil.com/steal",  # Untrusted ReturnUrl
            test_state,
        )
        assert code == test_code

        # Check ReturnUrl was NOT included
        call_kwargs = mock_oidc_session.post.call_args
        form_data = call_kwargs.kwargs.get("data", {})
        assert "ReturnUrl" not in form_data

    @pytest.mark.asyncio
    async def test_return_url_double_slash_is_excluded(self) -> None:
        """ReturnUrl starting with // (protocol-relative) is excluded."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        mock_oidc_session = AsyncMock(spec=aiohttp.ClientSession)

        test_state = "mystate"
        test_code = "testcode"
        redirect_url = _make_redirect_url(test_code, test_state)

        redirect_resp = _make_mock_response(
            status=302,
            headers={"Location": redirect_url},
        )
        redirect_resp.text = AsyncMock(return_value="")
        mock_oidc_session.post = AsyncMock(return_value=redirect_resp)

        code = await client._submit_login_credentials(
            mock_oidc_session,
            "https://www.brink-home.com/idsrv/login",
            "csrf-token",
            "//evil.com/steal",  # Protocol-relative URL
            test_state,
        )
        assert code == test_code

        call_kwargs = mock_oidc_session.post.call_args
        form_data = call_kwargs.kwargs.get("data", {})
        assert "ReturnUrl" not in form_data

    @pytest.mark.asyncio
    async def test_return_url_trusted_absolute_is_included(self) -> None:
        """Trusted absolute ReturnUrl is included."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        mock_oidc_session = AsyncMock(spec=aiohttp.ClientSession)

        test_state = "mystate"
        test_code = "testcode"
        redirect_url = _make_redirect_url(test_code, test_state)

        redirect_resp = _make_mock_response(
            status=302,
            headers={"Location": redirect_url},
        )
        redirect_resp.text = AsyncMock(return_value="")
        mock_oidc_session.post = AsyncMock(return_value=redirect_resp)

        trusted_return = "https://www.brink-home.com/idsrv/callback"
        code = await client._submit_login_credentials(
            mock_oidc_session,
            "https://www.brink-home.com/idsrv/login",
            "csrf-token",
            trusted_return,
            test_state,
        )
        assert code == test_code

        call_kwargs = mock_oidc_session.post.call_args
        form_data = call_kwargs.kwargs.get("data", {})
        assert form_data.get("ReturnUrl") == trusted_return

    @pytest.mark.asyncio
    async def test_return_url_none_is_not_included(self) -> None:
        """None ReturnUrl is not added to form data."""
        session = _make_mock_session()
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)

        mock_oidc_session = AsyncMock(spec=aiohttp.ClientSession)

        test_state = "mystate"
        test_code = "testcode"
        redirect_url = _make_redirect_url(test_code, test_state)

        redirect_resp = _make_mock_response(
            status=302,
            headers={"Location": redirect_url},
        )
        redirect_resp.text = AsyncMock(return_value="")
        mock_oidc_session.post = AsyncMock(return_value=redirect_resp)

        code = await client._submit_login_credentials(
            mock_oidc_session,
            "https://www.brink-home.com/idsrv/login",
            "csrf-token",
            None,  # No ReturnUrl
            test_state,
        )
        assert code == test_code

        call_kwargs = mock_oidc_session.post.call_args
        form_data = call_kwargs.kwargs.get("data", {})
        assert "ReturnUrl" not in form_data


# ---------------------------------------------------------------------------
# Close / cleanup
# ---------------------------------------------------------------------------


class TestClose:
    """Tests for BrinkHomeCloud.close."""

    @pytest.mark.asyncio
    async def test_close_clears_sensitive_state(self) -> None:
        """close() zeroes out credentials and tokens."""
        session = MagicMock(spec=aiohttp.ClientSession)
        client = BrinkHomeCloud(session, TEST_EMAIL, TEST_PASSWORD)
        client._access_token = "token"
        client._token_expiry = 99999.0
        client._refresh_token = "refresh"
        client._auth_cooldown_until = 123.0
        client._auth_fail_count = 5

        await client.close()

        assert client._access_token is None
        assert client._token_expiry == 0.0
        assert client._refresh_token is None
        assert client._auth_cooldown_until == 0.0
        assert client._auth_fail_count == 0
        assert client._username == ""
        assert client._password == ""


# ---------------------------------------------------------------------------
# BrinkAuthError
# ---------------------------------------------------------------------------


class TestBrinkAuthError:
    """Tests for the BrinkAuthError exception class."""

    def test_default_not_credentials_error(self) -> None:
        """Default is_credentials_error is False."""
        err = BrinkAuthError("something went wrong")
        assert err.is_credentials_error is False
        assert str(err) == "something went wrong"

    def test_credentials_error_flag(self) -> None:
        """is_credentials_error=True is stored correctly."""
        err = BrinkAuthError("bad password", is_credentials_error=True)
        assert err.is_credentials_error is True
        assert str(err) == "bad password"

    def test_is_exception_subclass(self) -> None:
        """BrinkAuthError is an Exception."""
        assert issubclass(BrinkAuthError, Exception)
