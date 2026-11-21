"""Work out entity ids from a device the user picked.

Asking for a SOC sensor when the Solarman device is already linked is asking the
user to do something the registry can do. These helpers match entities on a
device by name hints, and are only a fallback: anything set explicitly wins.
"""

from __future__ import annotations

from dataclasses import replace

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .models import BatteryConfig, DeviceConfig, GridConfig

#: Name hints per battery field, tried in order.
BATTERY_HINTS: dict[str, tuple[str, ...]] = {
    "soc_entity": ("state of charge", "battery soc", "soc"),
    "house_load_entity": ("total load", "load power", "house consumption"),
}

#: Name hints per car field for an EVCC loadpoint.
EV_HINTS: dict[str, tuple[str, ...]] = {
    "ev_soc_entity": ("vehicle soc", "car soc", "soc"),
    "ev_power_entity": ("charge power", "power"),
    "ev_departure_entity": ("plan", "departure", "target time"),
    "ev_capacity_entity": ("capacity", "battery size"),
}

#: Name hints for the Joulo ERE sensors.
JOULO_HINTS: dict[str, tuple[str, ...]] = {
    "credits_entity": ("credits", "ere"),
    "kwh_entity": ("total all kwh", "kwh"),
    "price_entity": ("marktprijs", "price"),
}

#: Name hints per grid field for a P1 meter.
GRID_HINTS: dict[str, tuple[str, ...]] = {
    "import_energy_entity": ("total_energy_import", "energy import", "import"),
    "export_energy_entity": ("total_energy_export", "energy export", "export"),
    "import_power_entity": ("active_power", "power"),
    "export_power_entity": ("active_power", "power"),
}


def match_for_device(
    hass: HomeAssistant, device_id: str, hints: dict[str, tuple[str, ...]]
) -> dict[str, str]:
    """Return the entity per field that best matches the hints on a device."""
    registry = er.async_get(hass)
    available: list[tuple[str, str]] = []
    for item in registry.entities.values():
        if item.device_id != device_id or item.disabled_by is not None:
            continue
        label = " ".join(
            part
            for part in (
                item.original_name,
                item.entity_id,
                item.translation_key,
            )
            if part
        ).lower()
        available.append((item.entity_id, label))

    found: dict[str, str] = {}
    for field, needles in hints.items():
        for entity_id, label in available:
            if any(needle in label for needle in needles):
                found[field] = entity_id
                break
    return found


def resolve_battery(hass: HomeAssistant, config: BatteryConfig) -> BatteryConfig:
    """Fill in the battery sensor fields from the linked inverter."""
    if config.inverter_device_id is None:
        return config
    if config.soc_entity and config.house_load_entity:
        return config

    found = match_for_device(hass, config.inverter_device_id, BATTERY_HINTS)
    changes: dict[str, str] = {}
    if not config.soc_entity and "soc_entity" in found:
        changes["soc_entity"] = found["soc_entity"]
    if not config.house_load_entity and "house_load_entity" in found:
        changes["house_load_entity"] = found["house_load_entity"]
    return replace(config, **changes) if changes else config


def resolve_grid(hass: HomeAssistant, config: GridConfig) -> GridConfig:
    """Fill in the metering fields from the meter the user picked."""
    if config.meter_device_id is None:
        return config

    found = match_for_device(hass, config.meter_device_id, GRID_HINTS)
    changes: dict[str, str] = {}
    for field in ("import_energy_entity", "export_energy_entity"):
        if getattr(config, field) is None and field in found:
            changes[field] = found[field]
    if config.import_power_entity is None and "import_power_entity" in found:
        # A signed power sensor serves both directions.
        changes["import_power_entity"] = found["import_power_entity"]
        if config.export_power_entity is None:
            changes["export_power_entity"] = found["import_power_entity"]
    return replace(config, **changes) if changes else config


def resolve_ev(hass: HomeAssistant, config: DeviceConfig) -> DeviceConfig:
    """Fill the car's sensors from the EVCC device that was picked."""
    if config.ev_device_id is None:
        return config

    found = match_for_device(hass, config.ev_device_id, EV_HINTS)
    changes: dict[str, str] = {}
    for field in EV_HINTS:
        if getattr(config, field) is None and field in found:
            changes[field] = found[field]
    return replace(config, **changes) if changes else config


def resolve_joulo(hass: HomeAssistant, config: DeviceConfig) -> DeviceConfig:
    """Fill the ERE sensors from Joulo, when it is installed."""
    ere = config.ere
    if ere.mode.value != "joulo":
        return config
    registry = er.async_get(hass)
    available: list[tuple[str, str]] = []
    for item in registry.entities.values():
        if item.platform != "joulo" or item.disabled_by is not None:
            continue
        label = " ".join(
            part for part in (item.original_name, item.entity_id) if part
        ).lower()
        available.append((item.entity_id, label))

    changes: dict[str, str] = {}
    for field, needles in JOULO_HINTS.items():
        if getattr(ere, field, None) is not None:
            continue
        for entity_id, label in available:
            if any(needle in label for needle in needles):
                changes[field] = entity_id
                break
    if not changes:
        return config
    return replace(config, ere=replace(ere, **changes))
