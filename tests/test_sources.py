"""Tests for reading power and energy entities."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.kd_helios.sources import read_energy, read_power


async def test_power_is_converted_to_watt(hass: HomeAssistant) -> None:
    """Kilowatt readings become watt, the unit the accounting uses."""
    hass.states.async_set("sensor.power_kw", "2.5", {"unit_of_measurement": "kW"})

    assert read_power(hass, "sensor.power_kw") == pytest.approx(2500.0)


async def test_current_is_not_read_as_power(hass: HomeAssistant) -> None:
    """An ampere sensor must not be mistaken for a power reading.

    Chargers often expose their current instead of their power. Converting it
    without a voltage would understate the car's draw, so the reader refuses it
    and the caller can fall back to a real power sensor.
    """
    hass.states.async_set("sensor.current", "16", {"unit_of_measurement": "A"})

    assert read_power(hass, "sensor.current") is None


async def test_energy_without_an_energy_unit_is_refused(hass: HomeAssistant) -> None:
    """A power sensor attached as an energy counter is refused, not scaled."""
    hass.states.async_set("sensor.power", "1200", {"unit_of_measurement": "W"})

    assert read_energy(hass, "sensor.power") is None


async def test_energy_is_converted_to_kwh(hass: HomeAssistant) -> None:
    """Watt-hours become kilowatt-hours."""
    hass.states.async_set("sensor.energy", "2500", {"unit_of_measurement": "Wh"})

    assert read_energy(hass, "sensor.energy") == pytest.approx(2.5)


async def test_unavailable_and_unknown_are_not_read(hass: HomeAssistant) -> None:
    """Unusable states yield no value instead of a bad conversion."""
    hass.states.async_set("sensor.off", "unavailable", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.unknown", "unknown", {"unit_of_measurement": "W"})

    assert read_power(hass, "sensor.off") is None
    assert read_power(hass, "sensor.unknown") is None
    assert read_power(hass, "sensor.missing") is None
    assert read_power(hass, None) is None
