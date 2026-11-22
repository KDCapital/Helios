"""The battery settings form exposes the grid-charge current ceiling."""

from __future__ import annotations

from custom_components.kd_helios.config_flow import _battery_schema
from custom_components.kd_helios.const import CONF_MAX_GRID_CHARGE_CURRENT


def test_the_battery_form_exposes_the_max_grid_charge_current() -> None:
    """The field must be on the same form as the other battery settings."""
    assert CONF_MAX_GRID_CHARGE_CURRENT in _battery_schema().schema
