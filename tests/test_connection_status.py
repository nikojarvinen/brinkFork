"""Tests for the connection status feature (coordinator tracking + sensor entity)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.brink_ventilation.const import (
    CONF_ADAPTIVE_ACTIVE,
    CONF_AUTO_SUMMER_BASE_LEVEL,
    CONF_AUTO_WINTER_BASE_LEVEL,
    CONF_EXTRA_VENT_DURATION,
    CONF_EXTRA_VENT_SUMMER_LEVEL,
    CONF_EXTRA_VENT_WINTER_LEVEL,
    CONF_FREEZING_THRESHOLD,
    CONF_HUMIDITY_SPIKE_THRESHOLD,
    CONNECTION_STATUS_AUTH_ERROR,
    CONNECTION_STATUS_CONNECTED,
    CONNECTION_STATUS_CONNECTION_ERROR,
    CONNECTION_STATUS_NO_INTERNET,
    CONNECTION_STATUS_OPTIONS,
    CONNECTION_STATUS_SERVER_ERROR,
    CONNECTION_STATUS_TIMEOUT,
    DEFAULT_AUTO_SUMMER_BASE_LEVEL,
    DEFAULT_AUTO_WINTER_BASE_LEVEL,
    DEFAULT_EXTRA_VENT_DURATION,
    DEFAULT_EXTRA_VENT_SUMMER_LEVEL,
    DEFAULT_EXTRA_VENT_WINTER_LEVEL,
    DEFAULT_FREEZING_THRESHOLD,
    DEFAULT_HUMIDITY_SPIKE_THRESHOLD,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.brink_ventilation.coordinator import BrinkDataCoordinator
from custom_components.brink_ventilation.core.brink_home_cloud import BrinkAuthError

from .conftest import SYSTEM_ID, TEST_EMAIL, TEST_PASSWORD, build_coordinator_data

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MOCK_SYSTEMS = [
    {
        "system_id": SYSTEM_ID,
        "name": "Test Brink System",
        "serial_number": "430001251603",
        "gateway_state": 1,
    }
]


def _make_mock_client(
    *,
    get_systems_return: list[dict[str, Any]] | None = None,
    get_device_data_return: dict[str, Any] | None = None,
    get_systems_side_effect: Exception | None = None,
    get_device_data_side_effect: Exception | None = None,
    login_side_effect: Exception | None = None,
) -> AsyncMock:
    """Return a fully mocked BrinkHomeCloud instance."""
    client = AsyncMock()
    client.login = AsyncMock(side_effect=login_side_effect)
    client._session = MagicMock()

    if get_systems_side_effect is not None:
        client.get_systems = AsyncMock(side_effect=get_systems_side_effect)
    else:
        client.get_systems = AsyncMock(
            return_value=get_systems_return
            if get_systems_return is not None
            else MOCK_SYSTEMS
        )

    if get_device_data_side_effect is not None:
        client.get_device_data = AsyncMock(side_effect=get_device_data_side_effect)
    else:
        client.get_device_data = AsyncMock(
            return_value=get_device_data_return
            if get_device_data_return is not None
            else build_coordinator_data()[SYSTEM_ID]
        )

    client.write_parameters = AsyncMock(return_value=None)
    client.close = AsyncMock(return_value=None)
    return client


async def _setup_coordinator(
    hass: HomeAssistant,
    client: AsyncMock,
    entry: MockConfigEntry | None = None,
) -> BrinkDataCoordinator:
    """Create and return a BrinkDataCoordinator with a mocked client."""
    if entry is None:
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Test Brink System",
            data={
                CONF_USERNAME: TEST_EMAIL,
                CONF_PASSWORD: TEST_PASSWORD,
            },
            options={"scan_interval": DEFAULT_SCAN_INTERVAL},
            unique_id=TEST_EMAIL,
        )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.brink_ventilation.coordinator.BrinkAutomationController",
    ) as mock_ac_cls:
        mock_ac = AsyncMock()
        mock_ac.async_on_coordinator_update = AsyncMock()
        mock_ac.async_restore_state = AsyncMock()
        mock_ac.async_start_humidity_monitoring = AsyncMock()
        mock_ac.async_cleanup = AsyncMock()
        mock_ac.async_options_updated = AsyncMock()
        mock_ac_cls.return_value = mock_ac

        coordinator = BrinkDataCoordinator(hass, client, entry)

    return coordinator


def _get_entity_id(hass: HomeAssistant, unique_id: str, platform: str = "sensor") -> str:
    """Look up entity_id from the registry by unique_id."""
    registry = er.async_get(hass)
    entry = registry.async_get_entity_id(platform, DOMAIN, unique_id)
    assert entry is not None, f"Entity with unique_id '{unique_id}' not found in registry"
    return entry


# ===========================================================================
# Coordinator connection tracking tests
# ===========================================================================


async def test_successful_poll_sets_connected(hass: HomeAssistant) -> None:
    """After a successful poll, status is 'connected', consecutive_errors is 0, and last_successful_poll is set."""
    mock_client = _make_mock_client()
    coordinator = await _setup_coordinator(hass, mock_client)

    # Initially connected, no poll yet
    assert coordinator.connection_status == CONNECTION_STATUS_CONNECTED
    assert coordinator.last_successful_poll is None

    await coordinator._async_update_data()

    assert coordinator.connection_status == CONNECTION_STATUS_CONNECTED
    assert coordinator.consecutive_errors == 0
    assert coordinator.last_successful_poll is not None
    assert isinstance(coordinator.last_successful_poll, datetime)
    assert coordinator.last_error_message is None


async def test_auth_error_sets_authentication_error(hass: HomeAssistant) -> None:
    """BrinkAuthError sets authentication_error status."""
    mock_client = _make_mock_client(
        get_systems_side_effect=BrinkAuthError("Token expired"),
    )
    coordinator = await _setup_coordinator(hass, mock_client)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    assert coordinator.connection_status == CONNECTION_STATUS_AUTH_ERROR
    assert coordinator.consecutive_errors == 1
    assert coordinator.last_error_message == "Token expired"


async def test_401_retry_fail_sets_authentication_error(hass: HomeAssistant) -> None:
    """401 that fails re-auth sets authentication_error status."""
    mock_client = _make_mock_client(
        login_side_effect=BrinkAuthError("Credentials invalid"),
    )
    mock_client.get_systems = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=401,
            message="Unauthorized",
        ),
    )

    coordinator = await _setup_coordinator(hass, mock_client)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    assert coordinator.connection_status == CONNECTION_STATUS_AUTH_ERROR
    assert coordinator.consecutive_errors == 1


async def test_server_error_sets_server_error(hass: HomeAssistant) -> None:
    """HTTP 5xx sets server_error status."""
    mock_client = _make_mock_client(
        get_systems_side_effect=aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=500,
            message="Internal Server Error",
        ),
    )
    coordinator = await _setup_coordinator(hass, mock_client)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert coordinator.connection_status == CONNECTION_STATUS_SERVER_ERROR
    assert coordinator.last_error_message == "HTTP 500: Internal Server Error"
    assert coordinator.consecutive_errors == 1


async def test_timeout_with_internet_sets_timeout(hass: HomeAssistant) -> None:
    """TimeoutError with internet check passing sets timeout status."""
    mock_client = _make_mock_client(
        get_systems_side_effect=asyncio.TimeoutError(),
    )
    coordinator = await _setup_coordinator(hass, mock_client)

    with patch.object(
        coordinator, "_check_internet_connectivity", return_value=True
    ):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    assert coordinator.connection_status == CONNECTION_STATUS_TIMEOUT
    assert coordinator.consecutive_errors == 1


async def test_timeout_without_internet_sets_no_internet(hass: HomeAssistant) -> None:
    """TimeoutError with internet check failing sets no_internet status."""
    mock_client = _make_mock_client(
        get_systems_side_effect=asyncio.TimeoutError(),
    )
    coordinator = await _setup_coordinator(hass, mock_client)

    with patch.object(
        coordinator, "_check_internet_connectivity", return_value=False
    ):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    assert coordinator.connection_status == CONNECTION_STATUS_NO_INTERNET
    assert coordinator.consecutive_errors == 1


async def test_connection_error_with_internet_sets_connection_error(
    hass: HomeAssistant,
) -> None:
    """ClientError with internet passing sets connection_error status."""
    mock_client = _make_mock_client(
        get_systems_side_effect=aiohttp.ClientError("Connection reset"),
    )
    coordinator = await _setup_coordinator(hass, mock_client)

    with patch.object(
        coordinator, "_check_internet_connectivity", return_value=True
    ):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    assert coordinator.connection_status == CONNECTION_STATUS_CONNECTION_ERROR
    assert coordinator.consecutive_errors == 1


async def test_connection_error_without_internet_sets_no_internet(
    hass: HomeAssistant,
) -> None:
    """ClientError with internet check failing sets no_internet status."""
    mock_client = _make_mock_client(
        get_systems_side_effect=aiohttp.ClientError("Connection reset"),
    )
    coordinator = await _setup_coordinator(hass, mock_client)

    with patch.object(
        coordinator, "_check_internet_connectivity", return_value=False
    ):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    assert coordinator.connection_status == CONNECTION_STATUS_NO_INTERNET
    assert coordinator.consecutive_errors == 1


async def test_consecutive_errors_increment(hass: HomeAssistant) -> None:
    """Each failed poll increments consecutive_errors."""
    mock_client = _make_mock_client(
        get_systems_side_effect=aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=503,
            message="Service Unavailable",
        ),
    )
    coordinator = await _setup_coordinator(hass, mock_client)

    assert coordinator.consecutive_errors == 0

    for expected_count in range(1, 4):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
        assert coordinator.consecutive_errors == expected_count


async def test_consecutive_errors_reset_on_success(hass: HomeAssistant) -> None:
    """Successful poll resets consecutive_errors to 0."""
    mock_client = _make_mock_client()
    coordinator = await _setup_coordinator(hass, mock_client)

    # Simulate some errors first by manually setting the counter
    coordinator.consecutive_errors = 5
    coordinator.connection_status = CONNECTION_STATUS_SERVER_ERROR
    coordinator.last_error_message = "HTTP 500: Internal Server Error"

    await coordinator._async_update_data()

    assert coordinator.consecutive_errors == 0
    assert coordinator.connection_status == CONNECTION_STATUS_CONNECTED
    assert coordinator.last_error_message is None


async def test_last_error_message_recorded(hass: HomeAssistant) -> None:
    """Error message string is stored in last_error_message."""
    mock_client = _make_mock_client(
        get_systems_side_effect=BrinkAuthError("Invalid credentials provided"),
    )
    coordinator = await _setup_coordinator(hass, mock_client)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    assert coordinator.last_error_message == "Invalid credentials provided"


async def test_last_error_message_cleared_on_success(hass: HomeAssistant) -> None:
    """Successful poll clears the last_error_message."""
    mock_client = _make_mock_client()
    coordinator = await _setup_coordinator(hass, mock_client)

    # Set an error message first
    coordinator.last_error_message = "Previous error"
    coordinator.consecutive_errors = 3

    await coordinator._async_update_data()

    assert coordinator.last_error_message is None


async def test_internet_check_success(hass: HomeAssistant) -> None:
    """Mock HEAD returning 204 should return True."""
    mock_client = _make_mock_client()
    coordinator = await _setup_coordinator(hass, mock_client)

    # Create a mock response with status 204
    mock_response = MagicMock()
    mock_response.status = 204
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.head = MagicMock(return_value=mock_response)
    mock_client._session = mock_session

    result = await coordinator._check_internet_connectivity()
    assert result is True


async def test_internet_check_failure(hass: HomeAssistant) -> None:
    """Mock HEAD raising exception should return False."""
    mock_client = _make_mock_client()
    coordinator = await _setup_coordinator(hass, mock_client)

    mock_session = MagicMock()
    mock_session.head = MagicMock(side_effect=aiohttp.ClientError("DNS failure"))
    mock_client._session = mock_session

    result = await coordinator._check_internet_connectivity()
    assert result is False


async def test_internet_check_non_204(hass: HomeAssistant) -> None:
    """Mock HEAD returning non-204 status should return False."""
    mock_client = _make_mock_client()
    coordinator = await _setup_coordinator(hass, mock_client)

    # Create a mock response with status 200 (not 204)
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.head = MagicMock(return_value=mock_response)
    mock_client._session = mock_session

    result = await coordinator._check_internet_connectivity()
    assert result is False


# ===========================================================================
# Sensor entity tests
# ===========================================================================


async def test_connection_status_sensor_exists(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Connection status sensor entity is created with correct unique_id."""
    expected_unique_id = f"{DOMAIN}_{SYSTEM_ID}_connection_status"
    entity_id = _get_entity_id(hass, expected_unique_id)
    assert entity_id is not None

    state = hass.states.get(entity_id)
    assert state is not None


async def test_connection_status_sensor_connected(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Connection status sensor shows 'connected' after successful setup."""
    entity_id = _get_entity_id(
        hass, f"{DOMAIN}_{SYSTEM_ID}_connection_status"
    )
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == CONNECTION_STATUS_CONNECTED


async def test_connection_status_sensor_always_available(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_brink_cloud: AsyncMock,
) -> None:
    """Connection status sensor available returns True even during errors."""
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_brink_cloud,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    entity_id = _get_entity_id(
        hass, f"{DOMAIN}_{SYSTEM_ID}_connection_status"
    )

    # Simulate a coordinator error by making the next poll fail
    mock_brink_cloud.get_systems = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=500,
            message="Internal Server Error",
        ),
    )

    coordinator = mock_config_entry.runtime_data.coordinator

    # Trigger a poll that will fail
    async_fire_time_changed(
        hass, utcnow() + timedelta(seconds=DEFAULT_SCAN_INTERVAL + 1)
    )
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state is not None
    # The sensor should still report a state (not "unavailable")
    # It may show "connected" from the last successful state or the error status
    assert state.state != "unavailable"


async def test_connection_status_sensor_extra_attributes(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Connection status sensor has expected extra state attributes after successful setup."""
    entity_id = _get_entity_id(
        hass, f"{DOMAIN}_{SYSTEM_ID}_connection_status"
    )
    state = hass.states.get(entity_id)
    assert state is not None

    attrs = state.attributes

    # After successful setup, last_successful_poll should be set
    assert "last_successful_poll" in attrs
    # consecutive_errors should be 0
    assert attrs["consecutive_errors"] == 0
    # last_error_message should not be present (it's None so omitted)
    assert "last_error_message" not in attrs


async def test_connection_status_sensor_attributes_on_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_brink_cloud: AsyncMock,
) -> None:
    """Connection status sensor attributes show error details after a poll failure."""
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_brink_cloud,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    entity_id = _get_entity_id(
        hass, f"{DOMAIN}_{SYSTEM_ID}_connection_status"
    )

    # First, verify initial state is connected
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == CONNECTION_STATUS_CONNECTED

    coordinator = mock_config_entry.runtime_data.coordinator

    # Now simulate a server error on the next poll
    mock_brink_cloud.get_systems = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=503,
            message="Service Unavailable",
        ),
    )

    # Trigger a coordinator update
    async_fire_time_changed(
        hass, utcnow() + timedelta(seconds=DEFAULT_SCAN_INTERVAL + 1)
    )
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state is not None

    attrs = state.attributes
    # After an error, consecutive_errors should be >= 1
    assert attrs["consecutive_errors"] >= 1
    # last_error_message should be populated
    assert "last_error_message" in attrs
    assert attrs["last_error_message"] is not None
    # last_successful_poll should still be set from the initial success
    assert "last_successful_poll" in attrs
