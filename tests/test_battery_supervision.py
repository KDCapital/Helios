"""Tests for battery supervision: reasons, repairs, and weekly health."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from freezegun import freeze_time
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kd_helios.battery import ChargePlan, ChargeWindow
from custom_components.kd_helios.const import (
    CONF_ALLOW_INVERTER_WRITES,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_HORIZON_HOURS,
    CONF_BATTERY_POWER_ENTITY,
    CONF_EFFICIENCY,
    CONF_HOUSE_LOAD_ENTITY,
    CONF_INVERTER_DEVICE,
    CONF_MAX_CHARGE_KW,
    CONF_PRICE_INTERVAL,
    CONF_PROVIDER,
    CONF_RESERVE_SOC,
    CONF_SOC_ENTITY,
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_SOLAR_HOLD_ENABLED,
    CONF_TARGET_SOC,
    CONF_WEEKLY_FULL_CHARGE_DAY,
    CONF_WEEKLY_FULL_CHARGE_ENABLED,
    DOMAIN,
    ISSUE_BATTERY_NOT_DELIVERING,
    ISSUE_INVERTER_WRITE_FAILED,
    SUBENTRY_TYPE_BATTERY,
)

FROZEN = "2026-11-20 12:00:00+00:00"
FRIDAY = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
USER_INPUT = {CONF_PROVIDER: "zonneplan", CONF_PRICE_INTERVAL: "quarterly"}
SOC = "sensor.battery_soc"
POWER = "sensor.battery_power"
INVERTER = "inverter-device-id"

BATTERY = {
    CONF_SOC_ENTITY: SOC,
    CONF_BATTERY_CAPACITY_KWH: 10.0,
    CONF_MAX_CHARGE_KW: 3.0,
    CONF_RESERVE_SOC: 10.0,
    CONF_TARGET_SOC: 100.0,
    CONF_EFFICIENCY: 90.0,
    CONF_BATTERY_HORIZON_HOURS: 24,
}


def make_entry(**overrides) -> MockConfigEntry:
    """Return an entry with one battery subentry."""
    return MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="zonneplan_quarterly",
        title="Zonneplan",
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_BATTERY,
                "data": {**BATTERY, **overrides},
                "title": "Home battery",
                "unique_id": None,
            }
        ],
    )


class FakeInverter:
    """A register store that can be made to refuse the schedule."""

    def __init__(self) -> None:
        self.registers: dict[int, int] = {}
        self.corrupt = False

    async def write_multiple(self, call) -> None:
        register = call.data["register"]
        for offset, value in enumerate(call.data["values"]):
            self.registers[register + offset] = 0 if self.corrupt else value

    async def write_single(self, call) -> None:
        self.registers[call.data["register"]] = call.data["value"]

    async def read_multiple(self, call) -> dict[int, int]:
        register = call.data["register"]
        return {
            register + offset: self.registers.get(register + offset, 0)
            for offset in range(call.data["count"])
        }


async def _setup(
    hass: HomeAssistant, entry: MockConfigEntry, *, verify: bool = True
) -> FakeInverter:
    """Set up an entry with a fake inverter."""
    inverter = FakeInverter()
    hass.services.async_register(
        "solarman", "write_multiple_holding_registers", inverter.write_multiple
    )
    hass.services.async_register(
        "solarman", "write_holding_register", inverter.write_single
    )
    if verify:
        hass.services.async_register(
            "solarman",
            "read_multiple_holding_registers",
            inverter.read_multiple,
            supports_response=SupportsResponse.OPTIONAL,
        )
    entry.add_to_hass(hass)
    hass.states.async_set(SOC, "25", {"unit_of_measurement": "%"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return inverter


def _entity(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    """Return the entity id for a translation key of the battery device."""
    registry = er.async_get(hass)
    for item in registry.entities.values():
        if item.config_entry_id == entry.entry_id and item.translation_key == key:
            return item.entity_id
    raise AssertionError(f"{key} entity not found")


async def _control_on(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Switch control on through the switch entity."""
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": _entity(hass, entry, "inverter_control")},
        blocking=True,
    )


async def test_control_off_is_named_as_the_reason(hass: HomeAssistant) -> None:
    """A plan nobody switched on must say so, not just plan silently."""
    entry = make_entry(
        **{CONF_ALLOW_INVERTER_WRITES: True, CONF_INVERTER_DEVICE: INVERTER}
    )
    with freeze_time(FROZEN):
        await _setup(hass, entry)
        engine = entry.runtime_data.engine
        engine._plan_fingerprint = None
        await engine.async_refresh()

    status = hass.states.get(_entity(hass, entry, "battery_status"))
    assert status is not None
    assert status.attributes["reason"] == engine.battery_plan.reason
    reasons = {blocker["reason"] for blocker in status.attributes["blockers"]}
    assert "control_off" in reasons


async def test_the_status_sensor_lists_every_blocker(hass: HomeAssistant) -> None:
    """The registry is the one explanation, and the sensor reads it whole."""
    entry = make_entry()
    with freeze_time(FROZEN):
        await _setup(hass, entry)

    status = hass.states.get(_entity(hass, entry, "battery_status"))
    assert status is not None
    reasons = {blocker["reason"] for blocker in status.attributes["blockers"]}
    assert "writes_disabled" in reasons
    for blocker in status.attributes["blockers"]:
        assert blocker["source"]
        assert blocker["since"]


async def test_a_refused_schedule_raises_a_repair(hass: HomeAssistant) -> None:
    """A write the inverter did not take must be visible, not just logged."""
    entry = make_entry(
        **{CONF_ALLOW_INVERTER_WRITES: True, CONF_INVERTER_DEVICE: INVERTER}
    )
    with freeze_time(FROZEN):
        inverter = await _setup(hass, entry)
        await _control_on(hass, entry)
        engine = entry.runtime_data.engine

        inverter.corrupt = True
        engine.applied_plan = None
        await engine.async_refresh()

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_INVERTER_WRITE_FAILED}_{entry.entry_id}"
    )
    assert issue is not None
    assert engine.last_control_error
    reasons = {blocker["reason"] for blocker in engine.blockers.as_list()}
    assert "write_failed" in reasons


async def test_a_recovered_write_clears_the_repair(hass: HomeAssistant) -> None:
    """The repair must not outlive the problem it describes."""
    entry = make_entry(
        **{CONF_ALLOW_INVERTER_WRITES: True, CONF_INVERTER_DEVICE: INVERTER}
    )
    with freeze_time(FROZEN):
        inverter = await _setup(hass, entry)
        await _control_on(hass, entry)
        engine = entry.runtime_data.engine

        inverter.corrupt = True
        engine.applied_plan = None
        await engine.async_refresh()
        assert (
            ir.async_get(hass).async_get_issue(
                DOMAIN, f"{ISSUE_INVERTER_WRITE_FAILED}_{entry.entry_id}"
            )
            is not None
        )

        inverter.corrupt = False
        engine.applied_plan = None
        with freeze_time("2026-11-20 12:30:00+00:00"):
            await engine.async_refresh()

    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"{ISSUE_INVERTER_WRITE_FAILED}_{entry.entry_id}"
        )
        is None
    )
    assert engine.last_verification is not None
    assert engine.last_verification["verified"] is True


async def test_a_weekly_full_charge_raises_the_target(hass: HomeAssistant) -> None:
    """On its day the weekly cycle charges to 100 instead of the normal target."""
    entry = make_entry(
        **{
            CONF_TARGET_SOC: 80.0,
            CONF_WEEKLY_FULL_CHARGE_ENABLED: True,
            CONF_WEEKLY_FULL_CHARGE_DAY: 4,  # Friday
        }
    )
    with freeze_time(FROZEN):
        await _setup(hass, entry)
        engine = entry.runtime_data.engine

    assert engine.weekly_result.active is True
    # 25 to 100 percent of 10 kWh, not 25 to 80.
    assert engine.battery_plan.energy_kwh == 7.5


async def test_a_weekly_cycle_does_not_run_on_another_day(
    hass: HomeAssistant,
) -> None:
    """The normal target stands when it is not the chosen day."""
    entry = make_entry(
        **{
            CONF_TARGET_SOC: 80.0,
            CONF_WEEKLY_FULL_CHARGE_ENABLED: True,
            CONF_WEEKLY_FULL_CHARGE_DAY: 6,  # Sunday
        }
    )
    with freeze_time(FROZEN):
        await _setup(hass, entry)
        engine = entry.runtime_data.engine

    assert engine.weekly_result.active is False
    assert engine.battery_plan.energy_kwh == 5.5


def _plan_with_a_window(now: datetime) -> ChargePlan:
    """Return a plan whose window is active at ``now``."""
    return ChargePlan(
        generated_at=now,
        soc_pct=40.0,
        windows=(
            ChargeWindow(
                start=now - timedelta(hours=1),
                end=now + timedelta(hours=1),
                energy_kwh=2.0,
                cost=0.1,
                value=0.5,
                profit=0.4,
                average_price=0.05,
                discharge_price=0.4,
            ),
        ),
        energy_kwh=2.0,
        cost=0.1,
        expected_saving=0.4,
    )


async def test_a_battery_that_delivers_nothing_gets_a_repair(
    hass: HomeAssistant,
) -> None:
    """A schedule in force with no delivery is invisible without this check."""
    entry = make_entry(
        **{
            CONF_ALLOW_INVERTER_WRITES: True,
            CONF_INVERTER_DEVICE: INVERTER,
            CONF_BATTERY_POWER_ENTITY: POWER,
        }
    )
    with freeze_time(FROZEN):
        await _setup(hass, entry)
        await _control_on(hass, entry)
        engine = entry.runtime_data.engine
        plan = _plan_with_a_window(FRIDAY)
        hass.states.async_set(POWER, "0", {"unit_of_measurement": "W"})

        blockers = engine._check_not_delivering(FRIDAY, plan, 40.0)
        assert [blocker.reason for blocker in blockers] == ["not_delivering"]
        # Three full grace periods of nothing delivered.
        for step in (11, 22, 33):
            blockers = engine._check_not_delivering(
                FRIDAY + timedelta(minutes=step), plan, 40.0
            )

    assert [blocker.reason for blocker in blockers] == ["not_delivering"]
    assert engine._delivery_strikes >= 3
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_BATTERY_NOT_DELIVERING}_{entry.entry_id}"
    )
    assert issue is not None


async def test_delivering_power_clears_the_strikes(hass: HomeAssistant) -> None:
    """A battery that recovers must not be punished for the episode."""
    entry = make_entry(
        **{
            CONF_ALLOW_INVERTER_WRITES: True,
            CONF_INVERTER_DEVICE: INVERTER,
            CONF_BATTERY_POWER_ENTITY: POWER,
        }
    )
    with freeze_time(FROZEN):
        await _setup(hass, entry)
        await _control_on(hass, entry)
        engine = entry.runtime_data.engine
        plan = _plan_with_a_window(FRIDAY)

        hass.states.async_set(POWER, "0", {"unit_of_measurement": "W"})
        for step in (0, 11, 22, 33):
            engine._check_not_delivering(FRIDAY + timedelta(minutes=step), plan, 40.0)
        assert engine._delivery_strikes >= 3

        hass.states.async_set(POWER, "2500", {"unit_of_measurement": "W"})
        blockers = engine._check_not_delivering(
            FRIDAY + timedelta(minutes=44), plan, 40.0
        )

    assert blockers == []
    assert engine._delivery_strikes == 0


async def test_the_solar_hold_stops_the_grid_charge(hass: HomeAssistant) -> None:
    """Sun that covers the need means nothing is bought from the grid."""
    entry = make_entry(
        **{
            CONF_SOLAR_HOLD_ENABLED: True,
            CONF_SOLAR_FORECAST_ENTITY: "sensor.solar_remaining",
            CONF_HOUSE_LOAD_ENTITY: "sensor.house_load",
        }
    )
    with freeze_time(FROZEN):
        inverter = await _setup(hass, entry)
        engine = entry.runtime_data.engine
        engine.load_profile.energy_kwh = lambda start, end: 1.0
        hass.states.async_set(
            "sensor.solar_remaining", "40", {"unit_of_measurement": "kWh"}
        )
        hass.states.async_set("sensor.house_load", "500", {"unit_of_measurement": "W"})
        engine.applied_plan = None
        await engine.async_refresh()

    assert engine.battery_plan.reason == "waiting_for_solar"
    assert engine.battery_plan.solar_credit_kwh == 7.5
    reasons = {blocker["reason"] for blocker in engine.blockers.as_list()}
    assert "waiting_for_solar" in reasons
    # Nothing to buy, so nothing is written.
    assert inverter.registers == {}
