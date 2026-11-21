"""Configuration through the panel instead of the config flows.

The panel owns the UI; the subentries stay the storage layer, because
``config_subentry_id`` is what binds entities to devices and keeps statistics
attached. Everything here writes those subentries through the public config
entry API, with validation in one place so the wizard and the section forms
cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_KIND,
    CONF_NAME,
    CONF_PRICE_INTERVAL,
    CONF_PROVIDER,
    PRICE_INTERVALS,
    PROVIDERS,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_DEVICE,
    SUBENTRY_TYPE_EV_CHARGER,
    SUBENTRY_TYPE_GRID,
)
from .models import device_subentries

#: Kinds the panel may create, and how each is titled by default.
KINDS: dict[str, str] = {
    SUBENTRY_TYPE_DEVICE: "Device",
    SUBENTRY_TYPE_EV_CHARGER: "EV charger",
    SUBENTRY_TYPE_BATTERY: "Home battery",
    SUBENTRY_TYPE_GRID: "Grid connection",
}

#: Setup step names, in wizard order.
SETUP_STEPS = (
    "welcome",
    "contract",
    "connection",
    "detect",
    "battery",
    "grid",
    "car",
    "control",
)


class ConfigError(Exception):
    """Raised when the panel sends something that cannot be stored."""

    def __init__(self, code: str, message: str) -> None:
        """Store the error code and a human message."""
        super().__init__(message)
        self.code = code
        self.message = message


def _validate(kind: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """Return cleaned data for a subentry, or raise a ConfigError."""
    if kind not in KINDS:
        raise ConfigError("invalid", f"Unknown kind: {kind}")
    if kind in (SUBENTRY_TYPE_GRID, SUBENTRY_TYPE_BATTERY):
        # These are singletons with a fixed title, so they carry no name field.
        return dict(data)

    name = str(data.get(CONF_NAME) or "").strip()
    if not name:
        raise ConfigError("invalid", "A name is required")

    cleaned = dict(data)
    cleaned[CONF_NAME] = name
    if kind == SUBENTRY_TYPE_DEVICE:
        cleaned[CONF_KIND] = "device"
    elif kind == SUBENTRY_TYPE_EV_CHARGER:
        cleaned[CONF_KIND] = "ev_charger"
    return cleaned


def _title(kind: str, data: Mapping[str, Any]) -> str:
    """Return the title to show in the device registry."""
    return str(data.get(CONF_NAME) or KINDS[kind])


def list_sections(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    """Return every configurable subentry, grouped by section."""
    sections: list[dict[str, Any]] = []
    for kind, default_title in KINDS.items():
        items = []
        for subentry in entry.get_subentries_of_type(kind):
            items.append(
                {
                    "subentry_id": subentry.subentry_id,
                    "title": subentry.title or default_title,
                    "data": dict(subentry.data),
                }
            )
        sections.append({"kind": kind, "title": default_title, "items": items})
    return sections


def create_subentry(
    hass: HomeAssistant, entry: ConfigEntry, kind: str, data: Mapping[str, Any]
) -> ConfigSubentry:
    """Add a subentry from the panel."""
    if kind == SUBENTRY_TYPE_GRID and entry.get_subentries_of_type(kind):
        raise ConfigError("invalid", "A grid connection already exists")
    if kind == SUBENTRY_TYPE_BATTERY and entry.get_subentries_of_type(kind):
        raise ConfigError("invalid", "A battery already exists")

    cleaned = _validate(kind, data)
    subentry = ConfigSubentry(
        data=cleaned,
        subentry_type=kind,
        title=_title(kind, cleaned),
        unique_id=None,
    )
    if not hass.config_entries.async_add_subentry(entry, subentry):
        raise ConfigError("invalid", "Could not save the subentry")
    return subentry


def update_subentry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    subentry_id: str,
    data: Mapping[str, Any],
    title: str | None = None,
) -> None:
    """Change an existing subentry."""
    subentry = entry.subentries.get(subentry_id)
    if subentry is None:
        raise ConfigError("not_found", "This configuration no longer exists")

    merged = {**dict(subentry.data), **dict(data)}
    cleaned = _validate(subentry.subentry_type, merged)
    hass.config_entries.async_update_subentry(
        entry,
        subentry,
        data=cleaned,
        title=title or _title(subentry.subentry_type, cleaned),
    )


def delete_subentry(hass: HomeAssistant, entry: ConfigEntry, subentry_id: str) -> None:
    """Remove a subentry and, with it, its entities."""
    if subentry_id not in entry.subentries:
        raise ConfigError("not_found", "This configuration no longer exists")
    if not hass.config_entries.async_remove_subentry(entry, subentry_id):
        raise ConfigError("invalid", "Could not remove the subentry")


def setup_state(entry: ConfigEntry) -> dict[str, Any]:
    """Return how far the wizard got."""
    stored = entry.options.get("setup")
    if not isinstance(stored, Mapping):
        stored = {}
    step = stored.get("step")
    return {
        "step": step if step in SETUP_STEPS else None,
        "completed": bool(stored.get("completed", False)),
        "skipped": list(stored.get("skipped", [])),
        "steps": list(SETUP_STEPS),
        "device_count": len(device_subentries(entry)),
    }


async def async_advance_setup(
    hass: HomeAssistant,
    entry: ConfigEntry,
    step: str | None = None,
    completed: bool | None = None,
    skipped: str | None = None,
) -> dict[str, Any]:
    """Move the wizard forward, and remember where it stopped."""
    state = setup_state(entry)
    if step is not None and step not in SETUP_STEPS:
        raise ConfigError("invalid", f"Unknown step: {step}")
    if skipped is not None and skipped not in SETUP_STEPS:
        raise ConfigError("invalid", f"Unknown step: {skipped}")

    new: dict[str, Any] = {
        "step": step if step is not None else state["step"],
        "completed": completed if completed is not None else state["completed"],
        "skipped": state["skipped"],
    }
    if skipped is not None and skipped not in new["skipped"]:
        new["skipped"].append(skipped)

    options = {**entry.options, "setup": new}
    hass.config_entries.async_update_entry(entry, options=options)
    return new


def detect(hass: HomeAssistant) -> dict[str, Any]:
    """Report which known integrations are present, so the wizard can offer them."""
    components = hass.config.components

    def has(*names: str) -> bool:
        return any(name in components for name in names)

    return {
        "evcc": has("evcc"),
        "solarman": has("solarman"),
        "homewizard": has("homewizard"),
        "recorder": has("recorder"),
        "helpers": {
            "has_evcc": has("evcc"),
            "evcc_url": "https://github.com/marq24/ha-evcc",
        },
    }


def update_contract(
    hass: HomeAssistant, entry: ConfigEntry, provider: str, interval: str
) -> dict[str, Any]:
    """Change the provider and interval from the panel."""
    if provider not in PROVIDERS:
        raise ConfigError("invalid", f"Unknown provider: {provider}")
    if interval not in PRICE_INTERVALS:
        raise ConfigError("invalid", f"Unknown interval: {interval}")
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_PROVIDER: provider,
            CONF_PRICE_INTERVAL: interval,
        },
    )
    return {"provider": provider, "interval": interval}


def contract(entry: ConfigEntry) -> dict[str, Any]:
    """Return the current contract and the options to choose from."""
    return {
        "provider": entry.data.get(CONF_PROVIDER),
        "interval": entry.data.get(CONF_PRICE_INTERVAL),
        "providers": [[key, label] for key, label in sorted(PROVIDERS.items())],
        "intervals": list(PRICE_INTERVALS),
    }


#: What a sensor name has to contain to be offered for a field, per platform.
_HINTS: dict[str, dict[str, tuple[str, ...]]] = {
    "homewizard": {
        "import_energy_entity": ("import", "total_energy_import"),
        "export_energy_entity": ("export",),
        "import_power_entity": ("power", "active_power"),
    },
    "solarman": {
        "soc_entity": ("soc", "state of charge"),
        "house_load_entity": ("load power", "total load"),
    },
    "evcc": {
        "ev_soc_entity": ("soc",),
        "ev_power_entity": ("charge power", "power"),
        "ev_departure_entity": ("plan", "departure", "target time"),
        "ev_capacity_entity": ("capacity", "battery size"),
    },
}


def detect_entities(hass: HomeAssistant) -> dict[str, dict[str, str]]:
    """Offer entity ids for the fields a user would otherwise have to find.

    Deliberately a suggestion: the panel prefills the form, the user confirms.
    Name matching is crude by nature, so anything ambiguous is simply not
    offered rather than guessed wrong.
    """
    registry = er.async_get(hass)
    by_platform: dict[str, list[tuple[str, str]]] = {}
    for item in registry.entities.values():
        if item.disabled_by is not None:
            continue
        label = " ".join(
            part for part in (item.original_name, item.entity_id) if part
        ).lower()
        by_platform.setdefault(item.platform, []).append((item.entity_id, label))

    suggestions: dict[str, dict[str, str]] = {}
    for kind, fields in _HINTS.items():
        available = by_platform.get(kind, [])
        found: dict[str, str] = {}
        for field, needles in fields.items():
            for entity_id, label in available:
                if any(needle in label for needle in needles):
                    found[field] = entity_id
                    break
        if not found:
            continue
        if kind == "homewizard":
            suggestions["grid"] = found
        elif kind == "solarman":
            suggestions["battery"] = found
        elif kind == "evcc":
            suggestions["ev_charger"] = found
    return suggestions
