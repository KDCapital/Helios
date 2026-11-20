"""Diagnostics support for KD Helios."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import HeliosConfigEntry


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
