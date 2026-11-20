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
    CONF_KIND,
    CONF_NAME,
    CONF_POWER_ENTITY,
    CONF_PRICE_INTERVAL,
    CONF_PROVIDER,
    CONF_REIMBURSEMENT_MODE,
    CONF_REIMBURSEMENT_RATE,
    CONF_REIMBURSEMENT_SURCHARGE,
    DOMAIN,
    SUBENTRY_TYPE_DEVICE,
)
from custom_components.kd_helios.prices import PriceApiError, UnknownProviderError

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
