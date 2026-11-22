"""Tests for proving that the inverter actually took a schedule."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kd_helios.battery import (
    ChargeSource,
    TouPlan,
    TouProgram,
)
from custom_components.kd_helios.inverter import (
    InverterNotReadyError,
    InverterWriteError,
    async_read_tou,
    async_write_tou_verified,
)

DEVICE = "inverter-1"


def make_plan(soc_pct: int = 100) -> TouPlan:
    """Return a six-slot plan."""
    starts = (0, 240, 480, 720, 960, 1200)
    return TouPlan(
        programs=tuple(
            TouProgram(minute, 3000, soc_pct, ChargeSource.GRID) for minute in starts
        )
    )


class FakeInverter:
    """A register store with configurable misbehaviour."""

    def __init__(self) -> None:
        self.registers: dict[int, int] = {}
        self.writes = 0
        self.reads = 0
        self.corrupt_next_read = False
        self.corrupt_always = False

    async def write_multiple(self, call) -> None:
        self.writes += 1
        register = call.data["register"]
        for offset, value in enumerate(call.data["values"]):
            self.registers[register + offset] = value
        if self.corrupt_always:
            # The link accepted the write but the device kept the old value.
            for offset in range(len(call.data["values"])):
                self.registers[register + offset] = 0

    async def write_single(self, call) -> None:
        self.registers[call.data["register"]] = call.data["value"]

    async def read_multiple(self, call) -> dict[int, int]:
        self.reads += 1
        register = call.data["register"]
        count = call.data["count"]
        if self.corrupt_next_read:
            self.corrupt_next_read = False
            return {register + offset: 1 for offset in range(count)}
        return {
            register + offset: self.registers.get(register + offset, 0)
            for offset in range(count)
        }


def _register(hass: HomeAssistant, inverter: FakeInverter) -> None:
    """Register the Solarman write and read services."""
    hass.services.async_register(
        "solarman", "write_multiple_holding_registers", inverter.write_multiple
    )
    hass.services.async_register(
        "solarman", "write_holding_register", inverter.write_single
    )
    hass.services.async_register(
        "solarman",
        "read_multiple_holding_registers",
        inverter.read_multiple,
        supports_response=SupportsResponse.OPTIONAL,
    )


async def test_a_schedule_that_reads_back_is_verified(hass: HomeAssistant) -> None:
    """The happy path: what was written is what the inverter holds."""
    inverter = FakeInverter()
    _register(hass, inverter)

    result = await async_write_tou_verified(hass, DEVICE, make_plan())

    assert result["verified"] is True
    assert result["attempts"] == 1
    assert inverter.reads == 4


async def test_one_bad_read_back_is_retried(hass: HomeAssistant) -> None:
    """A single dropped block is rewritten, not reported as a failure."""
    inverter = FakeInverter()
    inverter.corrupt_next_read = True
    _register(hass, inverter)

    result = await async_write_tou_verified(hass, DEVICE, make_plan())

    assert result["verified"] is True
    assert result["attempts"] == 2
    assert inverter.writes == 8


async def test_a_persistent_mismatch_is_an_error(hass: HomeAssistant) -> None:
    """A schedule the inverter never took must not be recorded as applied."""
    inverter = FakeInverter()
    inverter.corrupt_always = True
    _register(hass, inverter)

    with pytest.raises(InverterWriteError):
        await async_write_tou_verified(hass, DEVICE, make_plan())

    assert inverter.writes == 8


async def test_without_a_read_service_the_write_is_trusted(
    hass: HomeAssistant,
) -> None:
    """Older Solarman builds cannot read; control must still work."""
    inverter = FakeInverter()
    hass.services.async_register(
        "solarman", "write_multiple_holding_registers", inverter.write_multiple
    )
    hass.services.async_register(
        "solarman", "write_holding_register", inverter.write_single
    )

    result = await async_write_tou_verified(hass, DEVICE, make_plan())

    assert result["verified"] is None
    assert result["attempts"] == 1
    assert inverter.writes == 4


async def test_the_schedule_can_be_read_back(hass: HomeAssistant) -> None:
    """The read helper returns the four blocks the inverter holds."""
    inverter = FakeInverter()
    _register(hass, inverter)
    plan = make_plan(soc_pct=80)
    await async_write_tou_verified(hass, DEVICE, plan)

    blocks = await async_read_tou(hass, DEVICE)

    assert blocks is not None
    assert blocks[148] == [0, 400, 800, 1200, 1600, 2000]
    assert blocks[166] == [80] * 6


async def test_reading_is_unavailable_rather_than_empty(
    hass: HomeAssistant,
) -> None:
    """No read service is None, which is not the same as all zeroes."""
    assert await async_read_tou(hass, DEVICE) is None


async def test_an_unloaded_solarman_entry_is_not_written(
    hass: HomeAssistant,
) -> None:
    """A startup ordering race is not a refused write.

    Home Assistant sets up integrations concurrently. When KD Helios writes
    before Solarman finished its own setup, Solarman's config entry has no
    runtime data yet and its service raises AttributeError. The write must be
    retried later instead of being reported as an inverter refusal.
    """
    solarman = MockConfigEntry(domain="solarman", data={})
    solarman.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=solarman.entry_id,
        identifiers={("solarman", "inverter-1")},
    )
    inverter = FakeInverter()
    _register(hass, inverter)

    with pytest.raises(InverterNotReadyError):
        await async_write_tou_verified(hass, device.id, make_plan())

    assert inverter.writes == 0


async def test_missing_runtime_data_from_solarman_is_retryable(
    hass: HomeAssistant,
) -> None:
    """The exact Solarman failure is translated, even after the entry loaded."""

    async def broken_write(call) -> None:
        raise AttributeError("'ConfigEntry' object has no attribute 'runtime_data'")

    hass.services.async_register(
        "solarman", "write_multiple_holding_registers", broken_write
    )

    with pytest.raises(InverterNotReadyError):
        await async_write_tou_verified(hass, DEVICE, make_plan())
