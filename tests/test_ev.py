"""Tests for the car's charging window and the connection capacity."""

from __future__ import annotations

from datetime import datetime, timedelta

import homeassistant.util.dt as dt_util
import pytest

from custom_components.kd_helios.ev import (
    energy_needed_kwh,
    ev_charge_window,
    fuse_capacity_kw,
    in_ev_window,
)

UTC = dt_util.UTC


def points(
    now: datetime, prices: list[float]
) -> list[tuple[datetime, datetime, float]]:
    """Return quarter-hour priced slots from a list of all-in prices."""
    return [
        (
            now + timedelta(minutes=15 * index),
            now + timedelta(minutes=15 * (index + 1)),
            price,
        )
        for index, price in enumerate(prices)
    ]


def test_a_three_phase_connection_carries_its_nominal_power() -> None:
    """3 x 25 A is 17.25 kW, and the margin is what the user allows."""
    assert fuse_capacity_kw(3, 25, 100) == pytest.approx(17.25)
    assert fuse_capacity_kw(3, 25, 90) == pytest.approx(15.53)
    assert fuse_capacity_kw(1, 35, 100) == pytest.approx(8.05)


def test_the_margin_cannot_exceed_the_fuse() -> None:
    """A silly margin is clamped instead of promising more than the fuse."""
    assert fuse_capacity_kw(3, 25, 150) == pytest.approx(17.25)


def test_energy_needed_follows_the_soc() -> None:
    """A quarter-full 60 kWh car needs 45 kWh to reach full."""
    assert energy_needed_kwh(60.0, 100.0, 25.0) == pytest.approx(45.0)
    assert energy_needed_kwh(60.0, 100.0, 100.0) == 0.0
    assert energy_needed_kwh(60.0, 80.0, 90.0) == 0.0


def test_the_window_is_the_cheapest_slots_before_departure() -> None:
    """Cheap hours far before departure win over expensive ones near it."""
    now = datetime(2026, 11, 21, 0, 0, tzinfo=UTC)
    series = points(now, [0.10, 0.50, 0.05, 0.45, 0.30, 0.20])
    departure = now + timedelta(hours=1, minutes=30)

    window = ev_charge_window(series, departure, energy_kwh=11.0, power_kw=11.0)

    assert window is not None
    # 11 kW for a quarter hour is 2.75 kWh, so four cheap slots are needed.
    assert window.energy_kwh == pytest.approx(11.0)
    assert window.average_price == pytest.approx((0.05 + 0.10 + 0.20 + 0.30) / 4)
    assert window.end <= departure


def test_a_window_that_does_not_fit_before_departure_is_refused() -> None:
    """Too little time left means no honest window to plan with."""
    now = datetime(2026, 11, 21, 0, 0, tzinfo=UTC)
    series = points(now, [0.10] * 2)

    assert ev_charge_window(series, now + timedelta(minutes=20), 30.0, 11.0) is None


def test_no_energy_means_no_window() -> None:
    """A full car has nothing to plan."""
    now = datetime(2026, 11, 21, 0, 0, tzinfo=UTC)
    assert (
        ev_charge_window(points(now, [0.2] * 8), now + timedelta(hours=2), 0.0, 11.0)
        is None
    )


def test_moments_inside_the_window_are_recognised() -> None:
    """The battery policy keys off this test."""
    now = datetime(2026, 11, 21, 0, 0, tzinfo=UTC)
    series = points(now, [0.10, 0.10, 0.10, 0.10])
    window = ev_charge_window(series, now + timedelta(hours=1), 5.5, 11.0)
    assert window is not None

    assert in_ev_window(window.start, window)
    assert in_ev_window(window.start + timedelta(minutes=1), window)
    assert not in_ev_window(window.end, window)
    assert not in_ev_window(window.start - timedelta(minutes=1), window)
    assert not in_ev_window(now, None)
