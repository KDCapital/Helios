"""Persistent accounting state for KD Helios.

Coefficients are stored outside the entity registry so that totals, the last
source readings and the timestamp of the last accounting tick survive a restart
and a reload. ``Store`` writes are debounced: the in-memory dict is always
current, the file lags by at most :data:`STORAGE_SAVE_DELAY` seconds.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_SAVE_DELAY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)


class HeliosStorage:
    """Store per-device accumulator state for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialise the store for a config entry."""
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}"
        )
        self._devices: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        """Load persisted state, tolerating a missing or corrupt file."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            self._devices = {}
            return
        devices = data.get("devices")
        if isinstance(devices, dict):
            self._devices = {
                str(key): dict(value)
                for key, value in devices.items()
                if isinstance(value, dict)
            }
        else:
            self._devices = {}

    def device_state(self, subentry_id: str) -> dict[str, Any]:
        """Return the persisted state for one device, if any."""
        return self._devices.get(subentry_id, {})

    def async_set_devices(self, devices: dict[str, dict[str, Any]]) -> None:
        """Replace the stored device state and schedule a debounced write."""
        if devices == self._devices:
            return
        self._devices = devices
        self._store.async_delay_save(self._as_payload, STORAGE_SAVE_DELAY)

    def _as_payload(self) -> dict[str, Any]:
        """Return the payload written to disk."""
        return {"devices": self._devices}

    async def async_save(self) -> None:
        """Write state to disk immediately."""
        await self._store.async_save(self._as_payload())

    async def async_remove(self) -> None:
        """Remove the store, used when the config entry is deleted."""
        await self._store.async_remove()
