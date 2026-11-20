"""Integration setup, reload and persistence tests."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from freezegun import freeze_time
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kd_helios.const import (
    CONF_ENERGY_ENTITY,
    CONF_ERE_MODE,
    CONF_KIND,
    CONF_NAME,
    CONF_PRICE_INTERVAL,
    CONF_PROVIDER,
    CONF_REIMBURSEMENT_MODE,
    CONF_REIMBURSEMENT_RATE,
    DOMAIN,
    SUBENTRY_TYPE_DEVICE,
)
from custom_components.kd_helios.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.kd_helios.prices import PriceApiError

CRC = "custom_components.kd_helios.coordinator.async_fetch_provider_prices"

USER_INPUT = {CONF_PROVIDER: "zonneplan", CONF_PRICE_INTERVAL: "quarterly"}

EV_DATA = {
    CONF_NAME: "EV charger",
    CONF_KIND: "ev_charger",
    CONF_ENERGY_ENTITY: "sensor.ev_energy",
    CONF_REIMBURSEMENT_MODE: "fixed",
    CONF_REIMBURSEMENT_RATE: 0.35,
    CONF_ERE_MODE: "none",
}

EV_SUBENTRY = {
    "subentry_type": SUBENTRY_TYPE_DEVICE,
    "data": EV_DATA,
    "title": "EV charger",
    "unique_id": None,
}

#: All-in Zonneplan price for 2026-11-20T12:00Z in the recorded fixture.
NOON_PRICE = 0.381683

HUB_KEYS = {
    "current_price",
    "next_price",
    "spot_price",
    "average_price_today",
    "energy_tax",
    "provider_charge",
}
EV_KEYS = {
    "energy",
    "cost",
    "cost_today",
    "current_price",
    "reimbursement",
    "reimbursement_rate",
    "net_cost",
}


@pytest.fixture(name="entry")
def entry_fixture(hass) -> MockConfigEntry:
    """Return an entry with one EV charger device."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="zonneplan_quarterly",
        title="Zonneplan",
        subentries_data=[EV_SUBENTRY],
    )
    entry.add_to_hass(hass)
    return entry


def _registry_entries(hass, entry: MockConfigEntry) -> list:
    """Return the entity registry entries for an entry."""
    return [
        item
        for item in er.async_get(hass).entities.values()
        if item.config_entry_id == entry.entry_id
    ]


async def test_setup_creates_hub_and_device_entities(hass, entry) -> None:
    """Setting up creates tariff sensors and one set of device sensors."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry_entries = _registry_entries(hass, entry)
    hub = {
        item.translation_key
        for item in registry_entries
        if item.config_subentry_id is None
    }
    assert hub == HUB_KEYS

    subentry_id = entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE)[0].subentry_id
    device = {
        item.translation_key
        for item in registry_entries
        if item.config_subentry_id == subentry_id
    }
    assert device == EV_KEYS

    for item in registry_entries:
        assert hass.states.get(item.entity_id) is not None, item.entity_id


async def test_price_sensor_matches_the_provider_tariff(hass, entry) -> None:
    """The tariff sensors expose the recorded all-in and spot prices."""
    with freeze_time("2026-11-20 12:00:00+00:00"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        current = hass.states.get("sensor.kd_helios_current_price")
        spot = hass.states.get("sensor.kd_helios_epex_spot_price")

    assert current is not None and spot is not None
    assert float(current.state) == pytest.approx(NOON_PRICE)
    assert float(spot.state) == pytest.approx(0.2073)


async def test_totals_survive_a_reload(hass, entry) -> None:
    """Accumulated cost is restored from storage after a reload."""
    with freeze_time("2026-11-20 12:00:00+00:00") as frozen:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        subentry_id = entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE)[0].subentry_id
        engine = entry.runtime_data.engine

        hass.states.async_set(
            "sensor.ev_energy", "10.0", {"unit_of_measurement": "kWh"}
        )
        await engine.async_refresh()  # baseline at 12:00

        hass.states.async_set(
            "sensor.ev_energy", "11.0", {"unit_of_measurement": "kWh"}
        )
        frozen.tick(timedelta(seconds=60))
        await engine.async_refresh()

        snapshot = engine.data[subentry_id]
        assert snapshot.energy_kwh == pytest.approx(1.0)
        assert snapshot.cost == pytest.approx(NOON_PRICE)
        assert snapshot.reimbursement == pytest.approx(0.35)
        assert snapshot.net_cost == pytest.approx(NOON_PRICE - 0.35)

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        restored = entry.runtime_data.engine.trackers[subentry_id]
        assert restored.acc.total_kwh == pytest.approx(1.0)
        assert restored.acc.total_cost == pytest.approx(NOON_PRICE)
        assert restored.acc.total_reimbursement == pytest.approx(0.35)


async def test_adding_a_device_reloads_the_entry(hass, entry) -> None:
    """A new subentry becomes a device with entities without a manual reload."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    before = len(_registry_entries(hass, entry))

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_DEVICE), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Heat pump",
            CONF_KIND: "device",
            CONF_ENERGY_ENTITY: "sensor.heat_pump_energy",
        },
    )
    assert result["step_id"] == "ere"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_ERE_MODE: "none"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    after = len(_registry_entries(hass, entry))
    assert after > before
    assert len(entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE)) == 2


async def test_setup_retries_when_the_price_api_is_down(hass) -> None:
    """No prices at all means a retry rather than a broken entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="zonneplan_quarterly",
        title="Zonneplan",
    )
    entry.add_to_hass(hass)

    with patch(CRC, new=AsyncMock(side_effect=PriceApiError("down"))):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_last_known_prices_are_kept_on_failure(hass, entry) -> None:
    """A later API failure keeps the previous price series in use."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    prices = entry.runtime_data.prices
    points = prices.series.points
    assert points > 0

    with patch(CRC, new=AsyncMock(side_effect=PriceApiError("down"))):
        await prices.async_refresh()
        await hass.async_block_till_done()

    assert prices.last_update_success
    assert prices.series.points == points


async def test_diagnostics_expose_devices_and_tariff(hass, entry) -> None:
    """Diagnostics contain the provider, the series and the accumulators."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["provider"]["id"] == "zonneplan"
    assert diagnostics["provider"]["price_interval"] == "quarterly"
    assert diagnostics["price_series"]["points"] > 0
    assert len(diagnostics["devices"]) == 1
    device = next(iter(diagnostics["devices"].values()))
    assert device["config"][CONF_NAME] == "EV charger"
    assert "accumulators" in device


async def test_current_price_flips_at_the_quarter_boundary(hass, entry) -> None:
    """The current price follows the tariff without waiting for a poll.

    The price API is polled every ten minutes, so the sensor must recompute
    from the cached series on the accounting tick instead.
    """
    with freeze_time("2026-11-20 12:14:30+00:00") as frozen:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        before = hass.states.get("sensor.kd_helios_current_price")
        assert before is not None
        assert float(before.state) == pytest.approx(0.381683)

        frozen.tick(timedelta(seconds=60))  # 12:15:30, new quarter
        await entry.runtime_data.engine.async_refresh()
        await hass.async_block_till_done()

        after = hass.states.get("sensor.kd_helios_current_price")

    assert after is not None
    assert float(after.state) == pytest.approx(0.388096)


async def test_setup_without_devices_only_creates_tariff_sensors(hass) -> None:
    """A fresh install tracks prices before any device is added."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="zonneplan_quarterly",
        title="Zonneplan",
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry_entries = _registry_entries(hass, entry)
    assert {item.translation_key for item in registry_entries} == HUB_KEYS
    assert entry.runtime_data.engine.trackers == {}
