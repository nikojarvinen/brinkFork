"""Tests for the Brink HRV Control config flow."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.brink_ventilation.const import (
    CONF_ADAPTIVE_ACTIVE,
    CONF_AUTO_SUMMER_BASE_LEVEL,
    CONF_AUTO_WINTER_BASE_LEVEL,
    CONF_EXTRA_VENT_DURATION,
    CONF_EXTRA_VENT_SUMMER_LEVEL,
    CONF_EXTRA_VENT_WINTER_LEVEL,
    CONF_FREEZING_THRESHOLD,
    CONF_HUMIDITY_SENSOR_1,
    CONF_HUMIDITY_SENSOR_2,
    CONF_HUMIDITY_SENSOR_3,
    CONF_HUMIDITY_SPIKE_THRESHOLD,
    CONF_INDOOR_TEMPERATURE_ENTITY_1,
    CONF_INDOOR_TEMPERATURE_ENTITY_2,
    CONF_TEMPERATURE_SOURCE_ENTITY,
    DEFAULT_AUTO_SUMMER_BASE_LEVEL,
    DEFAULT_AUTO_WINTER_BASE_LEVEL,
    DEFAULT_EXTRA_VENT_DURATION,
    DEFAULT_EXTRA_VENT_SUMMER_LEVEL,
    DEFAULT_EXTRA_VENT_WINTER_LEVEL,
    DEFAULT_FREEZING_THRESHOLD,
    DEFAULT_HUMIDITY_SPIKE_THRESHOLD,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from custom_components.brink_ventilation.core.brink_home_cloud import BrinkAuthError

from pytest_homeassistant_custom_component.common import MockConfigEntry

TEST_EMAIL = "test@example.com"
TEST_PASSWORD = "testpassword123"
TEST_NEW_PASSWORD = "newpassword456"


def _patch_test_credentials(
    *,
    side_effect: Exception | None = None,
) -> Any:
    """Return a context manager that patches _async_test_credentials."""
    mock = AsyncMock(side_effect=side_effect)
    return patch(
        "custom_components.brink_ventilation.config_flow._async_test_credentials",
        mock,
    )


# ---------------------------------------------------------------------------
# User flow tests
# ---------------------------------------------------------------------------


async def test_user_flow_success(hass: HomeAssistant) -> None:
    """Test the happy path: user enters valid credentials and entry is created."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    # Patch both test_credentials AND the integration setup (HA auto-loads the
    # newly created entry, which would otherwise attempt a real API call)
    with (
        _patch_test_credentials(),
        patch("custom_components.brink_ventilation.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == TEST_EMAIL
    assert result["data"] == {
        CONF_USERNAME: TEST_EMAIL,
        CONF_PASSWORD: TEST_PASSWORD,
    }
    assert result["result"].unique_id == TEST_EMAIL.lower()


async def test_user_flow_invalid_auth(hass: HomeAssistant) -> None:
    """Test BrinkAuthError with is_credentials_error=True shows invalid_auth, then retry succeeds."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    # First attempt: invalid credentials
    with _patch_test_credentials(
        side_effect=BrinkAuthError("Bad creds", is_credentials_error=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: "wrong"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    # Retry with correct credentials
    with _patch_test_credentials():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_brink_auth_error_not_credentials(hass: HomeAssistant) -> None:
    """Test BrinkAuthError with is_credentials_error=False shows cannot_connect."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with _patch_test_credentials(
        side_effect=BrinkAuthError("OIDC failure", is_credentials_error=False),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_client_response_error_401(hass: HomeAssistant) -> None:
    """Test aiohttp.ClientResponseError with 401 status shows invalid_auth."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    error_401 = aiohttp.ClientResponseError(
        request_info=aiohttp.RequestInfo(
            url="https://example.com",
            method="POST",
            headers={},
            real_url="https://example.com",
        ),
        history=(),
        status=401,
        message="Unauthorized",
    )
    with _patch_test_credentials(side_effect=error_401):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_client_response_error_non_401(hass: HomeAssistant) -> None:
    """Test aiohttp.ClientResponseError with non-401 status shows cannot_connect."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    error_500 = aiohttp.ClientResponseError(
        request_info=aiohttp.RequestInfo(
            url="https://example.com",
            method="POST",
            headers={},
            real_url="https://example.com",
        ),
        history=(),
        status=500,
        message="Internal Server Error",
    )
    with _patch_test_credentials(side_effect=error_500):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_connection_error(hass: HomeAssistant) -> None:
    """Test aiohttp.ClientError shows cannot_connect, then retry succeeds."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with _patch_test_credentials(
        side_effect=aiohttp.ClientError("Connection failed"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    # Retry successfully
    with _patch_test_credentials():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_timeout_error(hass: HomeAssistant) -> None:
    """Test asyncio.TimeoutError shows cannot_connect."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with _patch_test_credentials(side_effect=asyncio.TimeoutError()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_unknown_error(hass: HomeAssistant) -> None:
    """Test generic Exception shows unknown error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with _patch_test_credentials(side_effect=RuntimeError("Boom")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_user_flow_duplicate(hass: HomeAssistant) -> None:
    """Test that a duplicate email aborts with already_configured."""
    # Create an existing entry with the same email
    existing_entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_EMAIL,
        data={CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        unique_id=TEST_EMAIL.lower(),
    )
    existing_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    with _patch_test_credentials():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Reauth flow tests
# ---------------------------------------------------------------------------


async def test_reauth_flow_success(hass: HomeAssistant) -> None:
    """Test reauth: new password updates and reloads entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_EMAIL,
        data={CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        unique_id=TEST_EMAIL.lower(),
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with _patch_test_credentials():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: TEST_NEW_PASSWORD},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == TEST_NEW_PASSWORD
    assert entry.data[CONF_USERNAME] == TEST_EMAIL


async def test_reauth_flow_invalid_auth(hass: HomeAssistant) -> None:
    """Test reauth with wrong password shows error, then retry succeeds."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_EMAIL,
        data={CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        unique_id=TEST_EMAIL.lower(),
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    # First attempt: invalid credentials
    with _patch_test_credentials(
        side_effect=BrinkAuthError("Wrong password", is_credentials_error=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: "badpassword"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    # Retry with correct password
    with _patch_test_credentials():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: TEST_NEW_PASSWORD},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"


async def test_reauth_flow_brink_auth_not_credentials(hass: HomeAssistant) -> None:
    """Test reauth with BrinkAuthError is_credentials_error=False shows cannot_connect."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_EMAIL,
        data={CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        unique_id=TEST_EMAIL.lower(),
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)

    with _patch_test_credentials(
        side_effect=BrinkAuthError("Server error", is_credentials_error=False),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: TEST_NEW_PASSWORD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauth_flow_connection_error(hass: HomeAssistant) -> None:
    """Test reauth with network error shows cannot_connect."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_EMAIL,
        data={CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        unique_id=TEST_EMAIL.lower(),
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)

    with _patch_test_credentials(
        side_effect=aiohttp.ClientError("Network down"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: TEST_NEW_PASSWORD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauth_flow_timeout_error(hass: HomeAssistant) -> None:
    """Test reauth with asyncio.TimeoutError shows cannot_connect."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_EMAIL,
        data={CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        unique_id=TEST_EMAIL.lower(),
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)

    with _patch_test_credentials(side_effect=asyncio.TimeoutError()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: TEST_NEW_PASSWORD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauth_flow_unknown_error(hass: HomeAssistant) -> None:
    """Test reauth with unexpected exception shows unknown error."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_EMAIL,
        data={CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        unique_id=TEST_EMAIL.lower(),
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)

    with _patch_test_credentials(side_effect=RuntimeError("Unexpected")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: TEST_NEW_PASSWORD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


# ---------------------------------------------------------------------------
# Reconfigure flow tests
# ---------------------------------------------------------------------------


async def test_reconfigure_flow_success(hass: HomeAssistant) -> None:
    """Test reconfigure with valid new credentials updates entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_EMAIL,
        data={CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        unique_id=TEST_EMAIL.lower(),
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    with _patch_test_credentials():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_NEW_PASSWORD},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PASSWORD] == TEST_NEW_PASSWORD
    assert entry.data[CONF_USERNAME] == TEST_EMAIL


async def test_reconfigure_flow_invalid_auth(hass: HomeAssistant) -> None:
    """Test reconfigure with BrinkAuthError is_credentials_error=True shows invalid_auth."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_EMAIL,
        data={CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        unique_id=TEST_EMAIL.lower(),
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)

    with _patch_test_credentials(
        side_effect=BrinkAuthError("Bad creds", is_credentials_error=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: "wrong"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reconfigure_flow_brink_auth_not_credentials(
    hass: HomeAssistant,
) -> None:
    """Test reconfigure with BrinkAuthError is_credentials_error=False shows cannot_connect."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_EMAIL,
        data={CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        unique_id=TEST_EMAIL.lower(),
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)

    with _patch_test_credentials(
        side_effect=BrinkAuthError("OIDC failure", is_credentials_error=False),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_flow_connection_error(hass: HomeAssistant) -> None:
    """Test reconfigure with network error shows cannot_connect."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_EMAIL,
        data={CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        unique_id=TEST_EMAIL.lower(),
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)

    with _patch_test_credentials(
        side_effect=aiohttp.ClientError("Connection failed"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_flow_timeout_error(hass: HomeAssistant) -> None:
    """Test reconfigure with asyncio.TimeoutError shows cannot_connect."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_EMAIL,
        data={CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        unique_id=TEST_EMAIL.lower(),
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)

    with _patch_test_credentials(side_effect=asyncio.TimeoutError()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_flow_unknown_error(hass: HomeAssistant) -> None:
    """Test reconfigure with unexpected exception shows unknown error."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_EMAIL,
        data={CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        unique_id=TEST_EMAIL.lower(),
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)

    with _patch_test_credentials(side_effect=RuntimeError("Unexpected")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_reconfigure_flow_account_mismatch(hass: HomeAssistant) -> None:
    """Test reconfigure with different email aborts with account_mismatch."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_EMAIL,
        data={CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        unique_id=TEST_EMAIL.lower(),
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)

    different_email = "other@example.com"
    with _patch_test_credentials():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: different_email, CONF_PASSWORD: TEST_PASSWORD},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "account_mismatch"


# ---------------------------------------------------------------------------
# Options flow tests
# ---------------------------------------------------------------------------


def _build_options_entry(**option_overrides: Any) -> MockConfigEntry:
    """Create a MockConfigEntry with standard options, applying overrides."""
    options: dict[str, Any] = {
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
        CONF_FREEZING_THRESHOLD: DEFAULT_FREEZING_THRESHOLD,
        CONF_EXTRA_VENT_DURATION: DEFAULT_EXTRA_VENT_DURATION,
        CONF_EXTRA_VENT_SUMMER_LEVEL: str(DEFAULT_EXTRA_VENT_SUMMER_LEVEL),
        CONF_EXTRA_VENT_WINTER_LEVEL: str(DEFAULT_EXTRA_VENT_WINTER_LEVEL),
        CONF_AUTO_SUMMER_BASE_LEVEL: str(DEFAULT_AUTO_SUMMER_BASE_LEVEL),
        CONF_AUTO_WINTER_BASE_LEVEL: str(DEFAULT_AUTO_WINTER_BASE_LEVEL),
        CONF_HUMIDITY_SPIKE_THRESHOLD: DEFAULT_HUMIDITY_SPIKE_THRESHOLD,
        CONF_ADAPTIVE_ACTIVE: False,
    }
    options.update(option_overrides)
    return MockConfigEntry(
        domain=DOMAIN,
        title=TEST_EMAIL,
        data={CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        options=options,
        unique_id=TEST_EMAIL.lower(),
    )


async def test_options_flow_full(hass: HomeAssistant) -> None:
    """Test the full 3-step options flow with custom values."""
    entry = _build_options_entry()
    entry.add_to_hass(hass)

    # Step 1: init (general settings)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: 90,
            CONF_FREEZING_THRESHOLD: -5.0,
            CONF_TEMPERATURE_SOURCE_ENTITY: "sensor.outside_temp",
            CONF_INDOOR_TEMPERATURE_ENTITY_1: "sensor.indoor_temp_1",
            CONF_INDOOR_TEMPERATURE_ENTITY_2: "sensor.indoor_temp_2",
        },
    )

    # Step 2: extra_ventilation
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "extra_ventilation"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_EXTRA_VENT_DURATION: 60,
            CONF_EXTRA_VENT_SUMMER_LEVEL: "2",
            CONF_EXTRA_VENT_WINTER_LEVEL: "1",
        },
    )

    # Step 3: adaptive
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "adaptive"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_AUTO_SUMMER_BASE_LEVEL: "1",
            CONF_AUTO_WINTER_BASE_LEVEL: "0",
            CONF_HUMIDITY_SENSOR_1: "sensor.bathroom_humidity",
            CONF_HUMIDITY_SENSOR_2: "sensor.kitchen_humidity",
            CONF_HUMIDITY_SENSOR_3: "sensor.bedroom_humidity",
            CONF_HUMIDITY_SPIKE_THRESHOLD: 2.0,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SCAN_INTERVAL] == 90
    assert result["data"][CONF_FREEZING_THRESHOLD] == -5.0
    assert result["data"][CONF_TEMPERATURE_SOURCE_ENTITY] == "sensor.outside_temp"
    assert result["data"][CONF_INDOOR_TEMPERATURE_ENTITY_1] == "sensor.indoor_temp_1"
    assert result["data"][CONF_INDOOR_TEMPERATURE_ENTITY_2] == "sensor.indoor_temp_2"
    assert result["data"][CONF_EXTRA_VENT_DURATION] == 60
    assert result["data"][CONF_EXTRA_VENT_SUMMER_LEVEL] == "2"
    assert result["data"][CONF_EXTRA_VENT_WINTER_LEVEL] == "1"
    assert result["data"][CONF_AUTO_SUMMER_BASE_LEVEL] == "1"
    assert result["data"][CONF_AUTO_WINTER_BASE_LEVEL] == "0"
    assert result["data"][CONF_HUMIDITY_SENSOR_1] == "sensor.bathroom_humidity"
    assert result["data"][CONF_HUMIDITY_SENSOR_2] == "sensor.kitchen_humidity"
    assert result["data"][CONF_HUMIDITY_SENSOR_3] == "sensor.bedroom_humidity"
    assert result["data"][CONF_HUMIDITY_SPIKE_THRESHOLD] == 2.0
    assert result["data"][CONF_ADAPTIVE_ACTIVE] is False


async def test_options_flow_defaults(hass: HomeAssistant) -> None:
    """Test the options flow with default values (minimal input at each step)."""
    entry = _build_options_entry()
    entry.add_to_hass(hass)

    # Step 1: init with defaults
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
            CONF_FREEZING_THRESHOLD: DEFAULT_FREEZING_THRESHOLD,
        },
    )

    # Step 2: extra_ventilation with defaults
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "extra_ventilation"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_EXTRA_VENT_DURATION: DEFAULT_EXTRA_VENT_DURATION,
            CONF_EXTRA_VENT_SUMMER_LEVEL: str(DEFAULT_EXTRA_VENT_SUMMER_LEVEL),
            CONF_EXTRA_VENT_WINTER_LEVEL: str(DEFAULT_EXTRA_VENT_WINTER_LEVEL),
        },
    )

    # Step 3: adaptive with defaults
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "adaptive"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_AUTO_SUMMER_BASE_LEVEL: str(DEFAULT_AUTO_SUMMER_BASE_LEVEL),
            CONF_AUTO_WINTER_BASE_LEVEL: str(DEFAULT_AUTO_WINTER_BASE_LEVEL),
            CONF_HUMIDITY_SPIKE_THRESHOLD: DEFAULT_HUMIDITY_SPIKE_THRESHOLD,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SCAN_INTERVAL] == DEFAULT_SCAN_INTERVAL
    assert result["data"][CONF_FREEZING_THRESHOLD] == DEFAULT_FREEZING_THRESHOLD
    assert result["data"][CONF_ADAPTIVE_ACTIVE] is False


async def test_options_flow_preserves_adaptive_active(hass: HomeAssistant) -> None:
    """Test that the internal CONF_ADAPTIVE_ACTIVE flag is preserved across options updates."""
    entry = _build_options_entry(**{CONF_ADAPTIVE_ACTIVE: True})
    entry.add_to_hass(hass)

    # Step 1
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
            CONF_FREEZING_THRESHOLD: DEFAULT_FREEZING_THRESHOLD,
        },
    )

    # Step 2
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_EXTRA_VENT_DURATION: DEFAULT_EXTRA_VENT_DURATION,
            CONF_EXTRA_VENT_SUMMER_LEVEL: str(DEFAULT_EXTRA_VENT_SUMMER_LEVEL),
            CONF_EXTRA_VENT_WINTER_LEVEL: str(DEFAULT_EXTRA_VENT_WINTER_LEVEL),
        },
    )

    # Step 3
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_AUTO_SUMMER_BASE_LEVEL: str(DEFAULT_AUTO_SUMMER_BASE_LEVEL),
            CONF_AUTO_WINTER_BASE_LEVEL: str(DEFAULT_AUTO_WINTER_BASE_LEVEL),
            CONF_HUMIDITY_SPIKE_THRESHOLD: DEFAULT_HUMIDITY_SPIKE_THRESHOLD,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The internal flag should be preserved from the existing entry options
    assert result["data"][CONF_ADAPTIVE_ACTIVE] is True


async def test_options_flow_scan_interval_min_max(hass: HomeAssistant) -> None:
    """Test that scan_interval at the boundary values (45, 300) are accepted."""
    # Test minimum
    entry = _build_options_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: MIN_SCAN_INTERVAL,
            CONF_FREEZING_THRESHOLD: DEFAULT_FREEZING_THRESHOLD,
        },
    )
    # Should proceed to extra_ventilation (not show error)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "extra_ventilation"

    # Now test maximum in a separate flow
    entry2 = _build_options_entry()
    entry2.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry2.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: MAX_SCAN_INTERVAL,
            CONF_FREEZING_THRESHOLD: DEFAULT_FREEZING_THRESHOLD,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "extra_ventilation"


async def test_options_flow_scan_interval_too_low(hass: HomeAssistant) -> None:
    """Test that scan_interval below minimum shows error.

    The NumberSelector schema enforces min/max, so this branch is defensive
    code.  We init the options flow normally, then call the step handler
    directly to bypass schema validation.
    """
    entry = _build_options_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    # Get the live flow handler and call async_step_init directly
    flow = hass.config_entries.options._progress[result["flow_id"]]
    result = await flow.async_step_init(
        {
            CONF_SCAN_INTERVAL: MIN_SCAN_INTERVAL - 1,
            CONF_FREEZING_THRESHOLD: DEFAULT_FREEZING_THRESHOLD,
        }
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "scan_interval_out_of_range"}


async def test_options_flow_scan_interval_too_high(hass: HomeAssistant) -> None:
    """Test that scan_interval above maximum shows error.

    The NumberSelector schema enforces min/max, so this branch is defensive
    code.  We init the options flow normally, then call the step handler
    directly to bypass schema validation.
    """
    entry = _build_options_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    flow = hass.config_entries.options._progress[result["flow_id"]]
    result = await flow.async_step_init(
        {
            CONF_SCAN_INTERVAL: MAX_SCAN_INTERVAL + 1,
            CONF_FREEZING_THRESHOLD: DEFAULT_FREEZING_THRESHOLD,
        }
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "scan_interval_out_of_range"}


async def test_options_flow_init_transition_error(hass: HomeAssistant) -> None:
    """Test that an exception during transition from init to extra_ventilation shows unknown error."""
    entry = _build_options_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    with patch(
        "custom_components.brink_ventilation.config_flow.OptionsFlowHandler.async_step_extra_ventilation",
        side_effect=RuntimeError("Transition failed"),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                CONF_FREEZING_THRESHOLD: DEFAULT_FREEZING_THRESHOLD,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "unknown"}


async def test_options_flow_init_uses_user_input_on_error(hass: HomeAssistant) -> None:
    """Test that the init form uses user_input for defaults when there's an error.

    The NumberSelector schema enforces min/max, so this branch is defensive
    code.  We init the options flow normally, then call the step handler
    directly to bypass schema validation.
    """
    entry = _build_options_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    flow = hass.config_entries.options._progress[result["flow_id"]]

    # Submit out-of-range value -- the form should be re-shown with the
    # user's submitted values as defaults (opts = user_input)
    result = await flow.async_step_init(
        {
            CONF_SCAN_INTERVAL: 10,  # too low
            CONF_FREEZING_THRESHOLD: 5.0,
        }
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "scan_interval_out_of_range"}


async def test_options_flow_extra_ventilation_empty_options_data(
    hass: HomeAssistant,
) -> None:
    """Test extra_ventilation step when _options_data is empty falls back to config_entry.options.

    This covers the defensive fallback on line 378-379 where _options_data
    is empty.  We init the options flow, then call async_step_extra_ventilation
    directly (bypassing step 1) to trigger the fallback.
    """
    entry = _build_options_entry()
    entry.add_to_hass(hass)

    # Init the flow to get a valid handler, then clear _options_data and
    # call extra_ventilation directly.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    flow = hass.config_entries.options._progress[result["flow_id"]]
    flow._options_data = {}  # force empty to trigger the fallback branch

    # Calling with no user_input should show the form and populate _options_data
    result = await flow.async_step_extra_ventilation(user_input=None)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "extra_ventilation"
    # _options_data should now be populated from config_entry.options
    assert flow._options_data == dict(entry.options)


async def test_options_flow_adaptive_preserves_false_adaptive_active(
    hass: HomeAssistant,
) -> None:
    """Test that adaptive step preserves CONF_ADAPTIVE_ACTIVE=False when not set."""
    entry = _build_options_entry()
    entry.add_to_hass(hass)

    # Walk through all 3 steps
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
            CONF_FREEZING_THRESHOLD: DEFAULT_FREEZING_THRESHOLD,
        },
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_EXTRA_VENT_DURATION: DEFAULT_EXTRA_VENT_DURATION,
            CONF_EXTRA_VENT_SUMMER_LEVEL: str(DEFAULT_EXTRA_VENT_SUMMER_LEVEL),
            CONF_EXTRA_VENT_WINTER_LEVEL: str(DEFAULT_EXTRA_VENT_WINTER_LEVEL),
        },
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_AUTO_SUMMER_BASE_LEVEL: str(DEFAULT_AUTO_SUMMER_BASE_LEVEL),
            CONF_AUTO_WINTER_BASE_LEVEL: str(DEFAULT_AUTO_WINTER_BASE_LEVEL),
            CONF_HUMIDITY_SPIKE_THRESHOLD: DEFAULT_HUMIDITY_SPIKE_THRESHOLD,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ADAPTIVE_ACTIVE] is False


async def test_options_flow_adaptive_missing_adaptive_active_key(
    hass: HomeAssistant,
) -> None:
    """Test adaptive step when CONF_ADAPTIVE_ACTIVE is absent from entry options defaults to False."""
    # Create entry without the adaptive_active key in options
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_EMAIL,
        data={CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
        options={
            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
            CONF_FREEZING_THRESHOLD: DEFAULT_FREEZING_THRESHOLD,
            CONF_EXTRA_VENT_DURATION: DEFAULT_EXTRA_VENT_DURATION,
            CONF_EXTRA_VENT_SUMMER_LEVEL: str(DEFAULT_EXTRA_VENT_SUMMER_LEVEL),
            CONF_EXTRA_VENT_WINTER_LEVEL: str(DEFAULT_EXTRA_VENT_WINTER_LEVEL),
            CONF_AUTO_SUMMER_BASE_LEVEL: str(DEFAULT_AUTO_SUMMER_BASE_LEVEL),
            CONF_AUTO_WINTER_BASE_LEVEL: str(DEFAULT_AUTO_WINTER_BASE_LEVEL),
            CONF_HUMIDITY_SPIKE_THRESHOLD: DEFAULT_HUMIDITY_SPIKE_THRESHOLD,
            # Deliberately omitting CONF_ADAPTIVE_ACTIVE
        },
        unique_id=TEST_EMAIL.lower(),
    )
    entry.add_to_hass(hass)

    # Walk through all 3 steps
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
            CONF_FREEZING_THRESHOLD: DEFAULT_FREEZING_THRESHOLD,
        },
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_EXTRA_VENT_DURATION: DEFAULT_EXTRA_VENT_DURATION,
            CONF_EXTRA_VENT_SUMMER_LEVEL: str(DEFAULT_EXTRA_VENT_SUMMER_LEVEL),
            CONF_EXTRA_VENT_WINTER_LEVEL: str(DEFAULT_EXTRA_VENT_WINTER_LEVEL),
        },
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_AUTO_SUMMER_BASE_LEVEL: str(DEFAULT_AUTO_SUMMER_BASE_LEVEL),
            CONF_AUTO_WINTER_BASE_LEVEL: str(DEFAULT_AUTO_WINTER_BASE_LEVEL),
            CONF_HUMIDITY_SPIKE_THRESHOLD: DEFAULT_HUMIDITY_SPIKE_THRESHOLD,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # .get(CONF_ADAPTIVE_ACTIVE, False) should default to False
    assert result["data"][CONF_ADAPTIVE_ACTIVE] is False


# ---------------------------------------------------------------------------
# _async_test_credentials tests (tests the helper directly)
# ---------------------------------------------------------------------------


async def test_async_test_credentials_calls_login_and_close(
    hass: HomeAssistant,
) -> None:
    """Test that _async_test_credentials calls login() then close() on success."""
    with patch(
        "custom_components.brink_ventilation.config_flow.BrinkHomeCloud",
    ) as mock_cloud_cls:
        mock_client = mock_cloud_cls.return_value
        mock_client.login = AsyncMock()
        mock_client.close = AsyncMock()

        from custom_components.brink_ventilation.config_flow import (
            _async_test_credentials,
        )

        await _async_test_credentials(hass, TEST_EMAIL, TEST_PASSWORD)

        mock_client.login.assert_awaited_once()
        mock_client.close.assert_awaited_once()


async def test_async_test_credentials_close_called_on_failure(
    hass: HomeAssistant,
) -> None:
    """Test that _async_test_credentials calls close() even when login() raises."""
    with patch(
        "custom_components.brink_ventilation.config_flow.BrinkHomeCloud",
    ) as mock_cloud_cls:
        mock_client = mock_cloud_cls.return_value
        mock_client.login = AsyncMock(
            side_effect=BrinkAuthError("fail", is_credentials_error=True)
        )
        mock_client.close = AsyncMock()

        from custom_components.brink_ventilation.config_flow import (
            _async_test_credentials,
        )

        with pytest.raises(BrinkAuthError):
            await _async_test_credentials(hass, TEST_EMAIL, TEST_PASSWORD)

        mock_client.close.assert_awaited_once()
