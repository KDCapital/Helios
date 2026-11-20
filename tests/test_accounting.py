"""Tests for the energy and cost accounting engine."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.kd_helios.accounting import DeviceAccumulators, DeviceTracker
from custom_components.kd_helios.const import (
    DeviceKind,
    EreMode,
    MarketPriceBasis,
    ReimbursementMode,
)
from custom_components.kd_helios.models import (
    DeviceConfig,
    EreConfig,
    ReimbursementConfig,
)
from tests.helpers import (
    FROZEN_TIME,
    UTC,
    build_series,
    flat_series,
    set_energy_state,
    set_power_state,
)

PRICE = 0.20


def make_tracker(
    hass,
    *,
    energy_entity: str | None = "sensor.device_energy",
    power_entity: str | None = None,
    kind: DeviceKind = DeviceKind.DEVICE,
    reimbursement: ReimbursementConfig | None = None,
    ere: EreConfig | None = None,
    state: dict | None = None,
) -> DeviceTracker:
    """Build a tracker with a minimal device config."""
    config = DeviceConfig(
        subentry_id="device-1",
        name="Test device",
        kind=kind,
        power_entity=power_entity,
        energy_entity=energy_entity,
        reimbursement=reimbursement or ReimbursementConfig(),
        ere=ere or EreConfig(),
    )
    return DeviceTracker(hass, config, state)


async def test_energy_meter_is_priced_per_kwh(hass) -> None:
    """A one kWh increase costs exactly the all-in price."""
    tracker = make_tracker(hass)
    series = flat_series(FROZEN_TIME, all_in=PRICE, spot=0.10)

    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(FROZEN_TIME, series)  # baseline only

    set_energy_state(hass, "sensor.device_energy", 11.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)

    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(minutes=1), series)
    assert snapshot.energy_kwh == pytest.approx(1.0)
    assert snapshot.cost == pytest.approx(PRICE)
    assert snapshot.cost_today == pytest.approx(PRICE)


async def test_first_tick_only_takes_a_baseline(hass) -> None:
    """The first reading must never be billed as consumption."""
    tracker = make_tracker(hass)
    series = flat_series(FROZEN_TIME, all_in=PRICE)

    set_energy_state(hass, "sensor.device_energy", 1000.0)
    tracker.update(FROZEN_TIME, series)

    snapshot = tracker.snapshot(FROZEN_TIME, series)
    assert snapshot.energy_kwh == 0.0
    assert snapshot.cost == 0.0


async def test_price_change_is_split_across_the_window(hass) -> None:
    """Energy consumed across a tariff change is priced per segment."""
    start = datetime(2026, 11, 20, 12, 14, tzinfo=UTC)
    series = build_series(
        {
            datetime(2026, 11, 20, 12, 0, tzinfo=UTC): (0.10, 0.05),
            datetime(2026, 11, 20, 12, 15, tzinfo=UTC): (0.30, 0.05),
        }
    )
    tracker = make_tracker(hass)

    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(start, series)

    # Two kWh over two minutes, one minute on each tariff.
    set_energy_state(hass, "sensor.device_energy", 12.0)
    tracker.update(start + timedelta(minutes=2), series)

    snapshot = tracker.snapshot(start + timedelta(minutes=2), series)
    assert snapshot.energy_kwh == pytest.approx(2.0)
    assert snapshot.cost == pytest.approx(1.0 * 0.10 + 1.0 * 0.30)


async def test_power_sensor_is_integrated_with_a_trapezoid(hass) -> None:
    """Power is integrated over the window using both endpoints."""
    tracker = make_tracker(hass, energy_entity=None, power_entity="sensor.device_power")
    series = flat_series(FROZEN_TIME, all_in=PRICE)

    set_power_state(hass, "sensor.device_power", 1000.0)
    tracker.update(FROZEN_TIME, series)

    set_power_state(hass, "sensor.device_power", 3000.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)

    expected_kwh = (1000.0 + 3000.0) / 2 * (60 / 3600) / 1000
    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(minutes=1), series)
    assert snapshot.energy_kwh == pytest.approx(expected_kwh)
    assert snapshot.cost == pytest.approx(expected_kwh * PRICE)


async def test_power_sensor_units_are_converted(hass) -> None:
    """A power sensor in kW is converted to watt."""
    tracker = make_tracker(hass, energy_entity=None, power_entity="sensor.device_power")
    series = flat_series(FROZEN_TIME, all_in=PRICE)

    set_power_state(hass, "sensor.device_power", 2.0, unit="kW")
    tracker.update(FROZEN_TIME, series)
    set_power_state(hass, "sensor.device_power", 2.0, unit="kW")
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)

    expected_kwh = 2000.0 * (60 / 3600) / 1000
    assert tracker.snapshot(
        FROZEN_TIME + timedelta(minutes=1), series
    ).energy_kwh == pytest.approx(expected_kwh)


async def test_energy_sensor_in_wh_is_converted(hass) -> None:
    """An energy sensor in Wh is converted to kWh."""
    tracker = make_tracker(hass)
    series = flat_series(FROZEN_TIME, all_in=PRICE)

    set_energy_state(hass, "sensor.device_energy", 10000.0, unit="Wh")
    tracker.update(FROZEN_TIME, series)
    set_energy_state(hass, "sensor.device_energy", 11000.0, unit="Wh")
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)

    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(minutes=1), series)
    assert snapshot.energy_kwh == pytest.approx(1.0)
    assert snapshot.cost == pytest.approx(PRICE)


async def test_unavailable_source_does_not_advance_the_clock(hass) -> None:
    """An outage is priced over its real duration once the source returns."""
    tracker = make_tracker(hass)
    series = flat_series(FROZEN_TIME, all_in=PRICE)

    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(FROZEN_TIME, series)

    hass.states.async_set("sensor.device_energy", "unavailable")
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)
    assert tracker.acc.last_tick == FROZEN_TIME

    set_energy_state(hass, "sensor.device_energy", 11.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=3), series)

    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(minutes=3), series)
    assert snapshot.energy_kwh == pytest.approx(1.0)
    assert snapshot.cost == pytest.approx(PRICE)


async def test_long_gap_is_not_priced(hass) -> None:
    """Energy consumed while Home Assistant was down is not silently billed."""
    tracker = make_tracker(hass)
    series = flat_series(FROZEN_TIME, all_in=PRICE, hours=8)

    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(FROZEN_TIME, series)

    set_energy_state(hass, "sensor.device_energy", 20.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=30), series)

    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(minutes=30), series)
    assert snapshot.energy_kwh == 0.0
    assert snapshot.cost == 0.0
    assert tracker.acc.untracked_gaps == 1
    # The baseline moved so the next tick accrues normally.
    assert tracker.acc.last_value == pytest.approx(20.0)


async def test_meter_reset_rebases_without_billing(hass) -> None:
    """A replaced meter must not produce a negative or huge delta."""
    tracker = make_tracker(hass)
    series = flat_series(FROZEN_TIME, all_in=PRICE)

    set_energy_state(hass, "sensor.device_energy", 500.0)
    tracker.update(FROZEN_TIME, series)
    set_energy_state(hass, "sensor.device_energy", 5.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)

    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(minutes=1), series)
    assert snapshot.energy_kwh == 0.0
    assert tracker.acc.last_value == pytest.approx(5.0)


async def test_negative_prices_credit_the_device(hass) -> None:
    """A negative tariff turns consumption into income."""
    tracker = make_tracker(hass)
    series = flat_series(FROZEN_TIME, all_in=-0.05, spot=-0.10)

    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(FROZEN_TIME, series)
    set_energy_state(hass, "sensor.device_energy", 12.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)

    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(minutes=1), series)
    assert snapshot.cost == pytest.approx(-0.10)


async def test_fixed_reimbursement_can_exceed_the_home_tariff(hass) -> None:
    """Company-paid charging produces a negative net cost."""
    tracker = make_tracker(
        hass,
        kind=DeviceKind.EV_CHARGER,
        reimbursement=ReimbursementConfig(
            mode=ReimbursementMode.FIXED, fixed_rate=0.35
        ),
    )
    series = flat_series(FROZEN_TIME, all_in=PRICE)

    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(FROZEN_TIME, series)
    set_energy_state(hass, "sensor.device_energy", 11.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)

    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(minutes=1), series)
    assert snapshot.reimbursement == pytest.approx(0.35)
    assert snapshot.reimbursement_rate == pytest.approx(0.35)
    assert snapshot.net_cost == pytest.approx(PRICE - 0.35)


async def test_dynamic_reimbursement_tracks_the_spot_price(hass) -> None:
    """The dynamic formula follows the spot price of each segment."""
    tracker = make_tracker(
        hass,
        kind=DeviceKind.EV_CHARGER,
        reimbursement=ReimbursementConfig(
            mode=ReimbursementMode.DYNAMIC,
            surcharge=0.02,
            percent=100.0,
            basis=MarketPriceBasis.SPOT,
        ),
    )
    series = flat_series(FROZEN_TIME, all_in=0.30, spot=0.10)

    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(FROZEN_TIME, series)
    set_energy_state(hass, "sensor.device_energy", 11.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)

    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(minutes=1), series)
    assert snapshot.reimbursement_rate == pytest.approx(0.12)
    assert snapshot.reimbursement == pytest.approx(0.12)
    assert snapshot.net_cost == pytest.approx(0.30 - 0.12)


async def test_dynamic_reimbursement_surcharge_percentage(hass) -> None:
    """The percentage multiplies the surcharged market price."""
    tracker = make_tracker(
        hass,
        kind=DeviceKind.EV_CHARGER,
        reimbursement=ReimbursementConfig(
            mode=ReimbursementMode.DYNAMIC,
            surcharge=0.02,
            percent=110.0,
            basis=MarketPriceBasis.SPOT,
        ),
    )
    series = flat_series(FROZEN_TIME, all_in=0.30, spot=0.10)

    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(FROZEN_TIME, series)
    set_energy_state(hass, "sensor.device_energy", 11.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)

    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(minutes=1), series)
    assert snapshot.reimbursement_rate == pytest.approx((0.10 + 0.02) * 1.10)


async def test_fixed_ere_value(hass) -> None:
    """A fixed ERE ratio credits the device per kWh."""
    tracker = make_tracker(
        hass,
        ere=EreConfig(mode=EreMode.FIXED, ere_per_kwh=0.25, ere_price=4.0),
    )
    series = flat_series(FROZEN_TIME, all_in=PRICE)

    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(FROZEN_TIME, series)
    set_energy_state(hass, "sensor.device_energy", 12.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)

    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(minutes=1), series)
    assert snapshot.ere_value == pytest.approx(2.0 * 0.25 * 4.0)
    assert snapshot.ere_credits == pytest.approx(2.0 * 0.25)
    assert snapshot.net_cost == pytest.approx(2.0 * PRICE - 2.0 * 0.25 * 4.0)


async def test_joulo_ere_ratio_is_derived_from_counters(hass) -> None:
    """The Joulo counters determine the ERE per kWh."""
    tracker = make_tracker(
        hass,
        ere=EreConfig(
            mode=EreMode.JOULO,
            ere_per_kwh=0.10,
            ere_price=4.0,
            credits_entity="sensor.joulo_ere",
            kwh_entity="sensor.joulo_kwh",
        ),
    )
    series = flat_series(FROZEN_TIME, all_in=PRICE)

    hass.states.async_set("sensor.joulo_ere", "100.0")
    hass.states.async_set("sensor.joulo_kwh", "1000.0")
    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(FROZEN_TIME, series)  # baseline for both counters

    # 50 kWh charged yields 10 ERE: 0.2 ERE/kWh.
    hass.states.async_set("sensor.joulo_ere", "110.0")
    hass.states.async_set("sensor.joulo_kwh", "1050.0")
    set_energy_state(hass, "sensor.device_energy", 11.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)

    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(minutes=1), series)
    assert snapshot.ere_rate == pytest.approx(0.2)
    assert snapshot.ere_value == pytest.approx(1.0 * 0.2 * 4.0)


async def test_joulo_falls_back_until_the_ratio_is_known(hass) -> None:
    """Without enough Joulo data the configured ratio is used."""
    tracker = make_tracker(
        hass,
        ere=EreConfig(
            mode=EreMode.JOULO,
            ere_per_kwh=0.10,
            ere_price=4.0,
            credits_entity="sensor.joulo_ere",
            kwh_entity="sensor.joulo_kwh",
        ),
    )
    series = flat_series(FROZEN_TIME, all_in=PRICE)

    hass.states.async_set("sensor.joulo_ere", "100.0")
    hass.states.async_set("sensor.joulo_kwh", "1000.0")
    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(FROZEN_TIME, series)

    # Only half a kWh on the account: far below the trusted window.
    hass.states.async_set("sensor.joulo_ere", "100.1")
    hass.states.async_set("sensor.joulo_kwh", "1000.5")
    set_energy_state(hass, "sensor.device_energy", 11.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)

    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(minutes=1), series)
    assert snapshot.ere_rate == pytest.approx(0.10)


async def test_energy_without_a_series_is_not_billed(hass) -> None:
    """With no tariff at all nothing is billed and nothing is invented."""
    tracker = make_tracker(hass)

    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(FROZEN_TIME, None)
    set_energy_state(hass, "sensor.device_energy", 11.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=1), None)

    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(minutes=1), None)
    assert snapshot.energy_kwh == 0.0
    assert snapshot.cost == 0.0
    assert snapshot.price is None


async def test_energy_with_an_unknown_price_is_flagged(hass) -> None:
    """Energy is still metered when a slot has no price, but not billed."""
    tracker = make_tracker(hass)
    series = build_series({FROZEN_TIME: (None, None)})

    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(FROZEN_TIME, series)
    set_energy_state(hass, "sensor.device_energy", 11.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)

    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(minutes=1), series)
    assert snapshot.energy_kwh == pytest.approx(1.0)
    assert snapshot.cost == 0.0
    assert snapshot.unpriced_kwh == pytest.approx(1.0)


async def test_daily_counter_resets_at_local_midnight(hass) -> None:
    """Cost today restarts at midnight in the local time zone."""
    previous_zone = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("Europe/Amsterdam"))
    tracker = make_tracker(hass)
    # 22:45 UTC is 23:45 in Amsterdam; the next ticks cross local midnight.
    start = datetime(2026, 11, 20, 22, 45, tzinfo=UTC)
    series = flat_series(start, all_in=PRICE, hours=4)

    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(start, series)

    set_energy_state(hass, "sensor.device_energy", 10.5)
    tracker.update(start + timedelta(minutes=3), series)
    assert tracker.snapshot(
        start + timedelta(minutes=3), series
    ).cost_today == pytest.approx(0.10)

    # 22:57 UTC is still 23:57 the same day in Amsterdam.
    set_energy_state(hass, "sensor.device_energy", 11.0)
    tracker.update(start + timedelta(minutes=12), series)
    assert tracker.snapshot(
        start + timedelta(minutes=12), series
    ).cost_today == pytest.approx(0.20)

    # 23:02 UTC is 00:02 the next day in Amsterdam.
    set_energy_state(hass, "sensor.device_energy", 11.5)
    tracker.update(start + timedelta(minutes=17), series)
    snapshot = tracker.snapshot(start + timedelta(minutes=17), series)
    assert snapshot.cost == pytest.approx(0.30)
    assert snapshot.cost_today == pytest.approx(0.10)
    dt_util.set_default_time_zone(previous_zone)


async def test_accumulators_round_trip_through_storage(hass) -> None:
    """Persisted state restores totals, baselines and gap counters."""
    original = DeviceAccumulators(
        total_kwh=12.5,
        total_cost=3.5,
        total_reimbursement=1.0,
        total_ere_value=0.5,
        total_ere_credits=2.5,
        day="2026-11-20",
        day_cost=0.9,
        day_kwh=1.5,
        last_value=42.0,
        last_tick=FROZEN_TIME,
        source="sensor.device_energy",
        untracked_gaps=2,
        rejected_deltas=1,
        unpriced_kwh=0.25,
        joulo_base_ere=100.0,
        joulo_base_kwh=1000.0,
    )
    restored = DeviceAccumulators.from_dict(original.as_dict())
    assert restored == original


async def test_restored_totals_continue_after_a_restart(hass) -> None:
    """A restored tracker keeps its totals and takes a fresh baseline."""
    state = DeviceAccumulators(
        total_kwh=5.0,
        total_cost=1.0,
        last_value=10.0,
        last_tick=FROZEN_TIME - timedelta(minutes=1),
    ).as_dict()
    tracker = make_tracker(hass, state=state)
    series = flat_series(FROZEN_TIME, all_in=PRICE)

    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(FROZEN_TIME, series)

    snapshot = tracker.snapshot(FROZEN_TIME, series)
    assert snapshot.energy_kwh == pytest.approx(5.0)
    assert snapshot.cost == pytest.approx(1.0)

    # The restored tick was long ago relative to a fresh start, so the first
    # real delta is measured from now on.
    set_energy_state(hass, "sensor.device_energy", 11.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)
    assert tracker.snapshot(
        FROZEN_TIME + timedelta(minutes=1), series
    ).energy_kwh == pytest.approx(6.0)


async def test_missing_entity_is_reported_as_unavailable(hass) -> None:
    """A source that does not exist leaves the snapshot without data."""
    tracker = make_tracker(hass, energy_entity="sensor.does_not_exist")
    series = flat_series(FROZEN_TIME, all_in=PRICE)
    tracker.update(FROZEN_TIME, series)

    snapshot = tracker.snapshot(FROZEN_TIME, series)
    assert snapshot.source_available is False
    assert snapshot.energy_kwh == 0.0


async def test_swapping_the_energy_entity_does_not_bill_the_difference(hass) -> None:
    """Pointing a device at a different meter must rebase, not invent energy."""
    tracker = make_tracker(hass)
    series = flat_series(FROZEN_TIME, all_in=PRICE)

    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(FROZEN_TIME, series)

    # The user reconfigures the device to a meter reading 1234.5 kWh.
    swapped = DeviceTracker(
        hass,
        DeviceConfig(
            subentry_id="device-1",
            name="Test device",
            energy_entity="sensor.other_energy",
        ),
        tracker.acc.as_dict(),
    )
    set_energy_state(hass, "sensor.other_energy", 1234.5)
    swapped.update(FROZEN_TIME + timedelta(seconds=60), series)

    snapshot = swapped.snapshot(FROZEN_TIME + timedelta(seconds=60), series)
    assert snapshot.energy_kwh == 0.0
    assert snapshot.cost == 0.0
    assert swapped.acc.source == "sensor.other_energy"

    # From the new baseline it accrues normally again.
    set_energy_state(hass, "sensor.other_energy", 1235.5)
    swapped.update(FROZEN_TIME + timedelta(seconds=120), series)
    assert swapped.snapshot(
        FROZEN_TIME + timedelta(seconds=120), series
    ).energy_kwh == pytest.approx(1.0)


async def test_implausible_delta_is_rejected(hass) -> None:
    """A meter that jumps by a year of energy is not billed."""
    tracker = make_tracker(hass)
    series = flat_series(FROZEN_TIME, all_in=PRICE)

    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(FROZEN_TIME, series)

    # 500 kWh in one minute implies 30 MW.
    set_energy_state(hass, "sensor.device_energy", 510.0)
    tracker.update(FROZEN_TIME + timedelta(seconds=60), series)

    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(seconds=60), series)
    assert snapshot.energy_kwh == 0.0
    assert snapshot.cost == 0.0
    assert tracker.acc.rejected_deltas == 1
    # The baseline moved, so normal accounting resumes.
    assert tracker.acc.last_value == pytest.approx(510.0)

    set_energy_state(hass, "sensor.device_energy", 511.0)
    tracker.update(FROZEN_TIME + timedelta(seconds=120), series)
    assert tracker.snapshot(
        FROZEN_TIME + timedelta(seconds=120), series
    ).energy_kwh == pytest.approx(1.0)


async def test_ere_credits_do_not_rewrite_history(hass) -> None:
    """Credits accrue at the ratio in force, they are not recomputed."""
    tracker = make_tracker(
        hass,
        ere=EreConfig(
            mode=EreMode.JOULO,
            ere_per_kwh=0.1,
            ere_price=4.0,
            credits_entity="sensor.joulo_ere",
            kwh_entity="sensor.joulo_kwh",
        ),
    )
    series = flat_series(FROZEN_TIME, all_in=PRICE)

    hass.states.async_set("sensor.joulo_ere", "100.0")
    hass.states.async_set("sensor.joulo_kwh", "1000.0")
    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(FROZEN_TIME, series)  # baselines

    # 10 ERE over 50 kWh: 0.2 ERE/kWh.
    hass.states.async_set("sensor.joulo_ere", "110.0")
    hass.states.async_set("sensor.joulo_kwh", "1050.0")
    set_energy_state(hass, "sensor.device_energy", 11.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)

    first = tracker.snapshot(FROZEN_TIME + timedelta(minutes=1), series)
    assert first.ere_credits == pytest.approx(0.2)
    assert first.ere_value == pytest.approx(0.8)

    # The cumulative ratio rises to 1.1; already booked credits must not move.
    hass.states.async_set("sensor.joulo_ere", "210.0")
    hass.states.async_set("sensor.joulo_kwh", "1100.0")
    set_energy_state(hass, "sensor.device_energy", 12.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=2), series)

    second = tracker.snapshot(FROZEN_TIME + timedelta(minutes=2), series)
    assert second.ere_rate == pytest.approx(1.1)
    assert second.ere_credits == pytest.approx(0.2 + 1.1)
    assert second.ere_value == pytest.approx(4.0 * (0.2 + 1.1))


async def test_joulo_counter_reset_is_detected(hass) -> None:
    """A reset Joulo counter rebases the ratio instead of drifting."""
    tracker = make_tracker(
        hass,
        ere=EreConfig(
            mode=EreMode.JOULO,
            ere_per_kwh=0.1,
            ere_price=4.0,
            credits_entity="sensor.joulo_ere",
            kwh_entity="sensor.joulo_kwh",
        ),
    )
    series = flat_series(FROZEN_TIME, all_in=PRICE)

    hass.states.async_set("sensor.joulo_ere", "100.0")
    hass.states.async_set("sensor.joulo_kwh", "1000.0")
    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(FROZEN_TIME, series)

    # Counters restart from zero.
    hass.states.async_set("sensor.joulo_ere", "0.0")
    hass.states.async_set("sensor.joulo_kwh", "0.0")
    set_energy_state(hass, "sensor.device_energy", 11.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)

    assert tracker.acc.joulo_base_ere == pytest.approx(0.0)
    assert tracker.acc.joulo_base_kwh == pytest.approx(0.0)
    # Falls back to the configured ratio right after the reset.
    assert tracker.snapshot(
        FROZEN_TIME + timedelta(minutes=1), series
    ).ere_rate == pytest.approx(0.1)

    # 10 ERE over 50 kWh after the reset: a clean 0.2.
    hass.states.async_set("sensor.joulo_ere", "10.0")
    hass.states.async_set("sensor.joulo_kwh", "50.0")
    set_energy_state(hass, "sensor.device_energy", 12.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=2), series)

    assert tracker.snapshot(
        FROZEN_TIME + timedelta(minutes=2), series
    ).ere_rate == pytest.approx(0.2)


async def test_clock_stays_open_while_prices_are_missing(hass) -> None:
    """Energy consumed without a tariff is priced once a tariff returns."""
    tracker = make_tracker(hass)
    series = flat_series(FROZEN_TIME, all_in=PRICE)

    set_energy_state(hass, "sensor.device_energy", 10.0)
    tracker.update(FROZEN_TIME, series)  # baseline

    set_energy_state(hass, "sensor.device_energy", 11.0)
    tracker.update(FROZEN_TIME + timedelta(seconds=60), None)
    # The clock did not advance, so the window is still open.
    assert tracker.acc.last_tick == FROZEN_TIME

    tracker.update(FROZEN_TIME + timedelta(seconds=120), series)
    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(seconds=120), series)
    assert snapshot.energy_kwh == pytest.approx(1.0)
    assert snapshot.cost == pytest.approx(PRICE)
    assert snapshot.unpriced_kwh == 0.0
