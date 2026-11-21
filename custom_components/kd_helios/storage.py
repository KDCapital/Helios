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
    """Store per-device and grid accumulator state for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialise the store for a config entry."""
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}"
        )
        self._devices: dict[str, dict[str, Any]] = {}
        self._grid: dict[str, Any] = {}
        self._battery: dict[str, Any] = {}
        self._control: dict[str, Any] = {}
        self._profile: dict[str, Any] = {}
        self._weekly: dict[str, Any] = {}
        self._integrity: dict[str, Any] = {}

    async def async_load(self) -> None:
        """Load persisted state, tolerating a missing or corrupt file."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            self._devices = {}
            self._grid = {}
            self._battery = {}
            self._control = {}
            self._profile = {}
            self._weekly = {}
            self._integrity = {}
            return

        devices = data.get("devices")
        self._devices = (
            {
                str(key): dict(value)
                for key, value in devices.items()
                if isinstance(value, dict)
            }
            if isinstance(devices, dict)
            else {}
        )

        grid = data.get("grid")
        self._grid = dict(grid) if isinstance(grid, dict) else {}

        battery = data.get("battery")
        self._battery = dict(battery) if isinstance(battery, dict) else {}

        profile = data.get("load_profile")
        self._profile = dict(profile) if isinstance(profile, dict) else {}

        control = data.get("battery_control")
        self._control = dict(control) if isinstance(control, dict) else {}

        weekly = data.get("weekly_charge")
        self._weekly = dict(weekly) if isinstance(weekly, dict) else {}

        integrity = data.get("integrity")
        self._integrity = dict(integrity) if isinstance(integrity, dict) else {}

    def device_state(self, subentry_id: str) -> dict[str, Any]:
        """Return the persisted state for one device, if any."""
        return self._devices.get(subentry_id, {})

    def grid_state(self) -> dict[str, Any]:
        """Return the persisted state for the grid connection, if any."""
        return self._grid

    def battery_state(self) -> dict[str, Any]:
        """Return the persisted applied battery plan, if any."""
        return self._battery

    def async_set_battery(self, battery: dict[str, Any]) -> None:
        """Replace the stored applied plan and schedule a debounced write."""
        if battery == self._battery:
            return
        self._battery = battery
        self._store.async_delay_save(self._as_payload, STORAGE_SAVE_DELAY)

    def profile_state(self) -> dict[str, Any]:
        """Return the stored load profile."""
        return self._profile

    def async_set_profile(self, profile: dict[str, Any]) -> None:
        """Replace the stored load profile and schedule a debounced write."""
        if profile == self._profile:
            return
        self._profile = profile
        self._store.async_delay_save(self._as_payload, STORAGE_SAVE_DELAY)

    def control_state(self) -> dict[str, Any]:
        """Return the persisted inverter control state."""
        return self._control

    def async_set_control(self, control: dict[str, Any]) -> None:
        """Replace the stored control state and schedule a debounced write."""
        if control == self._control:
            return
        self._control = control
        self._store.async_delay_save(self._as_payload, STORAGE_SAVE_DELAY)

    def weekly_state(self) -> dict[str, Any]:
        """Return the persisted weekly full-charge marker."""
        return self._weekly

    def async_set_weekly(self, weekly: dict[str, Any]) -> None:
        """Replace the weekly full-charge marker and schedule a write."""
        if weekly == self._weekly:
            return
        self._weekly = weekly
        self._store.async_delay_save(self._as_payload, STORAGE_SAVE_DELAY)

    def integrity_state(self) -> dict[str, Any]:
        """Return the persisted write/integrity counter state."""
        return self._integrity

    def async_set_integrity(self, integrity: dict[str, Any]) -> None:
        """Replace the integrity counters and schedule a write."""
        if integrity == self._integrity:
            return
        self._integrity = integrity
        self._store.async_delay_save(self._as_payload, STORAGE_SAVE_DELAY)

    def async_set_devices(self, devices: dict[str, dict[str, Any]]) -> None:
        """Replace the stored device state and schedule a debounced write."""
        if devices == self._devices:
            return
        self._devices = devices
        self._store.async_delay_save(self._as_payload, STORAGE_SAVE_DELAY)

    def async_set_grid(self, grid: dict[str, Any]) -> None:
        """Replace the stored grid state and schedule a debounced write."""
        if grid == self._grid:
            return
        self._grid = grid
        self._store.async_delay_save(self._as_payload, STORAGE_SAVE_DELAY)

    def _as_payload(self) -> dict[str, Any]:
        """Return the payload written to disk."""
        return {
            "devices": self._devices,
            "grid": self._grid,
            "battery": self._battery,
            "battery_control": self._control,
            "load_profile": self._profile,
            "weekly_charge": self._weekly,
            "integrity": self._integrity,
        }

    async def async_save(self) -> None:
        """Write state to disk immediately."""
        await self._store.async_save(self._as_payload())

    async def async_remove(self) -> None:
        """Remove the store, used when the config entry is deleted."""
        await self._store.async_remove()
