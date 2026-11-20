"""Reading power and energy entities from the state machine.

Kept separate from the accounting logic so that devices and the grid connection
share exactly one interpretation of units, unavailable states and bad values.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.util.unit_conversion import EnergyConverter, PowerConverter

from .const import UNIT_KWH, UNIT_W


def entity_state(hass: HomeAssistant, entity_id: str | None) -> State | None:
    """Return the state object for an entity, ignoring unusable states."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    return state


def read_number(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Read a plain numeric state without unit conversion."""
    state = entity_state(hass, entity_id)
    if state is None:
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _as_float(state: State) -> float | None:
    """Return the numeric state of a sensor."""
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def read_power(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Read a power entity in watt."""
    state = entity_state(hass, entity_id)
    if state is None:
        return None
    value = _as_float(state)
    if value is None:
        return None
    unit: Any = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) or UNIT_W
    try:
        return PowerConverter.convert(value, unit, UNIT_W)
    except (TypeError, ValueError):
        return value


def read_energy(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Read a cumulative energy entity in kWh."""
    state = entity_state(hass, entity_id)
    if state is None:
        return None
    value = _as_float(state)
    if value is None:
        return None
    unit: Any = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) or UNIT_KWH
    try:
        return EnergyConverter.convert(value, unit, UNIT_KWH)
    except (TypeError, ValueError):
        return value
