from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from freezegun import freeze_time
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.kd_helios.accounting import DeviceTracker
from custom_components.kd_helios.const import (
    CONF_ERE_MODE,
    CONF_NAME,
    CONF_KIND,
    CONF_PRICE_INTERVAL,
    CONF_PROVIDER,
    DOMAIN,
    EreMode,
    SUBENTRY_TYPE_DEVICE,
)
from custom_components.kd_helios.models import DeviceConfig, EreConfig
from tests.helpers import (
    FROZEN_TIME,
    flat_series,
    load_fixture,
    set_energy_state,
    set_power_state,
)

USER_INPUT = {CONF_PROVIDER: "zonneplan", CONF_PRICE_INTERVAL: "quarterly"}
EV_SUBENTRY = {
    "subentry_type": SUBENTRY_TYPE_DEVICE,
    "data": {
        CONF_NAME: "EV charger",
        CONF_KIND: "ev_charger",
        "energy_entity": "sensor.ev_energy",
        CONF_ERE_MODE: "none",
    },
    "title": "EV charger",
    "unique_id": None,
}
PRICE = 0.20


async def test_price_coordinator_refresh_count(hass):
    """How often does the price coordinator actually refetch?"""
    entry = MockConfigEntry(
        domain=DOMAIN, data=USER_INPUT, unique_id="zonneplan_quarterly",
        title="Zonneplan", subentries_data=[EV_SUBENTRY],
    )
    entry.add_to_hass(hass)
    provider = load_fixture("provider_zonneplan_quarterly.json")
    spot = load_fixture("spot_prices.json")
    mock = AsyncMock(return_value=provider)
    with (
        freeze_time("2026-11-20 12:00:00+00:00") as frozen,
        patch(
            "custom_components.kd_helios.coordinator.async_fetch_provider_prices",
            new=mock,
        ),
        patch(
            "custom_components.kd_helios.coordinator.async_fetch_spot_prices",
            new=AsyncMock(return_value=spot),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        orig = entry.runtime_data.engine._async_update_data
        engine_calls = []

        async def wrapper():
            engine_calls.append(1)
            return await orig()

        entry.runtime_data.engine._async_update_data = wrapper
        print("CALLS AFTER SETUP:", mock.call_count)
        frozen.tick(timedelta(minutes=11))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        print("CALLS AFTER +11min:", mock.call_count, "engine ticks:", len(engine_calls))
        frozen.tick(timedelta(minutes=11))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        print("CALLS AFTER +22min:", mock.call_count, "engine ticks:", len(engine_calls))
        # Prove the engine timer keeps running over hours while prices are frozen.
        for _ in range(6):
            frozen.tick(timedelta(minutes=10))
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()
        print("after 60 more min -> price calls:", mock.call_count, "engine ticks:", len(engine_calls))
        print("price series covered (stale):", entry.runtime_data.prices.series.covered)
        snap = entry.runtime_data.engine.data
        if snap:
            first = next(iter(snap.values()))
            print("snapshot last_update:", first.last_update, "price now:", first.price)
        print("ENGINE ticks:", entry.runtime_data.engine.data is not None)
        print("SERIES COVERED:", entry.runtime_data.prices.series.covered)


async def test_device_registry_subentry_link(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data=USER_INPUT, unique_id="zonneplan_quarterly",
        title="Zonneplan", subentries_data=[EV_SUBENTRY],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    subentry_id = entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE)[0].subentry_id
    devreg = dr.async_get(hass)
    for dev in dr.async_entries_for_config_entry(devreg, entry.entry_id):
        print("DEVICE", dev.name, "ids=", dev.identifiers,
              "config_entry_id=", dev.config_entry_id,
              "config_subentry_id=", dev.config_subentry_id)
    print("SUBENTRY", subentry_id)


def test_source_switch_from_power_to_energy(hass):
    """Add an energy entity to a device that previously used power only."""
    series = flat_series(FROZEN_TIME, all_in=PRICE)
    old = DeviceTracker(
        hass,
        DeviceConfig(subentry_id="d", name="Dev", power_entity="sensor.power_only"),
        None,
    )
    set_power_state(hass, "sensor.power_only", 500.0)
    old.update(FROZEN_TIME, series)
    persisted = old.acc.as_dict()
    print("PERSISTED last_value:", persisted["last_value"])

    new = DeviceTracker(
        hass,
        DeviceConfig(
            subentry_id="d",
            name="Dev",
            power_entity="sensor.power_only",
            energy_entity="sensor.energy_new",
        ),
        persisted,
    )
    set_energy_state(hass, "sensor.energy_new", 1234.5)
    new.update(FROZEN_TIME + timedelta(minutes=1), series)
    print("PHANTOM kWh:", new.acc.total_kwh, "PHANTOM COST:", new.acc.total_cost)


def test_ere_credits_vs_ere_value(hass):
    """ERE credits sensor is recomputed from total kWh at the current rate."""
    series = flat_series(FROZEN_TIME, all_in=PRICE)
    tracker = DeviceTracker(
        hass,
        DeviceConfig(
            subentry_id="d",
            name="Dev",
            energy_entity="sensor.e",
            ere=EreConfig(
                mode=EreMode.JOULO,
                ere_price=4.0,
                credits_entity="sensor.joulo_ere",
                kwh_entity="sensor.joulo_kwh",
            ),
        ),
        None,
    )
    hass.states.async_set("sensor.joulo_ere", "100.0")
    hass.states.async_set("sensor.joulo_kwh", "1000.0")
    set_energy_state(hass, "sensor.e", 10.0)
    tracker.update(FROZEN_TIME, series)

    hass.states.async_set("sensor.joulo_ere", "110.0")
    hass.states.async_set("sensor.joulo_kwh", "1050.0")
    set_energy_state(hass, "sensor.e", 11.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)
    snap = tracker.snapshot(FROZEN_TIME + timedelta(minutes=1), series)
    print("after 1st kWh: rate", snap.ere_rate, "credits", snap.ere_credits, "value", snap.ere_value)

    hass.states.async_set("sensor.joulo_ere", "210.0")
    hass.states.async_set("sensor.joulo_kwh", "1100.0")
    set_energy_state(hass, "sensor.e", 11.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=2), series)
    snap = tracker.snapshot(FROZEN_TIME + timedelta(minutes=2), series)
    print("after ratio change: rate", snap.ere_rate, "credits", snap.ere_credits, "value", snap.ere_value)


def test_joulo_counter_reset(hass):
    """What happens when the Joulo counters reset to zero?"""
    series = flat_series(FROZEN_TIME, all_in=PRICE)
    tracker = DeviceTracker(
        hass,
        DeviceConfig(
            subentry_id="d",
            name="Dev",
            energy_entity="sensor.e",
            ere=EreConfig(
                mode=EreMode.JOULO,
                ere_per_kwh=0.10,
                ere_price=4.0,
                credits_entity="sensor.joulo_ere",
                kwh_entity="sensor.joulo_kwh",
            ),
        ),
        None,
    )
    hass.states.async_set("sensor.joulo_ere", "100.0")
    hass.states.async_set("sensor.joulo_kwh", "1000.0")
    set_energy_state(hass, "sensor.e", 10.0)
    tracker.update(FROZEN_TIME, series)

    hass.states.async_set("sensor.joulo_ere", "110.0")
    hass.states.async_set("sensor.joulo_kwh", "1050.0")
    set_energy_state(hass, "sensor.e", 11.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)
    print("rate before reset:", tracker.snapshot(FROZEN_TIME + timedelta(minutes=1), series).ere_rate)

    hass.states.async_set("sensor.joulo_ere", "0.0")
    hass.states.async_set("sensor.joulo_kwh", "0.0")
    set_energy_state(hass, "sensor.e", 12.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=2), series)
    print("rate after reset (in-memory):", tracker.snapshot(FROZEN_TIME + timedelta(minutes=2), series).ere_rate)
    print("baseline still:", tracker.acc.joulo_base_ere, tracker.acc.joulo_base_kwh)

    hass.states.async_set("sensor.joulo_ere", "150.0")
    hass.states.async_set("sensor.joulo_kwh", "1100.0")
    set_energy_state(hass, "sensor.e", 13.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=3), series)
    print("rate after regrowth:", tracker.snapshot(FROZEN_TIME + timedelta(minutes=3), series).ere_rate)
