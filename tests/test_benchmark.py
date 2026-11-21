"""Planner timing, so the CPU question is answered with a number.

The estimate in the roadmap is ~10-100 ms for a constraint plan. This asserts a
generous ceiling across a simulated year: if the optimiser ever grows past it,
this test fails before the control loop starts missing ticks.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import homeassistant.util.dt as dt_util

from custom_components.kd_helios.battery import (
    BatterySettings,
    build_tou_programs,
    plan_charge,
)
from custom_components.kd_helios.prices import PriceMeta, PricePoint, PriceSeries

UTC = dt_util.UTC
DAYS = 365
SLOTS_PER_DAY = 96
BUDGET_SECONDS = 20.0


def _year_of_prices(start: datetime) -> PriceSeries:
    """Return a year of quarter-hour prices with a daily shape."""
    grid: dict[datetime, PricePoint] = {}
    for day in range(DAYS):
        for slot in range(SLOTS_PER_DAY):
            hour = slot / 4.0
            # Cheap at night, expensive in the evening.
            price = 0.20 + 0.12 * ((hour - 3) % 24) / 24.0
            moment = start + timedelta(days=day, minutes=15 * slot)
            grid[moment] = PricePoint(spot=price / 1.21, all_in=price)
    return PriceSeries(
        grid=grid,
        meta=PriceMeta(),
        provider="benchmark",
        interval="quarterly",
    )


def test_a_year_of_planning_fits_the_budget() -> None:
    """Planning one slot at a time for a year stays well inside the loop."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    series = _year_of_prices(start)
    settings = BatterySettings(
        capacity_kwh=10.0,
        max_charge_kw=5.0,
        reserve_soc_pct=10.0,
        target_soc_pct=100.0,
        efficiency_pct=90.0,
        horizon_hours=36,
    )

    samples = 500
    begin = time.perf_counter()
    for index in range(samples):
        now = start + timedelta(minutes=15 * index * 7)
        plan = plan_charge(series, settings, now, soc_pct=40.0)
        build_tou_programs(plan, settings, now)
    elapsed = time.perf_counter() - begin

    per_plan_ms = elapsed / samples * 1000
    print(f"\nplan+programs: {per_plan_ms:.1f} ms each over {samples} samples")
    assert per_plan_ms < 20.0, f"{per_plan_ms:.1f} ms per plan is too slow"
    assert elapsed < BUDGET_SECONDS
