"""Switch platform for KD Helios: the inverter control toggle."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HeliosConfigEntry
from .const import DOMAIN, NAME, SUBENTRY_TYPE_BATTERY
from .coordinator import HeliosEngine
from .models import BatteryConfig

PARALLEL_UPDATES = 0


class InverterControlSwitch(CoordinatorEntity[HeliosEngine], SwitchEntity):
    """Enable or disable automatic control of the inverter.

    Turning it on writes the current plan right away and keeps it in sync.
    Turning it off hands the inverter back: the time-of-use programs are
    rewritten so only the generator may charge, and the work mode returns to
    self-consumption.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "inverter_control"
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, coordinator: HeliosEngine, config: BatteryConfig) -> None:
        """Initialise the control switch."""
        super().__init__(coordinator)
        self._config = config
        self._attr_unique_id = f"{config.subentry_id}_inverter_control"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config.subentry_id)},
            name=config.name,
            manufacturer=NAME,
            model=config.model_name,
        )

    @property
    def available(self) -> bool:
        """Control is only possible when the inverter may be written."""
        return (
            super().available
            and self._config.allow_writes
            and self._config.inverter_device_id is not None
        )

    @property
    def is_on(self) -> bool:
        """Return whether KD Helios is driving the inverter."""
        return self.coordinator.control_enabled

    async def _async_set(self, enabled: bool) -> None:
        """Take or release control, surfacing failures to the user."""
        try:
            await self.coordinator.async_set_control(enabled)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        except Exception as err:
            raise HomeAssistantError(f"Could not write the inverter: {err}") from err
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Take control of the inverter."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Release the inverter back to self-consumption."""
        await self._async_set(False)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Explain the state and why it may be unavailable."""
        applied = self.coordinator.applied_plan
        return {
            "writes_enabled": self._config.allow_writes,
            "inverter_configured": self._config.inverter_device_id is not None,
            "applied_saving": round(applied.expected_saving, 2) if applied else None,
            "last_error": self.coordinator.last_control_error,
            "restore_work_mode": self._config.restore_work_mode,
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HeliosConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the inverter control switch."""
    for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_BATTERY):
        config = BatteryConfig.from_subentry(subentry)
        async_add_entities(
            [InverterControlSwitch(entry.runtime_data.engine, config)],
            config_subentry_id=subentry.subentry_id,
        )
