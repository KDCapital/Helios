"""Tests for the config flow and the device subentry flows."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kd_helios.const import (
    CONF_ENERGY_ENTITY,
    CONF_ERE_CREDITS_ENTITY,
    CONF_ERE_KWH_ENTITY,
    CONF_ERE_MODE,
    CONF_ERE_PER_KWH,
    CONF_ERE_PRICE,
    CONF_ERE_PRICE_ENTITY,
    CONF_EV_DEPARTURE_WEEKDAY,
    CONF_EV_DEPARTURE_WEEKEND,
    CONF_EV_DEVICE,
    CONF_EV_PACK_KWH,
    CONF_EV_SOC_ENTITY,
    CONF_EV_TARGET_SOC,
    CONF_EXPORT_DEDUCTION,
    CONF_EXPORT_DEDUCTION_CONFIRMED,
    CONF_EXPORT_ENTITY,
    CONF_EXPORT_PRICE_SOURCE,
    CONF_FUSE_AMPS,
    CONF_FUSE_MARGIN_PCT,
    CONF_GRID_FEE_MONTHLY,
    CONF_GRID_POWER_ENTITY,
    CONF_IMPORT_ENTITY,
    CONF_KIND,
    CONF_METER_DEVICE,
    CONF_NAME,
    CONF_PHASES,
    CONF_POWER_ENTITY,
    CONF_PRICE_INTERVAL,
    CONF_PROVIDER,
    CONF_REIMBURSEMENT_INTERVAL,
    CONF_REIMBURSEMENT_MODE,
    CONF_REIMBURSEMENT_RATE,
    CONF_REIMBURSEMENT_SURCHARGE,
    CONF_SALDERING,
    CONF_SUPPLY_COST_MONTHLY,
    DOMAIN,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_DEVICE,
    SUBENTRY_TYPE_EV_CHARGER,
    SUBENTRY_TYPE_GRID,
)
from custom_components.kd_helios.prices import PriceApiError, UnknownProviderError
from tests.helpers import add_meter_device, add_meter_sensor

USER_INPUT = {CONF_PROVIDER: "zonneplan", CONF_PRICE_INTERVAL: "quarterly"}

EV_INPUT = {
    CONF_NAME: "EV charger",
    CONF_ENERGY_ENTITY: "sensor.ev_energy",
    CONF_POWER_ENTITY: "sensor.ev_power",
}

PLAIN_INPUT = {
    CONF_NAME: "Heat pump",
    CONF_POWER_ENTITY: "sensor.heat_pump_power",
}


async def _start_user_flow(hass) -> dict:
    """Start the main config flow."""
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


async def _start_subentry_flow(hass, entry: MockConfigEntry) -> dict:
    """Start a device subentry flow."""
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_DEVICE), context={"source": SOURCE_USER}
    )


# ---------------------------------------------------------------------------
# Main config flow
# ---------------------------------------------------------------------------
async def test_user_flow_shows_the_provider_form(hass) -> None:
    """The first step asks for a provider and a price interval."""
    result = await _start_user_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_user_flow_creates_the_entry(hass) -> None:
    """A valid provider is stored with a provider based title."""
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Zonneplan"
    assert result["data"] == USER_INPUT
    assert result["result"].unique_id == "zonneplan_quarterly"


async def test_duplicate_provider_is_rejected(hass) -> None:
    """The same provider and interval cannot be added twice."""
    MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="zonneplan_quarterly",
        title="Zonneplan",
    ).add_to_hass(hass)

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_unknown_provider_shows_an_error(hass) -> None:
    """A provider without published prices is reported on the form."""
    with patch(
        "custom_components.kd_helios.config_flow.async_fetch_provider_prices",
        new=AsyncMock(side_effect=UnknownProviderError("nope")),
    ):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PROVIDER: "tibber", CONF_PRICE_INTERVAL: "hourly"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown_provider"}


async def test_connection_failure_shows_an_error(hass) -> None:
    """A network failure is reported on the form."""
    with patch(
        "custom_components.kd_helios.config_flow.async_fetch_provider_prices",
        new=AsyncMock(side_effect=PriceApiError("boom")),
    ):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_changes_the_interval(hass) -> None:
    """The provider can be reconfigured after setup."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="zonneplan_quarterly",
        title="Zonneplan",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PROVIDER: "tibber", CONF_PRICE_INTERVAL: "hourly"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PROVIDER] == "tibber"
    assert entry.unique_id == "tibber_hourly"


# ---------------------------------------------------------------------------
# Device and EV charger subentry flows
# ---------------------------------------------------------------------------
@pytest.fixture(name="loaded_entry")
async def loaded_entry_fixture(hass) -> MockConfigEntry:
    """Return a set up config entry without any subentries."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="zonneplan_quarterly",
        title="Zonneplan",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _start_flow(hass, entry: MockConfigEntry, subentry_type: str) -> dict:
    """Start a subentry flow of a given type."""
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, subentry_type), context={"source": SOURCE_USER}
    )


async def _add_ev_charger(
    hass,
    entry: MockConfigEntry,
    reimbursement: dict,
    ere: dict,
    vehicle: dict | None = None,
    basic: dict | None = None,
) -> dict:
    """Walk the EV charger flow to the end."""
    result = await _start_flow(hass, entry, SUBENTRY_TYPE_EV_CHARGER)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], basic or EV_INPUT
    )
    assert result["step_id"] == "vehicle"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], vehicle or {}
    )
    assert result["step_id"] == "reimbursement"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], reimbursement
    )
    assert result["step_id"] == "ere"
    return await hass.config_entries.subentries.async_configure(result["flow_id"], ere)


async def test_plain_device_is_added_in_one_step(hass, loaded_entry) -> None:
    """A normal device is never asked about reimbursement or ERE."""
    result = await _start_flow(hass, loaded_entry, SUBENTRY_TYPE_DEVICE)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    # No kind question any more: the entry point decides it.
    assert CONF_KIND not in result["data_schema"].schema

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], PLAIN_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    subentry = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE)[0]
    assert subentry.data[CONF_KIND] == "device"
    assert subentry.data[CONF_NAME] == "Heat pump"
    assert CONF_ERE_MODE not in subentry.data
    assert CONF_REIMBURSEMENT_MODE not in subentry.data


async def test_ev_charger_walks_all_four_steps(hass, loaded_entry) -> None:
    """Metering comes first, then the optional car, reimbursement and ERE."""
    result = await _add_ev_charger(
        hass,
        loaded_entry,
        {
            CONF_REIMBURSEMENT_MODE: "dynamic",
            CONF_REIMBURSEMENT_SURCHARGE: 0.025,
            CONF_REIMBURSEMENT_INTERVAL: "hourly",
        },
        {CONF_ERE_MODE: "fixed", CONF_ERE_PER_KWH: 0.25, CONF_ERE_PRICE: 4.0},
        vehicle={
            CONF_EV_PACK_KWH: 77,
            CONF_EV_TARGET_SOC: 80,
            CONF_EV_DEPARTURE_WEEKDAY: "07:00",
            CONF_EV_DEPARTURE_WEEKEND: "09:30",
            CONF_EV_SOC_ENTITY: "sensor.car_soc",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    subentries = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_EV_CHARGER)
    assert len(subentries) == 1
    subentry = subentries[0]
    assert subentry.title == "EV charger"
    assert subentry.data[CONF_KIND] == "ev_charger"
    assert subentry.data[CONF_REIMBURSEMENT_SURCHARGE] == pytest.approx(0.025)
    assert subentry.data[CONF_ERE_PER_KWH] == pytest.approx(0.25)
    assert subentry.data[CONF_EV_PACK_KWH] == pytest.approx(77)
    assert subentry.data[CONF_EV_TARGET_SOC] == 80
    # And nothing of it leaks into the plain device list.
    assert loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE) == []


async def test_the_car_page_is_optional(hass, loaded_entry) -> None:
    """Cost tracking alone never needs the car or its sensors."""
    result = await _add_ev_charger(
        hass,
        loaded_entry,
        {CONF_REIMBURSEMENT_MODE: "none"},
        {CONF_ERE_MODE: "none"},
        vehicle={},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    subentry = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_EV_CHARGER)[0]
    assert CONF_EV_PACK_KWH not in subentry.data
    assert CONF_EV_DEVICE not in subentry.data
    assert CONF_REIMBURSEMENT_MODE in subentry.data


async def test_two_ev_chargers_are_tracked_separately(hass, loaded_entry) -> None:
    """A household with two cars adds one charger per car."""
    first = await _add_ev_charger(
        hass,
        loaded_entry,
        {CONF_REIMBURSEMENT_MODE: "none"},
        {CONF_ERE_MODE: "none"},
        vehicle={CONF_EV_PACK_KWH: 77},
    )
    second = await _add_ev_charger(
        hass,
        loaded_entry,
        {CONF_REIMBURSEMENT_MODE: "fixed", CONF_REIMBURSEMENT_RATE: 0.35},
        {CONF_ERE_MODE: "none"},
        vehicle={CONF_EV_PACK_KWH: 52},
        basic={
            CONF_NAME: "Laadpaal tweede auto",
            CONF_ENERGY_ENTITY: "sensor.ev2_energy",
        },
    )
    assert first["type"] is FlowResultType.CREATE_ENTRY
    assert second["type"] is FlowResultType.CREATE_ENTRY

    subentries = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_EV_CHARGER)
    assert len(subentries) == 2
    first_subentry, second_subentry = subentries
    assert first_subentry.data[CONF_NAME] == "EV charger"
    assert second_subentry.data[CONF_NAME] == "Laadpaal tweede auto"
    assert first_subentry.data[CONF_ENERGY_ENTITY] == "sensor.ev_energy"
    assert second_subentry.data[CONF_ENERGY_ENTITY] == "sensor.ev2_energy"
    assert first_subentry.data[CONF_REIMBURSEMENT_MODE] == "none"
    assert second_subentry.data[CONF_REIMBURSEMENT_RATE] == pytest.approx(0.35)
    assert first_subentry.data[CONF_EV_PACK_KWH] == pytest.approx(77)
    assert second_subentry.data[CONF_EV_PACK_KWH] == pytest.approx(52)


async def test_device_needs_a_source_entity(hass, loaded_entry) -> None:
    """A device without any source entity is rejected."""
    result = await _start_flow(hass, loaded_entry, SUBENTRY_TYPE_DEVICE)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_NAME: "Ghost"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_source"}


async def test_device_needs_a_name(hass, loaded_entry) -> None:
    """A blank name is rejected."""
    result = await _start_flow(hass, loaded_entry, SUBENTRY_TYPE_DEVICE)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**PLAIN_INPUT, CONF_NAME: "   "}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_NAME: "name_required"}


async def test_fixed_reimbursement_needs_a_rate(hass, loaded_entry) -> None:
    """A fixed reimbursement without an amount is rejected."""
    result = await _start_flow(hass, loaded_entry, SUBENTRY_TYPE_EV_CHARGER)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], EV_INPUT
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_REIMBURSEMENT_MODE: "fixed", CONF_REIMBURSEMENT_RATE: 0.0},
    )

    assert result["step_id"] == "reimbursement"
    assert result["errors"] == {CONF_REIMBURSEMENT_RATE: "rate_required"}


async def test_joulo_needs_both_counters(hass, loaded_entry) -> None:
    """Joulo mode requires the credits and the energy counter."""
    result = await _add_ev_charger(
        hass,
        loaded_entry,
        {CONF_REIMBURSEMENT_MODE: "none"},
        {CONF_ERE_MODE: "joulo"},
    )

    assert result["step_id"] == "ere"
    assert result["errors"] == {
        CONF_ERE_CREDITS_ENTITY: "entity_required",
        CONF_ERE_KWH_ENTITY: "entity_required",
        CONF_ERE_PRICE: "ere_price_required",
    }


async def test_joulo_accepts_a_live_price_entity(hass, loaded_entry) -> None:
    """Joulo mode works with a live price sensor instead of a fixed price."""
    result = await _add_ev_charger(
        hass,
        loaded_entry,
        {CONF_REIMBURSEMENT_MODE: "none"},
        {
            CONF_ERE_MODE: "joulo",
            CONF_ERE_CREDITS_ENTITY: "sensor.joulo_total_ere_credits",
            CONF_ERE_KWH_ENTITY: "sensor.joulo_total_all_kwh",
            CONF_ERE_PRICE_ENTITY: "sensor.joulo_actuele_ere_marktprijs",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    subentry = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_EV_CHARGER)[0]
    assert subentry.data[CONF_ERE_KWH_ENTITY] == "sensor.joulo_total_all_kwh"


async def test_ev_reimbursement_interval_is_stored(hass, loaded_entry) -> None:
    """An EV can reimburse hourly while the contract settles per quarter."""
    result = await _add_ev_charger(
        hass,
        loaded_entry,
        {
            CONF_REIMBURSEMENT_MODE: "dynamic",
            CONF_REIMBURSEMENT_INTERVAL: "hourly",
            CONF_REIMBURSEMENT_SURCHARGE: 0.02,
        },
        {CONF_ERE_MODE: "none"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    subentry = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_EV_CHARGER)[0]
    assert subentry.data[CONF_REIMBURSEMENT_INTERVAL] == "hourly"


async def test_ev_reimbursement_interval_defaults_to_quarterly(
    hass, loaded_entry
) -> None:
    """The default matches the quarter hour contract."""
    result = await _add_ev_charger(
        hass,
        loaded_entry,
        {CONF_REIMBURSEMENT_MODE: "fixed", CONF_REIMBURSEMENT_RATE: 0.35},
        {CONF_ERE_MODE: "none"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    subentry = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_EV_CHARGER)[0]
    assert subentry.data[CONF_REIMBURSEMENT_INTERVAL] == "quarterly"


async def test_reconfigure_updates_a_device(hass, loaded_entry) -> None:
    """A plain device keeps a single, short form."""
    result = await _start_flow(hass, loaded_entry, SUBENTRY_TYPE_DEVICE)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], PLAIN_INPUT
    )
    subentry = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE)[0]

    result = await hass.config_entries.subentries.async_init(
        (loaded_entry.entry_id, SUBENTRY_TYPE_DEVICE),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry.subentry_id},
    )
    assert result["step_id"] == "reconfigure"
    assert CONF_ERE_MODE not in result["data_schema"].schema

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**PLAIN_INPUT, CONF_NAME: "Heat pump (basement)"}
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()

    subentries = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE)
    assert subentries[0].title == "Heat pump (basement)"


async def test_the_evcc_device_is_stored_on_the_car_page(hass, loaded_entry) -> None:
    """Picking the EVCC loadpoint is what derives the car's sensors at runtime."""
    result = await _add_ev_charger(
        hass,
        loaded_entry,
        {CONF_REIMBURSEMENT_MODE: "none"},
        {CONF_ERE_MODE: "none"},
        vehicle={CONF_EV_DEVICE: "evcc-loadpoint-1", CONF_EV_PACK_KWH: 77},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    subentry = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_EV_CHARGER)[0]
    assert subentry.data[CONF_EV_DEVICE] == "evcc-loadpoint-1"


async def test_reconfigure_exposes_the_car_plan(hass, loaded_entry) -> None:
    """Editing a charger must not hide the fields the new step collected."""
    await _add_ev_charger(
        hass,
        loaded_entry,
        {CONF_REIMBURSEMENT_MODE: "none"},
        {CONF_ERE_MODE: "none"},
        vehicle={CONF_EV_PACK_KWH: 77, CONF_EV_SOC_ENTITY: "sensor.car_soc"},
    )
    subentry = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_EV_CHARGER)[0]

    result = await hass.config_entries.subentries.async_init(
        (loaded_entry.entry_id, SUBENTRY_TYPE_EV_CHARGER),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry.subentry_id},
    )
    assert result["step_id"] == "reconfigure"
    schema = result["data_schema"].schema
    for key in (
        CONF_EV_DEVICE,
        CONF_EV_PACK_KWH,
        CONF_EV_TARGET_SOC,
        CONF_EV_SOC_ENTITY,
    ):
        assert key in schema

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            **EV_INPUT,
            CONF_EV_PACK_KWH: 82,
            CONF_EV_TARGET_SOC: 90,
            CONF_EV_DEPARTURE_WEEKDAY: "06:30",
            CONF_EV_DEPARTURE_WEEKEND: "09:00",
            CONF_REIMBURSEMENT_MODE: "none",
            CONF_ERE_MODE: "none",
        },
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()

    updated = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_EV_CHARGER)[0]
    assert updated.data[CONF_EV_PACK_KWH] == pytest.approx(82)
    assert updated.data[CONF_EV_TARGET_SOC] == 90


async def test_legacy_ev_device_keeps_its_reimbursement(hass, loaded_entry) -> None:
    """An EV charger stored as a plain device before the split is not wiped.

    Reconfiguring it through the short device form must preserve the keys that
    form does not show.
    """
    legacy = {
        "subentry_type": SUBENTRY_TYPE_DEVICE,
        "data": {
            CONF_NAME: "Old charger",
            CONF_KIND: "ev_charger",
            CONF_ENERGY_ENTITY: "sensor.old_energy",
            CONF_REIMBURSEMENT_MODE: "fixed",
            CONF_REIMBURSEMENT_RATE: 0.35,
            CONF_ERE_MODE: "fixed",
            CONF_ERE_PER_KWH: 0.2,
            CONF_ERE_PRICE: 4.0,
        },
        "title": "Old charger",
        "unique_id": None,
    }
    hass.config_entries.async_add_subentry(
        loaded_entry,
        __import__(
            "homeassistant.config_entries", fromlist=["ConfigSubentry"]
        ).ConfigSubentry(
            data=dict(legacy["data"]),
            subentry_type=legacy["subentry_type"],
            title=legacy["title"],
            unique_id=None,
        ),
    )
    await hass.async_block_till_done()
    subentry = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE)[0]

    result = await hass.config_entries.subentries.async_init(
        (loaded_entry.entry_id, SUBENTRY_TYPE_DEVICE),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry.subentry_id},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Old charger renamed",
            CONF_ENERGY_ENTITY: "sensor.old_energy",
        },
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()

    updated = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE)[0]
    assert updated.data[CONF_REIMBURSEMENT_RATE] == pytest.approx(0.35)
    assert updated.data[CONF_ERE_PRICE] == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# Grid connection subentry flow
# ---------------------------------------------------------------------------
GRID_ENTITIES = {
    CONF_IMPORT_ENTITY: "sensor.p1_import",
    CONF_EXPORT_ENTITY: "sensor.p1_export",
}

GRID_TARIFF = {
    CONF_EXPORT_DEDUCTION: -0.02,
    CONF_EXPORT_DEDUCTION_CONFIRMED: True,
    CONF_SUPPLY_COST_MONTHLY: 6.0,
    CONF_GRID_FEE_MONTHLY: 20.0,
}


def _suggested(schema, key: str):
    """Return the suggested value the form carries for a field."""
    for marker in schema.schema:
        if getattr(marker, "schema", None) == key:
            description = getattr(marker, "description", None) or {}
            return description.get("suggested_value")
    return None


async def _start_grid_flow(hass, entry: MockConfigEntry) -> dict:
    """Start a grid connection subentry flow."""
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_GRID), context={"source": SOURCE_USER}
    )


async def _add_grid(
    hass,
    entry: MockConfigEntry,
    *,
    meter: str | None = None,
    entities: dict | None = None,
    tariff: dict | None = None,
) -> dict:
    """Walk the three grid steps and return the final result."""
    result = await _start_grid_flow(hass, entry)
    meter_input = {CONF_SALDERING: True}
    if meter is not None:
        meter_input[CONF_METER_DEVICE] = meter
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], meter_input
    )
    assert result["step_id"] == "entities"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], entities if entities is not None else GRID_ENTITIES
    )
    assert result["step_id"] == "tariff"
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"], tariff if tariff is not None else GRID_TARIFF
    )


async def test_add_grid_connection_walks_all_steps(hass, loaded_entry) -> None:
    """The meter is chosen, the counters reviewed, then the tariff confirmed."""
    result = await _start_grid_flow(hass, loaded_entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    # Saldering is still in force today, so it defaults on.
    validated = result["data_schema"]({})
    assert validated[CONF_SALDERING] is True

    result = await _add_grid(hass, loaded_entry)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentry = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_GRID)[0]
    assert subentry.title == "Grid connection"
    assert subentry.data[CONF_IMPORT_ENTITY] == "sensor.p1_import"
    assert subentry.data[CONF_SUPPLY_COST_MONTHLY] == pytest.approx(6.0)
    # Zonneplan's published deduction is -0.02, so leaving it alone is recorded.
    assert subentry.data[CONF_EXPORT_PRICE_SOURCE] == "epexprijzen"


async def test_homewizard_p1_meter_prefills_the_counters(hass, loaded_entry) -> None:
    """Picking a P1 meter fills in its counters for the user to confirm."""
    device = add_meter_device(hass)
    add_meter_sensor(
        hass,
        device,
        "p1_meter_energy_import",
        translation_key="total_energy_import_kwh",
    )
    add_meter_sensor(
        hass,
        device,
        "p1_meter_energy_export",
        translation_key="total_energy_export_kwh",
    )
    add_meter_sensor(
        hass,
        device,
        "p1_meter_active_power",
        device_class="power",
        unit="W",
    )

    result = await _start_grid_flow(hass, loaded_entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SALDERING: True, CONF_METER_DEVICE: device.id}
    )

    assert result["step_id"] == "entities"
    assert _suggested(result["data_schema"], CONF_IMPORT_ENTITY) == (
        "sensor.p1_meter_energy_import"
    )
    assert _suggested(result["data_schema"], CONF_EXPORT_ENTITY) == (
        "sensor.p1_meter_energy_export"
    )
    # The P1 meter's signed power sensor drives both live power sensors.
    assert _suggested(result["data_schema"], CONF_GRID_POWER_ENTITY) == (
        "sensor.p1_meter_active_power"
    )
    # The user is told what was recognised on the meter.
    assert result["description_placeholders"]["meter"] == "P1 meter"
    assert (
        "sensor.p1_meter_energy_import"
        in (result["description_placeholders"]["detected"])
    )

    # Confirming the detected counters stores them.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_IMPORT_ENTITY: "sensor.p1_meter_energy_import",
            CONF_EXPORT_ENTITY: "sensor.p1_meter_energy_export",
        },
    )
    assert result["step_id"] == "tariff"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], GRID_TARIFF
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    subentry = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_GRID)[0]
    assert subentry.data[CONF_METER_DEVICE] == device.id
    assert subentry.data[CONF_IMPORT_ENTITY] == "sensor.p1_meter_energy_import"


async def test_grid_deduction_override_is_recorded(hass, loaded_entry) -> None:
    """Correcting the suggested deduction is recorded as a manual value."""
    result = await _add_grid(
        hass,
        loaded_entry,
        tariff={**GRID_TARIFF, CONF_EXPORT_DEDUCTION: 0.0115},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    subentry = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_GRID)[0]
    assert subentry.data[CONF_EXPORT_DEDUCTION] == pytest.approx(0.0115)
    assert subentry.data[CONF_EXPORT_PRICE_SOURCE] == "manual"


async def test_grid_requires_an_import_entity(hass, loaded_entry) -> None:
    """The connection cannot be metered without an import counter."""
    result = await _start_grid_flow(hass, loaded_entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SALDERING: True}
    )
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_IMPORT_ENTITY: "import_required"}


async def test_grid_requires_confirmation_of_the_deduction(hass, loaded_entry) -> None:
    """The user has to confirm the feed-in deduction before it is used."""
    result = await _start_grid_flow(hass, loaded_entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SALDERING: True}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], GRID_ENTITIES
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {**GRID_TARIFF, CONF_EXPORT_DEDUCTION_CONFIRMED: False},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_EXPORT_DEDUCTION_CONFIRMED: "confirm_required"}


async def test_grid_requires_both_fixed_costs(hass, loaded_entry) -> None:
    """Users must state their monthly fixed costs, even when they are zero."""
    result = await _start_grid_flow(hass, loaded_entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SALDERING: True}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], GRID_ENTITIES
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_EXPORT_DEDUCTION: -0.02, CONF_EXPORT_DEDUCTION_CONFIRMED: True},
    )

    assert result["type"] is FlowResultType.FORM
    assert set(result["errors"]) == {
        CONF_SUPPLY_COST_MONTHLY,
        CONF_GRID_FEE_MONTHLY,
    }


async def test_only_one_grid_connection(hass, loaded_entry) -> None:
    """A house has one connection, so a second one is refused."""
    assert (await _add_grid(hass, loaded_entry))["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    result = await _start_grid_flow(hass, loaded_entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance"


async def test_grid_reconfigure_walks_three_steps(hass, loaded_entry) -> None:
    """Editing the connection keeps the same three-step shape."""
    assert (await _add_grid(hass, loaded_entry))["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    subentry = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_GRID)[0]

    result = await hass.config_entries.subentries.async_init(
        (loaded_entry.entry_id, SUBENTRY_TYPE_GRID),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry.subentry_id},
    )
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SALDERING: False}
    )
    assert result["step_id"] == "reconfigure_entities"
    # The stored counters are suggested back, not re-detected.
    assert _suggested(result["data_schema"], CONF_IMPORT_ENTITY) == ("sensor.p1_import")

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], GRID_ENTITIES
    )
    assert result["step_id"] == "reconfigure_tariff"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**GRID_TARIFF, CONF_SUPPLY_COST_MONTHLY: 7.5}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    updated = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_GRID)[0]
    assert updated.data[CONF_SALDERING] is False
    assert updated.data[CONF_SUPPLY_COST_MONTHLY] == pytest.approx(7.5)


async def test_the_battery_flow_asks_for_the_connection(hass, loaded_entry) -> None:
    """The fuse rating is asked for, so the import cap can be derived."""
    result = await _start_flow(hass, loaded_entry, SUBENTRY_TYPE_BATTERY)
    keys = {str(key) for key in result["data_schema"].schema}
    assert CONF_PHASES in keys
    assert CONF_FUSE_AMPS in keys
    assert CONF_FUSE_MARGIN_PCT in keys


async def test_the_ev_flow_asks_for_the_cars_schedule(hass, loaded_entry) -> None:
    """Metering is its own page; the car's schedule comes after it."""
    result = await _start_flow(hass, loaded_entry, SUBENTRY_TYPE_EV_CHARGER)
    keys = {str(key) for key in result["data_schema"].schema}
    assert CONF_POWER_ENTITY in keys
    assert CONF_ENERGY_ENTITY in keys
    assert CONF_EV_PACK_KWH not in keys

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], EV_INPUT
    )
    assert result["step_id"] == "vehicle"
    vehicle_keys = {str(key) for key in result["data_schema"].schema}
    for key in (
        CONF_EV_DEVICE,
        CONF_EV_PACK_KWH,
        CONF_EV_TARGET_SOC,
        CONF_EV_DEPARTURE_WEEKDAY,
        CONF_EV_DEPARTURE_WEEKEND,
        CONF_EV_SOC_ENTITY,
    ):
        assert key in vehicle_keys

    device = await _start_flow(hass, loaded_entry, SUBENTRY_TYPE_DEVICE)
    assert CONF_EV_PACK_KWH not in {str(key) for key in device["data_schema"].schema}
