"""Tests for battery charge planning and Deye time-of-use programs."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.kd_helios.battery import (
    TOU_REGISTERS,
    TOU_SLOTS,
    BatterySettings,
    ChargePlan,
    ChargeSource,
    ChargeWindow,
    build_tou_programs,
    plan_charge,
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


#: A day with cheap hours, a flat middle and an expensive evening.
DAY = [0.05] * 6 + [0.20] * 11 + [0.40] * 4 + [0.20] * 3
#: Local midnight in Amsterdam (CET), so slot minutes start at zero.
NOON = datetime(2026, 11, 19, 23, 0, tzinfo=UTC)

SETTINGS = BatterySettings(
    capacity_kwh=10.0,
    max_charge_kw=5.0,
    reserve_soc_pct=10.0,
    target_soc_pct=100.0,
    efficiency_pct=90.0,
    horizon_hours=24,
)


def test_cheap_hours_are_chosen_and_valued_against_the_peak() -> None:
    """The plan buys the cheap night and values it at the evening peak."""
    plan = plan_charge(make_prices(NOON, DAY), SETTINGS, NOON, soc_pct=20.0)

    assert plan.reason == "planned"
    # 20 to 100 percent of 10 kWh is 8 kWh.
    assert plan.energy_kwh == pytest.approx(8.0)
    assert plan.cost == pytest.approx(8.0 * 0.05)
    assert len(plan.windows) == 1

    window = plan.windows[0]
    assert window.start == NOON
    # 8 kWh at 5 kW is 1 h 36 min, so seven quarter slots.
    assert window.end == NOON + timedelta(minutes=105)
    assert window.average_price == pytest.approx(0.05)
    assert window.discharge_price == pytest.approx(0.40)
    # 8 kWh in, 90 percent out, sold against 0.40.
    assert plan.expected_saving == pytest.approx(8.0 * 0.9 * 0.40 - 8.0 * 0.05)


def test_full_battery_needs_nothing() -> None:
    """Nothing is bought when the battery is already at the target."""
    plan = plan_charge(make_prices(NOON, DAY), SETTINGS, NOON, soc_pct=100.0)
    assert plan.reason == "full"
    assert not plan.has_windows


def test_missing_soc_yields_no_plan() -> None:
    """Without a SOC reading there is no defensible plan."""
    plan = plan_charge(make_prices(NOON, DAY), SETTINGS, NOON, soc_pct=None)
    assert plan.reason == "no_soc"


def test_missing_prices_yield_no_plan() -> None:
    """No tariff means no advice."""
    empty = PriceSeries.empty("zonneplan", "quarterly")
    plan = plan_charge(empty, SETTINGS, NOON, soc_pct=20.0)
    assert plan.reason == "no_prices"


def test_flat_prices_are_not_worth_cycling() -> None:
    """Rounding losses make a flat tariff pointless to cycle."""
    flat = [0.20] * 24
    plan = plan_charge(make_prices(NOON, flat), SETTINGS, NOON, soc_pct=20.0)
    assert plan.reason == "no_spread"
    assert not plan.has_windows


def test_charge_power_limits_the_energy_bought() -> None:
    """A weak charger cannot fill the battery within the cheap window."""
    settings = BatterySettings(
        capacity_kwh=40.0,
        max_charge_kw=2.0,
        reserve_soc_pct=10.0,
        target_soc_pct=100.0,
        efficiency_pct=90.0,
        horizon_hours=24,
    )
    # Two cheap hours only: 2 kW for 2 hours is 4 kWh, not the 36 requested.
    day = [0.05] * 2 + [0.40] * 22
    plan = plan_charge(make_prices(NOON, day), settings, NOON, soc_pct=10.0)

    assert plan.energy_kwh == pytest.approx(4.0)
    assert plan.windows[0].end == NOON + timedelta(hours=2)


def test_a_cheap_slot_after_the_peak_is_not_bought() -> None:
    """Energy bought after the expensive hours has nothing to displace."""
    day = [0.40] * 20 + [0.05] * 4
    plan = plan_charge(make_prices(NOON, day), SETTINGS, NOON, soc_pct=20.0)
    # The cheap hours are at the very end of the horizon.
    assert plan.reason in {"no_spread", "planned"}
    for window in plan.windows:
        assert window.start < NOON + timedelta(hours=20)


def test_two_separate_cheap_blocks_become_two_windows() -> None:
    """Non-adjacent cheap hours are planned as separate windows."""
    day = [0.05, 0.05, 0.40, 0.40, 0.05, 0.05] + [0.40] * 18
    settings = BatterySettings(
        capacity_kwh=6.0,
        max_charge_kw=2.0,
        reserve_soc_pct=10.0,
        target_soc_pct=100.0,
        efficiency_pct=90.0,
        horizon_hours=24,
    )
    plan = plan_charge(make_prices(NOON, day), settings, NOON, soc_pct=10.0)

    assert len(plan.windows) == 2
    assert plan.windows[0].start == NOON
    assert plan.windows[1].start == NOON + timedelta(hours=4)


def test_tou_programs_charge_at_the_window_and_idle_after() -> None:
    """One window becomes a charging slot and an idle slot at its end."""
    plan = plan_charge(make_prices(NOON, DAY), SETTINGS, NOON, soc_pct=20.0)
    tou = build_tou_programs(plan, SETTINGS, NOON)

    assert len(tou.programs) == TOU_SLOTS
    starts = [program.start_minute for program in tou.programs]
    assert starts == sorted(starts)
    assert not tou.truncated

    first = tou.programs[0]
    assert first.start_minute == 0
    assert first.hhmm == 0
    assert first.source is ChargeSource.GRID
    assert first.power_w == 5000
    assert first.soc_pct == 100

    second = tou.programs[1]
    assert second.start_minute == 105
    assert second.hhmm == 145
    assert second.source is ChargeSource.OFF
    assert second.soc_pct == 10


def test_tou_registers_match_the_deye_map() -> None:
    """The payload uses the documented register block and value forms."""
    plan = plan_charge(make_prices(NOON, DAY), SETTINGS, NOON, soc_pct=20.0)
    registers = build_tou_programs(plan, SETTINGS, NOON).registers()

    assert TOU_REGISTERS == {
        "times": 148,
        "power": 154,
        "soc": 166,
        "sources": 172,
    }
    assert set(registers) == {148, 154, 166, 172}
    for values in registers.values():
        assert len(values) == TOU_SLOTS
        assert all(0 <= value <= 65535 for value in values)
    assert registers[148][0] == 0
    assert registers[148][1] == 145
    assert registers[154][0] == 5000
    assert registers[166][0] == 100
    assert registers[172][0] == int(ChargeSource.GRID)
    assert registers[172][1] == int(ChargeSource.OFF)


def test_no_plan_means_no_grid_charging() -> None:
    """Without a profitable window every slot is idle at the reserve."""
    plan = plan_charge(make_prices(NOON, [0.20] * 24), SETTINGS, NOON, soc_pct=20.0)
    tou = build_tou_programs(plan, SETTINGS, NOON)

    assert len(tou.programs) == TOU_SLOTS
    assert all(program.source is ChargeSource.OFF for program in tou.programs)
    assert all(program.soc_pct == 10 for program in tou.programs)
    assert [program.hhmm for program in tou.programs] == [
        0,
        400,
        800,
        1200,
        1600,
        2000,
    ]


def test_too_many_windows_are_truncated_and_reported() -> None:
    """Six slots cannot hold more than three windows with their idle slots."""
    windows = tuple(
        ChargeWindow(
            start=NOON + timedelta(hours=2 * index),
            end=NOON + timedelta(hours=2 * index, minutes=30),
            energy_kwh=1.0,
            cost=0.05,
            value=0.36,
            profit=0.31,
            average_price=0.05,
            discharge_price=0.4,
        )
        for index in range(5)
    )
    plan = ChargePlan(
        generated_at=NOON,
        soc_pct=20.0,
        windows=windows,
        energy_kwh=5.0,
        cost=0.25,
        expected_saving=1.55,
    )
    tou = build_tou_programs(plan, SETTINGS, NOON)

    assert len(tou.programs) == TOU_SLOTS
    assert tou.truncated
    charging = [p for p in tou.programs if p.source is ChargeSource.GRID]
    assert len(charging) == 3


def test_plan_reports_active_and_next_window() -> None:
    """The plan can answer "charge now?" without extra state."""
    plan = plan_charge(make_prices(NOON, DAY), SETTINGS, NOON, soc_pct=20.0)
    window = plan.windows[0]

    assert plan.active_window(window.start) is not None
    assert plan.active_window(window.end) is None
    assert plan.next_window(NOON - timedelta(minutes=5)) is window
    assert plan.next_window(NOON) is None


def test_applied_plan_round_trips_through_storage() -> None:
    """The snapshot that is in force survives a restart unchanged."""
    from custom_components.kd_helios.battery import AppliedPlan

    plan = plan_charge(make_prices(NOON, DAY), SETTINGS, NOON, soc_pct=20.0)
    tou = build_tou_programs(plan, SETTINGS, NOON)
    applied = AppliedPlan.from_plan(plan, tou, NOON)

    restored = AppliedPlan.from_dict(applied.as_dict())

    assert restored == applied
    assert restored is not None
    assert restored.expected_saving == pytest.approx(plan.expected_saving)
    assert len(restored.programs) == TOU_SLOTS
    assert [program.hhmm for program in restored.programs] == [
        program.hhmm for program in tou.programs
    ]


def test_applied_plan_ignores_empty_state() -> None:
    """No stored plan is not an error."""
    from custom_components.kd_helios.battery import AppliedPlan

    assert AppliedPlan.from_dict(None) is None
    assert AppliedPlan.from_dict({}) is None
    assert AppliedPlan.from_dict({"applied_at": "not a date"}) is None


def test_the_charging_slot_is_what_decides_a_rewrite() -> None:
    """An idle slot earlier in the day must not look like a changed target.

    Slots are sorted by time of day, so the first program is usually an idle
    one at the reserve SOC. Comparing that to the target would rewrite the
    inverter on every tick.
    """
    from custom_components.kd_helios.battery import AppliedPlan, should_apply

    plan = plan_charge(make_prices(NOON, DAY), SETTINGS, NOON, soc_pct=20.0)
    tou = build_tou_programs(plan, SETTINGS, NOON)
    # Put an idle slot first, as the real ordering does.
    programs = tuple(sorted(tou.programs, key=lambda program: program.start_minute))
    applied = AppliedPlan.from_plan(
        plan, build_tou_programs(plan, SETTINGS, NOON), NOON
    )
    applied = AppliedPlan(
        applied_at=applied.applied_at,
        soc_pct=applied.soc_pct,
        energy_kwh=applied.energy_kwh,
        cost=applied.cost,
        expected_saving=applied.expected_saving,
        truncated=False,
        programs=programs,
        windows=applied.windows,
    )

    assert not should_apply(plan, applied, SETTINGS)

    stricter = BatterySettings(
        capacity_kwh=SETTINGS.capacity_kwh,
        max_charge_kw=SETTINGS.max_charge_kw,
        reserve_soc_pct=SETTINGS.reserve_soc_pct,
        target_soc_pct=80.0,
        efficiency_pct=SETTINGS.efficiency_pct,
        horizon_hours=SETTINGS.horizon_hours,
    )
    assert should_apply(plan, applied, stricter)


# -- priority and import cap ------------------------------------------------
def test_a_charging_car_stops_the_battery_filling() -> None:
    """With the EV first, the battery does not charge under a charging car."""
    from custom_components.kd_helios.battery import charge_power_limit
    from custom_components.kd_helios.const import Priority

    assert charge_power_limit(11.0, 0.0, 6.0, 11.0, Priority.EV) == 0.0
    # The same situation with the battery first still fills it.
    assert charge_power_limit(11.0, 0.0, 6.0, 11.0, Priority.BATTERY) == 11.0


def test_a_trickle_load_does_not_count_as_charging() -> None:
    """An idle car drawing a few hundred watts is not charging."""
    from custom_components.kd_helios.battery import charge_power_limit
    from custom_components.kd_helios.const import Priority

    assert charge_power_limit(11.0, 0.0, 0.3, 0.2, Priority.EV) == 11.0


def test_the_import_cap_leaves_room_for_the_house() -> None:
    """The battery only takes what is left under the connection limit."""
    from custom_components.kd_helios.battery import charge_power_limit
    from custom_components.kd_helios.const import Priority

    assert charge_power_limit(11.0, 17.0, 5.0, 0.0, Priority.EV) == 11.0
    assert charge_power_limit(11.0, 17.0, 15.0, 0.0, Priority.BATTERY) == 2.0
    assert charge_power_limit(11.0, 17.0, 20.0, 0.0, Priority.BATTERY) == 0.0
