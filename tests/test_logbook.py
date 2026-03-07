"""Tests for Brink Home Ventilation logbook event descriptions."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant

from custom_components.brink_ventilation.const import (
    BOOST_TRIGGER_HUMIDITY,
    DOMAIN,
    EVENT_BOOST_ACTIVATED,
    EVENT_BOOST_DEACTIVATED,
    EVENT_WRITE_FAILED,
)
from custom_components.brink_ventilation.logbook import async_describe_events


def _setup_logbook_handlers(hass: HomeAssistant) -> dict:
    """Set up logbook event handlers and return registered handler mapping."""
    handlers: dict[str, callable] = {}

    def mock_async_describe_event(domain: str, event_type: str, handler):
        handlers[event_type] = handler

    async_describe_events(hass, mock_async_describe_event)
    return handlers


def _make_event(event_type: str, data: dict) -> Event:
    """Create a mock Event with given type and data."""
    event = MagicMock(spec=Event)
    event.event_type = event_type
    event.data = data
    return event


async def test_boost_activated_humidity_description(hass: HomeAssistant) -> None:
    """Test boost activated event with humidity trigger produces correct description."""
    handlers = _setup_logbook_handlers(hass)
    handler = handlers[EVENT_BOOST_ACTIVATED]

    event = _make_event(EVENT_BOOST_ACTIVATED, {
        "trigger": BOOST_TRIGGER_HUMIDITY,
        "sensor": "sensor.bathroom_humidity",
        "rate": 2.5,
        "duration": 120,
        "level": 3,
        "season": "summer",
    })

    result = handler(event)

    assert result[LOGBOOK_ENTRY_NAME] == "Extra ventilation"
    message = result[LOGBOOK_ENTRY_MESSAGE]
    assert "humidity spike" in message
    assert "sensor.bathroom_humidity" in message
    assert "2.5%/min" in message
    assert "level 3" in message
    assert "120 min" in message


async def test_boost_activated_manual_description(hass: HomeAssistant) -> None:
    """Test boost activated event without trigger shows 'manually' in description."""
    handlers = _setup_logbook_handlers(hass)
    handler = handlers[EVENT_BOOST_ACTIVATED]

    event = _make_event(EVENT_BOOST_ACTIVATED, {
        "duration": 60,
        "level": 2,
        "season": "winter",
    })

    result = handler(event)

    assert result[LOGBOOK_ENTRY_NAME] == "Extra ventilation"
    message = result[LOGBOOK_ENTRY_MESSAGE]
    assert "manually" in message
    assert "level 2" in message
    assert "60 min" in message
    # Should not contain humidity-related info
    assert "humidity" not in message


async def test_boost_deactivated_description(hass: HomeAssistant) -> None:
    """Test boost deactivated event produces correct description."""
    handlers = _setup_logbook_handlers(hass)
    handler = handlers[EVENT_BOOST_DEACTIVATED]

    # Test timer expired reason
    event = _make_event(EVENT_BOOST_DEACTIVATED, {
        "reason": "timer_expired",
    })

    result = handler(event)

    assert result[LOGBOOK_ENTRY_NAME] == "Extra ventilation"
    message = result[LOGBOOK_ENTRY_MESSAGE]
    assert "deactivated" in message
    assert "timer expired" in message

    # Test cancelled reason
    event_cancelled = _make_event(EVENT_BOOST_DEACTIVATED, {
        "reason": "cancelled",
    })

    result_cancelled = handler(event_cancelled)
    message_cancelled = result_cancelled[LOGBOOK_ENTRY_MESSAGE]
    assert "deactivated" in message_cancelled
    assert "cancelled manually" in message_cancelled


async def test_boost_deactivated_unknown_reason(hass: HomeAssistant) -> None:
    """Test boost deactivated event with an unknown reason falls back to generic format."""
    handlers = _setup_logbook_handlers(hass)
    handler = handlers[EVENT_BOOST_DEACTIVATED]

    event = _make_event(EVENT_BOOST_DEACTIVATED, {
        "reason": "some_custom_reason",
    })

    result = handler(event)

    message = result[LOGBOOK_ENTRY_MESSAGE]
    assert "deactivated" in message
    assert "some_custom_reason" in message


async def test_boost_deactivated_missing_reason(hass: HomeAssistant) -> None:
    """Test boost deactivated event without reason key defaults to 'unknown'."""
    handlers = _setup_logbook_handlers(hass)
    handler = handlers[EVENT_BOOST_DEACTIVATED]

    event = _make_event(EVENT_BOOST_DEACTIVATED, {})

    result = handler(event)

    message = result[LOGBOOK_ENTRY_MESSAGE]
    assert "deactivated" in message
    assert "unknown" in message


async def test_boost_activated_humidity_no_rate(hass: HomeAssistant) -> None:
    """Test humidity trigger without rate omits rate from description."""
    handlers = _setup_logbook_handlers(hass)
    handler = handlers[EVENT_BOOST_ACTIVATED]

    event = _make_event(EVENT_BOOST_ACTIVATED, {
        "trigger": BOOST_TRIGGER_HUMIDITY,
        "sensor": "sensor.kitchen_humidity",
        "duration": 30,
        "level": 2,
    })

    result = handler(event)
    message = result[LOGBOOK_ENTRY_MESSAGE]
    assert "humidity spike" in message
    assert "sensor.kitchen_humidity" in message
    # Rate is None, so no rate string should appear
    assert "%/min" not in message


async def test_write_failed_description(hass: HomeAssistant) -> None:
    """Test write failed logbook event produces correct description."""
    handlers = _setup_logbook_handlers(hass)
    handler = handlers[EVENT_WRITE_FAILED]

    event = _make_event(EVENT_WRITE_FAILED, {
        "entity_key": "ventilation_level",
        "error": "ConnectionError",
    })

    result = handler(event)

    assert result[LOGBOOK_ENTRY_NAME] == "Brink write failed"
    message = result[LOGBOOK_ENTRY_MESSAGE]
    assert "ventilation_level" in message
    assert "ConnectionError" in message


async def test_write_failed_missing_data(hass: HomeAssistant) -> None:
    """Test write failed event with missing data fields uses defaults."""
    handlers = _setup_logbook_handlers(hass)
    handler = handlers[EVENT_WRITE_FAILED]

    event = _make_event(EVENT_WRITE_FAILED, {})

    result = handler(event)

    message = result[LOGBOOK_ENTRY_MESSAGE]
    assert "unknown" in message
