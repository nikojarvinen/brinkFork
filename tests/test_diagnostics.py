"""Tests for Brink Home Ventilation diagnostics."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from custom_components.brink_ventilation.const import DOMAIN
from custom_components.brink_ventilation.diagnostics import (
    _collect_unrecognized_params,
    async_get_config_entry_diagnostics,
)
from tests.conftest import SYSTEM_ID, TEST_EMAIL, TEST_PASSWORD


async def test_diagnostics_output_structure(
    hass: HomeAssistant, setup_integration
) -> None:
    """Test diagnostics output has required top-level keys."""
    entry = setup_integration

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert isinstance(result, dict)
    assert "entry_data" in result
    assert "entry_options" in result
    assert "devices" in result
    assert "automation_controller" in result

    # Check automation controller structure
    controller_data = result["automation_controller"]
    assert "state" in controller_data
    assert "season" in controller_data
    assert "boost_remaining_minutes" in controller_data
    assert "has_pending_writes" in controller_data
    assert "humidity_sensors_configured" in controller_data

    # Devices should be present (renamed to device_0, device_1, etc.)
    assert len(result["devices"]) >= 1


async def test_diagnostics_redacts_password(
    hass: HomeAssistant, setup_integration
) -> None:
    """Test diagnostics redacts sensitive data like password and username."""
    entry = setup_integration

    result = await async_get_config_entry_diagnostics(hass, entry)

    # entry_data should have password redacted
    entry_data = result["entry_data"]

    # The password value must never appear in plain text
    for value in entry_data.values():
        assert value != TEST_PASSWORD, "Password appears in plain text in diagnostics"

    # CONF_PASSWORD ("password") is in TO_REDACT, so it should be redacted
    if CONF_PASSWORD in entry_data:
        assert entry_data[CONF_PASSWORD] == "**REDACTED**"

    # CONF_USERNAME ("username") is in TO_REDACT
    # Depending on what key the config entry uses (CONF_EMAIL vs CONF_USERNAME),
    # either the username key is redacted or the email key is present unredacted.
    if CONF_USERNAME in entry_data:
        assert entry_data[CONF_USERNAME] == "**REDACTED**"

    # Verify no plain text credentials leak through any key
    for value in entry_data.values():
        assert value != TEST_EMAIL, "Email appears in plain text in diagnostics"


async def test_diagnostics_no_data(
    hass: HomeAssistant,
    mock_config_entry,
    mock_brink_cloud,
) -> None:
    """Test diagnostics handles gracefully when coordinator has no data."""
    # Make get_systems return empty so coordinator data is empty
    mock_brink_cloud.get_systems = AsyncMock(return_value=[])
    mock_brink_cloud.get_device_data = AsyncMock(return_value={"components": []})

    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.brink_ventilation.BrinkHomeCloud",
        return_value=mock_brink_cloud,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    result = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert isinstance(result, dict)
    assert "entry_data" in result
    assert "entry_options" in result
    # devices should be empty dict when no systems
    assert result["devices"] == {}
    assert "automation_controller" in result


def test_collect_unrecognized_params_with_unknown_keys() -> None:
    """Test _collect_unrecognized_params finds parameters with 'unknown_' prefix."""
    devices = {
        1234: {
            "components": [
                {
                    "name": "Flair 325",
                    "parameters": {
                        "ventilation_level": {"value": "2", "value_id": 1001},
                        "unknown_42": {
                            "name": "Unbekannter Parameter",
                            "numeric_id": 42,
                            "value": "99",
                            "unit_of_measure": "%",
                            "control_type": "slider",
                            "read_write": True,
                        },
                        "unknown_99": {
                            "name": "Weiterer Parameter",
                            "numeric_id": 99,
                            "value": "0",
                            "unit_of_measure": "",
                            "control_type": "dropdown",
                            "read_write": False,
                        },
                    },
                }
            ],
        }
    }

    result = _collect_unrecognized_params(devices)

    assert len(result) == 2
    keys = {r["key"] for r in result}
    assert "unknown_42" in keys
    assert "unknown_99" in keys
    # Check that the first entry has expected fields
    entry_42 = next(r for r in result if r["key"] == "unknown_42")
    assert entry_42["german_name"] == "Unbekannter Parameter"
    assert entry_42["numeric_id"] == 42
    assert entry_42["value"] == "99"
    assert entry_42["component"] == "Flair 325"


def test_collect_unrecognized_params_none_when_no_unknown() -> None:
    """Test _collect_unrecognized_params returns empty list when no unknown_ keys."""
    devices = {
        1234: {
            "components": [
                {
                    "name": "Flair 325",
                    "parameters": {
                        "ventilation_level": {"value": "2", "value_id": 1001},
                    },
                }
            ],
        }
    }

    result = _collect_unrecognized_params(devices)
    assert result == []


def test_collect_unrecognized_params_empty_devices() -> None:
    """Test _collect_unrecognized_params handles empty devices dict."""
    result = _collect_unrecognized_params({})
    assert result == []
