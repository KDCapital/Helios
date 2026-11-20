"""Integration tests for the battery planner: setup, sensors and the action."""

from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun import freeze_time
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kd_helios.battery import TOU_REGISTERS, ChargeSource
from custom_components.kd_helios.const import (
    CONF_ALLOW_INVERTER_WRITES,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_HORIZON_HOURS,
    CONF_EFFICIENCY,
    CONF_INVERTER_DEVICE,
    CONF_MAX_CHARGE_KW,
    CONF_PRICE_INTERVAL,
    CONF_PROVIDER,
    CONF_RESERVE_SOC,
    CONF_SOC_ENTITY,
    CONF_TARGET_SOC,
    DOMAIN,
    SERVICE_APPLY_TOU,
    SUBENTRY_TYPE_BATTERY,
)

USER_INPUT = {CONF_PROVIDER: "zonneplan", CONF_PRICE_INTERVAL: "quarterly"}
FROZEN = "2026-11-20 12:00:00+00:00"
SOC_ENTITY = "sensor.battery_soc"

BATTERY_DATA = {
    CONF_SOC_ENTITY: SOC_ENTITY,
    CONF_BATTERY_CAPACITY_KWH: 10.0,
    CONF_MAX_CHARGE_KW: 3.0,
    CONF_RESERVE_SOC: 10.0,
    CONF_TARGET_SOC: 100.0,
    CONF_EFFICIENCY: 90.0,
    CONF_BATTERY_HORIZON_HOURS: 24,
}


def battery_subentry(**overrides) -> dict:
    """Return a battery subentry payload."""
    return {
        "subentry_type": SUBENTRY_TYPE_BATTERY,
        "data": {**BATTERY_DATA, **overrides},
        "title": "Home battery",
        "unique_id": None,
    }


def make_entry(**overrides) -> MockConfigEntry:
    """Return an entry with one battery subentry."""
    return MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="zonneplan_quarterly",
        title="Zonneplan",
        subentries_data=[battery_subentry(**overrides)],
    )


async def test_battery_setup_creates_the_plan_sensors(hass: HomeAssistant) -> None:
    """A battery subentry produces the plan sensors and a computed plan."""
    entry = make_entry()
    entry.add_to_hass(hass)
    hass.states.async_set(SOC_ENTITY, "25", {"unit_of_measurement": "%"})

    with freeze_time(FROZEN):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        keys = {
            item.translation_key
            for item in registry.entities.values()
            if item.config_entry_id == entry.entry_id
            and item.config_subentry_id is not None
        }
        assert {
            "battery_charge_energy",
            "battery_plan_saving",
            "battery_next_charge",
        } <= keys

        engine = entry.runtime_data.engine
        assert engine.battery_config is not None
        assert engine.battery_plan is not None
        assert engine.battery_plan.soc_pct == pytest.approx(25.0)
        assert engine.tou_plan is not None
        assert len(engine.tou_plan.programs) == 6

        # The sensors reflect the plan.
        for item in registry.entities.values():
            if item.config_subentry_id is None:
                continue
            state = hass.states.get(item.entity_id)
            assert state is not None, item.entity_id


async def test_battery_plan_attributes_expose_windows_and_programs(
    hass: HomeAssistant,
) -> None:
    """The plan sensor carries the windows and the programs to be written."""
    entry = make_entry()
    entry.add_to_hass(hass)
    hass.states.async_set(SOC_ENTITY, "25", {"unit_of_measurement": "%"})

    with freeze_time(FROZEN):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        item = next(
            item
            for item in registry.entities.values()
            if item.config_entry_id == entry.entry_id
            and item.translation_key == "battery_charge_energy"
        )
        state = hass.states.get(item.entity_id)

    assert state is not None
    attributes = state.attributes
    assert attributes["reason"] in {"planned", "no_spread"}
    assert attributes["capacity_kwh"] == pytest.approx(10.0)
    assert len(attributes["tou_programs"]) == 6
    assert isinstance(attributes["windows"], list)
    for program in attributes["tou_programs"]:
        assert program["source"] in {int(source) for source in ChargeSource}


async def _setup_with_battery(hass: HomeAssistant, **overrides) -> MockConfigEntry:
    """Set up an entry with a battery, returning it."""
    entry = make_entry(**overrides)
    entry.add_to_hass(hass)
    hass.states.async_set(SOC_ENTITY, "25", {"unit_of_measurement": "%"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_action_is_registered(hass: HomeAssistant) -> None:
    """The write action is available once KD Helios is set up."""
    entry = await _setup_with_battery(hass)
    assert entry
    assert hass.services.has_service(DOMAIN, SERVICE_APPLY_TOU)


async def test_action_refuses_without_writes_enabled(hass: HomeAssistant) -> None:
    """Writes disabled is the default and blocks the action."""
    entry = make_entry()
    entry.add_to_hass(hass)
    with freeze_time(FROZEN):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_APPLY_TOU,
                {"entry_id": entry.entry_id, "confirm": True},
                blocking=True,
            )


async def test_action_refuses_without_confirmation(hass: HomeAssistant) -> None:
    """The action needs an explicit confirmation."""
    entry = await _setup_with_battery(
        hass,
        **{
            CONF_ALLOW_INVERTER_WRITES: True,
            CONF_INVERTER_DEVICE: "inverter-device-id",
        },
    )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_APPLY_TOU,
            {"entry_id": entry.entry_id, "confirm": False},
            blocking=True,
        )


async def test_action_writes_the_four_register_blocks(hass: HomeAssistant) -> None:
    """With writes enabled and confirmed, four blocks go to Solarman."""
    calls: list[dict] = []

    async def fake_write(call) -> None:
        calls.append(dict(call.data))

    hass.services.async_register(
        "solarman", "write_multiple_holding_registers", fake_write
    )
    entry = await _setup_with_battery(
        hass,
        **{
            CONF_ALLOW_INVERTER_WRITES: True,
            CONF_INVERTER_DEVICE: "inverter-device-id",
        },
    )

    result = await hass.services.async_call(
        DOMAIN,
        SERVICE_APPLY_TOU,
        {"entry_id": entry.entry_id, "confirm": True},
        blocking=True,
        return_response=True,
    )

    assert len(calls) == 4
    assert [call["register"] for call in calls] == [
        TOU_REGISTERS["times"],
        TOU_REGISTERS["power"],
        TOU_REGISTERS["soc"],
        TOU_REGISTERS["sources"],
    ]
    for call in calls:
        assert call["device"] == "inverter-device-id"
        assert len(call["values"]) == 6
        assert all(0 <= value <= 65535 for value in call["values"])

    assert result["entry_id"] == entry.entry_id
    assert len(result["programs"]) == 6


async def test_battery_flow_creates_the_subentry(hass: HomeAssistant) -> None:
    """The battery form creates the subentry with the given parameters."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="zonneplan_quarterly",
        title="Zonneplan",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_BATTERY), context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            **BATTERY_DATA,
            CONF_BATTERY_CAPACITY_KWH: 20.0,
            CONF_MAX_CHARGE_KW: 5.0,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    subentry = entry.get_subentries_of_type(SUBENTRY_TYPE_BATTERY)[0]
    assert subentry.data[CONF_BATTERY_CAPACITY_KWH] == pytest.approx(20.0)
    assert subentry.title == "Home battery"


async def test_battery_flow_rejects_a_target_below_the_reserve(
    hass: HomeAssistant,
) -> None:
    """A target below the reserve would plan nothing."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="zonneplan_quarterly",
        title="Zonneplan",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_BATTERY), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {**BATTERY_DATA, CONF_RESERVE_SOC: 60.0, CONF_TARGET_SOC: 50.0},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_TARGET_SOC: "target_above_reserve"}


async def test_battery_flow_requires_an_inverter_for_writes(
    hass: HomeAssistant,
) -> None:
    """Enabling writes without an inverter device is refused."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="zonneplan_quarterly",
        title="Zonneplan",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_BATTERY), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**BATTERY_DATA, CONF_ALLOW_INVERTER_WRITES: True}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_INVERTER_DEVICE: "inverter_required"}


async def test_battery_reconfigure_keeps_a_single_battery(
    hass: HomeAssistant,
) -> None:
    """Editing works, and a second battery is refused."""
    entry = await _setup_with_battery(hass)
    subentry = entry.get_subentries_of_type(SUBENTRY_TYPE_BATTERY)[0]

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_BATTERY),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry.subentry_id},
    )
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**BATTERY_DATA, CONF_TARGET_SOC: 90.0}
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()

    updated = entry.get_subentries_of_type(SUBENTRY_TYPE_BATTERY)[0]
    assert updated.data[CONF_TARGET_SOC] == pytest.approx(90.0)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_BATTERY), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance"


async def test_unavailable_soc_still_sets_up(hass: HomeAssistant) -> None:
    """A missing SOC reading yields no plan, not a broken entry."""
    entry = make_entry()
    entry.add_to_hass(hass)

    with freeze_time(FROZEN):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        plan = entry.runtime_data.engine.battery_plan
        assert plan is not None
        assert plan.reason == "no_soc"
        assert plan.energy_kwh == 0.0


async def test_plan_updates_when_soc_moves(hass: HomeAssistant) -> None:
    """A new SOC reading changes the energy the plan wants to buy."""
    entry = make_entry()
    entry.add_to_hass(hass)
    hass.states.async_set(SOC_ENTITY, "25", {"unit_of_measurement": "%"})

    with freeze_time(FROZEN):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        first = entry.runtime_data.engine.battery_plan

        hass.states.async_set(SOC_ENTITY, "80", {"unit_of_measurement": "%"})
        await entry.runtime_data.engine.async_refresh()

        second = entry.runtime_data.engine.battery_plan

    assert first is not None and second is not None
    assert second.energy_kwh <= first.energy_kwh
    assert second.soc_pct == pytest.approx(80.0)


async def test_plan_horizon_is_respected(hass: HomeAssistant) -> None:
    """A shorter horizon cannot plan further than its prices."""
    entry = await _setup_with_battery(hass, **{CONF_BATTERY_HORIZON_HOURS: 6})
    plan = entry.runtime_data.engine.battery_plan
    assert plan is not None
    for window in plan.windows:
        assert window.start <= plan.generated_at + timedelta(hours=6)
