"""Diagnostics support for KD Helios."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import HeliosConfigEntry
from .const import SUBENTRY_TYPE_GRID


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HeliosConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    series = runtime.prices.series
    covered = series.covered

    return {
        "entry": {
            "data": dict(entry.data),
            "title": entry.title,
            "unique_id": entry.unique_id,
            "version": entry.version,
        },
        "provider": {
            "id": runtime.prices.provider,
            "price_interval": runtime.prices.interval,
            "source": "https://epexprijzen.nl",
        },
        "price_series": {
            "points": series.points,
            "covered_from": covered[0].isoformat() if covered else None,
            "covered_to": covered[1].isoformat() if covered else None,
            "energy_tax": series.meta.energy_tax,
            "provider_charge": series.meta.provider_charge,
            "spot_coefficient": series.meta.spot_coefficient,
        },
        "grid": (
            {
                "config": dict(
                    next(
                        subentry
                        for subentry in entry.subentries.values()
                        if subentry.subentry_type == SUBENTRY_TYPE_GRID
                    ).data
                ),
                "accumulators": runtime.engine.grid_tracker.acc.as_dict(),
            }
            if runtime.engine.grid_tracker is not None
            else None
        ),
        "battery": {
            "plan": (
                {
                    "reason": runtime.engine.battery_plan.reason,
                    "energy_kwh": runtime.engine.battery_plan.energy_kwh,
                    "expected_saving": runtime.engine.battery_plan.expected_saving,
                    "windows": len(runtime.engine.battery_plan.windows),
                }
                if runtime.engine.battery_plan
                else None
            ),
            "applied": (
                runtime.engine.applied_plan.as_dict()
                if runtime.engine.applied_plan
                else None
            ),
            "tou_programs": (
                len(runtime.engine.tou_plan.programs)
                if runtime.engine.tou_plan
                else None
            ),
            "decisions": list(runtime.engine.decision_log),
        },
        "devices": {
            subentry_id: {
                "subentry_type": subentry.subentry_type,
                "title": subentry.title,
                "config": dict(subentry.data),
                "accumulators": tracker.acc.as_dict(),
            }
            for subentry_id, subentry in entry.subentries.items()
            for tracker in (runtime.engine.trackers.get(subentry_id),)
            if tracker is not None
        },
    }
