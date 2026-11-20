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
    CONF_EXPORT_DEDUCTION,
    CONF_EXPORT_DEDUCTION_CONFIRMED,
    CONF_EXPORT_ENTITY,
    CONF_EXPORT_PRICE_SOURCE,
    CONF_GRID_FEE_MONTHLY,
    CONF_IMPORT_ENTITY,
    CONF_KIND,
    CONF_METER_DEVICE,
    CONF_NAME,
    CONF_POWER_ENTITY,
    CONF_PRICE_INTERVAL,
    CONF_PROVIDER,
    CONF_REIMBURSEMENT_MODE,
    CONF_REIMBURSEMENT_RATE,
    CONF_REIMBURSEMENT_SURCHARGE,
    CONF_SALDERING,
    CONF_SUPPLY_COST_MONTHLY,
    DOMAIN,
    SUBENTRY_TYPE_DEVICE,
    SUBENTRY_TYPE_GRID,
)
from custom_components.kd_helios.prices import PriceApiError, UnknownProviderError
from tests.helpers import add_meter_device, add_meter_sensor

USER_INPUT = {CONF_PROVIDER: "zonneplan", CONF_PRICE_INTERVAL: "quarterly"}

EV_INPUT = {
    CONF_NAME: "EV charger",
    CONF_KIND: "ev_charger",
    CONF_ENERGY_ENTITY: "sensor.ev_energy",
    CONF_POWER_ENTITY: "sensor.ev_power",
}

PLAIN_INPUT = {
    CONF_NAME: "Heat pump",
    CONF_KIND: "device",
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
# Device subentry flow
# ---------------------------------------------------------------------------
@pytest.fixture(name="loaded_entry")
async def loaded_entry_fixture(hass) -> MockConfigEntry:
    """Return a set up config entry without any devices."""
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


async def test_add_ev_charger_walks_all_steps(hass, loaded_entry) -> None:
    """An EV charger collects reimbursement and ERE settings."""
    result = await _start_subentry_flow(hass, loaded_entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], EV_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reimbursement"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_REIMBURSEMENT_MODE: "dynamic",
            CONF_REIMBURSEMENT_RATE: 0.0,
            CONF_REIMBURSEMENT_SURCHARGE: 0.025,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "ere"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_ERE_MODE: "fixed",
            CONF_ERE_PER_KWH: 0.25,
            CONF_ERE_PRICE: 4.0,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    subentries = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE)
    assert len(subentries) == 1
    subentry = subentries[0]
    assert subentry.title == "EV charger"
    assert subentry.data[CONF_KIND] == "ev_charger"
    assert subentry.data[CONF_REIMBURSEMENT_SURCHARGE] == pytest.approx(0.025)
    assert subentry.data[CONF_ERE_PER_KWH] == pytest.approx(0.25)


async def test_plain_device_skips_reimbursement(hass, loaded_entry) -> None:
    """A normal appliance goes straight to the ERE step."""
    result = await _start_subentry_flow(hass, loaded_entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], PLAIN_INPUT
    )
    assert result["step_id"] == "ere"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_ERE_MODE: "none"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    subentry = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE)[0]
    assert subentry.data[CONF_KIND] == "device"
    assert subentry.data[CONF_NAME] == "Heat pump"


async def test_device_needs_a_source_entity(hass, loaded_entry) -> None:
    """A device without any source entity is rejected."""
    result = await _start_subentry_flow(hass, loaded_entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_NAME: "Ghost", CONF_KIND: "device"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_source"}


async def test_device_needs_a_name(hass, loaded_entry) -> None:
    """A blank name is rejected."""
    result = await _start_subentry_flow(hass, loaded_entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**PLAIN_INPUT, CONF_NAME: "   "}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_NAME: "name_required"}


async def test_fixed_reimbursement_needs_a_rate(hass, loaded_entry) -> None:
    """A fixed reimbursement without an amount is rejected."""
    result = await _start_subentry_flow(hass, loaded_entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], EV_INPUT
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_REIMBURSEMENT_MODE: "fixed", CONF_REIMBURSEMENT_RATE: 0.0},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reimbursement"
    assert result["errors"] == {CONF_REIMBURSEMENT_RATE: "rate_required"}


async def test_joulo_needs_both_counters(hass, loaded_entry) -> None:
    """Joulo mode requires the credits and the energy counter."""
    result = await _start_subentry_flow(hass, loaded_entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], EV_INPUT
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_REIMBURSEMENT_MODE: "none"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_ERE_MODE: "joulo"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "ere"
    assert result["errors"] == {
        CONF_ERE_CREDITS_ENTITY: "entity_required",
        CONF_ERE_KWH_ENTITY: "entity_required",
        # Without a price the credits would silently be worth nothing.
        CONF_ERE_PRICE: "ere_price_required",
    }


async def test_joulo_accepts_a_live_price_entity(hass, loaded_entry) -> None:
    """Joulo mode works with a live price sensor instead of a fixed price."""
    result = await _start_subentry_flow(hass, loaded_entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], EV_INPUT
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_REIMBURSEMENT_MODE: "none"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_ERE_MODE: "joulo",
            CONF_ERE_CREDITS_ENTITY: "sensor.joulo_total_ere_credits",
            CONF_ERE_KWH_ENTITY: "sensor.joulo_total_all_kwh",
            CONF_ERE_PRICE_ENTITY: "sensor.joulo_actuele_ere_marktprijs",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    subentry = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE)[0]
    assert subentry.data[CONF_ERE_KWH_ENTITY] == "sensor.joulo_total_all_kwh"


async def test_reconfigure_updates_a_device(hass, loaded_entry) -> None:
    """An existing device can be edited in a single form."""
    result = await _start_subentry_flow(hass, loaded_entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], PLAIN_INPUT
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_ERE_MODE: "none"}
    )
    subentry = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE)[0]

    result = await hass.config_entries.subentries.async_init(
        (loaded_entry.entry_id, SUBENTRY_TYPE_DEVICE),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry.subentry_id},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            **PLAIN_INPUT,
            CONF_NAME: "Heat pump (basement)",
            CONF_ERE_MODE: "none",
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    subentries = loaded_entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE)
    assert subentries[0].title == "Heat pump (basement)"


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
