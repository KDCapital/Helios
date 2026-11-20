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
