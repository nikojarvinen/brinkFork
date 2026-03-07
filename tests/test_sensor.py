"""Tests for Brink Home Ventilation sensor entities."""
from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er

from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.brink_ventilation.const import (
    ACTIVE_CONTROL_STATUS_MAP,
    BYPASS_VALVE_STATUS_MAP,
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
    CONF_INDOOR_TEMPERATURE_ENTITY_2,
    DEFAULT_AUTO_SUMMER_BASE_LEVEL,
    DEFAULT_AUTO_WINTER_BASE_LEVEL,
    DEFAULT_EXTRA_VENT_DURATION,
    DEFAULT_EXTRA_VENT_SUMMER_LEVEL,
    DEFAULT_EXTRA_VENT_WINTER_LEVEL,
    DEFAULT_FREEZING_THRESHOLD,
    DEFAULT_HUMIDITY_SPIKE_THRESHOLD,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PARAM_BYPASS_VALVE_STATUS,
    PARAM_FRESH_AIR_TEMP,
    PARAM_SUPPLY_TEMP,
    PREHEATER_STATUS_MAP,
    SEASON_SUMMER,
    SEASON_WINTER,
)
from tests.conftest import SYSTEM_ID, TEST_EMAIL, TEST_PASSWORD, build_coordinator_data


def _get_entity_id(hass: HomeAssistant, unique_id: str, platform: str = "sensor") -> str:
    """Look up entity_id from the registry by unique_id."""
    registry = er.async_get(hass)
    entry = registry.async_get_entity_id(platform, DOMAIN, unique_id)
    assert entry is not None, f"Entity with unique_id '{unique_id}' not found in registry"
    return entry


async def test_supply_airflow_sensor(hass: HomeAssistant, setup_integration) -> None:
    """Test supply airflow sensor reports correct value and unit."""
    entity_id = _get_entity_id(hass, f"{DOMAIN}_{SYSTEM_ID}_supply_air_flow")
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "200.0"
    assert state.attributes.get("unit_of_measurement") == "m\u00b3/h"


async def test_exhaust_airflow_sensor(hass: HomeAssistant, setup_integration) -> None:
    """Test exhaust airflow sensor reports correct value and unit."""
    entity_id = _get_entity_id(hass, f"{DOMAIN}_{SYSTEM_ID}_exhaust_air_flow")
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "195.0"
    assert state.attributes.get("unit_of_measurement") == "m\u00b3/h"


async def test_fresh_air_temp_sensor(hass: HomeAssistant, setup_integration) -> None:
    """Test fresh air temperature sensor reports Celsius value."""
    entity_id = _get_entity_id(hass, f"{DOMAIN}_{SYSTEM_ID}_fresh_air_temp")
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "15.0"
    assert state.attributes.get("unit_of_measurement") == "\u00b0C"


async def test_supply_temp_sensor(hass: HomeAssistant, setup_integration) -> None:
    """Test supply temperature sensor reports Celsius value."""
    entity_id = _get_entity_id(hass, f"{DOMAIN}_{SYSTEM_ID}_supply_temp")
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "20.0"
    assert state.attributes.get("unit_of_measurement") == "\u00b0C"


async def test_filter_days_sensor(hass: HomeAssistant, setup_integration) -> None:
    """Test filter days sensor reports correct value in days."""
    entity_id = _get_entity_id(hass, f"{DOMAIN}_{SYSTEM_ID}_days_since_filter_reset")
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "30.0"
    assert state.attributes.get("unit_of_measurement") == "d"


async def test_bypass_valve_status_enum(hass: HomeAssistant, setup_integration) -> None:
    """Test bypass valve status enum maps value '4' to 'closed'."""
    entity_id = _get_entity_id(hass, f"{DOMAIN}_{SYSTEM_ID}_bypass_valve_status")
    state = hass.states.get(entity_id)
    assert state is not None
    # Default bypass_valve_status in build_coordinator_data is "4" -> "closed"
    assert state.state == BYPASS_VALVE_STATUS_MAP["4"]
    assert state.state == "closed"


async def test_preheater_status_enum(hass: HomeAssistant, setup_integration) -> None:
    """Test preheater status enum maps value '1' to 'auto'."""
    entity_id = _get_entity_id(hass, f"{DOMAIN}_{SYSTEM_ID}_preheater_status")
    state = hass.states.get(entity_id)
    assert state is not None
    # Default preheater_status in build_coordinator_data is "1" -> "auto"
    assert state.state == PREHEATER_STATUS_MAP["1"]
    assert state.state == "auto"


async def test_active_control_status_enum(
    hass: HomeAssistant,
    mock_brink_cloud,
) -> None:
    """Test active control status enum maps value '4' to 'manual'.

    This entity has entity_registry_enabled_default=False, so it must be
    enabled in the entity registry before setup for its state to populate.
    """
    # Pre-register the entity as enabled before integration setup
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{SYSTEM_ID}_active_control_status",
        disabled_by=None,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test Brink System",
        data={CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
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
    entry.add_to_hass(hass)

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_brink_cloud,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_id = _get_entity_id(hass, f"{DOMAIN}_{SYSTEM_ID}_active_control_status")
    state = hass.states.get(entity_id)
    assert state is not None
    # Default active_control_status in build_coordinator_data is "4" -> "manual"
    assert state.state == ACTIVE_CONTROL_STATUS_MAP["4"]
    assert state.state == "manual"


async def test_sensor_missing_parameter(
    hass: HomeAssistant,
    mock_config_entry,
    mock_brink_cloud,
) -> None:
    """Test sensor is unavailable when parameter is missing from data."""
    # Build coordinator data with supply_air_flow removed
    data = build_coordinator_data()
    components = data[SYSTEM_ID]["components"][0]["parameters"]
    del components["supply_air_flow"]

    mock_brink_cloud.get_device_data = AsyncMock(return_value=data[SYSTEM_ID])

    mock_config_entry.add_to_hass(hass)
    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_brink_cloud,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    # The supply_air_flow sensor should not be created since the param is missing
    registry = er.async_get(hass)
    entry = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_{SYSTEM_ID}_supply_air_flow"
    )
    if entry is not None:
        state = hass.states.get(entry)
        # If entity exists, it should be unavailable
        assert state is None or state.state == "unavailable"
    # If no entity was created at all, that also satisfies the requirement


async def test_sensor_unique_ids(hass: HomeAssistant, setup_integration) -> None:
    """Test each sensor has a unique ID with the expected format."""
    registry = er.async_get(hass)
    sensor_entries = er.async_entries_for_config_entry(
        registry, setup_integration.entry_id
    )

    # Filter to sensor platform entries
    sensor_unique_ids = [
        e.unique_id for e in sensor_entries if e.domain == "sensor"
    ]

    # All unique IDs should follow the pattern brink_ventilation_{system_id}_{key}
    for uid in sensor_unique_ids:
        assert uid.startswith(f"{DOMAIN}_{SYSTEM_ID}_"), (
            f"Unique ID {uid} does not follow expected pattern"
        )

    # There should be no duplicates
    assert len(sensor_unique_ids) == len(set(sensor_unique_ids)), (
        "Duplicate unique IDs found among sensors"
    )

    # Verify some specific expected unique IDs exist
    assert f"{DOMAIN}_{SYSTEM_ID}_supply_air_flow" in sensor_unique_ids
    assert f"{DOMAIN}_{SYSTEM_ID}_exhaust_air_flow" in sensor_unique_ids
    assert f"{DOMAIN}_{SYSTEM_ID}_fresh_air_temp" in sensor_unique_ids
    assert f"{DOMAIN}_{SYSTEM_ID}_extra_ventilation_remaining" in sensor_unique_ids
    assert f"{DOMAIN}_{SYSTEM_ID}_current_season" in sensor_unique_ids
    assert f"{DOMAIN}_{SYSTEM_ID}_heat_recovery_efficiency" in sensor_unique_ids


async def test_extra_vent_remaining_sensor(hass: HomeAssistant, setup_integration) -> None:
    """Test extra ventilation remaining sensor reads from automation controller."""
    entity_id = _get_entity_id(
        hass, f"{DOMAIN}_{SYSTEM_ID}_extra_ventilation_remaining"
    )
    state = hass.states.get(entity_id)
    assert state is not None
    # Default state: automation controller is IDLE, so remaining is 0
    assert state.state == "0"
    assert state.attributes.get("unit_of_measurement") == "min"


async def test_current_season_sensor(hass: HomeAssistant, setup_integration) -> None:
    """Test current season sensor reads season from automation controller."""
    entity_id = _get_entity_id(hass, f"{DOMAIN}_{SYSTEM_ID}_current_season")
    state = hass.states.get(entity_id)
    assert state is not None
    # After setup, the coordinator has done its first refresh.
    # The season is evaluated from fresh_air_temp (15.0C) which is above
    # the default freezing threshold (-2.0C), so season should be summer.
    # However, if automation is IDLE, season evaluation only runs during
    # async_on_coordinator_update which is called during refresh.
    # The season property returns self._season from the automation controller.
    # Since the integration was set up and first refresh happened, season
    # should have been evaluated.
    # With CONF_ADAPTIVE_ACTIVE=False, the controller stays IDLE.
    # In IDLE state, async_on_coordinator_update returns early without
    # evaluating season, so _season remains None -> HA shows "unknown".
    # Season is only actively evaluated when adaptive mode is activated.
    assert state.state == "unknown"


def _make_heat_recovery_entry(**extra_options: Any) -> MockConfigEntry:
    """Create a config entry with indoor temperature entity configured."""
    options: dict[str, Any] = {
        "scan_interval": DEFAULT_SCAN_INTERVAL,
        CONF_FREEZING_THRESHOLD: DEFAULT_FREEZING_THRESHOLD,
        CONF_EXTRA_VENT_DURATION: DEFAULT_EXTRA_VENT_DURATION,
        CONF_EXTRA_VENT_SUMMER_LEVEL: DEFAULT_EXTRA_VENT_SUMMER_LEVEL,
        CONF_EXTRA_VENT_WINTER_LEVEL: DEFAULT_EXTRA_VENT_WINTER_LEVEL,
        CONF_AUTO_SUMMER_BASE_LEVEL: DEFAULT_AUTO_SUMMER_BASE_LEVEL,
        CONF_AUTO_WINTER_BASE_LEVEL: DEFAULT_AUTO_WINTER_BASE_LEVEL,
        CONF_HUMIDITY_SPIKE_THRESHOLD: DEFAULT_HUMIDITY_SPIKE_THRESHOLD,
        CONF_ADAPTIVE_ACTIVE: False,
        CONF_INDOOR_TEMPERATURE_ENTITY_1: "sensor.indoor_temp",
    }
    options.update(extra_options)
    return MockConfigEntry(
        domain=DOMAIN,
        title="Test Brink System",
        data={
            CONF_USERNAME: TEST_EMAIL,
            CONF_PASSWORD: TEST_PASSWORD,
        },
        options=options,
        unique_id=TEST_EMAIL,
    )


async def test_heat_recovery_efficiency_calculation(
    hass: HomeAssistant,
    mock_brink_cloud,
) -> None:
    """Test heat recovery efficiency: (Tsupply - Tfresh) / (Tindoor - Tfresh) * 100."""
    entry = _make_heat_recovery_entry()

    # Build data: Tsupply=20, Tfresh=5, bypass closed (value "4")
    data = build_coordinator_data(
        supply_temp="20.0",
        fresh_air_temp="5.0",
        bypass_valve_status="4",  # closed
    )
    mock_brink_cloud.get_device_data = AsyncMock(return_value=data[SYSTEM_ID])

    entry.add_to_hass(hass)

    # Set up the indoor temperature entity state
    hass.states.async_set("sensor.indoor_temp", "22.0")

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_brink_cloud,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_id = _get_entity_id(
        hass, f"{DOMAIN}_{SYSTEM_ID}_heat_recovery_efficiency"
    )
    state = hass.states.get(entity_id)
    assert state is not None

    # Expected: (20 - 5) / (22 - 5) * 100 = 15/17 * 100 = 88.2%
    value = float(state.state)
    assert abs(value - 88.2) < 0.2


async def test_heat_recovery_bypass_open(
    hass: HomeAssistant,
    mock_brink_cloud,
) -> None:
    """Test heat recovery efficiency is 0% when bypass is open."""
    entry = _make_heat_recovery_entry()

    # Bypass open is value "3" (BYPASS_OPEN_VALUE)
    data = build_coordinator_data(
        supply_temp="20.0",
        fresh_air_temp="5.0",
        bypass_valve_status="3",  # open
    )
    mock_brink_cloud.get_device_data = AsyncMock(return_value=data[SYSTEM_ID])

    entry.add_to_hass(hass)
    hass.states.async_set("sensor.indoor_temp", "22.0")

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_brink_cloud,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_id = _get_entity_id(
        hass, f"{DOMAIN}_{SYSTEM_ID}_heat_recovery_efficiency"
    )
    state = hass.states.get(entity_id)
    assert state is not None
    assert float(state.state) == 0.0


async def test_heat_recovery_equal_temps(
    hass: HomeAssistant,
    mock_brink_cloud,
) -> None:
    """Test heat recovery efficiency is 0% when indoor == fresh (division by zero guard)."""
    entry = _make_heat_recovery_entry()

    # Make Tindoor == Tfresh to trigger division-by-zero guard
    data = build_coordinator_data(
        supply_temp="15.0",
        fresh_air_temp="15.0",
        bypass_valve_status="4",  # closed
    )
    mock_brink_cloud.get_device_data = AsyncMock(return_value=data[SYSTEM_ID])

    entry.add_to_hass(hass)
    hass.states.async_set("sensor.indoor_temp", "15.0")

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_brink_cloud,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_id = _get_entity_id(
        hass, f"{DOMAIN}_{SYSTEM_ID}_heat_recovery_efficiency"
    )
    state = hass.states.get(entity_id)
    assert state is not None
    # When abs(Tindoor - Tfresh) < 0.1, returns 0.0
    assert float(state.state) == 0.0


async def test_heat_recovery_unavailable_without_indoor_entity(
    hass: HomeAssistant,
    mock_brink_cloud,
) -> None:
    """Test heat recovery sensor is unavailable when no indoor temp entity configured."""
    # No indoor temp entity configured (default options)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test Brink System",
        data={CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
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
    entry.add_to_hass(hass)

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_brink_cloud,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_id = _get_entity_id(
        hass, f"{DOMAIN}_{SYSTEM_ID}_heat_recovery_efficiency"
    )
    state = hass.states.get(entity_id)
    assert state is not None
    # Without indoor temp entity, sensor should be unavailable
    assert state.state == "unavailable"


async def test_humidity_delta_sensor_no_sensor_configured(
    hass: HomeAssistant, setup_integration
) -> None:
    """Test humidity delta sensor is unavailable when no humidity sensor configured in slot."""
    # Default options have no humidity sensor configured
    entity_id = _get_entity_id(
        hass, f"{DOMAIN}_{SYSTEM_ID}_humidity_delta_1"
    )
    state = hass.states.get(entity_id)
    assert state is not None
    # No humidity_sensor_1 configured -> unavailable
    assert state.state == "unavailable"


async def test_humidity_delta_sensor_with_configured_sensor(
    hass: HomeAssistant,
    mock_brink_cloud,
) -> None:
    """Test humidity delta sensor shows a value when a humidity sensor is configured."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test Brink System",
        data={CONF_USERNAME: TEST_EMAIL, CONF_PASSWORD: TEST_PASSWORD},
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
            CONF_HUMIDITY_SENSOR_1: "sensor.bathroom_humidity",
        },
        unique_id=TEST_EMAIL,
    )
    entry.add_to_hass(hass)

    # Set up a humidity sensor state
    hass.states.async_set("sensor.bathroom_humidity", "65.0")

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_brink_cloud,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_id = _get_entity_id(
        hass, f"{DOMAIN}_{SYSTEM_ID}_humidity_delta_1"
    )
    state = hass.states.get(entity_id)
    assert state is not None
    # Sensor is configured -> should be available with a numeric value (default 0.0)
    assert state.state != "unavailable"
    # The delta starts at 0.0 since there's only one reading
    assert float(state.state) == 0.0
