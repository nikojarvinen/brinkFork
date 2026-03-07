"""Tests for Brink Home Ventilation switch entity (extra ventilation boost)."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.brink_ventilation.automation_controller import AutomationState
from custom_components.brink_ventilation.const import (
    BOOST_TRIGGER_HUMIDITY,
    DOMAIN,
)
from tests.conftest import SYSTEM_ID


def _get_entity_id(hass: HomeAssistant, unique_id: str, platform: str = "switch") -> str:
    """Look up entity_id from the registry by unique_id."""
    registry = er.async_get(hass)
    entry = registry.async_get_entity_id(platform, DOMAIN, unique_id)
    assert entry is not None, f"Entity with unique_id '{unique_id}' not found in registry"
    return entry


async def test_switch_off_when_idle(hass: HomeAssistant, setup_integration) -> None:
    """Test switch is OFF when automation controller state is IDLE."""
    entity_id = _get_entity_id(hass, f"{DOMAIN}_{SYSTEM_ID}_extra_ventilation")
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    assert coordinator.automation_controller.state == AutomationState.IDLE

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "off"


async def test_switch_on_when_boosted(hass: HomeAssistant, setup_integration) -> None:
    """Test switch is ON when automation controller state is BOOSTED."""
    entity_id = _get_entity_id(hass, f"{DOMAIN}_{SYSTEM_ID}_extra_ventilation")
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    # Turn on the switch (which activates extra ventilation)
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert coordinator.automation_controller.state == AutomationState.BOOSTED

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "on"


async def test_turn_on_calls_controller(hass: HomeAssistant, setup_integration) -> None:
    """Test turn_on calls async_activate_extra_ventilation on the controller."""
    entity_id = _get_entity_id(hass, f"{DOMAIN}_{SYSTEM_ID}_extra_ventilation")
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator
    controller = coordinator.automation_controller

    with patch.object(
        controller, "async_activate_extra_ventilation", new_callable=AsyncMock
    ) as mock_activate:
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": entity_id},
            blocking=True,
        )

        mock_activate.assert_called_once()


async def test_turn_off_calls_controller(hass: HomeAssistant, setup_integration) -> None:
    """Test turn_off calls async_cancel_extra_ventilation on the controller."""
    entity_id = _get_entity_id(hass, f"{DOMAIN}_{SYSTEM_ID}_extra_ventilation")
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator
    controller = coordinator.automation_controller

    # First activate the boost so we can cancel it
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    with patch.object(
        controller, "async_cancel_extra_ventilation", new_callable=AsyncMock
    ) as mock_cancel:
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": entity_id},
            blocking=True,
        )

        mock_cancel.assert_called_once()


async def test_extra_attributes_humidity_trigger(
    hass: HomeAssistant, setup_integration
) -> None:
    """Test extra attributes are present when boosted with humidity trigger."""
    entity_id = _get_entity_id(hass, f"{DOMAIN}_{SYSTEM_ID}_extra_ventilation")
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator
    controller = coordinator.automation_controller

    # Directly activate extra ventilation with humidity trigger info
    await controller.async_activate_extra_ventilation(
        trigger=BOOST_TRIGGER_HUMIDITY,
        trigger_entity="sensor.bathroom_humidity",
        trigger_rate=2.5,
    )
    await hass.async_block_till_done()

    assert controller.state == AutomationState.BOOSTED

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "on"
    assert state.attributes.get("boost_trigger") == BOOST_TRIGGER_HUMIDITY
    assert state.attributes.get("boost_trigger_sensor") == "sensor.bathroom_humidity"
    assert state.attributes.get("boost_trigger_rate") == 2.5


async def test_extra_attributes_manual_trigger(
    hass: HomeAssistant, setup_integration
) -> None:
    """Test no extra trigger attributes when boosted manually."""
    entity_id = _get_entity_id(hass, f"{DOMAIN}_{SYSTEM_ID}_extra_ventilation")
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator
    controller = coordinator.automation_controller

    # Activate extra ventilation manually (no trigger info)
    await controller.async_activate_extra_ventilation()
    await hass.async_block_till_done()

    assert controller.state == AutomationState.BOOSTED

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "on"
    # Manual activation: boost_trigger is None, so extra_state_attributes returns None
    # which means no extra attributes are set
    assert state.attributes.get("boost_trigger") is None
    assert state.attributes.get("boost_trigger_sensor") is None
    assert state.attributes.get("boost_trigger_rate") is None


async def test_switch_turn_on_error(hass: HomeAssistant, setup_integration) -> None:
    """Test that errors during turn_on raise HomeAssistantError."""
    from homeassistant.exceptions import HomeAssistantError

    entity_id = _get_entity_id(hass, f"{DOMAIN}_{SYSTEM_ID}_extra_ventilation")
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator
    controller = coordinator.automation_controller

    with patch.object(
        controller,
        "async_activate_extra_ventilation",
        new_callable=AsyncMock,
        side_effect=RuntimeError("API failure"),
    ):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "switch",
                "turn_on",
                {"entity_id": entity_id},
                blocking=True,
            )


async def test_switch_turn_off_error(hass: HomeAssistant, setup_integration) -> None:
    """Test that errors during turn_off raise HomeAssistantError."""
    from homeassistant.exceptions import HomeAssistantError

    entity_id = _get_entity_id(hass, f"{DOMAIN}_{SYSTEM_ID}_extra_ventilation")
    entry = setup_integration
    coordinator = entry.runtime_data.coordinator
    controller = coordinator.automation_controller

    # First activate to allow cancel
    await controller.async_activate_extra_ventilation()
    await hass.async_block_till_done()

    with patch.object(
        controller,
        "async_cancel_extra_ventilation",
        new_callable=AsyncMock,
        side_effect=RuntimeError("API failure"),
    ):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "switch",
                "turn_off",
                {"entity_id": entity_id},
                blocking=True,
            )
