"""Reading power and energy entities from the state machine.

Kept separate from the accounting logic so that devices and the grid connection
share exactly one interpretation of units, unavailable states and bad values.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_conversion import EnergyConverter, PowerConverter

from .const import UNIT_KWH, UNIT_W

_LOGGER = logging.getLogger(__name__)

#: Units already reported, so one misconfigured sensor does not log every tick.
_REPORTED_UNITS: set[tuple[str, str]] = set()


def _report_unusable_unit(entity_id: str | None, unit: Any, kind: str) -> None:
    """Warn once when a sensor's unit cannot be converted to ``kind``."""
    key = (entity_id or "", str(unit))
    if key in _REPORTED_UNITS:
        return
    _REPORTED_UNITS.add(key)
    _LOGGER.warning(
        "%s reports %r, which is not a %s unit; ignoring the reading",
        entity_id,
        unit,
        kind,
    )


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
    """Read a power entity in watt, or None when it is not a power value.

    A sensor whose unit is not a power unit is refused rather than passed
    through: a charging current in ampere read as watt would understate the
    car's draw, and a refused reading merely makes the caller fall back to
    another source.
    """
    state = entity_state(hass, entity_id)
    if state is None:
        return None
    value = _as_float(state)
    if value is None:
        return None
    unit: Any = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) or UNIT_W
    try:
        return PowerConverter.convert(value, unit, UNIT_W)
    except (HomeAssistantError, TypeError, ValueError):
        _report_unusable_unit(entity_id, unit, "power")
        return None


def read_energy(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Read a cumulative energy entity in kWh, or None when it is not energy."""
    state = entity_state(hass, entity_id)
    if state is None:
        return None
    value = _as_float(state)
    if value is None:
        return None
    unit: Any = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) or UNIT_KWH
    try:
        return EnergyConverter.convert(value, unit, UNIT_KWH)
    except (HomeAssistantError, TypeError, ValueError):
        _report_unusable_unit(entity_id, unit, "energy")
        return None
