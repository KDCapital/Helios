"""Energy and cost accounting for KD Helios.

Each tracked device owns a :class:`DeviceTracker`. The tracker samples one
source entity (a cumulative energy meter is preferred over a power meter),
integrates the energy consumed since the previous successful reading and prices
that energy against the tariff that was actually in force during the elapsed
period. Because the accounting clock only advances on a successful reading, an
unavailable entity never silently prices a long outage at a single tariff.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, PowerConverter

from .const import (
    MAX_TRACKING_GAP,
    MIN_ERE_RATIO_WINDOW_KWH,
    UNIT_KWH,
    UNIT_W,
)
from .models import DeviceConfig
from .prices import PriceSeries
from .tariff import ere_ratio, ere_value_per_kwh

_LOGGER = logging.getLogger(__name__)


def _as_float(value: Any) -> float | None:
    """Coerce a stored value to float."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_datetime(value: Any) -> datetime | None:
    """Coerce a stored value to an aware UTC datetime."""
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    return dt_util.as_utc(parsed)


@dataclass(slots=True)
class DeviceAccumulators:
    """Persisted running totals and integration baselines for one device."""

    total_kwh: float = 0.0
    total_cost: float = 0.0
    total_reimbursement: float = 0.0
    total_ere_value: float = 0.0
    day: str | None = None
    day_cost: float = 0.0
    day_kwh: float = 0.0
    last_value: float | None = None
    last_tick: datetime | None = None
    untracked_gaps: int = 0
    joulo_base_ere: float | None = None
    joulo_base_kwh: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON serialisable representation."""
        return {
            "total_kwh": self.total_kwh,
            "total_cost": self.total_cost,
            "total_reimbursement": self.total_reimbursement,
            "total_ere_value": self.total_ere_value,
            "day": self.day,
            "day_cost": self.day_cost,
            "day_kwh": self.day_kwh,
            "last_value": self.last_value,
            "last_tick": self.last_tick.isoformat() if self.last_tick else None,
            "untracked_gaps": self.untracked_gaps,
            "joulo_base_ere": self.joulo_base_ere,
            "joulo_base_kwh": self.joulo_base_kwh,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DeviceAccumulators:
        """Restore accumulators from stored data."""
        return cls(
            total_kwh=_as_float(data.get("total_kwh")) or 0.0,
            total_cost=_as_float(data.get("total_cost")) or 0.0,
            total_reimbursement=_as_float(data.get("total_reimbursement")) or 0.0,
            total_ere_value=_as_float(data.get("total_ere_value")) or 0.0,
            day=data.get("day") if isinstance(data.get("day"), str) else None,
            day_cost=_as_float(data.get("day_cost")) or 0.0,
            day_kwh=_as_float(data.get("day_kwh")) or 0.0,
            last_value=_as_float(data.get("last_value")),
            last_tick=_as_datetime(data.get("last_tick")),
            untracked_gaps=int(_as_float(data.get("untracked_gaps")) or 0),
            joulo_base_ere=_as_float(data.get("joulo_base_ere")),
            joulo_base_kwh=_as_float(data.get("joulo_base_kwh")),
        )


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    """Immutable view of a device used to render its sensors."""

    energy_kwh: float
    cost: float
    cost_today: float
    power: float | None
    price: float | None
    spot: float | None
    reimbursement: float
    reimbursement_rate: float | None
    ere_value: float
    ere_credits: float
    ere_rate: float | None
    net_cost: float
    source_available: bool
    source_entity: str | None
    unpriced_kwh: float
    last_update: datetime


class DeviceTracker:
    """Meters one device, integrating energy against the dynamic tariff."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: DeviceConfig,
        state: Mapping[str, Any] | None = None,
    ) -> None:
        """Initialise the tracker and restore persisted accumulators."""
        self.hass = hass
        self.config = config
        self.acc = DeviceAccumulators.from_dict(state or {})
        # A cumulative energy meter is authoritative: it already integrates
        # power, so falling back to the power sensor mid-flight would double
        # count. Power is then only used for the live power sensor.
        self.source_entity = config.energy_entity or config.power_entity
        self._uses_energy_meter = config.energy_entity is not None
        self._power: float | None = None
        self._ere_rate: float | None = None
        self._ere_price: float | None = None
        self._cached_ere_rate: float | None = None
        self._source_ok = True
        self._unpriced_kwh = 0.0

    # -- source reading ----------------------------------------------------
    def _state(self, entity_id: str | None) -> State | None:
        """Return the state object for an entity, ignoring unusable states."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        return state

    def _read_number(self, entity_id: str | None) -> float | None:
        """Read a plain numeric state without unit conversion."""
        state = self._state(entity_id)
        if state is None:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _read_power(self) -> float | None:
        """Read the power entity in watt."""
        state = self._state(self.config.power_entity)
        if state is None:
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) or UNIT_W
        try:
            return PowerConverter.convert(value, unit, UNIT_W)
        except (TypeError, ValueError):
            return value

    def _read_energy(self) -> float | None:
        """Read the energy entity in kWh."""
        state = self._state(self.config.energy_entity)
        if state is None:
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) or UNIT_KWH
        try:
            return EnergyConverter.convert(value, unit, UNIT_KWH)
        except (TypeError, ValueError):
            return value

    def _read_source(self) -> float | None:
        """Read the configured accounting source."""
        return self._read_energy() if self._uses_energy_meter else self._read_power()

    # -- ERE ---------------------------------------------------------------
    def _resolve_ere_rate(self) -> float | None:
        """Return the effective ERE credits per kWh."""
        ere = self.config.ere
        if not ere.enabled:
            return None
        if not ere.uses_joulo:
            return ere.ere_per_kwh

        credits = self._read_number(ere.credits_entity)
        energy = self._read_number(ere.kwh_entity)
        if credits is not None and energy is not None:
            if self.acc.joulo_base_ere is None or self.acc.joulo_base_kwh is None:
                self.acc.joulo_base_ere = credits
                self.acc.joulo_base_kwh = energy
            ratio = ere_ratio(
                credits,
                energy,
                self.acc.joulo_base_ere,
                self.acc.joulo_base_kwh,
                MIN_ERE_RATIO_WINDOW_KWH,
            )
            if ratio is not None:
                self._cached_ere_rate = ratio
                return ratio

        if self._cached_ere_rate is not None:
            return self._cached_ere_rate
        # Not enough Joulo data yet: fall back to the configured manual ratio
        # when the user supplied one.
        return ere.ere_per_kwh or None

    def _resolve_ere_price(self) -> float | None:
        """Return the effective price per ERE in EUR."""
        ere = self.config.ere
        if not ere.enabled:
            return None
        if ere.price_entity:
            live = self._read_number(ere.price_entity)
            if live is not None:
                return live
        return ere.ere_price

    # -- accounting --------------------------------------------------------
    def _roll_day(self, now: datetime) -> None:
        """Reset the daily counters when the local date changes."""
        key = dt_util.as_local(now).date().isoformat()
        if self.acc.day != key:
            self.acc.day = key
            self.acc.day_cost = 0.0
            self.acc.day_kwh = 0.0

    def update(self, now: datetime, series: PriceSeries | None) -> None:
        """Sample the source, integrate energy and price it."""
        now = dt_util.as_utc(now)
        self._roll_day(now)

        power = self._read_power()
        self._power = power
        self._ere_rate = self._resolve_ere_rate()
        self._ere_price = self._resolve_ere_price()

        reading = self._read_source()
        self._source_ok = reading is not None
        if reading is None:
            # Keep the accounting clock where it is: the energy consumed while
            # the source is unavailable is integrated on the next good reading.
            return

        previous_value = self.acc.last_value
        previous_tick = self.acc.last_tick

        self.acc.last_value = reading
        self.acc.last_tick = now

        if previous_value is None or previous_tick is None:
            return

        if self._uses_energy_meter and reading < previous_value:
            _LOGGER.info(
                "%s: energy meter went backwards (%.3f -> %.3f), rebasing",
                self.config.name,
                previous_value,
                reading,
            )
            return

        gap = now - previous_tick
        if gap > MAX_TRACKING_GAP:
            self.acc.untracked_gaps += 1
            _LOGGER.debug(
                "%s: %.0f s tracking gap, energy in the gap is not priced",
                self.config.name,
                gap.total_seconds(),
            )
            return

        if gap.total_seconds() <= 0 or series is None:
            return

        if self._uses_energy_meter:
            delta_kwh = reading - previous_value
        else:
            delta_kwh = (
                (previous_value + reading) / 2 * gap.total_seconds() / 3600 / 1000
            )

        if delta_kwh <= 0:
            return

        self._accrue(previous_tick, now, delta_kwh, series)

    def _accrue(
        self,
        window_start: datetime,
        window_end: datetime,
        delta_kwh: float,
        series: PriceSeries,
    ) -> None:
        """Price ``delta_kwh`` over the window it was consumed in."""
        total_seconds = (window_end - window_start).total_seconds()
        if total_seconds <= 0:
            return

        acc = self.acc
        for seg_start, seg_end, point in series.segments(window_start, window_end):
            weight = (seg_end - seg_start).total_seconds() / total_seconds
            kwh = delta_kwh * weight
            acc.total_kwh += kwh
            acc.day_kwh += kwh

            if point is not None and point.all_in is not None:
                slice_cost = kwh * point.all_in
                acc.total_cost += slice_cost
                acc.day_cost += slice_cost
            else:
                self._unpriced_kwh += kwh

            rate = self.config.reimbursement.rate(point)
            if rate is not None:
                acc.total_reimbursement += kwh * rate

            if self._ere_rate is not None and self._ere_price is not None:
                acc.total_ere_value += kwh * ere_value_per_kwh(
                    self._ere_rate, self._ere_price
                )

    # -- reporting ---------------------------------------------------------
    def snapshot(self, now: datetime, series: PriceSeries | None) -> DeviceSnapshot:
        """Return the current device values."""
        point = series.point_at(now) if series is not None else None
        acc = self.acc
        ere_rate = self._ere_rate if self.config.ere.enabled else None
        return DeviceSnapshot(
            energy_kwh=acc.total_kwh,
            cost=acc.total_cost,
            cost_today=acc.day_cost,
            power=self._power,
            price=point.all_in if point else None,
            spot=point.spot if point else None,
            reimbursement=acc.total_reimbursement,
            reimbursement_rate=self.config.reimbursement.rate(point),
            ere_value=acc.total_ere_value,
            ere_credits=acc.total_kwh * ere_rate if ere_rate else 0.0,
            ere_rate=ere_rate,
            net_cost=acc.total_cost - acc.total_reimbursement - acc.total_ere_value,
            source_available=self._source_ok,
            source_entity=self.source_entity,
            unpriced_kwh=self._unpriced_kwh,
            last_update=dt_util.as_utc(now),
        )
