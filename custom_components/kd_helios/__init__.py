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
from homeassistant.core import EVENT_HOMEASSISTANT_STARTED, HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_PRICE_INTERVAL,
    CONF_PROVIDER,
    DEFAULT_PRICE_INTERVAL,
    DOMAIN,
    ISSUE_NO_DEVICES,
    SUBENTRY_TYPE_DEVICE,
    SUBENTRY_TYPE_GRID,
)
from .coordinator import HeliosEngine, PriceCoordinator
from .panel import async_register_panel
from .services import async_setup_services
from .storage import HeliosStorage
from .websocket import async_register_websocket_api

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


@dataclass
class HeliosRuntimeData:
    """Runtime objects owned by one config entry."""

    prices: PriceCoordinator
    engine: HeliosEngine
    storage: HeliosStorage


type HeliosConfigEntry = ConfigEntry[HeliosRuntimeData]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the actions, the panel API and the sidebar panel."""
    async_setup_services(hass)
    async_register_websocket_api(hass)

    async def _register_panel(_event: object = None) -> None:
        await async_register_panel(hass)

    if "frontend" in hass.config.components:
        await _register_panel()
    else:
        # The panel needs the frontend; in a normal install it is set up before
        # startup completes, so this covers a cold start.
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _register_panel)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HeliosConfigEntry) -> bool:
    """Set up KD Helios from a config entry."""
    provider: str = entry.data[CONF_PROVIDER]
    interval: str = entry.data.get(CONF_PRICE_INTERVAL, DEFAULT_PRICE_INTERVAL)

    prices = PriceCoordinator(hass, provider, interval)
    await prices.async_config_entry_first_refresh()

    storage = HeliosStorage(hass, entry.entry_id)
    await storage.async_load()

    engine = HeliosEngine(hass, entry, prices, storage)
    # The engine listens to the price coordinator: it recomputes as soon as new
    # prices land, and the listener keeps the polling interval armed.
    # The engine listens to the price coordinator: it recomputes snapshots as
    # soon as new prices land, and that listener is what keeps the price
    # coordinator's polling interval armed.
    entry.async_on_unload(prices.async_add_listener(engine.async_prices_updated))
    await engine.async_config_entry_first_refresh()

    entry.runtime_data = HeliosRuntimeData(
        prices=prices, engine=engine, storage=storage
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Adding, editing or removing a device subentry must rebuild the trackers
    # and the entities, so a reload is the simplest correct response.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    _async_sync_no_device_issue(hass, entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HeliosConfigEntry) -> bool:
    """Unload a config entry and flush the accumulators."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.storage.async_save()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: HeliosConfigEntry) -> None:
    """Remove persisted accounting state when the entry is deleted."""
    ir.async_delete_issue(hass, DOMAIN, _no_device_issue_id(entry))
    await HeliosStorage(hass, entry.entry_id).async_remove()


def _no_device_issue_id(entry: HeliosConfigEntry) -> str:
    """Return the repair issue id for an entry without devices."""
    return f"{ISSUE_NO_DEVICES}_{entry.entry_id}"


def _async_sync_no_device_issue(hass: HomeAssistant, entry: HeliosConfigEntry) -> None:
    """Nudge the user to add a device, since the tariff alone meters nothing.

    Devices are config subentries, which are added from the integration page
    rather than from the initial setup wizard. Without this hint a fresh
    install looks finished but tracks nothing.
    """
    issue_id = _no_device_issue_id(entry)
    tracks_something = entry.get_subentries_of_type(
        SUBENTRY_TYPE_DEVICE
    ) or entry.get_subentries_of_type(SUBENTRY_TYPE_GRID)
    if tracks_something:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_NO_DEVICES,
    )


async def _async_reload_entry(hass: HomeAssistant, entry: HeliosConfigEntry) -> None:
    """Reload the entry after a subentry change."""
    await hass.config_entries.async_reload(entry.entry_id)
