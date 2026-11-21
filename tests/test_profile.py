"""Tests for the learned house-load profile."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import homeassistant.util.dt as dt_util
import pytest

from custom_components.kd_helios.profile import WEEK_HOURS, LoadProfile

UTC = dt_util.UTC


@pytest.fixture(autouse=True)
def _amsterdam():
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("Europe/Amsterdam"))
    yield
    dt_util.set_default_time_zone(previous)


def test_nothing_known_is_none_not_zero() -> None:
    """An empty profile must not pretend the house uses nothing."""
    profile = LoadProfile()
    assert profile.forecast_kw(datetime(2026, 11, 20, 12, tzinfo=UTC)) is None
    assert (
        profile.energy_kwh(
            datetime(2026, 11, 20, 12, tzinfo=UTC),
            datetime(2026, 11, 20, 14, tzinfo=UTC),
        )
        is None
    )


def test_the_first_sample_is_taken_almost_as_is() -> None:
    """Cold start: one evening sample should be close to that value."""
    profile = LoadProfile()
    moment = datetime(2026, 11, 20, 18, tzinfo=UTC)
    profile.add(moment, 2.0)

    assert profile.forecast_kw(moment) == pytest.approx(2.0)


def test_the_average_moves_slowly() -> None:
    """One odd sample does not rewrite the profile."""
    profile = LoadProfile()
    moment = datetime(2026, 11, 20, 18, tzinfo=UTC)
    for _ in range(10):
        profile.add(moment, 1.0)
    before = profile.forecast_kw(moment)
    assert before is not None

    profile.add(moment, 10.0)
    after = profile.forecast_kw(moment)
    assert after is not None
    assert after < 3.0
    assert after > before


def test_another_weekday_at_the_same_hour_fills_the_gap() -> None:
    """Monday evening teaches Tuesday evening until Tuesday has samples."""
    profile = LoadProfile()
    monday = datetime(2026, 11, 16, 18, tzinfo=UTC)
    tuesday = monday + timedelta(days=1)
    profile.add(monday, 1.5)

    assert profile.forecast_kw(tuesday) == pytest.approx(1.5)


def test_energy_is_integrated_from_the_hourly_profile() -> None:
    """Two hours at two kilowatts is four kilowatt hours."""
    profile = LoadProfile()
    start = datetime(2026, 11, 20, 12, tzinfo=UTC)
    profile.add(start, 2.0)
    profile.add(start + timedelta(hours=1), 2.0)

    assert profile.energy_kwh(start, start + timedelta(hours=2)) == pytest.approx(4.0)


def test_the_profile_survives_a_round_trip() -> None:
    """Stored data reloads into the same averages."""
    profile = LoadProfile()
    moment = datetime(2026, 11, 20, 7, tzinfo=UTC)
    profile.add(moment, 0.8)
    profile.add(moment, 1.2)

    restored = LoadProfile.from_dict(profile.as_dict())
    assert restored.forecast_kw(moment) == pytest.approx(profile.forecast_kw(moment))
    assert restored.filled_buckets() == profile.filled_buckets()


def test_malformed_stored_data_is_ignored() -> None:
    """Bad keys or values must not break startup."""
    profile = LoadProfile.from_dict({"nonsense": [1, 1], "999": [1, 1], "-1": [1, 1]})
    assert profile.filled_buckets() == 0


def test_coverage_counts_the_known_week() -> None:
    """Coverage is a share of the 168 weekly buckets."""
    profile = LoadProfile()
    start = datetime(2026, 11, 16, 0, tzinfo=UTC)
    for hour in range(24):
        profile.add(start + timedelta(hours=hour), 1.0)

    assert profile.filled_buckets() == 24
    assert profile.coverage_pct() == pytest.approx(100.0 * 24 / WEEK_HOURS, abs=0.1)
