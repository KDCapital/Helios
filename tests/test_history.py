"""Tests for the historical cost view built on long-term statistics."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import homeassistant.util.dt as dt_util
import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.kd_helios.history import (
    RANGES,
    async_history,
    bucket_changes,
    range_window,
)
from tests.test_panel import FROZEN, _setup_entry, full_entry

UTC = dt_util.UTC


@pytest.fixture(autouse=True)
def _amsterdam():
    """Bucket labels are local, so pin the zone."""
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("Europe/Amsterdam"))
    yield
    dt_util.set_default_time_zone(previous)


def test_ranges_cover_the_expected_periods() -> None:
    """Each range maps to a start, an end and a statistics period."""
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)

    start, _end, period = range_window("today", now)
    local_start = dt_util.as_local(start)
    assert (local_start.hour, local_start.minute) == (0, 0)
    assert local_start.date() == dt_util.as_local(now).date()
    assert period == "hour"

    start, _, period = range_window("7d", now)
    assert period == "day"
    assert (dt_util.as_local(now).date() - dt_util.as_local(start).date()).days == 6

    start, _, period = range_window("month", now)
    assert dt_util.as_local(start).day == 1
    assert period == "day"

    start, _, period = range_window("year", now)
    local_start = dt_util.as_local(start)
    assert (local_start.month, local_start.day) == (1, 1)
    assert period == "month"

    assert set(RANGES) == {"today", "7d", "month", "year"}


def test_changes_are_bucketed_per_day() -> None:
    """Statistics rows are grouped into day buckets."""
    rows = [
        {"start": datetime(2026, 11, 19, 0, 0, tzinfo=UTC), "change": 1.0},
        {"start": datetime(2026, 11, 19, 1, 0, tzinfo=UTC), "change": 2.0},
        {"start": datetime(2026, 11, 20, 0, 0, tzinfo=UTC), "change": 0.5},
    ]

    assert bucket_changes(rows, "day") == [
        {"bucket": "2026-11-19", "value": 3.0},
        {"bucket": "2026-11-20", "value": 0.5},
    ]


def test_rows_without_a_change_are_ignored() -> None:
    """A row with an unknown change is not counted as zero-cost noise."""
    rows = [
        {"start": datetime(2026, 11, 19, 0, 0, tzinfo=UTC), "change": None},
        {"start": datetime(2026, 11, 19, 1, 0, tzinfo=UTC), "change": 4.0},
    ]
    assert bucket_changes(rows, "day") == [{"bucket": "2026-11-19", "value": 4.0}]


async def test_history_totals_the_change_per_sensor(hass: HomeAssistant) -> None:
    """The history view sums what each cumulative sensor grew by."""
    entry = full_entry()
    with freeze_time(FROZEN):
        await _setup_entry(hass, entry)

    start = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)
    rows = {
        "sensor.ev_charger_cost": [
            {"start": start, "change": 1.5},
            {"start": start + timedelta(hours=1), "change": 2.5},
        ],
        "sensor.ev_charger_energy": [
            {"start": start, "change": 4.0},
        ],
    }

    # Pretend the recorder is loaded, which is the normal case in production.
    hass.config.components.add("recorder")
    try:
        with (
            freeze_time(FROZEN),
            patch(
                "homeassistant.components.recorder.statistics.statistics_during_period",
                return_value=rows,
            ),
        ):
            result = await async_history(hass, entry, "today")
    finally:
        hass.config.components.remove("recorder")

    assert result["available"] is True
    assert result["range"] == "today"
    device = result["devices"][0]
    assert device["cost"] == pytest.approx(4.0)
    assert device["energy_kwh"] == pytest.approx(4.0)
    assert result["series"] == [{"bucket": "2026-11-20", "value": 4.0}] or result[
        "series"
    ] == [{"bucket": "2026-11-20", "value": 4.0}]


async def test_history_is_unavailable_without_a_recorder(hass: HomeAssistant) -> None:
    """A recorder-less install reports that history is unavailable."""
    entry = full_entry()
    with freeze_time(FROZEN):
        await _setup_entry(hass, entry)
        result = await async_history(hass, entry, "month")

    if "recorder" in hass.config.components:
        pytest.skip("this fixture has a recorder")
    assert result["available"] is False
    assert result["devices"] == []
