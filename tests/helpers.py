"""Shared test helpers for KD Helios."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from custom_components.kd_helios.prices import PriceMeta, PricePoint, PriceSeries

FIXTURES = Path(__file__).parent / "fixtures"

#: A fixed instant inside the recorded price fixtures.
FROZEN_TIME = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def load_fixture(name: str) -> dict[str, Any]:
    """Load a recorded epexprijzen.nl payload."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def build_series(
    points: Mapping[datetime, tuple[float | None, float | None]],
    *,
    meta: PriceMeta | None = None,
) -> PriceSeries:
    """Build a price series from explicit grid points."""
    grid = {
        when: PricePoint(all_in=all_in, spot=spot)
        for when, (all_in, spot) in points.items()
    }
    return PriceSeries(grid, meta or PriceMeta(), "test", "quarterly")


def align(when: datetime) -> datetime:
    """Floor a timestamp to the quarter hour price grid."""
    return when.replace(minute=when.minute - when.minute % 15, second=0, microsecond=0)


def flat_series(
    start: datetime,
    *,
    all_in: float | None,
    spot: float | None = None,
    hours: int = 4,
) -> PriceSeries:
    """Build a series with a single price repeated every quarter hour."""
    step = timedelta(minutes=15)
    start = align(start)
    count = hours * 4 + 1
    return build_series(
        {start + step * index: (all_in, spot) for index in range(count)}
    )


def set_energy_state(hass, entity_id: str, value: float, unit: str = "kWh") -> None:
    """Set a cumulative energy sensor state."""
    hass.states.async_set(entity_id, str(value), {"unit_of_measurement": unit})


def set_power_state(hass, entity_id: str, value: float, unit: str = "W") -> None:
    """Set a power sensor state."""
    hass.states.async_set(entity_id, str(value), {"unit_of_measurement": unit})


def add_meter_device(hass, name: str = "P1 meter"):
    """Register a device owned by a throwaway config entry."""
    from homeassistant.helpers import device_registry as dr
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain="homewizard", title=name, data={})
    entry.add_to_hass(hass)
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("homewizard", name)},
        name=name,
    )


def add_meter_sensor(
    hass,
    device,
    object_id: str,
    *,
    platform: str = "homewizard",
    translation_key: str | None = None,
    device_class: str | None = "energy",
    unit: str = "kWh",
    state: str = "1000",
    disabled: bool = False,
):
    """Register a sensor entity for a device."""
    from homeassistant.helpers import entity_registry as er

    entry = er.async_get(hass).async_get_or_create(
        "sensor",
        platform,
        f"{platform}-{object_id}-uid",
        suggested_object_id=object_id,
        device_id=device.id,
        original_device_class=device_class,
        translation_key=translation_key,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION if disabled else None,
    )
    if not disabled:
        hass.states.async_set(entry.entity_id, state, {"unit_of_measurement": unit})
    return entry
