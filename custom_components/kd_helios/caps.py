"""The charge power ceiling, built as a chain of caps.

Each cap takes the limit decided so far and may only lower it. Two rules make
the chain safe to extend:

* **A cap never raises.** It can veto, and it can shrink, but it cannot undo a
  ceiling another cap imposed.
* **A cap with no input does nothing.** A missing temperature sensor or an
  unknown grid reading passes the limit through unchanged, because a sensor gap
  must never be the reason a battery stops charging.

The chain is ordered from the widest bound to the narrowest, so the reason on
record is the one that actually bit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .const import (
    DEFAULT_TEMP_CHARGE_LIMIT_BAND_C,
    DEFAULT_TEMP_CHARGE_LIMIT_C,
    DEFAULT_TEMP_CHARGE_LIMIT_FLOOR_PCT,
    Priority,
)
from .decisions import SOURCE_CAP, Blocker

#: A car drawing more than this counts as charging.
EV_CHARGING_KW = 0.5

#: The thermal derate mirrors the shape Omnibattery uses: full power at or
#: below the limit, ramping linearly to the floor across the band. The defaults
#: live in ``const`` so the config flow and the cap cannot disagree.
DEFAULT_TEMP_LIMIT_C = DEFAULT_TEMP_CHARGE_LIMIT_C
DEFAULT_TEMP_BAND_C = DEFAULT_TEMP_CHARGE_LIMIT_BAND_C
DEFAULT_TEMP_FLOOR_PCT = DEFAULT_TEMP_CHARGE_LIMIT_FLOOR_PCT


class ChargeCap(Protocol):
    """One ceiling in the chain."""

    def apply(self, limit_kw: float) -> tuple[float, Blocker | None]:
        """Return the (possibly lower) limit and why it was lowered."""


@dataclass(frozen=True, slots=True)
class EnvelopeCap:
    """The battery's own maximum charge power."""

    max_charge_kw: float

    def apply(self, limit_kw: float) -> tuple[float, Blocker | None]:
        """Clamp to the hardware envelope."""
        return min(max(0.0, limit_kw), max(0.0, self.max_charge_kw)), None


@dataclass(frozen=True, slots=True)
class EvPriorityCap:
    """Give the cheap hours to the car when it is charging and has priority."""

    ev_power_kw: float
    priority: Priority = Priority.EV

    def apply(self, limit_kw: float) -> tuple[float, Blocker | None]:
        """Refuse battery charging while the car owns the capacity."""
        if self.priority is not Priority.EV or self.ev_power_kw < EV_CHARGING_KW:
            return limit_kw, None
        return 0.0, Blocker(
            SOURCE_CAP,
            "ev_charging",
            {"ev_kw": round(self.ev_power_kw, 2)},
        )


@dataclass(frozen=True, slots=True)
class ImportHeadroomCap:
    """Keep the battery and the rest of the house under the connection's limit.

    ``grid_import_kw`` is the load the battery has to fit beside, not the meter
    total: when the battery is already charging, its own draw is part of that
    total and must be removed before calculating headroom.
    """

    import_cap_kw: float
    grid_import_kw: float

    def apply(self, limit_kw: float) -> tuple[float, Blocker | None]:
        """Shrink to whatever import room is left."""
        if self.import_cap_kw <= 0:
            return limit_kw, None
        headroom = max(0.0, self.import_cap_kw - max(0.0, self.grid_import_kw))
        if headroom >= limit_kw:
            return limit_kw, None
        return headroom, Blocker(
            SOURCE_CAP,
            "import_cap",
            {
                "cap_kw": round(self.import_cap_kw, 2),
                "load_kw": round(max(0.0, self.grid_import_kw), 2),
                "headroom_kw": round(headroom, 2),
            },
        )


@dataclass(frozen=True, slots=True)
class InverterHeadroomCap:
    """Keep the inverter's own AC supply under its branch breaker.

    The house load only belongs in this sum when it is on the inverter's backup
    output; otherwise the inverter branch carries battery charging alone.
    """

    inverter_cap_kw: float
    inverter_load_kw: float

    def apply(self, limit_kw: float) -> tuple[float, Blocker | None]:
        """Shrink to the inverter branch's remaining headroom."""
        if self.inverter_cap_kw <= 0:
            return limit_kw, None
        headroom = max(0.0, self.inverter_cap_kw - max(0.0, self.inverter_load_kw))
        if headroom >= limit_kw:
            return limit_kw, None
        return headroom, Blocker(
            SOURCE_CAP,
            "inverter_cap",
            {
                "cap_kw": round(self.inverter_cap_kw, 2),
                "load_kw": round(max(0.0, self.inverter_load_kw), 2),
                "headroom_kw": round(headroom, 2),
            },
        )


@dataclass(frozen=True, slots=True)
class TemperatureCap:
    """Derate the charge power as the battery warms up.

    Optional and off unless configured. The ramp is continuous, so there is no
    latch and no hysteresis: it settles by itself, and it recovers as the
    battery cools.
    """

    temperature_c: float | None = None
    limit_c: float = DEFAULT_TEMP_LIMIT_C
    band_c: float = DEFAULT_TEMP_BAND_C
    floor_pct: float = DEFAULT_TEMP_FLOOR_PCT

    @property
    def factor(self) -> float:
        """Return the charge-power scale (0..1) for the measured temperature."""
        temperature = self.temperature_c
        if temperature is None:
            return 1.0
        if temperature <= self.limit_c:
            return 1.0
        floor = max(0.0, min(1.0, self.floor_pct / 100.0))
        if self.band_c <= 0 or temperature >= self.limit_c + self.band_c:
            return floor
        across = (temperature - self.limit_c) / self.band_c
        return 1.0 - across * (1.0 - floor)

    def apply(self, limit_kw: float) -> tuple[float, Blocker | None]:
        """Cap the limit by the thermal factor."""
        factor = self.factor
        if factor >= 1.0:
            return limit_kw, None
        derated = min(limit_kw, limit_kw * factor)
        return derated, Blocker(
            SOURCE_CAP,
            "temperature",
            {
                "temperature_c": round(float(self.temperature_c or 0.0), 1),
                "limit_c": self.limit_c,
                "factor": round(factor, 3),
            },
        )


def apply_caps(
    limit_kw: float,
    caps: list[ChargeCap],
) -> tuple[float, tuple[Blocker, ...]]:
    """Run the chain, returning the ceiling and every cap that lowered it."""
    blockers: list[Blocker] = []
    for cap in caps:
        limit_kw, blocker = cap.apply(limit_kw)
        if blocker is not None:
            blockers.append(blocker)
    return max(0.0, limit_kw), tuple(blockers)


def charge_caps(
    max_charge_kw: float,
    import_cap_kw: float,
    grid_import_kw: float,
    ev_power_kw: float,
    priority: Priority,
    inverter_cap_kw: float = 0.0,
    inverter_load_kw: float = 0.0,
    *,
    temperature_c: float | None = None,
    temp_limit_c: float = DEFAULT_TEMP_LIMIT_C,
    temp_band_c: float = DEFAULT_TEMP_BAND_C,
    temp_floor_pct: float = DEFAULT_TEMP_FLOOR_PCT,
) -> list[ChargeCap]:
    """Build the chain for one control cycle, widest bound first."""
    return [
        EnvelopeCap(max_charge_kw),
        EvPriorityCap(ev_power_kw, priority),
        ImportHeadroomCap(import_cap_kw, grid_import_kw),
        InverterHeadroomCap(inverter_cap_kw, inverter_load_kw),
        TemperatureCap(
            temperature_c=temperature_c,
            limit_c=temp_limit_c,
            band_c=temp_band_c,
            floor_pct=temp_floor_pct,
        ),
    ]
