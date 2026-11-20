"""Historical cost from Home Assistant's long-term statistics.

Home Assistant already aggregates every sensor with a ``state_class`` per hour
and keeps those rows forever; only the five-minute short-term table is purged.
Asking for the ``change`` type therefore gives "how much did this total grow in
this period", which is exactly the cost of a day, week, month or year.

The cumulative sensors are used rather than the daily ones: ``cost_today``
resets at midnight, so its changes only ever describe a single day.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import SUBENTRY_TYPE_GRID
from .models import device_subentries

_LOGGER = logging.getLogger(__name__)

#: Ranges the dashboard offers.
RANGES: dict[str, str] = {
    "today": "hour",
    "7d": "day",
    "month": "day",
    "year": "month",
}

#: Statistic keys per subentry type.
_DEVICE_KEYS = {"cost": "cost", "energy": "energy"}
_GRID_KEYS = {
    "cost": "grid_cost",
    "import_kwh": "grid_import_energy",
    "export_kwh": "grid_export_energy",
    "credit": "grid_export_credit",
}


def range_window(key: str, now: datetime) -> tuple[datetime, datetime, str]:
    """Return the start, end and statistics period for a range key."""
    local = dt_util.as_local(now)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    period = RANGES.get(key, "day")

    if key == "today":
        start = midnight
    elif key == "7d":
        start = midnight - timedelta(days=6)
    elif key == "month":
        start = midnight.replace(day=1)
    elif key == "year":
        start = midnight.replace(month=1, day=1)
    else:
        start = midnight - timedelta(days=6)
        period = "day"

    return dt_util.as_utc(start), dt_util.as_utc(now), period


def _bucket_key(start: Any, period: str) -> str:
    """Return a stable bucket label for a statistics row start."""
    if isinstance(start, (int, float)):
        moment = dt_util.utc_from_timestamp(start)
    else:
        moment = start
    local = dt_util.as_local(moment)
    if period in ("month", "year"):
        return local.strftime("%Y-%m")
    return local.strftime("%Y-%m-%d")


def bucket_changes(rows: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
    """Group statistics rows into buckets and sum their change.

    Statistics rows arrive per period already, but a ``month`` range can contain
    a mix of periods after a database restart, so the sum is done here rather
    than trusting the row layout.
    """
    buckets: dict[str, float] = {}
    for row in rows:
        change = row.get("change")
        if change is None:
            continue
        key = _bucket_key(row["start"], period)
        buckets[key] = buckets.get(key, 0.0) + float(change)
    return [
        {"bucket": key, "value": round(value, 3)}
        for key, value in sorted(buckets.items())
    ]


def _statistic_entities(
    hass: HomeAssistant, entry: ConfigEntry
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """Map subentries to the entity ids that hold their cumulative totals."""
    registry = er.async_get(hass)
    entities: dict[str, dict[str, str]] = {}
    for item in registry.entities.values():
        if item.config_entry_id != entry.entry_id or item.config_subentry_id is None:
            continue
        key = item.translation_key
        if key is None:
            continue
        entities.setdefault(item.config_subentry_id, {})[key] = item.entity_id

    devices: dict[str, dict[str, str]] = {}
    for subentry in device_subentries(entry):
        found = entities.get(subentry.subentry_id, {})
        devices[subentry.subentry_id] = {
            field: found[key] for field, key in _DEVICE_KEYS.items() if key in found
        }

    grid: dict[str, str] = {}
    for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_GRID):
        found = entities.get(subentry.subentry_id, {})
        grid = {field: found[key] for field, key in _GRID_KEYS.items() if key in found}
        break

    return devices, grid


def _changes(result: dict[str, list[dict[str, Any]]], entity_id: str) -> float:
    """Total the change of one statistic over the window."""
    rows = result.get(entity_id) or []
    return sum(float(row["change"]) for row in rows if row.get("change") is not None)


async def async_history(
    hass: HomeAssistant, entry: ConfigEntry, range_key: str
) -> dict[str, Any]:
    """Return costs over a range from the recorder's long-term statistics."""
    now = dt_util.utcnow()
    start, end, period = range_window(range_key, now)
    devices_map, grid_map = _statistic_entities(hass, entry)

    wanted = {
        entity_id
        for fields in (*devices_map.values(), grid_map)
        for entity_id in fields.values()
    }

    if "recorder" not in hass.config.components or not wanted:
        return {
            "range": range_key,
            "period": period,
            "available": False,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "devices": [],
            "grid": None,
            "series": [],
        }

    from homeassistant.components.recorder.statistics import (
        statistics_during_period,
    )

    result = await hass.async_add_executor_job(
        lambda: statistics_during_period(
            hass, start, end, wanted, period, None, {"change"}
        )
    )

    devices: list[dict[str, Any]] = []
    for subentry in device_subentries(entry):
        fields = devices_map.get(subentry.subentry_id, {})
        if not fields:
            continue
        cost = _changes(result, fields["cost"]) if "cost" in fields else None
        energy = _changes(result, fields["energy"]) if "energy" in fields else None
        devices.append(
            {
                "subentry_id": subentry.subentry_id,
                "name": subentry.title,
                "cost": round(cost, 2) if cost is not None else None,
                "energy_kwh": round(energy, 2) if energy is not None else None,
            }
        )

    grid = None
    if grid_map:
        grid = {
            field: round(_changes(result, entity_id), 2)
            for field, entity_id in grid_map.items()
        }

    series: dict[str, float] = {}
    for fields in devices_map.values():
        entity_id = fields.get("cost")
        if entity_id is None:
            continue
        for row in bucket_changes(result.get(entity_id) or [], period):
            series[row["bucket"]] = series.get(row["bucket"], 0.0) + row["value"]

    return {
        "range": range_key,
        "period": period,
        "available": True,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "devices": devices,
        "grid": grid,
        "series": [
            {"bucket": bucket, "value": round(value, 2)}
            for bucket, value in sorted(series.items())
        ],
    }
