"""Tests for the Brink HRV automation controller."""
from __future__ import annotations

import math
import time
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.brink_ventilation.automation_controller import (
    AutomationState,
    BrinkAutomationController,
)
from custom_components.brink_ventilation.const import (
    BOOST_TRIGGER_HUMIDITY,
    CONF_ADAPTIVE_ACTIVE,
    CONF_ADAPTIVE_ACTIVE_LEGACY,
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
    CONF_TEMPERATURE_SOURCE_ENTITY,
    DEFAULT_AUTO_SUMMER_BASE_LEVEL,
    DEFAULT_AUTO_WINTER_BASE_LEVEL,
    DEFAULT_EXTRA_VENT_DURATION,
    DEFAULT_EXTRA_VENT_SUMMER_LEVEL,
    DEFAULT_EXTRA_VENT_WINTER_LEVEL,
    DEFAULT_FREEZING_THRESHOLD,
    DEFAULT_HUMIDITY_SPIKE_THRESHOLD,
    DOMAIN,
    EVENT_BOOST_ACTIVATED,
    EVENT_BOOST_DEACTIVATED,
    EVENT_WRITE_FAILED,
    PARAM_FRESH_AIR_TEMP,
    PARAM_OPERATING_MODE,
    PARAM_VENTILATION_LEVEL,
    SEASON_SUMMER,
    SEASON_WINTER,
)
from custom_components.brink_ventilation.core.brink_home_cloud import BrinkAuthError
from tests.conftest import SYSTEM_ID, build_coordinator_data

from pytest_homeassistant_custom_component.common import MockConfigEntry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_coordinator_data() -> dict[int, dict[str, Any]]:
    """Return sample coordinator data."""
    return build_coordinator_data()


@pytest.fixture
def mock_coordinator(mock_coordinator_data: dict[int, dict[str, Any]]) -> MagicMock:
    """Create a mock coordinator with standard data and client."""
    coordinator = MagicMock()
    coordinator.data = mock_coordinator_data
    coordinator.client = AsyncMock()
    coordinator.client.write_parameters = AsyncMock()
    coordinator.start_expedited_polling = MagicMock()
    coordinator.async_set_updated_data = MagicMock()
    return coordinator


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Create a mock config entry with default options."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Test Brink System",
        data={
            "email": "test@example.com",
            "password": "testpassword123",
        },
        options={
            "scan_interval": 60,
            CONF_FREEZING_THRESHOLD: DEFAULT_FREEZING_THRESHOLD,
            CONF_EXTRA_VENT_DURATION: DEFAULT_EXTRA_VENT_DURATION,
            CONF_EXTRA_VENT_SUMMER_LEVEL: DEFAULT_EXTRA_VENT_SUMMER_LEVEL,
            CONF_EXTRA_VENT_WINTER_LEVEL: DEFAULT_EXTRA_VENT_WINTER_LEVEL,
            CONF_AUTO_SUMMER_BASE_LEVEL: DEFAULT_AUTO_SUMMER_BASE_LEVEL,
            CONF_AUTO_WINTER_BASE_LEVEL: DEFAULT_AUTO_WINTER_BASE_LEVEL,
            CONF_HUMIDITY_SPIKE_THRESHOLD: DEFAULT_HUMIDITY_SPIKE_THRESHOLD,
            CONF_ADAPTIVE_ACTIVE: False,
        },
        unique_id="test@example.com",
    )


@pytest.fixture
async def controller(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_coordinator: MagicMock,
) -> AsyncGenerator[BrinkAutomationController]:
    """Create an automation controller instance wired to hass, entry, and coordinator."""
    mock_config_entry.add_to_hass(hass)
    ctrl = BrinkAutomationController(hass, mock_coordinator, mock_config_entry)
    yield ctrl
    await ctrl.async_cleanup()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_coordinator_param(
    coordinator: MagicMock,
    param_key: str,
    value: str,
) -> None:
    """Set a parameter value inside the mock coordinator data."""
    for device in coordinator.data.values():
        for component in device.get("components", []):
            param = component.get("parameters", {}).get(param_key)
            if param is not None:
                param["value"] = value
                return


def _manual_humidity_tick(controller: BrinkAutomationController) -> None:
    """Call _process_humidity_tick directly for testing.

    Cancels the pending timer first to avoid orphaned timers that trigger
    the HA 'lingering timer' teardown check, then calls the tick which
    reschedules a fresh timer.
    """
    controller._cancel_humidity_timer()
    controller._process_humidity_tick(None)


# =====================================================================
# State machine tests
# =====================================================================


class TestStateMachine:
    """Tests for IDLE / BASE / BOOSTED state transitions."""

    async def test_activate_idle_to_base(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """IDLE -> activate -> BASE, writes seasonal level."""
        assert controller.state == AutomationState.IDLE
        await controller.async_activate()
        assert controller.state == AutomationState.BASE
        # write_parameters should be called with mode=1 and a level value
        mock_coordinator.client.write_parameters.assert_awaited_once()

    async def test_activate_when_not_idle(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """BASE -> activate -> no-op (debug log, no extra write)."""
        await controller.async_activate()
        assert controller.state == AutomationState.BASE
        call_count = mock_coordinator.client.write_parameters.await_count

        # Call activate again while in BASE
        await controller.async_activate()
        assert controller.state == AutomationState.BASE
        # No additional write should have been made
        assert mock_coordinator.client.write_parameters.await_count == call_count

    async def test_deactivate_base_to_idle(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """BASE -> deactivate -> IDLE, clears pending writes."""
        await controller.async_activate()
        assert controller.state == AutomationState.BASE
        # Simulate a pending write
        controller._pending_writes = [(1001, "2")]

        await controller.async_deactivate()
        assert controller.state == AutomationState.IDLE
        assert controller._pending_writes is None

    async def test_deactivate_boosted_to_idle(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """BOOSTED -> deactivate -> IDLE, cancels boost timer."""
        await controller.async_activate()
        await controller.async_activate_extra_ventilation()
        assert controller.state == AutomationState.BOOSTED

        await controller.async_deactivate()
        assert controller.state == AutomationState.IDLE
        assert controller._boost_timer_unsub is None
        assert controller._countdown_timer_unsub is None

    async def test_deactivate_when_idle(
        self,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """IDLE -> deactivate -> stays IDLE, just updates options."""
        assert controller.state == AutomationState.IDLE
        await controller.async_deactivate()
        assert controller.state == AutomationState.IDLE

    async def test_state_persisted_on_activate(
        self,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """activate -> entry.options has adaptive_active=True."""
        await controller.async_activate()
        assert mock_config_entry.options[CONF_ADAPTIVE_ACTIVE] is True

    async def test_state_persisted_on_deactivate(
        self,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """deactivate -> entry.options has adaptive_active=False."""
        # First activate to set the flag to True
        await controller.async_activate()
        assert mock_config_entry.options[CONF_ADAPTIVE_ACTIVE] is True

        await controller.async_deactivate()
        assert mock_config_entry.options[CONF_ADAPTIVE_ACTIVE] is False

    async def test_restore_state_active(
        self,
        hass: HomeAssistant,
        mock_coordinator: MagicMock,
    ) -> None:
        """options has adaptive_active=True -> restores to BASE."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Test",
            data={"email": "t@t.com", "password": "p"},
            options={
                CONF_ADAPTIVE_ACTIVE: True,
                CONF_FREEZING_THRESHOLD: DEFAULT_FREEZING_THRESHOLD,
                CONF_EXTRA_VENT_DURATION: DEFAULT_EXTRA_VENT_DURATION,
                CONF_EXTRA_VENT_SUMMER_LEVEL: DEFAULT_EXTRA_VENT_SUMMER_LEVEL,
                CONF_EXTRA_VENT_WINTER_LEVEL: DEFAULT_EXTRA_VENT_WINTER_LEVEL,
                CONF_AUTO_SUMMER_BASE_LEVEL: DEFAULT_AUTO_SUMMER_BASE_LEVEL,
                CONF_AUTO_WINTER_BASE_LEVEL: DEFAULT_AUTO_WINTER_BASE_LEVEL,
                CONF_HUMIDITY_SPIKE_THRESHOLD: DEFAULT_HUMIDITY_SPIKE_THRESHOLD,
            },
            unique_id="restore@test.com",
        )
        entry.add_to_hass(hass)
        ctrl = BrinkAutomationController(hass, mock_coordinator, entry)
        await ctrl.async_restore_state()
        assert ctrl.state == AutomationState.BASE

    async def test_restore_state_inactive(
        self,
        hass: HomeAssistant,
        mock_coordinator: MagicMock,
    ) -> None:
        """options has adaptive_active=False -> stays IDLE."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Test",
            data={"email": "t@t.com", "password": "p"},
            options={CONF_ADAPTIVE_ACTIVE: False},
            unique_id="norestore@test.com",
        )
        entry.add_to_hass(hass)
        ctrl = BrinkAutomationController(hass, mock_coordinator, entry)
        await ctrl.async_restore_state()
        assert ctrl.state == AutomationState.IDLE

    async def test_restore_state_legacy_key(
        self,
        hass: HomeAssistant,
        mock_coordinator: MagicMock,
    ) -> None:
        """options has ha_automated_active=True -> migrates and activates."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Test",
            data={"email": "t@t.com", "password": "p"},
            options={
                CONF_ADAPTIVE_ACTIVE_LEGACY: True,
                CONF_FREEZING_THRESHOLD: DEFAULT_FREEZING_THRESHOLD,
                CONF_EXTRA_VENT_DURATION: DEFAULT_EXTRA_VENT_DURATION,
                CONF_EXTRA_VENT_SUMMER_LEVEL: DEFAULT_EXTRA_VENT_SUMMER_LEVEL,
                CONF_EXTRA_VENT_WINTER_LEVEL: DEFAULT_EXTRA_VENT_WINTER_LEVEL,
                CONF_AUTO_SUMMER_BASE_LEVEL: DEFAULT_AUTO_SUMMER_BASE_LEVEL,
                CONF_AUTO_WINTER_BASE_LEVEL: DEFAULT_AUTO_WINTER_BASE_LEVEL,
                CONF_HUMIDITY_SPIKE_THRESHOLD: DEFAULT_HUMIDITY_SPIKE_THRESHOLD,
            },
            unique_id="legacy@test.com",
        )
        entry.add_to_hass(hass)
        ctrl = BrinkAutomationController(hass, mock_coordinator, entry)
        await ctrl.async_restore_state()
        assert ctrl.state == AutomationState.BASE
        # Legacy key should have been migrated
        assert entry.options.get(CONF_ADAPTIVE_ACTIVE) is True
        assert CONF_ADAPTIVE_ACTIVE_LEGACY not in entry.options


# =====================================================================
# Extra ventilation / boost tests
# =====================================================================


class TestExtraVentilation:
    """Tests for extra ventilation boost activation and cancellation."""

    async def test_activate_extra_vent_from_base(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """BASE -> BOOSTED, starts boost timer."""
        await controller.async_activate()
        assert controller.state == AutomationState.BASE
        mock_coordinator.client.write_parameters.reset_mock()

        await controller.async_activate_extra_ventilation()
        assert controller.state == AutomationState.BOOSTED
        assert controller._was_in_base_before_boost is True
        assert controller._boost_timer_unsub is not None
        mock_coordinator.client.write_parameters.assert_awaited_once()

    async def test_activate_extra_vent_from_idle(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """IDLE -> BOOSTED (direct, _was_in_base_before_boost=False)."""
        assert controller.state == AutomationState.IDLE
        await controller.async_activate_extra_ventilation()
        assert controller.state == AutomationState.BOOSTED
        assert controller._was_in_base_before_boost is False

    async def test_activate_extra_vent_manual_trigger(
        self,
        controller: BrinkAutomationController,
    ) -> None:
        """No trigger info -> trigger properties return None."""
        await controller.async_activate()
        await controller.async_activate_extra_ventilation()
        assert controller.state == AutomationState.BOOSTED
        assert controller.boost_trigger is None
        assert controller.boost_trigger_entity is None
        assert controller.boost_trigger_rate is None

    async def test_activate_extra_vent_humidity_trigger(
        self,
        controller: BrinkAutomationController,
    ) -> None:
        """With trigger info -> boost_trigger, boost_trigger_entity, boost_trigger_rate set."""
        await controller.async_activate()
        await controller.async_activate_extra_ventilation(
            trigger=BOOST_TRIGGER_HUMIDITY,
            trigger_entity="sensor.bathroom_humidity",
            trigger_rate=2.5,
        )
        assert controller.state == AutomationState.BOOSTED
        assert controller.boost_trigger == BOOST_TRIGGER_HUMIDITY
        assert controller.boost_trigger_entity == "sensor.bathroom_humidity"
        assert controller.boost_trigger_rate == 2.5

    async def test_cancel_extra_vent_returns_to_base(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """BOOSTED (was BASE) -> cancel -> BASE."""
        await controller.async_activate()
        await controller.async_activate_extra_ventilation()
        assert controller.state == AutomationState.BOOSTED
        assert controller._was_in_base_before_boost is True

        await controller.async_cancel_extra_ventilation()
        assert controller.state == AutomationState.BASE

    async def test_cancel_extra_vent_returns_to_idle(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """BOOSTED (was IDLE) -> cancel -> IDLE."""
        await controller.async_activate_extra_ventilation()
        assert controller.state == AutomationState.BOOSTED
        assert controller._was_in_base_before_boost is False

        await controller.async_cancel_extra_ventilation()
        assert controller.state == AutomationState.IDLE

    async def test_cancel_extra_vent_when_not_boosted(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Not BOOSTED -> cancel -> no-op."""
        await controller.async_activate()
        assert controller.state == AutomationState.BASE
        call_count = mock_coordinator.client.write_parameters.await_count

        await controller.async_cancel_extra_ventilation()
        assert controller.state == AutomationState.BASE
        # No additional write should have occurred
        assert mock_coordinator.client.write_parameters.await_count == call_count

    async def test_boost_remaining_minutes(
        self,
        controller: BrinkAutomationController,
    ) -> None:
        """Returns correct countdown when boosted."""
        await controller.async_activate_extra_ventilation()
        assert controller.state == AutomationState.BOOSTED
        # Default duration is 120 min; remaining should be approximately 120
        remaining = controller.boost_remaining_minutes
        assert remaining > 0
        assert remaining <= DEFAULT_EXTRA_VENT_DURATION

    async def test_boost_remaining_minutes_not_boosted(
        self,
        controller: BrinkAutomationController,
    ) -> None:
        """Returns 0 when not BOOSTED."""
        assert controller.state == AutomationState.IDLE
        assert controller.boost_remaining_minutes == 0

        await controller.async_activate()
        assert controller.state == AutomationState.BASE
        assert controller.boost_remaining_minutes == 0


# =====================================================================
# Humidity monitoring tests
# =====================================================================


class TestHumidityMonitoring:
    """Tests for humidity delta tracking and spike detection."""

    async def test_humidity_tick_normal_rate(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """Rate below threshold -> no boost."""
        # Configure a humidity sensor
        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={**mock_config_entry.options, CONF_HUMIDITY_SENSOR_1: "sensor.humidity_1"},
        )
        await controller.async_activate()

        hass.states.async_set("sensor.humidity_1", "60.0")

        # First tick: stores baseline
        _manual_humidity_tick(controller)
        assert controller.state == AutomationState.BASE

        # Advance time slightly and set a small increase
        with patch("custom_components.brink_ventilation.automation_controller.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 60
            hass.states.async_set("sensor.humidity_1", "60.5")
            _manual_humidity_tick(controller)

        # Rate ~0.5 %/min, below default threshold of 1.5 -> still BASE
        assert controller.state == AutomationState.BASE

    async def test_humidity_tick_spike_triggers_boost(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """Rate >= threshold in BASE -> triggers extra vent."""
        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={**mock_config_entry.options, CONF_HUMIDITY_SENSOR_1: "sensor.humidity_1"},
        )
        await controller.async_activate()
        assert controller.state == AutomationState.BASE

        # Seed first reading
        t0 = time.monotonic()
        hass.states.async_set("sensor.humidity_1", "50.0")
        with patch("custom_components.brink_ventilation.automation_controller.time") as mock_time:
            mock_time.monotonic.return_value = t0
            _manual_humidity_tick(controller)

        # Second tick: big spike (50 -> 55 in 1 min = 5.0 %/min > threshold 1.5)
        hass.states.async_set("sensor.humidity_1", "55.0")

        # Patch async_create_task to capture the boost call
        created_tasks = []
        original_create_task = hass.async_create_task

        def capture_task(coro, name=None, eager_start=False):
            task = original_create_task(coro, name=name, eager_start=eager_start)
            created_tasks.append(task)
            return task

        with (
            patch("custom_components.brink_ventilation.automation_controller.time") as mock_time,
            patch.object(hass, "async_create_task", side_effect=capture_task),
        ):
            mock_time.monotonic.return_value = t0 + 60
            _manual_humidity_tick(controller)

        # Let the created task run
        for task in created_tasks:
            try:
                await task
            except Exception:
                pass

        assert controller.state == AutomationState.BOOSTED

    async def test_humidity_tick_spike_not_in_base(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """Rate >= threshold but not in BASE -> no boost."""
        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={**mock_config_entry.options, CONF_HUMIDITY_SENSOR_1: "sensor.humidity_1"},
        )
        # Controller stays IDLE
        assert controller.state == AutomationState.IDLE

        t0 = time.monotonic()
        hass.states.async_set("sensor.humidity_1", "50.0")
        with patch("custom_components.brink_ventilation.automation_controller.time") as mock_time:
            mock_time.monotonic.return_value = t0
            _manual_humidity_tick(controller)

        hass.states.async_set("sensor.humidity_1", "55.0")
        with patch("custom_components.brink_ventilation.automation_controller.time") as mock_time:
            mock_time.monotonic.return_value = t0 + 60
            _manual_humidity_tick(controller)

        # Should remain IDLE despite spike
        assert controller.state == AutomationState.IDLE

    async def test_humidity_tick_unavailable_sensor(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """Sensor unavailable -> skipped."""
        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={**mock_config_entry.options, CONF_HUMIDITY_SENSOR_1: "sensor.humidity_1"},
        )
        await controller.async_activate()

        hass.states.async_set("sensor.humidity_1", "unavailable")
        _manual_humidity_tick(controller)

        # No rates should be recorded
        assert "sensor.humidity_1" not in controller._humidity_rates
        assert controller.state == AutomationState.BASE

    async def test_humidity_tick_non_numeric(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """Non-float state -> skipped."""
        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={**mock_config_entry.options, CONF_HUMIDITY_SENSOR_1: "sensor.humidity_1"},
        )
        await controller.async_activate()

        hass.states.async_set("sensor.humidity_1", "not_a_number")
        _manual_humidity_tick(controller)

        assert "sensor.humidity_1" not in controller._humidity_rates

    async def test_humidity_tick_out_of_range(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """Value > 100 or < 0 -> skipped."""
        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={**mock_config_entry.options, CONF_HUMIDITY_SENSOR_1: "sensor.humidity_1"},
        )
        await controller.async_activate()

        # Value above 100
        hass.states.async_set("sensor.humidity_1", "105.0")
        _manual_humidity_tick(controller)
        assert "sensor.humidity_1" not in controller._humidity_rates

        # Value below 0
        hass.states.async_set("sensor.humidity_1", "-5.0")
        _manual_humidity_tick(controller)
        assert "sensor.humidity_1" not in controller._humidity_rates

    async def test_humidity_tick_rate_clamping(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """Extreme rate -> clamped to [-50, 50]."""
        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={**mock_config_entry.options, CONF_HUMIDITY_SENSOR_1: "sensor.humidity_1"},
        )
        await controller.async_activate()

        t0 = time.monotonic()
        hass.states.async_set("sensor.humidity_1", "10.0")
        with patch("custom_components.brink_ventilation.automation_controller.time") as mock_time:
            mock_time.monotonic.return_value = t0
            _manual_humidity_tick(controller)

        # Huge jump: 10 -> 99 in just 1 second (=5340 %/min raw, clamped to 50)
        hass.states.async_set("sensor.humidity_1", "99.0")
        with patch("custom_components.brink_ventilation.automation_controller.time") as mock_time:
            mock_time.monotonic.return_value = t0 + 1  # 1 second elapsed
            _manual_humidity_tick(controller)

        rate = controller._humidity_rates.get("sensor.humidity_1", 0.0)
        assert rate <= 50.0

    async def test_humidity_delta_tracking(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """humidity_deltas returns per-sensor rates."""
        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={
                **mock_config_entry.options,
                CONF_HUMIDITY_SENSOR_1: "sensor.hum1",
                CONF_HUMIDITY_SENSOR_2: "sensor.hum2",
            },
        )
        await controller.async_activate()

        t0 = time.monotonic()
        hass.states.async_set("sensor.hum1", "50.0")
        hass.states.async_set("sensor.hum2", "60.0")
        with patch("custom_components.brink_ventilation.automation_controller.time") as mock_time:
            mock_time.monotonic.return_value = t0
            _manual_humidity_tick(controller)

        hass.states.async_set("sensor.hum1", "51.0")
        hass.states.async_set("sensor.hum2", "62.0")
        with patch("custom_components.brink_ventilation.automation_controller.time") as mock_time:
            mock_time.monotonic.return_value = t0 + 60
            _manual_humidity_tick(controller)

        deltas = controller.humidity_deltas
        assert "sensor.hum1" in deltas
        assert "sensor.hum2" in deltas
        # hum1: (51-50)/1min = 1.0, hum2: (62-60)/1min = 2.0
        assert deltas["sensor.hum1"] == pytest.approx(1.0, abs=0.2)
        assert deltas["sensor.hum2"] == pytest.approx(2.0, abs=0.2)

    async def test_humidity_timer_restart_on_options_update(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """Options changed -> timer restarted and old data cleared."""
        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={**mock_config_entry.options, CONF_HUMIDITY_SENSOR_1: "sensor.hum1"},
        )
        await controller.async_activate()

        # Seed some humidity data
        hass.states.async_set("sensor.hum1", "50.0")
        _manual_humidity_tick(controller)
        assert "sensor.hum1" in controller._humidity_previous

        # Update options (simulates config flow change)
        await controller.async_options_updated()

        # Previous data should be cleared
        assert len(controller._humidity_previous) == 0
        assert len(controller._humidity_rates) == 0

    async def test_humidity_tick_nan_value(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """NaN humidity value -> skipped (no crash, no rate recorded)."""
        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={**mock_config_entry.options, CONF_HUMIDITY_SENSOR_1: "sensor.humidity_1"},
        )
        await controller.async_activate()

        hass.states.async_set("sensor.humidity_1", "nan")
        _manual_humidity_tick(controller)
        assert "sensor.humidity_1" not in controller._humidity_rates

    async def test_humidity_tick_inf_value(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """Inf humidity value -> skipped (no crash, no rate recorded)."""
        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={**mock_config_entry.options, CONF_HUMIDITY_SENSOR_1: "sensor.humidity_1"},
        )
        await controller.async_activate()

        hass.states.async_set("sensor.humidity_1", "inf")
        _manual_humidity_tick(controller)
        assert "sensor.humidity_1" not in controller._humidity_rates

    async def test_humidity_tick_empty_state(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """Empty string state -> skipped."""
        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={**mock_config_entry.options, CONF_HUMIDITY_SENSOR_1: "sensor.humidity_1"},
        )
        await controller.async_activate()

        hass.states.async_set("sensor.humidity_1", "")
        _manual_humidity_tick(controller)
        assert "sensor.humidity_1" not in controller._humidity_rates

    async def test_humidity_tick_nonexistent_sensor(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """Sensor not in hass.states (never existed) -> skipped."""
        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={**mock_config_entry.options, CONF_HUMIDITY_SENSOR_1: "sensor.nonexistent"},
        )
        await controller.async_activate()

        _manual_humidity_tick(controller)
        assert "sensor.nonexistent" not in controller._humidity_rates

    async def test_humidity_no_sensors_configured(
        self,
        controller: BrinkAutomationController,
    ) -> None:
        """No humidity sensors configured -> start_humidity_timer is a no-op."""
        # Default mock_config_entry has no humidity sensors
        assert controller.configured_humidity_sensors == []
        await controller.async_start_humidity_monitoring()
        # Timer should not be set (no sensors to monitor)
        assert controller._humidity_timer_unsub is None

    async def test_max_humidity_delta_empty(
        self,
        controller: BrinkAutomationController,
    ) -> None:
        """max_humidity_delta returns 0.0 when no sensors are tracked."""
        assert controller.max_humidity_delta == 0.0

    async def test_configured_humidity_sensors_property(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """configured_humidity_sensors returns only non-empty entity_ids."""
        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={
                **mock_config_entry.options,
                CONF_HUMIDITY_SENSOR_1: "sensor.hum1",
                CONF_HUMIDITY_SENSOR_2: "",
                CONF_HUMIDITY_SENSOR_3: "sensor.hum3",
            },
        )
        sensors = controller.configured_humidity_sensors
        assert sensors == ["sensor.hum1", "sensor.hum3"]


# =====================================================================
# Season evaluation tests
# =====================================================================


class TestSeasonEvaluation:
    """Tests for season detection from temperature data."""

    async def test_season_summer(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Temp >= threshold -> SEASON_SUMMER."""
        # Default threshold is -2.0, API reports 15.0 -> summer
        await controller.async_activate()
        assert controller.season == SEASON_SUMMER

    async def test_season_winter(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Temp < threshold -> SEASON_WINTER."""
        _set_coordinator_param(mock_coordinator, PARAM_FRESH_AIR_TEMP, "-5.0")
        await controller.async_activate()
        assert controller.season == SEASON_WINTER

    async def test_season_from_config_entity(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """External temp entity used for season detection."""
        hass.states.async_set("sensor.outdoor_temp", "20.0")
        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={
                **mock_config_entry.options,
                CONF_TEMPERATURE_SOURCE_ENTITY: "sensor.outdoor_temp",
            },
        )
        await controller.async_activate()
        assert controller.season == SEASON_SUMMER

    async def test_season_from_api_param(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """Falls back to PARAM_FRESH_AIR_TEMP when no temp entity configured."""
        # No temp entity configured (default); API has 15.0 -> summer
        assert mock_config_entry.options.get(CONF_TEMPERATURE_SOURCE_ENTITY, "") == ""
        await controller.async_activate()
        assert controller.season == SEASON_SUMMER

    async def test_season_unavailable_temp(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """No temp available -> season unchanged."""
        hass.states.async_set("sensor.outdoor_temp", "unavailable")
        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={
                **mock_config_entry.options,
                CONF_TEMPERATURE_SOURCE_ENTITY: "sensor.outdoor_temp",
            },
        )
        # Remove API temp to simulate total unavailability
        for device in mock_coordinator.data.values():
            for component in device.get("components", []):
                component["parameters"].pop(PARAM_FRESH_AIR_TEMP, None)

        # Set an initial season manually so we can verify it does not change
        controller._season = SEASON_WINTER
        await controller.async_activate()
        assert controller.season == SEASON_WINTER  # unchanged

    async def test_season_change_on_coordinator_update(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Season changes -> level re-applied."""
        await controller.async_activate()
        assert controller.season == SEASON_SUMMER
        mock_coordinator.client.write_parameters.reset_mock()

        # Change API temp to winter and update the ventilation level to the
        # expected winter base level so the watchdog does not also fire.
        _set_coordinator_param(mock_coordinator, PARAM_FRESH_AIR_TEMP, "-10.0")
        _set_coordinator_param(
            mock_coordinator,
            PARAM_VENTILATION_LEVEL,
            str(DEFAULT_AUTO_WINTER_BASE_LEVEL),
        )
        await controller.async_on_coordinator_update()

        assert controller.season == SEASON_WINTER
        # Should have re-applied level for the new season
        mock_coordinator.client.write_parameters.assert_awaited_once()


# =====================================================================
# Level watchdog tests
# =====================================================================


class TestLevelWatchdog:
    """Tests for the level watchdog that corrects drift."""

    async def test_watchdog_level_matches(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Actual level matches expected -> no correction."""
        await controller.async_activate()
        # After activation, expected is summer base level (default=2), mode=1
        # Coordinator data already has ventilation_level=2, operating_mode=1
        mock_coordinator.client.write_parameters.reset_mock()

        await controller.async_on_coordinator_update()
        # No correction write should happen
        mock_coordinator.client.write_parameters.assert_not_awaited()

    async def test_watchdog_level_mismatch_corrects(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Actual level != expected -> re-applies correct level."""
        await controller.async_activate()
        mock_coordinator.client.write_parameters.reset_mock()

        # Simulate someone changing the level externally
        _set_coordinator_param(mock_coordinator, PARAM_VENTILATION_LEVEL, "0")

        await controller.async_on_coordinator_update()
        # Watchdog should correct the level
        mock_coordinator.client.write_parameters.assert_awaited_once()

    async def test_watchdog_mode_mismatch_corrects(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Operating mode != '1' (manual) -> re-applies."""
        await controller.async_activate()
        mock_coordinator.client.write_parameters.reset_mock()

        # Simulate mode changed externally to automatic (0)
        _set_coordinator_param(mock_coordinator, PARAM_OPERATING_MODE, "0")

        await controller.async_on_coordinator_update()
        mock_coordinator.client.write_parameters.assert_awaited_once()

    async def test_watchdog_skips_when_idle(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """IDLE state -> watchdog does not run."""
        assert controller.state == AutomationState.IDLE

        # Set a level mismatch
        _set_coordinator_param(mock_coordinator, PARAM_VENTILATION_LEVEL, "0")

        await controller.async_on_coordinator_update()
        # No write should have happened since we are IDLE
        mock_coordinator.client.write_parameters.assert_not_awaited()

    async def test_watchdog_skips_when_pending_writes(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Pending writes exist -> watchdog skips (tested via _verify_level directly)."""
        await controller.async_activate()
        mock_coordinator.client.write_parameters.reset_mock()

        # Simulate pending writes
        controller._pending_writes = [(1001, "2")]

        # Change level to trigger mismatch
        _set_coordinator_param(mock_coordinator, PARAM_VENTILATION_LEVEL, "0")

        # Call _verify_level directly to isolate it from the retry logic
        await controller._verify_level()

        # No correction should have been made because pending_writes is set
        mock_coordinator.client.write_parameters.assert_not_awaited()

    async def test_watchdog_cooldown(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Correction made -> skip next correction within 5 minutes."""
        await controller.async_activate()
        mock_coordinator.client.write_parameters.reset_mock()

        # First mismatch -> triggers correction
        _set_coordinator_param(mock_coordinator, PARAM_VENTILATION_LEVEL, "0")
        await controller.async_on_coordinator_update()
        mock_coordinator.client.write_parameters.assert_awaited_once()
        mock_coordinator.client.write_parameters.reset_mock()

        # Second mismatch within cooldown -> should be skipped
        _set_coordinator_param(mock_coordinator, PARAM_VENTILATION_LEVEL, "0")
        await controller.async_on_coordinator_update()
        mock_coordinator.client.write_parameters.assert_not_awaited()

    async def test_watchdog_cooldown_expired(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """5+ minutes since last correction -> allows new correction."""
        await controller.async_activate()
        mock_coordinator.client.write_parameters.reset_mock()

        # First correction
        _set_coordinator_param(mock_coordinator, PARAM_VENTILATION_LEVEL, "0")
        await controller.async_on_coordinator_update()
        mock_coordinator.client.write_parameters.assert_awaited_once()
        mock_coordinator.client.write_parameters.reset_mock()

        # Simulate cooldown expired (set last correction to >5 min ago)
        controller._last_level_correction = time.monotonic() - 301

        # Second mismatch after cooldown -> should correct
        _set_coordinator_param(mock_coordinator, PARAM_VENTILATION_LEVEL, "0")
        await controller.async_on_coordinator_update()
        mock_coordinator.client.write_parameters.assert_awaited_once()

    async def test_watchdog_no_coordinator_data(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """No data -> watchdog skips."""
        await controller.async_activate()
        mock_coordinator.client.write_parameters.reset_mock()

        # Empty coordinator data
        mock_coordinator.data = {}

        await controller.async_on_coordinator_update()
        # No correction because there is no data to compare
        mock_coordinator.client.write_parameters.assert_not_awaited()


# =====================================================================
# Write queue tests
# =====================================================================


class TestWriteQueue:
    """Tests for the resilient write queue."""

    async def test_write_params_success(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Writes succeed -> pending cleared, expedited polling started."""
        params = [(1002, "1"), (1001, "2")]
        await controller.async_write_params(params)

        mock_coordinator.client.write_parameters.assert_awaited_once_with(SYSTEM_ID, params)
        mock_coordinator.start_expedited_polling.assert_called_once()
        assert controller._pending_writes is None

    async def test_write_params_auth_error(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """BrinkAuthError -> not retried, event fired."""
        mock_coordinator.client.write_parameters.side_effect = BrinkAuthError("auth fail")

        events = []
        hass.bus.async_listen(EVENT_WRITE_FAILED, lambda e: events.append(e))

        params = [(1002, "1"), (1001, "2")]
        await controller.async_write_params(params)

        # Should NOT be queued for retry
        assert controller._pending_writes is None
        mock_coordinator.start_expedited_polling.assert_not_called()

        # Event should have been fired
        await hass.async_block_till_done()
        assert len(events) == 1
        assert events[0].data["error"] == "BrinkAuthError"

    async def test_write_params_other_error(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Other exception -> queued for retry, event fired."""
        mock_coordinator.client.write_parameters.side_effect = ConnectionError("network")

        events = []
        hass.bus.async_listen(EVENT_WRITE_FAILED, lambda e: events.append(e))

        params = [(1002, "1"), (1001, "2")]
        await controller.async_write_params(params)

        # Should be queued
        assert controller._pending_writes == params
        mock_coordinator.start_expedited_polling.assert_not_called()

        await hass.async_block_till_done()
        assert len(events) == 1
        assert events[0].data["error"] == "ConnectionError"

    async def test_retry_pending_writes(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """On coordinator update -> retries pending writes."""
        await controller.async_activate()

        # Queue a failed write
        params = [(1002, "1"), (1001, "2")]
        mock_coordinator.client.write_parameters.side_effect = ConnectionError("fail")
        await controller.async_write_params(params)
        assert controller._pending_writes == params

        # Now make writes succeed
        mock_coordinator.client.write_parameters.side_effect = None
        mock_coordinator.client.write_parameters.reset_mock()

        await controller.async_on_coordinator_update()

        # Pending writes should have been retried and cleared
        mock_coordinator.client.write_parameters.assert_awaited()
        assert controller._pending_writes is None

    async def test_write_params_no_system_id(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """No system_id -> queued without calling API."""
        mock_coordinator.data = {}  # No data means no system_id

        params = [(1002, "1"), (1001, "2")]
        await controller.async_write_params(params)

        mock_coordinator.client.write_parameters.assert_not_awaited()
        assert controller._pending_writes == params


# =====================================================================
# Seasonal level tests
# =====================================================================


class TestSeasonalLevels:
    """Tests for seasonal level computation."""

    async def test_seasonal_base_level_summer(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Returns configured summer base level."""
        # Default API temp is 15.0 -> summer, default summer base = 2
        await controller.async_activate()
        assert controller.season == SEASON_SUMMER
        assert controller._get_seasonal_base_level() == DEFAULT_AUTO_SUMMER_BASE_LEVEL

    async def test_seasonal_base_level_winter(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Returns configured winter base level."""
        _set_coordinator_param(mock_coordinator, PARAM_FRESH_AIR_TEMP, "-5.0")
        await controller.async_activate()
        assert controller.season == SEASON_WINTER
        assert controller._get_seasonal_base_level() == DEFAULT_AUTO_WINTER_BASE_LEVEL

    async def test_seasonal_max_level_summer(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Returns configured summer max level."""
        await controller.async_activate()
        assert controller.season == SEASON_SUMMER
        assert controller._get_seasonal_max_level() == DEFAULT_EXTRA_VENT_SUMMER_LEVEL

    async def test_seasonal_max_level_winter(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Returns configured winter max level."""
        _set_coordinator_param(mock_coordinator, PARAM_FRESH_AIR_TEMP, "-5.0")
        await controller.async_activate()
        assert controller.season == SEASON_WINTER
        assert controller._get_seasonal_max_level() == DEFAULT_EXTRA_VENT_WINTER_LEVEL


# =====================================================================
# Cleanup test
# =====================================================================


class TestCleanup:
    """Tests for async_cleanup teardown."""

    async def test_async_cleanup(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
        mock_config_entry: MockConfigEntry,
    ) -> None:
        """Cancels all timers, resets all state."""
        # Get into a complex state: BASE with humidity monitoring
        hass = controller._hass
        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={**mock_config_entry.options, CONF_HUMIDITY_SENSOR_1: "sensor.hum"},
        )
        await controller.async_activate()
        assert controller.state == AutomationState.BASE

        # Add a pending write
        controller._pending_writes = [(1001, "2")]
        controller._humidity_rates["sensor.hum"] = 1.5
        controller._humidity_previous["sensor.hum"] = (time.monotonic(), 50.0)

        await controller.async_cleanup()

        assert controller.state == AutomationState.IDLE
        assert controller.season is None
        assert controller._pending_writes is None
        assert controller._boost_end_monotonic == 0.0
        assert controller._boost_trigger is None
        assert controller._boost_trigger_entity is None
        assert controller._boost_trigger_rate is None
        assert controller._last_level_correction == 0.0
        assert len(controller._humidity_previous) == 0
        assert len(controller._humidity_rates) == 0
        assert controller._boost_timer_unsub is None
        assert controller._countdown_timer_unsub is None
        assert controller._humidity_timer_unsub is None


# =====================================================================
# Boost timer expiry tests
# =====================================================================


class TestBoostTimerExpiry:
    """Tests for boost timer expiration callbacks."""

    async def test_boost_timer_returns_to_base(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Timer expired with _was_in_base -> returns to BASE."""
        await controller.async_activate()
        await controller.async_activate_extra_ventilation()
        assert controller.state == AutomationState.BOOSTED
        assert controller._was_in_base_before_boost is True

        # Cancel the real boost timer to avoid lingering timers in teardown,
        # then simulate the callback firing as if it had expired.
        controller._cancel_boost_timer()
        controller._cancel_countdown_timer()
        controller._async_boost_timer_expired(None)
        await hass.async_block_till_done()

        assert controller.state == AutomationState.BASE

    async def test_boost_timer_returns_to_idle(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Timer expired without _was_in_base -> returns to IDLE."""
        # Activate boost directly from IDLE (not from BASE)
        await controller.async_activate_extra_ventilation()
        assert controller.state == AutomationState.BOOSTED
        assert controller._was_in_base_before_boost is False

        # Cancel the real boost timer to avoid lingering timers in teardown,
        # then simulate the callback firing as if it had expired.
        controller._cancel_boost_timer()
        controller._cancel_countdown_timer()
        controller._async_boost_timer_expired(None)
        await hass.async_block_till_done()

        assert controller.state == AutomationState.IDLE

    async def test_boost_timer_expired_while_not_boosted(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Timer fires but state is not BOOSTED (edge case) -> no-op."""
        await controller.async_activate()
        assert controller.state == AutomationState.BASE

        # Simulate timer callback firing unexpectedly
        controller._async_boost_timer_expired(None)

        # Should remain in BASE
        assert controller.state == AutomationState.BASE


# =====================================================================
# Parameter lookup helper tests
# =====================================================================


class TestParameterLookup:
    """Tests for _find_param_value_id, _build_mode_and_level_params, etc."""

    async def test_find_param_value_id_success(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Finds the correct value_id for a known parameter."""
        value_id = controller._find_param_value_id(PARAM_VENTILATION_LEVEL)
        assert value_id == 1001

    async def test_find_param_value_id_not_found(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Returns None for a parameter that does not exist."""
        value_id = controller._find_param_value_id("nonexistent_param")
        assert value_id is None

    async def test_find_param_value_id_no_data(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Returns None when coordinator data is empty."""
        mock_coordinator.data = {}
        value_id = controller._find_param_value_id(PARAM_VENTILATION_LEVEL)
        assert value_id is None

    async def test_build_mode_and_level_params(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Builds correct param list for mode and level."""
        params = controller._build_mode_and_level_params("1", "2")
        assert len(params) == 2
        # mode_vid=1002, level_vid=1001
        assert (1002, "1") in params
        assert (1001, "2") in params

    async def test_build_mode_and_level_params_missing_ids(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Returns empty list when value_ids cannot be found."""
        mock_coordinator.data = {}
        params = controller._build_mode_and_level_params("1", "2")
        assert params == []

    async def test_get_system_id(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Returns the first system_id from coordinator data."""
        system_id = controller._get_system_id()
        assert system_id == SYSTEM_ID

    async def test_get_system_id_no_data(
        self,
        controller: BrinkAutomationController,
        mock_coordinator: MagicMock,
    ) -> None:
        """Returns None when coordinator data is empty."""
        mock_coordinator.data = {}
        system_id = controller._get_system_id()
        assert system_id is None


# =====================================================================
# Options update tests
# =====================================================================


class TestOptionsUpdate:
    """Tests for async_options_updated behavior."""

    async def test_options_update_when_idle(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
        mock_coordinator: MagicMock,
    ) -> None:
        """Options update while IDLE -> restarts humidity timer only."""
        assert controller.state == AutomationState.IDLE
        mock_coordinator.client.write_parameters.reset_mock()

        await controller.async_options_updated()

        # No level write should happen when IDLE
        mock_coordinator.client.write_parameters.assert_not_awaited()

    async def test_options_update_season_change(
        self,
        hass: HomeAssistant,
        controller: BrinkAutomationController,
        mock_config_entry: MockConfigEntry,
        mock_coordinator: MagicMock,
    ) -> None:
        """Options update in BASE with season change -> re-applies level."""
        await controller.async_activate()
        assert controller.season == SEASON_SUMMER
        mock_coordinator.client.write_parameters.reset_mock()

        # Change the freezing threshold so that 15.0C is now below it -> winter
        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={**mock_config_entry.options, CONF_FREEZING_THRESHOLD: 20.0},
        )
        await controller.async_options_updated()

        assert controller.season == SEASON_WINTER
        # Should have written winter base level
        mock_coordinator.client.write_parameters.assert_awaited_once()
