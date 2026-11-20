"""Binary sensor platform for KD Helios."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HeliosConfigEntry
from .const import DOMAIN, NAME, SALDERING_END_DATE, SUBENTRY_TYPE_GRID
from .coordinator import HeliosEngine
from .grid import GridSnapshot
from .models import GridConfig

PARALLEL_UPDATES = 0


class SalderingActiveSensor(CoordinatorEntity[HeliosEngine], BinarySensorEntity):
    """Whether net metering still applies to the grid connection.

    Turns off by itself on :data:`SALDERING_END_DATE`, whatever the stored
    preference says, so the accounting mode and this sensor can never disagree.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "saldering_active"
    _attr_icon = "mdi:scale-balance"

    def __init__(self, coordinator: HeliosEngine, config: GridConfig) -> None:
        """Initialise the saldering sensor."""
        super().__init__(coordinator)
        self._subentry_id = config.subentry_id
        self._attr_unique_id = f"{config.subentry_id}_saldering_active"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config.subentry_id)},
            name=config.name,
            manufacturer=NAME,
            model=config.model_name,
        )

    @property
    def _snapshot(self) -> GridSnapshot | None:
        """Return the grid snapshot."""
        data = self.coordinator.data
        return data.grid if data is not None else None

    @property
    def is_on(self) -> bool:
        """Return True while exported kWh are netted against imports."""
        snapshot = self._snapshot
        return bool(snapshot and snapshot.saldering_active)

    @property
    def extra_state_attributes(self) -> dict[str, str | float | bool | None] | None:
        """Explain the state and when it expires."""
        snapshot = self._snapshot
        if snapshot is None:
            return None
        return {
            "saldering_enabled": snapshot.saldering_enabled,
            "saldering_ends_on": SALDERING_END_DATE.isoformat(),
            "billing_year": snapshot.year,
            "netted_kwh": round(snapshot.netted_kwh, 3),
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HeliosConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the grid connection binary sensor."""
    for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_GRID):
        config = GridConfig.from_subentry(subentry)
        async_add_entities(
            [SalderingActiveSensor(entry.runtime_data.engine, config)],
            config_subentry_id=subentry.subentry_id,
        )
