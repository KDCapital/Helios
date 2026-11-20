"""Integration setup, reload and persistence tests."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import orjson
import pytest
from freezegun import freeze_time
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntryState,
)
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.kd_helios.const import (
    CONF_ENERGY_ENTITY,
    CONF_ERE_MODE,
    CONF_EXPORT_DEDUCTION,
    CONF_EXPORT_ENTITY,
    CONF_GRID_FEE_MONTHLY,
    CONF_IMPORT_ENTITY,
    CONF_KIND,
    CONF_NAME,
    CONF_PRICE_INTERVAL,
    CONF_PROVIDER,
    CONF_REIMBURSEMENT_MODE,
    CONF_REIMBURSEMENT_RATE,
    CONF_SALDERING,
    CONF_SUPPLY_COST_MONTHLY,
    DOMAIN,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_DEVICE,
    SUBENTRY_TYPE_GRID,
)
from custom_components.kd_helios.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.kd_helios.prices import PriceApiError
from tests.helpers import load_fixture

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

        snapshot = engine.data.devices[subentry_id]
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


async def test_cost_today_declares_its_daily_reset_point(hass, entry) -> None:
    """The daily cost sensor tells statistics where its cycle restarts."""
    with freeze_time("2026-11-20 12:00:00+00:00"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        state = hass.states.get("sensor.ev_charger_cost_today")

    assert state is not None
    assert "last_reset" in state.attributes
    assert state.attributes["last_reset"].startswith("2026-11-20T00:00:00")


async def test_removing_a_device_drops_its_entities(hass, entry) -> None:
    """Deleting a device removes its entities and its tracker."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    subentry_id = entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE)[0].subentry_id
    assert hass.config_entries.async_remove_subentry(entry, subentry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    remaining = _registry_entries(hass, entry)
    assert {item.translation_key for item in remaining} == HUB_KEYS
    assert entry.runtime_data.engine.trackers == {}


async def test_prices_keep_polling_while_running(hass, entry) -> None:
    """The tariff must be re-fetched periodically, not just once at setup.

    A DataUpdateCoordinator only re-arms its interval while it has a listener,
    so without the engine subscribing the prices would freeze after setup.
    """
    fetch = AsyncMock(return_value=load_fixture("provider_zonneplan_quarterly.json"))
    with freeze_time("2026-11-20 12:00:00+00:00") as frozen, patch(CRC, new=fetch):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert fetch.call_count == 1

        frozen.tick(timedelta(minutes=11))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    assert fetch.call_count >= 2


async def test_disabling_reimbursement_removes_its_entities(hass, entry) -> None:
    """Turning reimbursement off must not leave dead entities behind."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    before = {item.translation_key for item in _registry_entries(hass, entry)}
    assert {"reimbursement", "reimbursement_rate", "net_cost"} <= before

    subentry_id = entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE)[0].subentry_id
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_DEVICE),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id},
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "EV charger",
            CONF_KIND: "ev_charger",
            CONF_ENERGY_ENTITY: "sensor.ev_energy",
            CONF_REIMBURSEMENT_MODE: "none",
            CONF_ERE_MODE: "none",
        },
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()

    after = {item.translation_key for item in _registry_entries(hass, entry)}
    assert "reimbursement" not in after
    assert "reimbursement_rate" not in after
    assert "net_cost" not in after
    assert "energy" in after and "cost" in after


async def test_subentry_type_is_exposed_to_the_frontend(hass, entry) -> None:
    """The UI needs this payload field to render the "Add device" button."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    expected = {
        SUBENTRY_TYPE_DEVICE: {"supports_reconfigure": True},
        SUBENTRY_TYPE_GRID: {"supports_reconfigure": True},
        SUBENTRY_TYPE_BATTERY: {"supports_reconfigure": True},
    }
    assert entry.supported_subentry_types == expected
    # The same value is serialised into the WebSocket payload the UI reads.
    payload = orjson.loads(orjson.dumps(entry.as_json_fragment))
    assert payload["supported_subentry_types"] == expected


async def test_repair_hint_points_at_adding_a_device(hass) -> None:
    """A fresh install with no devices shows a repair, and clears it later."""
    from homeassistant.helpers import issue_registry as ir

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="zonneplan_quarterly",
        title="Zonneplan",
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    issues = ir.async_get(hass)
    issue_id = f"no_devices_{entry.entry_id}"
    issue = issues.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "no_devices"
    assert issue.is_fixable is False

    # Adding a device clears the hint.
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
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_ERE_MODE: "none"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert issues.async_get_issue(DOMAIN, issue_id) is None


GRID_DATA = {
    CONF_IMPORT_ENTITY: "sensor.p1_import",
    CONF_EXPORT_ENTITY: "sensor.p1_export",
    CONF_SALDERING: True,
    CONF_EXPORT_DEDUCTION: 0.01361,
    CONF_SUPPLY_COST_MONTHLY: 6.0,
    CONF_GRID_FEE_MONTHLY: 20.0,
}

GRID_SUBENTRY = {
    "subentry_type": SUBENTRY_TYPE_GRID,
    "data": GRID_DATA,
    "title": "Grid connection",
    "unique_id": None,
}

GRID_KEYS = {
    "grid_import_energy",
    "grid_export_energy",
    "grid_import_cost",
    "grid_export_credit",
    "grid_fixed_cost",
    "grid_cost",
    "grid_export_price",
}


async def test_grid_connection_creates_its_entities(hass) -> None:
    """A grid subentry produces the tariff sensors and the saldering switch."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="zonneplan_quarterly",
        title="Zonneplan",
        subentries_data=[GRID_SUBENTRY],
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry_entries = _registry_entries(hass, entry)
    grid_sensors = {
        item.translation_key
        for item in registry_entries
        if item.entity_id.startswith("sensor.") and item.config_subentry_id is not None
    }
    assert grid_sensors == GRID_KEYS

    binary = [
        item for item in registry_entries if item.entity_id.startswith("binary_sensor.")
    ]
    assert len(binary) == 1
    assert binary[0].translation_key == "saldering_active"
    state = hass.states.get(binary[0].entity_id)
    assert state is not None
    assert state.state == "on"
    assert state.attributes["saldering_ends_on"] == "2027-01-01"

    cost = next(
        item for item in registry_entries if item.translation_key == "grid_cost"
    )
    cost_state = hass.states.get(cost.entity_id)
    assert cost_state is not None
    assert "last_reset" in cost_state.attributes
    year = cost_state.attributes["billing_year"]
    assert cost_state.attributes["last_reset"].startswith(f"{year}-01-01T00:00:00")
    assert cost_state.attributes["saldering_active"] is True
    assert cost_state.attributes["fixed_monthly"] == pytest.approx(26.0)

    assert entry.runtime_data.engine.grid_tracker is not None


async def test_grid_only_install_has_no_repair_hint(hass) -> None:
    """A connection that meters the grid is tracking something."""
    from homeassistant.helpers import issue_registry as ir

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="zonneplan_quarterly",
        title="Zonneplan",
        subentries_data=[GRID_SUBENTRY],
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"no_devices_{entry.entry_id}")
        is None
    )
