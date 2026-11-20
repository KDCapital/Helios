"""The KD Helios integration.

KD Helios tracks what energy actually costs. It prices energy from the
epexprijzen.nl tariff of the user's own energy provider, meters individual
devices from an existing power or energy entity, and understands that an
employer-paid EV charger and ERE certificates change the net cost of a device.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_PRICE_INTERVAL, CONF_PROVIDER, DEFAULT_PRICE_INTERVAL
from .coordinator import HeliosEngine, PriceCoordinator
from .storage import HeliosStorage

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


@dataclass
class HeliosRuntimeData:
    """Runtime objects owned by one config entry."""

    prices: PriceCoordinator
    engine: HeliosEngine
    storage: HeliosStorage


type HeliosConfigEntry = ConfigEntry[HeliosRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: HeliosConfigEntry) -> bool:
    """Set up KD Helios from a config entry."""
    provider: str = entry.data[CONF_PROVIDER]
    interval: str = entry.data.get(CONF_PRICE_INTERVAL, DEFAULT_PRICE_INTERVAL)

    prices = PriceCoordinator(hass, provider, interval)
    await prices.async_config_entry_first_refresh()

    storage = HeliosStorage(hass, entry.entry_id)
    await storage.async_load()

    engine = HeliosEngine(hass, entry, prices, storage)
    await engine.async_config_entry_first_refresh()

    entry.runtime_data = HeliosRuntimeData(
        prices=prices, engine=engine, storage=storage
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Adding, editing or removing a device subentry must rebuild the trackers
    # and the entities, so a reload is the simplest correct response.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HeliosConfigEntry) -> bool:
    """Unload a config entry and flush the accumulators."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.storage.async_save()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: HeliosConfigEntry) -> None:
    """Remove persisted accounting state when the entry is deleted."""
    await HeliosStorage(hass, entry.entry_id).async_remove()


async def _async_reload_entry(hass: HomeAssistant, entry: HeliosConfigEntry) -> None:
    """Reload the entry after a subentry change."""
    await hass.config_entries.async_reload(entry.entry_id)
