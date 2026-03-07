"""Tests for the BrinkDataCoordinator."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.brink_ventilation.const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EXPEDITED_DURATION,
    EXPEDITED_INTERVAL,
)
from custom_components.brink_ventilation.coordinator import BrinkDataCoordinator
from custom_components.brink_ventilation.core.brink_home_cloud import BrinkAuthError

from .conftest import SYSTEM_ID, build_coordinator_data

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_USERNAME = "test@example.com"
TEST_PASSWORD = "testpassword123"

MOCK_SYSTEMS = [
    {
        "system_id": SYSTEM_ID,
        "name": "Test Brink System",
        "serial_number": "430001251603",
        "gateway_state": 1,
    }
]


def _make_config_entry(
    *,
    scan_interval: int = DEFAULT_SCAN_INTERVAL,
) -> MockConfigEntry:
    """Create a MockConfigEntry for coordinator tests."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Test Brink System",
        data={
            CONF_USERNAME: TEST_USERNAME,
            CONF_PASSWORD: TEST_PASSWORD,
        },
        options={"scan_interval": scan_interval},
        unique_id=TEST_USERNAME,
    )


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
        entry = _make_config_entry()
    entry.add_to_hass(hass)

    # Mock the automation controller to avoid its full initialisation
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


# ---------------------------------------------------------------------------
# _async_update_data - success
# ---------------------------------------------------------------------------


async def test_coordinator_fetch_success(hass: HomeAssistant) -> None:
    """Test that data is returned correctly on a successful fetch."""
    mock_client = _make_mock_client()
    coordinator = await _setup_coordinator(hass, mock_client)

    data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(data)

    assert coordinator.data is not None
    assert SYSTEM_ID in coordinator.data
    assert coordinator.data[SYSTEM_ID]["system_id"] == SYSTEM_ID
    assert coordinator.data[SYSTEM_ID]["name"] == "Test Brink System"
    mock_client.get_systems.assert_awaited_once()
    mock_client.get_device_data.assert_awaited_once_with(SYSTEM_ID)


# ---------------------------------------------------------------------------
# _async_update_data - error paths
# ---------------------------------------------------------------------------


async def test_coordinator_fetch_auth_error(hass: HomeAssistant) -> None:
    """Test that BrinkAuthError raises ConfigEntryAuthFailed."""
    mock_client = _make_mock_client(
        get_systems_side_effect=BrinkAuthError(
            "Token expired", is_credentials_error=False
        ),
    )
    coordinator = await _setup_coordinator(hass, mock_client)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_coordinator_fetch_client_error(hass: HomeAssistant) -> None:
    """Test that aiohttp.ClientError raises UpdateFailed."""
    mock_client = _make_mock_client(
        get_systems_side_effect=aiohttp.ClientError("Connection reset"),
    )
    coordinator = await _setup_coordinator(hass, mock_client)

    with pytest.raises(UpdateFailed, match="Connection error"):
        await coordinator._async_update_data()


async def test_coordinator_fetch_timeout(hass: HomeAssistant) -> None:
    """Test that asyncio.TimeoutError raises UpdateFailed."""
    mock_client = _make_mock_client(
        get_systems_side_effect=asyncio.TimeoutError(),
    )
    coordinator = await _setup_coordinator(hass, mock_client)

    with pytest.raises(UpdateFailed, match="Connection error"):
        await coordinator._async_update_data()


async def test_coordinator_fetch_server_error(hass: HomeAssistant) -> None:
    """Test that ClientResponseError(status=500) raises UpdateFailed."""
    mock_client = _make_mock_client(
        get_systems_side_effect=aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=500,
            message="Internal Server Error",
        ),
    )
    coordinator = await _setup_coordinator(hass, mock_client)

    with pytest.raises(UpdateFailed, match="HTTP 500"):
        await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# 401 retry logic
# ---------------------------------------------------------------------------


async def test_coordinator_401_retry_success(hass: HomeAssistant) -> None:
    """Test that a 401 triggers re-login and a successful retry."""
    mock_client = _make_mock_client()

    # First call to get_systems raises 401, second succeeds
    mock_client.get_systems = AsyncMock(
        side_effect=[
            aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=401,
                message="Unauthorized",
            ),
            MOCK_SYSTEMS,
        ]
    )
    # get_device_data always succeeds (called after the retry)
    mock_client.get_device_data = AsyncMock(
        return_value=build_coordinator_data()[SYSTEM_ID]
    )

    coordinator = await _setup_coordinator(hass, mock_client)
    data = await coordinator._async_update_data()

    assert SYSTEM_ID in data
    mock_client.login.assert_awaited_once()
    # get_systems called twice (initial 401 + retry)
    assert mock_client.get_systems.await_count == 2


async def test_coordinator_401_retry_fails_auth(hass: HomeAssistant) -> None:
    """Test that 401 + re-login BrinkAuthError raises ConfigEntryAuthFailed."""
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

    with pytest.raises(ConfigEntryAuthFailed, match="Re-authentication failed"):
        await coordinator._async_update_data()

    mock_client.login.assert_awaited_once()


async def test_coordinator_401_retry_still_401(hass: HomeAssistant) -> None:
    """Test that 401 + login OK + still 401 raises ConfigEntryAuthFailed."""
    mock_client = _make_mock_client()

    # Both get_systems calls return 401
    mock_client.get_systems = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=401,
            message="Unauthorized",
        ),
    )

    coordinator = await _setup_coordinator(hass, mock_client)

    with pytest.raises(ConfigEntryAuthFailed, match="HTTP 401"):
        await coordinator._async_update_data()

    mock_client.login.assert_awaited_once()


async def test_coordinator_401_retry_connection_error(
    hass: HomeAssistant,
) -> None:
    """Test that 401 + connection error during retry raises UpdateFailed."""
    mock_client = _make_mock_client()

    # First get_systems returns 401, second raises connection error
    mock_client.get_systems = AsyncMock(
        side_effect=[
            aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=401,
                message="Unauthorized",
            ),
            aiohttp.ClientError("Network down"),
        ]
    )

    coordinator = await _setup_coordinator(hass, mock_client)

    with pytest.raises(UpdateFailed, match="Connection lost during re-auth"):
        await coordinator._async_update_data()


async def test_coordinator_401_retry_server_error(hass: HomeAssistant) -> None:
    """Test that 401 + 500 during retry raises UpdateFailed."""
    mock_client = _make_mock_client()

    # First get_systems returns 401, second returns 500
    mock_client.get_systems = AsyncMock(
        side_effect=[
            aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=401,
                message="Unauthorized",
            ),
            aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=500,
                message="Internal Server Error",
            ),
        ]
    )

    coordinator = await _setup_coordinator(hass, mock_client)

    with pytest.raises(UpdateFailed, match="API error during re-auth"):
        await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# Expedited polling
# ---------------------------------------------------------------------------


async def test_expedited_polling_start(hass: HomeAssistant) -> None:
    """Test that start_expedited_polling reduces interval to EXPEDITED_INTERVAL."""
    mock_client = _make_mock_client()
    coordinator = await _setup_coordinator(hass, mock_client)

    original_interval = coordinator.update_interval
    assert original_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL)

    coordinator.start_expedited_polling()

    assert coordinator.update_interval == timedelta(seconds=EXPEDITED_INTERVAL)
    # The original interval should be saved for restoration
    assert coordinator._expedited_normal_interval == original_interval
    assert coordinator._expedited_unsub is not None

    # Clean up to avoid lingering timer
    coordinator.cancel_expedited_polling()


async def test_expedited_polling_restore(hass: HomeAssistant) -> None:
    """Test that after EXPEDITED_DURATION, interval is restored to normal."""
    mock_client = _make_mock_client()
    coordinator = await _setup_coordinator(hass, mock_client)

    original_interval = coordinator.update_interval
    coordinator.start_expedited_polling()

    assert coordinator.update_interval == timedelta(seconds=EXPEDITED_INTERVAL)

    # Advance time past the expedited duration to trigger the restore callback
    async_fire_time_changed(
        hass, utcnow() + timedelta(seconds=EXPEDITED_DURATION + 1)
    )
    await hass.async_block_till_done()

    assert coordinator.update_interval == original_interval
    assert coordinator._expedited_normal_interval is None
    assert coordinator._expedited_unsub is None


async def test_expedited_polling_cancel(hass: HomeAssistant) -> None:
    """Test that cancel_expedited_polling clears the timer state."""
    mock_client = _make_mock_client()
    coordinator = await _setup_coordinator(hass, mock_client)

    coordinator.start_expedited_polling()
    assert coordinator.update_interval == timedelta(seconds=EXPEDITED_INTERVAL)

    coordinator.cancel_expedited_polling()

    # After cancel, the unsub and saved interval should be cleared
    assert coordinator._expedited_unsub is None
    assert coordinator._expedited_normal_interval is None


async def test_expedited_polling_multiple_calls_reset_timer(
    hass: HomeAssistant,
) -> None:
    """Test that calling start_expedited_polling twice resets the timer."""
    mock_client = _make_mock_client()
    coordinator = await _setup_coordinator(hass, mock_client)

    coordinator.start_expedited_polling()
    first_unsub = coordinator._expedited_unsub

    coordinator.start_expedited_polling()
    second_unsub = coordinator._expedited_unsub

    # The unsub callback should be different (old one cancelled, new one set)
    assert second_unsub is not first_unsub
    assert coordinator.update_interval == timedelta(seconds=EXPEDITED_INTERVAL)

    # Clean up to avoid lingering timer
    coordinator.cancel_expedited_polling()


# ---------------------------------------------------------------------------
# Dynamic scan interval update
# ---------------------------------------------------------------------------


async def test_update_scan_interval(hass: HomeAssistant) -> None:
    """Test that async_update_scan_interval changes the interval."""
    mock_client = _make_mock_client()
    coordinator = await _setup_coordinator(hass, mock_client)

    assert coordinator.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL)

    # Patch async_request_refresh to avoid lingering scheduled timers
    with patch.object(coordinator, "async_request_refresh", new_callable=AsyncMock):
        await coordinator.async_update_scan_interval(120)

    assert coordinator.update_interval == timedelta(seconds=120)


async def test_update_scan_interval_during_expedited(
    hass: HomeAssistant,
) -> None:
    """Test that scan interval change during expedited polling is deferred."""
    mock_client = _make_mock_client()
    coordinator = await _setup_coordinator(hass, mock_client)

    coordinator.start_expedited_polling()
    assert coordinator.update_interval == timedelta(seconds=EXPEDITED_INTERVAL)

    # Change the interval while expedited polling is active
    await coordinator.async_update_scan_interval(120)

    # The current interval should remain expedited
    assert coordinator.update_interval == timedelta(seconds=EXPEDITED_INTERVAL)
    # But the saved normal interval should be updated
    assert coordinator._expedited_normal_interval == timedelta(seconds=120)

    # Clean up to avoid lingering timer
    coordinator.cancel_expedited_polling()


# ---------------------------------------------------------------------------
# Automation controller callback
# ---------------------------------------------------------------------------


async def test_automation_callback_called(hass: HomeAssistant) -> None:
    """Test that the coordinator calls automation controller on update."""
    mock_client = _make_mock_client()
    coordinator = await _setup_coordinator(hass, mock_client)

    await coordinator._async_update_data()

    coordinator.automation_controller.async_on_coordinator_update.assert_awaited_once()


async def test_automation_callback_not_called_on_error(
    hass: HomeAssistant,
) -> None:
    """Test that automation controller is NOT called when fetch fails."""
    mock_client = _make_mock_client(
        get_systems_side_effect=aiohttp.ClientError("Offline"),
    )
    coordinator = await _setup_coordinator(hass, mock_client)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    coordinator.automation_controller.async_on_coordinator_update.assert_not_awaited()


# ---------------------------------------------------------------------------
# _fetch_devices edge cases
# ---------------------------------------------------------------------------


async def test_fetch_devices_skips_missing_system_id(
    hass: HomeAssistant,
) -> None:
    """Test that systems with no system_id are skipped."""
    mock_client = _make_mock_client(
        get_systems_return=[
            {"name": "No ID System", "serial_number": ""},
        ],
    )
    coordinator = await _setup_coordinator(hass, mock_client)

    # No systems have valid system_id, but 'systems' list is non-empty
    # and 'devices' is empty => should raise UpdateFailed
    with pytest.raises(UpdateFailed, match="Failed to fetch data for all"):
        await coordinator._async_update_data()


async def test_fetch_devices_skips_failed_system(hass: HomeAssistant) -> None:
    """Test that a system whose device_data fetch fails is skipped but others succeed."""
    second_system_id = 99999
    mock_client = _make_mock_client(
        get_systems_return=[
            {
                "system_id": SYSTEM_ID,
                "name": "Good System",
                "serial_number": "111",
                "gateway_state": 1,
            },
            {
                "system_id": second_system_id,
                "name": "Bad System",
                "serial_number": "222",
                "gateway_state": 1,
            },
        ],
    )
    # First call succeeds, second raises
    mock_client.get_device_data = AsyncMock(
        side_effect=[
            build_coordinator_data()[SYSTEM_ID],
            aiohttp.ClientError("Timeout for system 99999"),
        ]
    )

    coordinator = await _setup_coordinator(hass, mock_client)
    data = await coordinator._async_update_data()

    assert SYSTEM_ID in data
    assert second_system_id not in data


async def test_fetch_devices_all_systems_fail(hass: HomeAssistant) -> None:
    """Test that UpdateFailed is raised when all device_data fetches fail."""
    mock_client = _make_mock_client(
        get_device_data_side_effect=aiohttp.ClientError("All failed"),
    )
    coordinator = await _setup_coordinator(hass, mock_client)

    with pytest.raises(UpdateFailed, match="Failed to fetch data for all"):
        await coordinator._async_update_data()


async def test_fetch_devices_empty_systems(hass: HomeAssistant) -> None:
    """Test that an empty systems list returns empty dict (no error)."""
    mock_client = _make_mock_client(get_systems_return=[])
    coordinator = await _setup_coordinator(hass, mock_client)

    data = await coordinator._async_update_data()

    assert data == {}
