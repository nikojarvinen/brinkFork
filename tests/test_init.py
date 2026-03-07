"""Tests for the Brink HRV Control integration setup, unload, and migrations."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.brink_ventilation import (
    PLATFORMS,
    _migrate_device_identifiers,
    _migrate_options,
    async_remove_config_entry_device,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.brink_ventilation.const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from custom_components.brink_ventilation.core.brink_home_cloud import BrinkAuthError

from .conftest import SYSTEM_ID, build_coordinator_data

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_USERNAME = "test@example.com"
TEST_PASSWORD = "testpassword123"


def _make_config_entry(
    *,
    scan_interval: int = DEFAULT_SCAN_INTERVAL,
    options_extra: dict[str, Any] | None = None,
) -> MockConfigEntry:
    """Create a MockConfigEntry using CONF_USERNAME (as __init__.py expects)."""
    opts: dict[str, Any] = {"scan_interval": scan_interval}
    if options_extra:
        opts.update(options_extra)
    return MockConfigEntry(
        domain=DOMAIN,
        title="Test Brink System",
        data={
            CONF_USERNAME: TEST_USERNAME,
            CONF_PASSWORD: TEST_PASSWORD,
        },
        options=opts,
        unique_id=TEST_USERNAME,
    )


def _make_mock_client(
    *,
    login_side_effect: Exception | None = None,
) -> AsyncMock:
    """Return a fully mocked BrinkHomeCloud instance."""
    client = AsyncMock()
    client.login = AsyncMock(side_effect=login_side_effect)
    client.get_systems = AsyncMock(
        return_value=[
            {
                "system_id": SYSTEM_ID,
                "name": "Test Brink System",
                "serial_number": "430001251603",
                "gateway_state": 1,
            }
        ]
    )
    client.get_device_data = AsyncMock(
        return_value=build_coordinator_data()[SYSTEM_ID]
    )
    client.write_parameters = AsyncMock(return_value=None)
    client.close = AsyncMock(return_value=None)
    return client


# ---------------------------------------------------------------------------
# async_setup_entry
# ---------------------------------------------------------------------------


async def test_setup_entry_success(hass: HomeAssistant) -> None:
    """Test that a normal setup results in LOADED state."""
    entry = _make_config_entry()
    entry.add_to_hass(hass)
    mock_client = _make_mock_client()

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_client,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    mock_client.login.assert_awaited_once()


async def test_setup_entry_auth_failure(hass: HomeAssistant) -> None:
    """Test that BrinkAuthError during login raises ConfigEntryAuthFailed."""
    entry = _make_config_entry()
    entry.add_to_hass(hass)
    mock_client = _make_mock_client(
        login_side_effect=BrinkAuthError(
            "Invalid credentials", is_credentials_error=True
        ),
    )

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_client,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    mock_client.close.assert_awaited_once()


async def test_setup_entry_connection_error(hass: HomeAssistant) -> None:
    """Test that aiohttp.ClientError during login raises ConfigEntryNotReady."""
    entry = _make_config_entry()
    entry.add_to_hass(hass)
    mock_client = _make_mock_client(
        login_side_effect=aiohttp.ClientError("Connection refused"),
    )

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_client,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    mock_client.close.assert_awaited_once()


async def test_setup_entry_timeout_error(hass: HomeAssistant) -> None:
    """Test that TimeoutError during login raises ConfigEntryNotReady."""
    entry = _make_config_entry()
    entry.add_to_hass(hass)
    mock_client = _make_mock_client(
        login_side_effect=TimeoutError("Timed out"),
    )

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_client,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    mock_client.close.assert_awaited_once()


async def test_setup_entry_http_401_error(hass: HomeAssistant) -> None:
    """Test that ClientResponseError(401) during login raises ConfigEntryAuthFailed."""
    entry = _make_config_entry()
    entry.add_to_hass(hass)
    mock_client = _make_mock_client(
        login_side_effect=aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=401,
            message="Unauthorized",
        ),
    )

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_client,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    mock_client.close.assert_awaited_once()


async def test_setup_entry_http_500_error(hass: HomeAssistant) -> None:
    """Test that ClientResponseError(500) during login raises ConfigEntryNotReady."""
    entry = _make_config_entry()
    entry.add_to_hass(hass)
    mock_client = _make_mock_client(
        login_side_effect=aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=500,
            message="Internal Server Error",
        ),
    )

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_client,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    mock_client.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# async_unload_entry
# ---------------------------------------------------------------------------


async def test_unload_entry(hass: HomeAssistant) -> None:
    """Test that unloading cleans up platforms and closes the client."""
    entry = _make_config_entry()
    entry.add_to_hass(hass)
    mock_client = _make_mock_client()

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_client,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    mock_client.close.assert_awaited()


# ---------------------------------------------------------------------------
# _migrate_options
# ---------------------------------------------------------------------------


async def test_migrate_scan_interval_below_minimum(hass: HomeAssistant) -> None:
    """Test that scan_interval below MIN_SCAN_INTERVAL is clamped."""
    entry = _make_config_entry(scan_interval=20)
    entry.add_to_hass(hass)

    _migrate_options(hass, entry)

    assert entry.options[CONF_SCAN_INTERVAL] == MIN_SCAN_INTERVAL


async def test_migrate_scan_interval_above_minimum(hass: HomeAssistant) -> None:
    """Test that scan_interval at or above MIN_SCAN_INTERVAL is unchanged."""
    entry = _make_config_entry(scan_interval=60)
    entry.add_to_hass(hass)

    _migrate_options(hass, entry)

    assert entry.options[CONF_SCAN_INTERVAL] == 60


async def test_migrate_scan_interval_at_minimum(hass: HomeAssistant) -> None:
    """Test that scan_interval exactly at MIN_SCAN_INTERVAL is unchanged."""
    entry = _make_config_entry(scan_interval=MIN_SCAN_INTERVAL)
    entry.add_to_hass(hass)

    _migrate_options(hass, entry)

    assert entry.options[CONF_SCAN_INTERVAL] == MIN_SCAN_INTERVAL


async def test_migrate_scan_interval_not_present(hass: HomeAssistant) -> None:
    """Test that missing scan_interval in options does not cause errors."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test Brink System",
        data={
            CONF_USERNAME: TEST_USERNAME,
            CONF_PASSWORD: TEST_PASSWORD,
        },
        options={},
        unique_id=TEST_USERNAME,
    )
    entry.add_to_hass(hass)

    # Should not raise
    _migrate_options(hass, entry)

    assert CONF_SCAN_INTERVAL not in entry.options


# ---------------------------------------------------------------------------
# _migrate_device_identifiers
# ---------------------------------------------------------------------------


async def test_migrate_device_identifiers_3tuple(hass: HomeAssistant) -> None:
    """Test that 3-tuple device identifiers are migrated to 2-tuple format."""
    entry = _make_config_entry()
    entry.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    # Create a device with old 3-tuple identifier: (DOMAIN, system_id, gateway_id)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "13090", "86891")},
        name="Test Device",
    )

    _migrate_device_identifiers(hass, entry)

    # Verify the device now has 2-tuple identifiers
    devices = dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
    assert len(devices) == 1
    assert devices[0].identifiers == {(DOMAIN, "13090")}


async def test_migrate_device_identifiers_already_2tuple(
    hass: HomeAssistant,
) -> None:
    """Test that 2-tuple device identifiers are not modified."""
    entry = _make_config_entry()
    entry.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "13090")},
        name="Test Device",
    )

    _migrate_device_identifiers(hass, entry)

    devices = dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
    assert len(devices) == 1
    assert devices[0].identifiers == {(DOMAIN, "13090")}


async def test_migrate_device_identifiers_no_devices(
    hass: HomeAssistant,
) -> None:
    """Test migration when there are no devices registered."""
    entry = _make_config_entry()
    entry.add_to_hass(hass)

    # Should not raise when there are no devices
    _migrate_device_identifiers(hass, entry)

    dev_reg = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
    assert len(devices) == 0


# ---------------------------------------------------------------------------
# _async_update_listener (options update)
# ---------------------------------------------------------------------------


async def test_options_update_listener(hass: HomeAssistant) -> None:
    """Test that options change triggers coordinator interval update."""
    from datetime import timedelta

    entry = _make_config_entry(scan_interval=60)
    entry.add_to_hass(hass)
    mock_client = _make_mock_client()

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_client,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    coordinator = entry.runtime_data.coordinator

    # Update options with a new scan interval
    new_options = dict(entry.options)
    new_options[CONF_SCAN_INTERVAL] = 90
    hass.config_entries.async_update_entry(entry, options=new_options)
    await hass.async_block_till_done()

    # The coordinator's update_interval should reflect the new value
    assert coordinator.update_interval == timedelta(seconds=90)


# ---------------------------------------------------------------------------
# async_remove_config_entry_device
# ---------------------------------------------------------------------------


async def test_remove_config_entry_device_stale(hass: HomeAssistant) -> None:
    """Test that a device NOT in current data can be removed (returns True)."""
    entry = _make_config_entry()
    entry.add_to_hass(hass)
    mock_client = _make_mock_client()

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_client,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    dev_reg = dr.async_get(hass)
    # Create a device with a system_id NOT in the coordinator data
    stale_device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "99999")},
        name="Stale Device",
    )

    result = await async_remove_config_entry_device(hass, entry, stale_device)
    assert result is True


async def test_remove_config_entry_device_active(hass: HomeAssistant) -> None:
    """Test that a device IN current data cannot be removed (returns False)."""
    entry = _make_config_entry()
    entry.add_to_hass(hass)
    mock_client = _make_mock_client()

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_client,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    dev_reg = dr.async_get(hass)
    # Create a device with the SYSTEM_ID that IS in coordinator data
    active_device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, str(SYSTEM_ID))},
        name="Active Device",
    )

    result = await async_remove_config_entry_device(hass, entry, active_device)
    assert result is False
