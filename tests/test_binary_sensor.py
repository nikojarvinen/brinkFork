"""Tests for Brink Home Ventilation binary sensor entity (filter status)."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.brink_ventilation.const import DOMAIN
from tests.conftest import SYSTEM_ID, build_coordinator_data, TEST_EMAIL, TEST_PASSWORD

from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.brink_ventilation.const import (
    CONF_ADAPTIVE_ACTIVE,
    CONF_AUTO_SUMMER_BASE_LEVEL,
    CONF_AUTO_WINTER_BASE_LEVEL,
    CONF_EXTRA_VENT_DURATION,
    CONF_EXTRA_VENT_SUMMER_LEVEL,
    CONF_EXTRA_VENT_WINTER_LEVEL,
    CONF_FREEZING_THRESHOLD,
    CONF_HUMIDITY_SPIKE_THRESHOLD,
    DEFAULT_AUTO_SUMMER_BASE_LEVEL,
    DEFAULT_AUTO_WINTER_BASE_LEVEL,
    DEFAULT_EXTRA_VENT_DURATION,
    DEFAULT_EXTRA_VENT_SUMMER_LEVEL,
    DEFAULT_EXTRA_VENT_WINTER_LEVEL,
    DEFAULT_FREEZING_THRESHOLD,
    DEFAULT_HUMIDITY_SPIKE_THRESHOLD,
    DEFAULT_SCAN_INTERVAL,
)


def _get_entity_id(hass: HomeAssistant, unique_id: str, platform: str = "binary_sensor") -> str:
    """Look up entity_id from the registry by unique_id."""
    registry = er.async_get(hass)
    entry = registry.async_get_entity_id(platform, DOMAIN, unique_id)
    assert entry is not None, f"Entity with unique_id '{unique_id}' not found in registry"
    return entry


async def test_filter_status_clean(hass: HomeAssistant, setup_integration) -> None:
    """Test filter status binary sensor shows OFF (clean) when value is '0'."""
    entity_id = _get_entity_id(hass, f"{DOMAIN}_{SYSTEM_ID}_filter_status")
    state = hass.states.get(entity_id)
    assert state is not None
    # Default filter_status in build_coordinator_data is "0" -> not dirty -> off
    assert state.state == "off"


async def test_filter_status_dirty(
    hass: HomeAssistant,
    mock_brink_cloud,
) -> None:
    """Test filter status binary sensor shows ON (dirty) when value is '1'."""
    data = build_coordinator_data(filter_status="1")
    mock_brink_cloud.get_device_data = AsyncMock(return_value=data[SYSTEM_ID])

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

    entity_id = _get_entity_id(hass, f"{DOMAIN}_{SYSTEM_ID}_filter_status")
    state = hass.states.get(entity_id)
    assert state is not None
    # filter_status "1" -> dirty -> on (problem detected)
    assert state.state == "on"


async def test_filter_status_unique_id(hass: HomeAssistant, setup_integration) -> None:
    """Test filter status binary sensor has expected unique ID."""
    registry = er.async_get(hass)
    entry = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{DOMAIN}_{SYSTEM_ID}_filter_status"
    )
    assert entry is not None
