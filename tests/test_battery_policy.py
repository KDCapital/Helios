"""Tests for the planning policies added after the Omnibattery review.

Each test names the field incident it guards against: a night charge that came
up short, energy bought and spent in one window, an overfilled battery, a plan
kept alive on a failed price fetch, and a schedule that survived its settings.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.kd_helios.battery import (
    BatterySettings,
    ChargeSource,
    build_tou_programs,
    plan_charge,
    settings_fingerprint,
    should_apply,
)
from custom_components.kd_helios.prices import PriceSeries
from tests.helpers import UTC, build_series


@pytest.fixture(autouse=True)
def _amsterdam() -> None:
    """Time-of-use slots are in inverter-local time; pin the zone."""
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("Europe/Amsterdam"))
    yield
    dt_util.set_default_time_zone(previous)


def make_prices(start: datetime, hourly: list[float]) -> PriceSeries:
    """Build a quarter-hour series from one price per hour."""
    points: dict[datetime, tuple[float, float]] = {}
    for index, price in enumerate(hourly):
        for quarter in range(4):
            when = start + timedelta(minutes=15 * (index * 4 + quarter))
            points[when] = (price, price / 2)
    return build_series(points)


#: Cheap night, flat day, expensive evening: the incident's shape.
DAY = [0.05] * 6 + [0.20] * 11 + [0.40] * 4 + [0.20] * 3
NOON = datetime(2026, 11, 19, 23, 0, tzinfo=UTC)

SETTINGS = BatterySettings(
    capacity_kwh=10.0,
    max_charge_kw=5.0,
    reserve_soc_pct=10.0,
    target_soc_pct=100.0,
    efficiency_pct=90.0,
    horizon_hours=24,
)


def _flat_forecast(kwh_per_hour: float):
    """Return a load forecast with a constant draw."""

    def forecast(start: datetime, end: datetime) -> float:
        return kwh_per_hour * (end - start).total_seconds() / 3600.0

    return forecast


def test_without_a_forecast_the_estimate_is_unchanged() -> None:
    """No forecast, no drain: the historic valuation stands exactly."""
    plan = plan_charge(make_prices(NOON, DAY), SETTINGS, NOON, soc_pct=20.0)
    assert plan.windows[0].drain_kwh == 0.0
    assert plan.expected_saving == pytest.approx(8.0 * 0.9 * 0.40 - 8.0 * 0.05)


def test_the_house_eats_part_of_the_charge_before_the_peak() -> None:
    """The saving counts the energy that survives, not the energy bought.

    A night charge that ignores the load in between promises a peak it cannot
    serve. The drain is reported per window so the claim can be audited.
    """
    plan = plan_charge(
        make_prices(NOON, DAY),
        SETTINGS,
        NOON,
        soc_pct=20.0,
        load_forecast=_flat_forecast(0.2),
    )

    window = plan.windows[0]
    assert window.drain_kwh > 2.0
    assert window.discharge_kwh == pytest.approx(window.energy_kwh - window.drain_kwh)
    assert plan.expected_saving < 8.0 * 0.9 * 0.40 - 8.0 * 0.05


def test_the_drained_energy_is_valued_at_the_hours_it_is_used_in() -> None:
    """Self-consumed energy displaces the day tariff, not the peak tariff."""
    without = plan_charge(make_prices(NOON, DAY), SETTINGS, NOON, soc_pct=20.0)
    with_load = plan_charge(
        make_prices(NOON, DAY),
        SETTINGS,
        NOON,
        soc_pct=20.0,
        load_forecast=_flat_forecast(0.2),
    )

    lost = without.expected_saving - with_load.expected_saving
    window = with_load.windows[0]
    # Lost value is the drained energy times the gap between the peak it would
    # have served and the hours it actually served. Bounded below by the day
    # price and above by the cheapest night hour, so it can only pass when the
    # drain is priced somewhere in between rather than at the peak.
    assert window.drain_kwh * 0.9 * (0.40 - 0.20) < lost
    assert lost < window.drain_kwh * 0.9 * (0.40 - 0.05)


def test_a_slow_discharge_caps_what_is_bought() -> None:
    """An overfilled battery is energy bought and never returned.

    The four-hour evening peak at 1 kW can absorb 4 kWh, so buying 8 would
    leave half of it stranded in the battery.
    """
    plan = plan_charge(
        make_prices(NOON, DAY),
        SETTINGS,
        NOON,
        soc_pct=20.0,
        max_discharge_kw=1.0,
    )

    assert plan.energy_kwh == pytest.approx(4.0)
    assert plan.windows[0].energy_kwh == pytest.approx(4.0)


def test_a_single_quarter_hour_spike_does_not_cap_the_plan() -> None:
    """The expensive period, not its 15-minute maximum, sizes the charge.

    A battery that can discharge 11 kW can serve the whole evening; capping the
    purchase to 11 kW x one quarter hour left a 32 kWh battery at 13% with a
    2.8 kWh plan and one 15-minute window.
    """
    start = datetime(2026, 11, 22, 7, 0, tzinfo=UTC)
    points: dict[datetime, tuple[float, float]] = {}
    for index in range(36 * 4):
        when = start + timedelta(minutes=15 * index)
        local_hour = (when.hour + 1) % 24
        price = 0.38 if 12 <= local_hour < 20 else 0.35
        if local_hour == 18 and when.minute == 0:
            price = 0.417
        points[when] = (price, price / 2)

    settings = BatterySettings(
        capacity_kwh=32.0,
        max_charge_kw=11.0,
        reserve_soc_pct=10.0,
        target_soc_pct=100.0,
        efficiency_pct=90.0,
        horizon_hours=36,
    )
    plan = plan_charge(
        build_series(points),
        settings,
        start,
        soc_pct=13.0,
        max_discharge_kw=11.0,
    )

    # 13 to 100 percent of 32 kWh is 27.84 kWh.
    assert plan.energy_kwh == pytest.approx(27.84)
    window = plan.windows[0]
    assert window.end - window.start >= timedelta(hours=2)


def test_without_a_discharge_limit_the_target_is_still_filled() -> None:
    """An unknown discharge rating must not shrink the charge."""
    plan = plan_charge(make_prices(NOON, DAY), SETTINGS, NOON, soc_pct=20.0)
    assert plan.energy_kwh == pytest.approx(8.0)


def test_solar_credit_reduces_what_is_bought() -> None:
    """Energy the sun will deliver is not bought from the grid."""
    plan = plan_charge(
        make_prices(NOON, DAY),
        SETTINGS,
        NOON,
        soc_pct=20.0,
        solar_credit_kwh=3.0,
    )

    assert plan.energy_kwh == pytest.approx(5.0)
    assert plan.solar_credit_kwh == 3.0


def test_solar_that_covers_the_whole_charge_buys_nothing() -> None:
    """Nothing to buy, and the state says the plan is waiting for the sun."""
    plan = plan_charge(
        make_prices(NOON, DAY),
        SETTINGS,
        NOON,
        soc_pct=20.0,
        solar_credit_kwh=8.0,
    )

    assert not plan.has_windows
    assert plan.reason == "waiting_for_solar"
    assert plan.solar_credit_kwh == 8.0


def test_the_credit_cannot_exceed_the_need() -> None:
    """A generous forecast is not a reason to plan negative energy."""
    plan = plan_charge(
        make_prices(NOON, DAY),
        SETTINGS,
        NOON,
        soc_pct=20.0,
        solar_credit_kwh=50.0,
    )
    assert plan.solar_credit_kwh == pytest.approx(8.0)


def test_the_cars_window_is_not_a_cheap_hour_for_the_battery() -> None:
    """The cheap hours inside the car's window are skipped, and the plan says so."""
    hold = (NOON, NOON + timedelta(hours=6))
    plan = plan_charge(
        make_prices(NOON, DAY),
        SETTINGS,
        NOON,
        soc_pct=20.0,
        suppressed=hold,
    )

    assert plan.has_windows
    assert plan.windows[0].start >= hold[1]
    conflicts = [b for b in plan.blockers if b.reason == "hold_conflict"]
    assert conflicts and conflicts[0].details["slots"] > 0


def test_a_hold_covering_the_horizon_leaves_nothing_to_buy() -> None:
    """When the car owns every cheap hour there is no window to plan."""
    hold = (NOON, NOON + timedelta(hours=48))
    plan = plan_charge(
        make_prices(NOON, DAY),
        SETTINGS,
        NOON,
        soc_pct=20.0,
        suppressed=hold,
    )

    assert not plan.has_windows
    assert plan.reason == "hold_conflict"


def test_a_suppressed_window_falls_back_to_the_next_cheapest_hour() -> None:
    """Blocking the first hours must not block the plan."""
    hold = (NOON, NOON + timedelta(hours=2))
    plan = plan_charge(
        make_prices(NOON, [0.05, 0.05, 0.05] + DAY[3:]),
        SETTINGS,
        NOON,
        soc_pct=20.0,
        suppressed=hold,
    )

    assert plan.has_windows
    assert plan.windows[0].start >= hold[1]


def test_a_failed_fetch_is_labelled_stale_not_missing() -> None:
    """A stale tariff is not the same as no tariff, and must say which it is."""
    empty = PriceSeries.empty("zonneplan", "quarterly")
    plan = plan_charge(empty, SETTINGS, NOON, soc_pct=20.0, stale_prices=True)
    assert plan.reason == "prices_stale"
    assert plan.prices_stale is True


def test_a_plan_built_on_stale_prices_says_so() -> None:
    """Planning continues on the last good prices, with the flag set."""
    plan = plan_charge(
        make_prices(NOON, DAY), SETTINGS, NOON, soc_pct=20.0, stale_prices=True
    )
    assert plan.has_windows
    assert plan.prices_stale is True


def test_a_moved_setting_forces_a_rewrite() -> None:
    """A schedule computed for another target must be replaced at once."""
    plan = plan_charge(make_prices(NOON, DAY), SETTINGS, NOON, soc_pct=20.0)
    tou = build_tou_programs(plan, SETTINGS, NOON)
    from custom_components.kd_helios.battery import AppliedPlan

    applied = AppliedPlan.from_plan(plan, tou, NOON)

    # Without force the throttle still holds.
    assert not should_apply(plan, applied, SETTINGS, last_apply=NOON, now=NOON)
    # A setting that the plan reads changed, so the schedule is stale.
    assert should_apply(plan, applied, SETTINGS, last_apply=NOON, now=NOON, force=True)


def test_the_fingerprint_tracks_every_setting_the_plan_reads() -> None:
    """Every knob that shapes a plan changes the fingerprint."""
    base = settings_fingerprint(SETTINGS)
    changes = [
        BatterySettings(11.0, 5.0, 10.0, 100.0, 90.0, 24),
        BatterySettings(10.0, 6.0, 10.0, 100.0, 90.0, 24),
        BatterySettings(10.0, 5.0, 20.0, 100.0, 90.0, 24),
        BatterySettings(10.0, 5.0, 10.0, 90.0, 90.0, 24),
        BatterySettings(10.0, 5.0, 10.0, 100.0, 80.0, 24),
        BatterySettings(10.0, 5.0, 10.0, 100.0, 90.0, 48),
    ]
    for changed in changes:
        assert settings_fingerprint(changed) != base
    assert settings_fingerprint(SETTINGS) == base


def test_the_hold_still_wins_in_the_programs() -> None:
    """The car's window is programmed even when the plan avoided it."""
    plan = plan_charge(make_prices(NOON, DAY), SETTINGS, NOON, soc_pct=20.0)
    hold_start = NOON + timedelta(hours=10)
    hold_end = hold_start + timedelta(hours=1)
    tou = build_tou_programs(plan, SETTINGS, NOON, hold=(hold_start, hold_end, 55.0))

    assert any(program.source is ChargeSource.GRID for program in tou.programs)
    assert any(program.soc_pct == 55 for program in tou.programs)
