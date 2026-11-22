"""Tests for automatic inverter control and releasing it safely."""

from __future__ import annotations

from datetime import timedelta

from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kd_helios.battery import (
    WORK_MODE_REGISTER,
    ChargePlan,
    ChargeSource,
    ChargeWindow,
)
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
    CONF_RESTORE_WORK_MODE,
    CONF_SOC_ENTITY,
    CONF_TARGET_SOC,
    DOMAIN,
    ISSUE_INVERTER_WRITE_FAILED,
    SUBENTRY_TYPE_BATTERY,
)
from custom_components.kd_helios.decisions import SOURCE_WRITE

FROZEN = "2026-11-20 12:00:00+00:00"
USER_INPUT = {CONF_PROVIDER: "zonneplan", CONF_PRICE_INTERVAL: "quarterly"}
SOC = "sensor.battery_soc"
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


class Recorder:
    """Record what is written to the inverter."""

    def __init__(self) -> None:
        self.blocks: list[dict] = []
        self.work_modes: list[int] = []

    async def write_multiple(self, call) -> None:
        self.blocks.append(dict(call.data))

    async def write_single(self, call) -> None:
        self.work_modes.append(call.data["value"])

    def clear(self) -> None:
        self.blocks.clear()
        self.work_modes.clear()


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> Recorder:
    """Set up an entry with a fake Solarman write service."""
    recorder = Recorder()
    hass.services.async_register(
        "solarman", "write_multiple_holding_registers", recorder.write_multiple
    )
    hass.services.async_register(
        "solarman", "write_holding_register", recorder.write_single
    )
    entry.add_to_hass(hass)
    hass.states.async_set(SOC, "25", {"unit_of_measurement": "%"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return recorder


def _switch_entity(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the control switch entity id."""
    registry = er.async_get(hass)
    for item in registry.entities.values():
        if item.config_entry_id == entry.entry_id and (
            item.translation_key == "inverter_control"
        ):
            return item.entity_id
    raise AssertionError("control switch not found")


async def _add_control(hass: HomeAssistant) -> tuple[MockConfigEntry, Recorder]:
    """Set up a battery with writes enabled and an inverter."""
    entry = make_entry(
        **{
            CONF_ALLOW_INVERTER_WRITES: True,
            CONF_INVERTER_DEVICE: INVERTER,
        }
    )
    with freeze_time(FROZEN):
        recorder = await _setup(hass, entry)
    return entry, recorder


async def test_control_switch_exists_and_starts_off(hass: HomeAssistant) -> None:
    """A battery with writes enabled gets a control switch, off by default."""
    entry, _ = await _add_control(hass)
    entity_id = _switch_entity(hass, entry)

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "off"
    assert state.attributes["writes_enabled"] is True
    assert state.attributes["inverter_configured"] is True


async def test_control_switch_is_unavailable_without_writes(
    hass: HomeAssistant,
) -> None:
    """Without permission to write, the switch cannot be used."""
    entry = make_entry()
    with freeze_time(FROZEN):
        await _setup(hass, entry)

    state = hass.states.get(_switch_entity(hass, entry))
    assert state is not None
    assert state.state == "unavailable"


async def test_turning_control_on_writes_the_plan(hass: HomeAssistant) -> None:
    """Enabling control writes the six programs immediately."""
    entry, recorder = await _add_control(hass)
    engine = entry.runtime_data.engine
    recorder.clear()

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _switch_entity(hass, entry)}, blocking=True
    )

    assert engine.control_enabled is True
    assert engine.applied_plan is not None
    assert len(recorder.blocks) == 7
    by_register = {block["register"]: block["values"] for block in recorder.blocks}
    assert by_register[130] == [1]
    assert by_register[146] == [255]
    assert by_register[142] == [2]
    assert 148 not in by_register
    assert hass.states.get(_switch_entity(hass, entry)).state == "on"


async def test_turning_control_off_releases_the_inverter(hass: HomeAssistant) -> None:
    """Releasing control allows generator charging only, back to self-consumption."""
    entry, recorder = await _add_control(hass)
    engine = entry.runtime_data.engine
    entity_id = _switch_entity(hass, entry)

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": entity_id}, blocking=True
    )
    recorder.clear()

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": entity_id}, blocking=True
    )

    assert engine.control_enabled is False
    assert engine.applied_plan is None
    sources = next(block for block in recorder.blocks if block["register"] == 172)
    assert sources["values"] == [int(ChargeSource.OFF)] * 6
    assert all(value != int(ChargeSource.GRID) for value in sources["values"])
    current = next(block for block in recorder.blocks if block["register"] == 128)
    assert current["values"] == [0]
    # And the work mode goes back to self-consumption.
    by_register = {block["register"]: block["values"] for block in recorder.blocks}
    assert by_register[142] == [1]


async def test_release_honours_the_configured_work_mode(
    hass: HomeAssistant,
) -> None:
    """The restore work mode is configurable."""
    entry = make_entry(
        **{
            CONF_ALLOW_INVERTER_WRITES: True,
            CONF_INVERTER_DEVICE: INVERTER,
            CONF_RESTORE_WORK_MODE: 2,
        }
    )
    with freeze_time(FROZEN):
        recorder = await _setup(hass, entry)
    entity_id = _switch_entity(hass, entry)

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": entity_id}, blocking=True
    )
    recorder.clear()
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": entity_id}, blocking=True
    )

    by_register = {block["register"]: block["values"] for block in recorder.blocks}
    assert by_register[142] == [2]
    assert WORK_MODE_REGISTER == 142


async def test_plan_is_reapplied_when_it_changes(hass: HomeAssistant) -> None:
    """With control on, a different plan is written without being asked."""
    entry, recorder = await _add_control(hass)
    engine = entry.runtime_data.engine
    entity_id = _switch_entity(hass, entry)

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": entity_id}, blocking=True
    )
    recorder.clear()

    # Forget what was written, so the next tick has to write the state again.
    engine._control_key = None
    with freeze_time("2026-11-20 13:00:00+00:00"):
        await engine.async_refresh()

    assert engine.applied_plan is not None
    assert len(recorder.blocks) == 7


async def test_control_follows_the_active_window(
    hass: HomeAssistant, monkeypatch
) -> None:
    """The controller writes the state of the window active right now."""
    start = dt_util.parse_datetime(FROZEN)
    assert start is not None

    def window(offset_hours: int) -> ChargeWindow:
        return ChargeWindow(
            start=start + timedelta(hours=offset_hours),
            end=start + timedelta(hours=offset_hours, minutes=30),
            energy_kwh=2.0,
            cost=0.10,
            value=0.70,
            profit=0.60,
            average_price=0.05,
            discharge_price=0.35,
        )

    first = window(1)
    second = window(3)
    proposal = ChargePlan(
        generated_at=start,
        soc_pct=25.0,
        windows=(first, second),
        energy_kwh=4.0,
        cost=0.20,
        expected_saving=1.20,
    )
    monkeypatch.setattr(
        "custom_components.kd_helios.coordinator.plan_charge",
        lambda *args, **kwargs: proposal,
    )

    entry = make_entry(
        **{
            CONF_ALLOW_INVERTER_WRITES: True,
            CONF_INVERTER_DEVICE: INVERTER,
        }
    )
    with freeze_time(FROZEN):
        recorder = await _setup(hass, entry)
    engine = entry.runtime_data.engine
    entity_id = _switch_entity(hass, entry)

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": entity_id}, blocking=True
    )
    # Before the first window the battery is idle.
    assert engine.schedule_plan is not None
    assert engine.schedule_plan.windows == ()
    recorder.clear()

    # 14:15 CET: inside the first window, so it must charge from the grid.
    with freeze_time("2026-11-20 13:15:00+00:00"):
        await engine.async_refresh()

    by_register = {block["register"]: block["values"] for block in recorder.blocks}
    assert by_register[172] == [int(ChargeSource.GRID)] * 6
    assert by_register[166] == [100] * 6
    assert by_register[128][0] > 0
    assert 148 not in by_register
    assert engine.schedule_plan is not None
    assert engine.schedule_plan.windows == (first,)
    recorder.clear()

    # 15:00 CET: the first window has ended and the second has not started.
    with freeze_time("2026-11-20 14:00:00+00:00"):
        await engine.async_refresh()

    by_register = {block["register"]: block["values"] for block in recorder.blocks}
    assert by_register[172] == [int(ChargeSource.OFF)] * 6
    assert by_register[128] == [0]
    assert 148 not in by_register
    assert engine.schedule_plan is not None
    assert engine.schedule_plan.windows == ()

    # 16:15 CET: inside the second window, charging starts again.
    recorder.clear()
    with freeze_time("2026-11-20 15:15:00+00:00"):
        await engine.async_refresh()

    by_register = {block["register"]: block["values"] for block in recorder.blocks}
    assert by_register[172] == [int(ChargeSource.GRID)] * 6
    assert by_register[128][0] > 0
    assert engine.schedule_plan is not None
    assert engine.schedule_plan.windows == (second,)


async def test_a_write_before_solarman_loads_is_retried_without_a_repair(
    hass: HomeAssistant,
) -> None:
    """A startup race shows up as a blocker, not as a broken inverter."""
    solarman = MockConfigEntry(domain="solarman", data={})
    solarman.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=solarman.entry_id,
        identifiers={("solarman", "inverter-1")},
    )
    entry = make_entry(
        **{
            CONF_ALLOW_INVERTER_WRITES: True,
            CONF_INVERTER_DEVICE: device.id,
        }
    )
    with freeze_time(FROZEN):
        recorder = await _setup(hass, entry)
    engine = entry.runtime_data.engine
    engine.control_enabled = True
    engine.applied_plan = None
    recorder.clear()

    with freeze_time("2026-11-20 13:00:00+00:00"):
        await engine.async_refresh()

    assert recorder.blocks == []
    blocker = engine.blockers.get(SOURCE_WRITE)
    assert blocker is not None
    assert blocker.reason == "inverter_not_ready"
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"{ISSUE_INVERTER_WRITE_FAILED}_{entry.entry_id}"
        )
        is None
    )


async def test_unchanged_plan_is_not_rewritten(hass: HomeAssistant) -> None:
    """An unchanged plan must not hammer the Modbus link."""
    entry, recorder = await _add_control(hass)
    engine = entry.runtime_data.engine
    entity_id = _switch_entity(hass, entry)

    with freeze_time(FROZEN):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": entity_id}, blocking=True
        )
        recorder.clear()

        await engine.async_refresh()
        await engine.async_refresh()

    assert recorder.blocks == []


async def test_control_state_survives_a_reload(hass: HomeAssistant) -> None:
    """Control stays on across a restart, without rewriting on every start."""
    entry, recorder = await _add_control(hass)
    entity_id = _switch_entity(hass, entry)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": entity_id}, blocking=True
    )

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    recorder.clear()
    with freeze_time(FROZEN):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    engine = entry.runtime_data.engine
    assert engine.control_enabled is True
    assert engine.applied_plan is not None
    # A restart does not know the last written state, so it writes it once.
    by_register = {block["register"]: block["values"] for block in recorder.blocks}
    assert by_register[172] == [int(ChargeSource.OFF)] * 6
    assert by_register[142] == [int(engine.control_plan.work_mode)]
