"""DataUpdateCoordinator for Brink Home ventilation."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    CONNECTION_STATUS_AUTH_ERROR,
    CONNECTION_STATUS_CONNECTED,
    CONNECTION_STATUS_CONNECTION_ERROR,
    CONNECTION_STATUS_NO_INTERNET,
    CONNECTION_STATUS_SERVER_ERROR,
    CONNECTION_STATUS_TIMEOUT,
    CONNECTION_STATUS_UNKNOWN_ERROR,
    CONNECTIVITY_CHECK_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EXPEDITED_DURATION,
    EXPEDITED_INTERVAL,
)
from .automation_controller import BrinkAutomationController
from .core.brink_home_cloud import BrinkAuthError, BrinkHomeCloud

_LOGGER = logging.getLogger(__name__)


class BrinkDataCoordinator(DataUpdateCoordinator[dict[int, dict[str, Any]]]):
    """Coordinator that fetches data from Brink Home API.

    Data is a dict keyed by system_id, where each value is a device dict
    containing system_id, name, serial_number, gateway_state,
    and components (with parameters).
    """

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: BrinkHomeCloud,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        scan_interval = int(
            entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.config_entry = entry
        self.client = client
        self._expedited_unsub: CALLBACK_TYPE | None = None
        self._expedited_normal_interval: timedelta | None = None
        self.automation_controller = BrinkAutomationController(hass, self, entry)

        # Connection status tracking
        self.connection_status: str = CONNECTION_STATUS_CONNECTED
        self.last_successful_poll: datetime | None = None
        self.last_error_message: str | None = None
        self.consecutive_errors: int = 0

    async def _async_update_data(self) -> dict[int, dict[str, Any]]:
        """Fetch data from Brink Home API."""
        try:
            data = await self._fetch_devices()
        except BrinkAuthError as ex:
            self._record_error(CONNECTION_STATUS_AUTH_ERROR, str(ex))
            _LOGGER.warning(
                "Brink authentication failed: %s — "
                "check Settings > Devices & Services > Brink Ventilation "
                "for re-authentication",
                ex,
            )
            raise ConfigEntryAuthFailed(f"Authentication failed: {ex}") from ex
        except aiohttp.ClientResponseError as ex:
            if ex.status == 401:
                _LOGGER.debug(
                    "Brink API returned 401, attempting re-authentication"
                )
                try:
                    await self.client.login()
                    data = await self._fetch_devices()
                except BrinkAuthError as retry_ex:
                    self._record_error(CONNECTION_STATUS_AUTH_ERROR, str(retry_ex))
                    _LOGGER.warning(
                        "Brink re-authentication failed: %s", retry_ex
                    )
                    raise ConfigEntryAuthFailed(
                        f"Re-authentication failed: {retry_ex}"
                    ) from retry_ex
                except aiohttp.ClientResponseError as retry_ex:
                    if retry_ex.status == 401:
                        self._record_error(CONNECTION_STATUS_AUTH_ERROR, str(retry_ex))
                        _LOGGER.warning(
                            "Brink API still returning 401 after "
                            "re-authentication — credentials may have changed"
                        )
                        raise ConfigEntryAuthFailed(
                            "Re-authentication failed (HTTP 401)"
                        ) from retry_ex
                    self._record_error(
                        CONNECTION_STATUS_SERVER_ERROR,
                        f"HTTP {retry_ex.status}: {retry_ex.message}",
                    )
                    _LOGGER.warning(
                        "Brink API error HTTP %s during re-authentication",
                        retry_ex.status,
                    )
                    raise UpdateFailed(
                        f"API error during re-auth (HTTP {retry_ex.status})"
                    ) from retry_ex
                except (aiohttp.ClientError, asyncio.TimeoutError) as retry_ex:
                    await self._record_connectivity_error(retry_ex)
                    _LOGGER.warning(
                        "Connection lost to Brink during re-authentication: %s",
                        retry_ex,
                    )
                    raise UpdateFailed(
                        f"Connection lost during re-auth: {retry_ex}"
                    ) from retry_ex
            else:
                self._record_error(
                    CONNECTION_STATUS_SERVER_ERROR,
                    f"HTTP {ex.status}: {ex.message}",
                )
                _LOGGER.warning(
                    "Brink API returned HTTP %s: %s",
                    ex.status,
                    ex.message,
                )
                raise UpdateFailed(
                    f"API error (HTTP {ex.status}): {ex.message}"
                ) from ex
        except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
            await self._record_connectivity_error(ex)
            _LOGGER.warning("Cannot reach Brink Home portal: %s", ex)
            raise UpdateFailed(f"Connection error: {ex}") from ex

        # Success — reset error tracking
        self.connection_status = CONNECTION_STATUS_CONNECTED
        self.last_successful_poll = datetime.now()
        self.last_error_message = None
        self.consecutive_errors = 0

        # Notify automation controller of fresh data
        await self.automation_controller.async_on_coordinator_update()
        return data

    def _record_error(self, status: str, message: str) -> None:
        """Record a connection error without internet check."""
        self.connection_status = status
        self.last_error_message = message
        self.consecutive_errors += 1

    async def _record_connectivity_error(
        self, ex: aiohttp.ClientError | asyncio.TimeoutError
    ) -> None:
        """Record a connectivity error, checking internet if needed.

        When the Brink API is unreachable (timeout or connection error),
        perform a lightweight internet connectivity check to distinguish
        'no_internet' from 'connection_error' / 'timeout'.
        """
        has_internet = await self._check_internet_connectivity()
        if has_internet:
            if isinstance(ex, asyncio.TimeoutError):
                status = CONNECTION_STATUS_TIMEOUT
            else:
                status = CONNECTION_STATUS_CONNECTION_ERROR
        else:
            status = CONNECTION_STATUS_NO_INTERNET
        self._record_error(status, str(ex))

    async def _check_internet_connectivity(self) -> bool:
        """Check general internet connectivity with a lightweight request.

        Uses Google's generate_204 endpoint which returns a tiny 204 response.
        Returns True if internet is reachable, False otherwise.
        """
        try:
            session = self.client._session
            async with asyncio.timeout(5):
                async with session.head(CONNECTIVITY_CHECK_URL) as resp:
                    return resp.status == 204
        except Exception:  # noqa: BLE001
            return False

    async def _fetch_devices(self) -> dict[int, dict[str, Any]]:
        """Fetch all systems and their parameters."""
        systems = await self.client.get_systems()

        devices: dict[int, dict[str, Any]] = {}
        for system in systems:
            system_id = system.get("system_id")
            if system_id is None:
                _LOGGER.warning("Skipping system with missing system_id")
                continue

            try:
                device_data = await self.client.get_device_data(system_id)
            except (
                aiohttp.ClientResponseError,
                aiohttp.ClientError,
                asyncio.TimeoutError,
            ) as ex:
                _LOGGER.warning(
                    "Failed to fetch data for system %s: %s", system_id, ex
                )
                continue

            devices[system_id] = {
                "system_id": system_id,
                "name": system.get("name", "Brink Ventilation"),
                "serial_number": system.get("serial_number", ""),
                "gateway_state": system.get("gateway_state"),
                "components": device_data.get("components", []),
            }

        if systems and not devices:
            raise UpdateFailed(
                f"Failed to fetch data for all {len(systems)} systems"
            )

        _LOGGER.debug("Fetched %s devices with parameters", len(devices))
        return devices

    @callback
    def start_expedited_polling(self) -> None:
        """Temporarily boost polling to every 15s for 3 minutes after a write.

        Multiple calls reset the 3-minute timer so expedited mode stays active
        while the user is actively making changes.
        """
        if self._expedited_unsub is not None:
            self._expedited_unsub()

        if self._expedited_normal_interval is None:
            self._expedited_normal_interval = self.update_interval

        self.update_interval = timedelta(seconds=EXPEDITED_INTERVAL)
        _LOGGER.debug(
            "Expedited polling started (every %ss for %ss)",
            EXPEDITED_INTERVAL,
            EXPEDITED_DURATION,
        )

        @callback
        def _restore(_now: Any) -> None:
            if self._expedited_normal_interval is not None:
                self.update_interval = self._expedited_normal_interval
                _LOGGER.debug(
                    "Expedited polling ended, restored interval to %s",
                    self._expedited_normal_interval,
                )
            self._expedited_normal_interval = None
            self._expedited_unsub = None

        self._expedited_unsub = async_call_later(
            self.hass, EXPEDITED_DURATION, _restore
        )

    @callback
    def cancel_expedited_polling(self) -> None:
        """Cancel expedited polling timer if active."""
        if self._expedited_unsub is not None:
            self._expedited_unsub()
            self._expedited_unsub = None
            self._expedited_normal_interval = None

    async def async_update_scan_interval(self, new_interval: int) -> None:
        """Update the scan interval, respecting expedited polling state."""
        new_td = timedelta(seconds=new_interval)
        if self._expedited_normal_interval is not None:
            self._expedited_normal_interval = new_td
            _LOGGER.info(
                "Scan interval updated to %ss (applies after expedited polling ends)",
                new_interval,
            )
        else:
            self.update_interval = new_td
            _LOGGER.info("Scan interval updated to %s seconds", new_interval)
            # Trigger a refresh so the new interval takes effect immediately
            await self.async_request_refresh()
