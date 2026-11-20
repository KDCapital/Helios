"""Tests for grid connection accounting and saldering."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.kd_helios.const import SALDERING_END_DATE
from custom_components.kd_helios.grid import (
    GridAccumulators,
    GridTracker,
    feed_in_price,
)
from custom_components.kd_helios.models import GridConfig
from tests.helpers import FROZEN_TIME, UTC, flat_series, set_energy_state

IMPORT = "sensor.grid_import"
EXPORT = "sensor.grid_export"

#: One minute of a 12 kW load is 0.2 kWh; anything above 500 kW is rejected as
#: a sensor glitch, so test deltas must stay realistic.
TICK = timedelta(minutes=1)
IMPORT_KWH = 0.20
EXPORT_KWH = 0.10


def make_grid(hass, **kwargs) -> GridTracker:
    """Build a grid tracker with sensible defaults."""
    return GridTracker(hass, GridConfig(subentry_id="grid-1", **kwargs))


def test_feed_in_price_subtracts_the_deduction() -> None:
    """A normal deduction leaves less than the spot price."""
    assert feed_in_price(0.20, 0.01361) == pytest.approx(0.18639)


def test_feed_in_price_can_be_a_bonus() -> None:
    """A negative deduction pays more than the spot price."""
    assert feed_in_price(0.20, -0.02) == pytest.approx(0.22)


def test_feed_in_price_without_spot() -> None:
    """No spot price means no feed-in price."""
    assert feed_in_price(None, 0.01) is None


async def test_import_is_priced_at_the_all_in_tariff(hass) -> None:
    """Imported kWh cost the all-in consumer price."""
    tracker = make_grid(hass, import_entity=IMPORT)
    series = flat_series(FROZEN_TIME, all_in=0.30, spot=0.20)

    set_energy_state(hass, IMPORT, 1000.0)
    tracker.update(FROZEN_TIME, series)
    set_energy_state(hass, IMPORT, 1000.0 + IMPORT_KWH)
    tracker.update(FROZEN_TIME + TICK, series)

    snapshot = tracker.snapshot(FROZEN_TIME + TICK, series)
    assert snapshot.imported_kwh == pytest.approx(IMPORT_KWH)
    assert snapshot.imported_cost == pytest.approx(IMPORT_KWH * 0.30)
    assert snapshot.export_credit == 0.0
    assert snapshot.total_cost == pytest.approx(IMPORT_KWH * 0.30)


async def test_export_earns_the_feed_in_price_without_saldering(hass) -> None:
    """After saldering ends, export is simply paid at the feed-in price."""
    tracker = make_grid(
        hass,
        export_entity=EXPORT,
        saldering=False,
        export_deduction=0.02,
    )
    series = flat_series(FROZEN_TIME, all_in=0.30, spot=0.20)

    set_energy_state(hass, EXPORT, 500.0)
    tracker.update(FROZEN_TIME, series)
    set_energy_state(hass, EXPORT, 500.0 + EXPORT_KWH)
    tracker.update(FROZEN_TIME + TICK, series)

    snapshot = tracker.snapshot(FROZEN_TIME + TICK, series)
    assert snapshot.exported_kwh == pytest.approx(EXPORT_KWH)
    assert snapshot.export_price == pytest.approx(0.18)
    assert snapshot.export_credit == pytest.approx(EXPORT_KWH * 0.18)
    # Export turns the grid cost negative: the connection earned money.
    assert snapshot.total_cost == pytest.approx(-EXPORT_KWH * 0.18)


async def test_saldering_values_export_at_the_import_tariff(hass) -> None:
    """While saldering applies, exported kWh are worth the import tariff."""
    tracker = make_grid(
        hass,
        import_entity=IMPORT,
        export_entity=EXPORT,
        saldering=True,
        export_deduction=0.02,
    )
    series = flat_series(FROZEN_TIME, all_in=0.30, spot=0.20)

    set_energy_state(hass, IMPORT, 100.0)
    set_energy_state(hass, EXPORT, 500.0)
    tracker.update(FROZEN_TIME, series)
    set_energy_state(hass, IMPORT, 100.0 + IMPORT_KWH)
    set_energy_state(hass, EXPORT, 500.0 + EXPORT_KWH)
    tracker.update(FROZEN_TIME + TICK, series)

    snapshot = tracker.snapshot(FROZEN_TIME + TICK, series)
    assert snapshot.imported_kwh == pytest.approx(IMPORT_KWH)
    assert snapshot.exported_kwh == pytest.approx(EXPORT_KWH)
    # Exported kWh are worth the 0.30 import tariff, not the 0.18 feed-in price.
    assert snapshot.export_credit == pytest.approx(EXPORT_KWH * 0.30)
    assert snapshot.netted_kwh == pytest.approx(EXPORT_KWH)
    assert snapshot.open_export_kwh == 0.0
    assert snapshot.total_cost == pytest.approx(IMPORT_KWH * 0.30 - EXPORT_KWH * 0.30)


async def test_saldering_only_nets_what_you_imported(hass) -> None:
    """Export beyond your own consumption is paid at the feed-in price."""
    tracker = make_grid(
        hass,
        import_entity=IMPORT,
        export_entity=EXPORT,
        saldering=True,
        export_deduction=0.02,
    )
    series = flat_series(FROZEN_TIME, all_in=0.30, spot=0.20)

    set_energy_state(hass, IMPORT, 100.0)
    set_energy_state(hass, EXPORT, 500.0)
    tracker.update(FROZEN_TIME, series)
    used_import = 0.10
    used_export = 0.25
    set_energy_state(hass, IMPORT, 100.0 + used_import)
    set_energy_state(hass, EXPORT, 500.0 + used_export)
    tracker.update(FROZEN_TIME + TICK, series)

    snapshot = tracker.snapshot(FROZEN_TIME + TICK, series)
    # 0.10 of 0.25 kWh are netted at 0.30, the remaining 0.15 earn 0.18.
    expected = used_import * 0.30 + (used_export - used_import) * 0.18
    assert snapshot.netted_kwh == pytest.approx(used_import)
    assert snapshot.open_export_kwh == pytest.approx(used_export - used_import)
    assert snapshot.export_credit == pytest.approx(expected)
    assert snapshot.total_cost == pytest.approx(used_import * 0.30 - expected)


async def test_saldering_switches_off_after_2026(hass) -> None:
    """The toggle is honoured only until saldering legally ends."""
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("Europe/Amsterdam"))
    tracker = make_grid(hass, saldering=True)

    try:
        assert tracker.saldering_active(datetime(2026, 12, 31, 22, 0, tzinfo=UTC))
        # 2026-12-31T23:00Z is midnight in Amsterdam: saldering is over.
        assert not tracker.saldering_active(datetime(2026, 12, 31, 23, 0, tzinfo=UTC))
        assert not tracker.saldering_active(datetime(2027, 6, 1, tzinfo=UTC))
    finally:
        dt_util.set_default_time_zone(previous)


async def test_export_after_2026_is_not_saldered(hass) -> None:
    """Export in 2027 is paid at the feed-in price even with the toggle on."""
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("Europe/Amsterdam"))
    tracker = make_grid(
        hass, export_entity=EXPORT, saldering=True, export_deduction=0.02
    )
    series = flat_series(
        datetime(2027, 3, 1, 12, 0, tzinfo=UTC), all_in=0.30, spot=0.20
    )

    try:
        when = datetime(2027, 3, 1, 12, 0, tzinfo=UTC)
        set_energy_state(hass, EXPORT, 500.0)
        tracker.update(when, series)
        set_energy_state(hass, EXPORT, 500.0 + EXPORT_KWH)
        tracker.update(when + TICK, series)

        snapshot = tracker.snapshot(when + TICK, series)
        assert snapshot.saldering_enabled is True
        assert snapshot.saldering_active is False
        assert snapshot.export_credit == pytest.approx(EXPORT_KWH * 0.18)
    finally:
        dt_util.set_default_time_zone(previous)


async def test_fixed_costs_accrue_per_second(hass) -> None:
    """Monthly fixed costs are spread over the days of the month."""
    tracker = make_grid(hass, supply_cost_monthly=30.0, grid_fee_monthly=0.0)

    # No series is needed: fixed costs do not depend on consumption.
    tracker.update(FROZEN_TIME, None)
    tracker.update(FROZEN_TIME + timedelta(seconds=60), None)

    expected = 30.0 / (30 * 86400) * 60
    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(seconds=60), None)
    assert snapshot.fixed_cost == pytest.approx(expected, rel=1e-6)
    assert snapshot.fixed_monthly == pytest.approx(30.0)
    # Even without any energy, fixed costs are part of the bill.
    assert snapshot.total_cost == pytest.approx(expected, rel=1e-6)


async def test_fixed_costs_skip_a_tracking_gap(hass) -> None:
    """Downtime is not back-billed as fixed costs."""
    tracker = make_grid(hass, supply_cost_monthly=30.0)

    tracker.update(FROZEN_TIME, None)
    tracker.update(FROZEN_TIME + timedelta(minutes=30), None)

    assert tracker.snapshot(FROZEN_TIME + timedelta(minutes=30), None).fixed_cost == 0.0


async def test_year_rollover_resets_the_ledger(hass) -> None:
    """A new calendar year starts a fresh saldering ledger."""
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("Europe/Amsterdam"))
    tracker = make_grid(hass, import_entity=IMPORT)
    series = flat_series(
        datetime(2026, 12, 31, 22, 50, tzinfo=UTC), all_in=0.30, hours=4
    )

    try:
        start = datetime(2026, 12, 31, 22, 50, tzinfo=UTC)
        set_energy_state(hass, IMPORT, 1000.0)
        tracker.update(start, series)

        set_energy_state(hass, IMPORT, 1000.0 + IMPORT_KWH)
        tracker.update(start + timedelta(minutes=5), series)
        assert tracker.snapshot(
            start + timedelta(minutes=5), series
        ).imported_kwh == pytest.approx(IMPORT_KWH)

        # 23:05Z on 31 December is 00:05 on 1 January in Amsterdam.
        set_energy_state(hass, IMPORT, 1000.0 + 2 * IMPORT_KWH)
        tracker.update(start + timedelta(minutes=15), series)
        snapshot = tracker.snapshot(start + timedelta(minutes=15), series)

        assert snapshot.year == 2027
        assert snapshot.imported_kwh == pytest.approx(IMPORT_KWH)
        assert snapshot.imported_cost == pytest.approx(IMPORT_KWH * 0.30)
    finally:
        dt_util.set_default_time_zone(previous)


async def test_changing_the_import_meter_rebases(hass) -> None:
    """A different meter must not be billed as grid consumption."""
    tracker = make_grid(hass, import_entity=IMPORT)
    series = flat_series(FROZEN_TIME, all_in=0.30)

    set_energy_state(hass, IMPORT, 1000.0)
    tracker.update(FROZEN_TIME, series)

    swapped = GridTracker(
        hass,
        GridConfig(subentry_id="grid-1", import_entity="sensor.other_import"),
        tracker.acc.as_dict(),
    )
    set_energy_state(hass, "sensor.other_import", 9000.0)
    swapped.update(FROZEN_TIME + timedelta(seconds=60), series)

    snapshot = swapped.snapshot(FROZEN_TIME + timedelta(seconds=60), series)
    assert snapshot.imported_kwh == 0.0
    assert swapped.acc.import_source == "sensor.other_import"


async def test_implausible_grid_delta_is_rejected(hass) -> None:
    """A meter that jumps by megawatt hours is not billed."""
    tracker = make_grid(hass, import_entity=IMPORT)
    series = flat_series(FROZEN_TIME, all_in=0.30)

    set_energy_state(hass, IMPORT, 1000.0)
    tracker.update(FROZEN_TIME, series)
    set_energy_state(hass, IMPORT, 1600.0)  # 600 kWh in one minute
    tracker.update(FROZEN_TIME + timedelta(seconds=60), series)

    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(seconds=60), series)
    assert snapshot.imported_kwh == 0.0
    assert snapshot.rejected_deltas == 1


async def test_grid_accumulators_round_trip(hass) -> None:
    """The ledger survives storage, including datetimes and counters."""
    original = GridAccumulators(
        year=2026,
        imported_kwh=1234.5,
        imported_cost=321.0,
        exported_kwh=987.6,
        export_credit_saldered=290.0,
        export_credit_feedin=180.0,
        fixed_cost=42.0,
        unpriced_kwh=1.5,
        import_source=IMPORT,
        import_last_value=1234.5,
        import_last_tick=FROZEN_TIME,
        import_gaps=2,
        import_rejected=1,
        export_source=EXPORT,
        export_last_value=987.6,
        export_last_tick=FROZEN_TIME,
        export_gaps=3,
        export_rejected=0,
        fixed_tick=FROZEN_TIME,
    )
    restored = GridAccumulators.from_dict(original.as_dict())
    assert restored == original


async def test_unpriced_export_is_flagged(hass) -> None:
    """Without a spot price, export is metered but not valued."""
    tracker = make_grid(hass, export_entity=EXPORT, saldering=False)
    series = flat_series(FROZEN_TIME, all_in=0.30, spot=None)

    set_energy_state(hass, EXPORT, 500.0)
    tracker.update(FROZEN_TIME, series)
    set_energy_state(hass, EXPORT, 500.0 + EXPORT_KWH)
    tracker.update(FROZEN_TIME + TICK, series)

    snapshot = tracker.snapshot(FROZEN_TIME + TICK, series)
    assert snapshot.exported_kwh == pytest.approx(EXPORT_KWH)
    assert snapshot.export_credit == 0.0
    assert snapshot.export_price is None


def test_saldering_end_date_is_january_2027() -> None:
    """The legal end of net metering is 1 January 2027."""
    assert SALDERING_END_DATE.isoformat() == "2027-01-01"
