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
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kd_helios.battery import TOU_REGISTERS, ChargeSource
from custom_components.kd_helios.const import (
    CONF_ALLOW_INVERTER_WRITES,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_HORIZON_HOURS,
    CONF_EFFICIENCY,
    CONF_ENERGY_ENTITY,
    CONF_EV_DEPARTURE_WEEKDAY,
    CONF_EV_PACK_KWH,
    CONF_EV_SOC_ENTITY,
    CONF_EV_TARGET_SOC,
    CONF_INVERTER_DEVICE,
    CONF_KIND,
    CONF_MAX_CHARGE_KW,
    CONF_NAME,
    CONF_POWER_ENTITY,
    CONF_PRICE_INTERVAL,
    CONF_PROVIDER,
    CONF_RESERVE_SOC,
    CONF_SOC_ENTITY,
    CONF_TARGET_SOC,
    DOMAIN,
    SERVICE_APPLY_TOU,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_EV_CHARGER,
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


async def test_the_car_window_falls_back_to_the_chargers_power(
    hass: HomeAssistant,
) -> None:
    """Two cars are supported: skip an unreadable one and size the other."""
    def ev_subentry(name: str, energy: str, **extra) -> dict:
        return {
            "subentry_type": SUBENTRY_TYPE_EV_CHARGER,
            "data": {
                CONF_NAME: name,
                CONF_KIND: "ev_charger",
                CONF_ENERGY_ENTITY: energy,
                CONF_EV_PACK_KWH: 60.0,
                CONF_EV_TARGET_SOC: 80.0,
                CONF_EV_DEPARTURE_WEEKDAY: "07:00",
                **extra,
            },
            "title": name,
            "unique_id": None,
        }

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="zonneplan_quarterly",
        title="Zonneplan",
        subentries_data=[
            battery_subentry(),
            # No SOC reading, so this car cannot supply a window.
            ev_subentry("Car without SOC", "sensor.ev1_energy"),
            ev_subentry(
                "Car with SOC",
                "sensor.ev2_energy",
                **{
                    CONF_EV_SOC_ENTITY: "sensor.car_soc",
                    # No car power sensor: the charger's W sensor is the fallback.
                    CONF_POWER_ENTITY: "sensor.ev2_power",
                },
            ),
        ],
    )
    entry.add_to_hass(hass)
    hass.states.async_set(SOC_ENTITY, "25", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.car_soc", "50", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.ev2_power", "11000", {"unit_of_measurement": "W"})

    with freeze_time(FROZEN):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        engine = entry.runtime_data.engine
        hold = engine._ev_hold(dt_util.utcnow(), engine.prices.series)

    assert hold is not None
    start, end, soc = hold
    assert end > start
    assert soc == pytest.approx(50.0)


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


async def test_applied_plan_is_recorded_with_its_prediction(
    hass: HomeAssistant,
) -> None:
    """Writing a plan stores it, with the saving it was expected to deliver."""

    async def fake_write(call) -> None:
        return None

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
    engine = entry.runtime_data.engine
    proposal = engine.battery_plan
    assert proposal is not None

    result = await hass.services.async_call(
        DOMAIN,
        SERVICE_APPLY_TOU,
        {"entry_id": entry.entry_id, "confirm": True},
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()

    applied = engine.applied_plan
    assert applied is not None
    assert applied.expected_saving == pytest.approx(proposal.expected_saving)
    assert applied.energy_kwh == pytest.approx(proposal.energy_kwh)
    # The action rounds its response to four decimals.
    assert result["expected_saving"] == pytest.approx(
        proposal.expected_saving, abs=0.001
    )

    registry = er.async_get(hass)
    item = next(
        item
        for item in registry.entities.values()
        if item.config_entry_id == entry.entry_id
        and item.translation_key == "battery_applied_saving"
    )
    state = hass.states.get(item.entity_id)
    assert state is not None
    assert float(state.state) == pytest.approx(proposal.expected_saving, abs=0.01)
    assert state.attributes["applied_at"]
    assert len(state.attributes["tou_programs"]) == 6


async def test_applied_plan_survives_a_reload(hass: HomeAssistant) -> None:
    """The plan in force, and its prediction, are still there after a restart."""

    async def fake_write(call) -> None:
        return None

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

    await hass.services.async_call(
        DOMAIN,
        SERVICE_APPLY_TOU,
        {"entry_id": entry.entry_id, "confirm": True},
        blocking=True,
    )
    applied = entry.runtime_data.engine.applied_plan
    assert applied is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    restored = entry.runtime_data.engine.applied_plan
    assert restored is not None
    assert restored.expected_saving == pytest.approx(applied.expected_saving)
    assert [program.hhmm for program in restored.programs] == [
        program.hhmm for program in applied.programs
    ]


async def test_diagnostics_include_both_plans(hass: HomeAssistant) -> None:
    """Diagnostics show the live proposal and the applied plan."""
    from custom_components.kd_helios.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = await _setup_with_battery(hass)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["battery"]["plan"] is not None
    assert diagnostics["battery"]["applied"] is None
    assert diagnostics["battery"]["tou_programs"] == 6
