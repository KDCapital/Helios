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
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.core import EVENT_HOMEASSISTANT_STARTED, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_PRICE_INTERVAL,
    CONF_PROVIDER,
    DEFAULT_PRICE_INTERVAL,
    DOMAIN,
    ISSUE_NO_DEVICES,
    SUBENTRY_TYPE_GRID,
)
from .coordinator import HeliosEngine, PriceCoordinator
from .models import device_subentries
from .panel import async_register_panel
from .services import async_setup_services
from .storage import HeliosStorage
from .websocket import async_register_websocket_api

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
]


@dataclass
class HeliosRuntimeData:
    """Runtime objects owned by one config entry."""

    prices: PriceCoordinator
    engine: HeliosEngine
    storage: HeliosStorage


type HeliosConfigEntry = ConfigEntry[HeliosRuntimeData]

#: Entity fields whose first usable state should trigger an accounting tick.
#: Home Assistant sets custom integrations up concurrently, so the inverter's
#: sensors can still be unavailable (or not created yet) during our first
#: refresh; a reload was the old way to pick them up.
_SOURCE_ENTITY_FIELDS = (
    "soc_entity",
    "house_load_entity",
    "battery_power_entity",
    "battery_voltage_entity",
    "battery_temp_entity",
    "solar_forecast_entity",
    "import_entity",
    "export_entity",
    "import_power_entity",
    "export_power_entity",
    "power_entity",
    "energy_entity",
    "ev_soc_entity",
    "ev_power_entity",
    "ev_capacity_entity",
    "ev_target_soc_entity",
    "ev_departure_entity",
)


def _source_entities(engine: HeliosEngine) -> set[str]:
    """Return the configured source entities the engine reads."""
    configs = [engine.battery_config]
    if engine.grid_tracker is not None:
        configs.append(engine.grid_tracker.config)
    configs.extend(tracker.config for tracker in engine.trackers.values())

    entities: set[str] = set()
    for config in configs:
        if config is None:
            continue
        for field in _SOURCE_ENTITY_FIELDS:
            value = getattr(config, field, None)
            if isinstance(value, str) and value:
                entities.add(value)
    return entities


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
    _async_refresh_when_sources_are_ready(hass, entry, engine)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Adding, editing or removing a device subentry must rebuild the trackers
    # and the entities, so a reload is the simplest correct response.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    _async_sync_no_device_issue(hass, entry)
    return True


def _async_refresh_when_sources_are_ready(
    hass: HomeAssistant,
    entry: HeliosConfigEntry,
    engine: HeliosEngine,
) -> None:
    """Refresh once the inverter's sensors become usable.

    The first refresh during a cold start can run before Solarman has created
    or populated its entities. Reloading the integration then worked because it
    ran the refresh later. This does that automatically: a source entity that
    turns usable triggers a tick, and a refresh after Home Assistant has
    started catches everything that was already usable but not yet created.
    """

    @callback
    def _source_ready(event) -> None:
        old = event.data.get("old_state")
        new = event.data.get("new_state")
        if new is None or new.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        if old is not None and old.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        hass.async_create_task(engine.async_refresh())

    entities = _source_entities(engine)
    if entities:
        entry.async_on_unload(
            async_track_state_change_event(hass, list(entities), _source_ready)
        )

    async def _refresh_after_start(_event: object = None) -> None:
        await engine.async_request_refresh()

    if hass.is_running:
        hass.async_create_task(engine.async_refresh())
    else:
        entry.async_on_unload(
            hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, _refresh_after_start
            )
        )


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
    tracks_something = device_subentries(entry) or entry.get_subentries_of_type(
        SUBENTRY_TYPE_GRID
    )
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
