"""Fixtures for Brink HRV Control tests."""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Generator
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.core import HomeAssistant


import threading

if sys.platform == "win32":
    import socket as _socket

    import pytest_socket

    # On Windows there is no AF_UNIX, so asyncio's self-pipe uses AF_INET
    # socketpair.  The HA test harness (pytest-socket with allow_unix_socket)
    # blocks ALL non-Unix sockets, breaking event-loop creation.
    # Fix: monkey-patch disable_socket to allow AF_INET sockets too, so the
    # event loop's internal self-pipe can be created.
    _orig_disable = pytest_socket.disable_socket

    def _win_disable_socket(allow_unix_socket: bool = False) -> None:
        """Windows-safe socket guard: allow AF_INET (needed by event loop self-pipe)."""

        class WinGuardedSocket(_socket.socket):
            """Allow AF_INET/AF_INET6 for internal asyncio use on Windows."""

            def __new__(cls, family: int = -1, type: int = -1, proto: int = -1, fileno: object = None):  # noqa: A002
                return super().__new__(cls, family, type, proto, fileno)

        _socket.socket = WinGuardedSocket  # type: ignore[misc]

    pytest_socket.disable_socket = _win_disable_socket

    # Also force SelectorEventLoop to avoid ProactorEventLoop issues.
    asyncio.get_event_loop_policy()._loop_factory = asyncio.SelectorEventLoop  # type: ignore[attr-defined]

    pass  # pycares/aiodns thread workaround is handled by the fixture below

from custom_components.brink_ventilation.const import (
    CONF_ADAPTIVE_ACTIVE,
    CONF_AUTO_SUMMER_BASE_LEVEL,
    CONF_AUTO_WINTER_BASE_LEVEL,
    CONF_EXTRA_VENT_DURATION,
    CONF_EXTRA_VENT_SUMMER_LEVEL,
    CONF_EXTRA_VENT_WINTER_LEVEL,
    CONF_FREEZING_THRESHOLD,
    CONF_HUMIDITY_SENSOR_1,
    CONF_HUMIDITY_SPIKE_THRESHOLD,
    CONF_INDOOR_TEMPERATURE_ENTITY_1,
    DEFAULT_AUTO_SUMMER_BASE_LEVEL,
    DEFAULT_AUTO_WINTER_BASE_LEVEL,
    DEFAULT_EXTRA_VENT_DURATION,
    DEFAULT_EXTRA_VENT_SUMMER_LEVEL,
    DEFAULT_EXTRA_VENT_WINTER_LEVEL,
    DEFAULT_FREEZING_THRESHOLD,
    DEFAULT_HUMIDITY_SPIKE_THRESHOLD,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PARAM_BYPASS_OPERATION,
    PARAM_BYPASS_VALVE_STATUS,
    PARAM_CO2_SENSOR_1,
    PARAM_DAYS_SINCE_FILTER_RESET,
    PARAM_EXHAUST_AIR_FLOW,
    PARAM_EXHAUST_TEMP,
    PARAM_FILTER_STATUS,
    PARAM_FRESH_AIR_TEMP,
    PARAM_HUMIDITY,
    PARAM_OPERATING_MODE,
    PARAM_PREHEATER_STATUS,
    PARAM_REMAINING_DURATION,
    PARAM_SUPPLY_AIR_FLOW,
    PARAM_SUPPLY_TEMP,
    PARAM_VENTILATION_LEVEL,
    PARAM_ACTIVE_CONTROL_STATUS,
    PARAM_DEVICE_TYPE,
    PARAM_SOFTWARE_LABEL,
)

from pytest_homeassistant_custom_component.common import MockConfigEntry


SYSTEM_ID = 13090
GATEWAY_ID = 86891
COMPONENT_ID = 13107

TEST_EMAIL = "test@example.com"
TEST_PASSWORD = "testpassword123"


def _make_param(
    param_id: str,
    value: str,
    value_id: int,
    *,
    read_write: bool = False,
) -> dict[str, Any]:
    """Create a parameter dict matching the API uidescription shape."""
    return {
        "value": value,
        "value_id": value_id,
        "read_write": read_write,
    }


def build_coordinator_data(
    *,
    ventilation_level: str = "2",
    operating_mode: str = "1",
    fresh_air_temp: str = "15.0",
    supply_temp: str = "20.0",
    exhaust_temp: str = "21.0",
    supply_air_flow: str = "200",
    exhaust_air_flow: str = "195",
    filter_status: str = "0",
    bypass_valve_status: str = "4",
    humidity: str = "55",
    co2_sensor_1: str = "450",
    days_since_filter_reset: str = "30",
    remaining_duration: str = "0",
    active_control_status: str = "4",
    preheater_status: str = "1",
    bypass_operation: str = "0",
) -> dict[int, dict[str, Any]]:
    """Build mock coordinator data matching the real API structure.

    Returns: {system_id: {gateway_id: ..., components: [...]}}
    """
    parameters = {
        PARAM_VENTILATION_LEVEL: _make_param(
            PARAM_VENTILATION_LEVEL, ventilation_level, 1001, read_write=True
        ),
        PARAM_OPERATING_MODE: _make_param(
            PARAM_OPERATING_MODE, operating_mode, 1002, read_write=True
        ),
        PARAM_FRESH_AIR_TEMP: _make_param(PARAM_FRESH_AIR_TEMP, fresh_air_temp, 1003),
        PARAM_SUPPLY_TEMP: _make_param(PARAM_SUPPLY_TEMP, supply_temp, 1004),
        PARAM_EXHAUST_TEMP: _make_param(PARAM_EXHAUST_TEMP, exhaust_temp, 1005),
        PARAM_SUPPLY_AIR_FLOW: _make_param(PARAM_SUPPLY_AIR_FLOW, supply_air_flow, 1006),
        PARAM_EXHAUST_AIR_FLOW: _make_param(
            PARAM_EXHAUST_AIR_FLOW, exhaust_air_flow, 1007
        ),
        PARAM_FILTER_STATUS: _make_param(PARAM_FILTER_STATUS, filter_status, 1008),
        PARAM_BYPASS_VALVE_STATUS: _make_param(
            PARAM_BYPASS_VALVE_STATUS, bypass_valve_status, 1009
        ),
        PARAM_HUMIDITY: _make_param(PARAM_HUMIDITY, humidity, 1010),
        PARAM_CO2_SENSOR_1: _make_param(PARAM_CO2_SENSOR_1, co2_sensor_1, 1011),
        PARAM_DAYS_SINCE_FILTER_RESET: _make_param(
            PARAM_DAYS_SINCE_FILTER_RESET, days_since_filter_reset, 1012
        ),
        PARAM_REMAINING_DURATION: _make_param(
            PARAM_REMAINING_DURATION, remaining_duration, 1013
        ),
        PARAM_ACTIVE_CONTROL_STATUS: _make_param(
            PARAM_ACTIVE_CONTROL_STATUS, active_control_status, 1014
        ),
        PARAM_PREHEATER_STATUS: _make_param(
            PARAM_PREHEATER_STATUS, preheater_status, 1015
        ),
        PARAM_BYPASS_OPERATION: _make_param(
            PARAM_BYPASS_OPERATION, bypass_operation, 1016, read_write=True
        ),
        PARAM_DEVICE_TYPE: _make_param(PARAM_DEVICE_TYPE, "Flair 325", 1017),
        PARAM_SOFTWARE_LABEL: _make_param(
            PARAM_SOFTWARE_LABEL, "S3.01.07", 1018
        ),
    }

    return {
        SYSTEM_ID: {
            "gateway_id": GATEWAY_ID,
            "system_name": "Test Brink System",
            "components": [
                {
                    "component_id": COMPONENT_ID,
                    "parameters": parameters,
                }
            ],
        }
    }


if sys.platform == "win32":

    @pytest.fixture(autouse=True)
    def _reclassify_pycares_threads() -> Generator[None, None, None]:
        """Mark pycares/aiodns daemon threads as _DummyThread before HA teardown.

        On Windows, pycares/aiodns spawns ``_run_safe_shutdown_loop`` daemon
        threads that outlive the test.  The HA test harness ``verify_cleanup``
        fixture rejects any non-DummyThread / non-waitpid thread.  By
        reclassifying them here (in the test-level conftest teardown, which
        runs *before* the plugin-level verify_cleanup teardown) the assertion
        is satisfied.
        """
        yield
        for t in threading.enumerate():
            if t.daemon and "_run_safe_shutdown_loop" in t.name:
                t.__class__ = threading._DummyThread


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations for all tests in the test suite."""


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Create a mock config entry for the Brink integration."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Test Brink System",
        data={
            CONF_USERNAME: TEST_EMAIL,
            CONF_PASSWORD: TEST_PASSWORD,
        },
        options={
            "scan_interval": DEFAULT_SCAN_INTERVAL,
            CONF_FREEZING_THRESHOLD: DEFAULT_FREEZING_THRESHOLD,
            CONF_EXTRA_VENT_DURATION: DEFAULT_EXTRA_VENT_DURATION,
            CONF_EXTRA_VENT_SUMMER_LEVEL: DEFAULT_EXTRA_VENT_SUMMER_LEVEL,
            CONF_EXTRA_VENT_WINTER_LEVEL: DEFAULT_EXTRA_VENT_WINTER_LEVEL,
            CONF_AUTO_SUMMER_BASE_LEVEL: DEFAULT_AUTO_SUMMER_BASE_LEVEL,
            CONF_AUTO_WINTER_BASE_LEVEL: DEFAULT_AUTO_WINTER_BASE_LEVEL,
            CONF_HUMIDITY_SPIKE_THRESHOLD: DEFAULT_HUMIDITY_SPIKE_THRESHOLD,
            CONF_ADAPTIVE_ACTIVE: False,
        },
        unique_id=TEST_EMAIL,
    )


@pytest.fixture
def mock_coordinator_data() -> dict[int, dict[str, Any]]:
    """Return sample coordinator data."""
    return build_coordinator_data()


@pytest.fixture
def mock_brink_cloud() -> Generator[AsyncMock, None, None]:
    """Mock the BrinkHomeCloud API client."""
    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
    ) as mock_cls:
        client = mock_cls.return_value
        client.login = AsyncMock(return_value=None)
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
        yield client


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_brink_cloud: AsyncMock,
) -> MockConfigEntry:
    """Set up the Brink integration with mocked API client."""
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_brink_cloud,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    return mock_config_entry
