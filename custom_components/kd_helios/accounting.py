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

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    MAX_PLAUSIBLE_POWER_KW,
    MAX_TRACKING_GAP,
    MIN_ERE_RATIO_WINDOW_KWH,
)
from .models import DeviceConfig
from .prices import PriceSeries
from .sources import read_energy, read_number, read_power
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
    total_ere_credits: float = 0.0
    day: str | None = None
    day_cost: float = 0.0
    day_kwh: float = 0.0
    last_value: float | None = None
    last_tick: datetime | None = None
    #: Entity id the last_value came from, so a swapped source can be detected.
    source: str | None = None
    untracked_gaps: int = 0
    rejected_deltas: int = 0
    #: Energy that was metered but had no tariff. Persisted so it stays visible.
    unpriced_kwh: float = 0.0
    joulo_base_ere: float | None = None
    joulo_base_kwh: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON serialisable representation."""
        return {
            "total_kwh": self.total_kwh,
            "total_cost": self.total_cost,
            "total_reimbursement": self.total_reimbursement,
            "total_ere_value": self.total_ere_value,
            "total_ere_credits": self.total_ere_credits,
            "day": self.day,
            "day_cost": self.day_cost,
            "day_kwh": self.day_kwh,
            "last_value": self.last_value,
            "last_tick": self.last_tick.isoformat() if self.last_tick else None,
            "source": self.source,
            "untracked_gaps": self.untracked_gaps,
            "rejected_deltas": self.rejected_deltas,
            "unpriced_kwh": self.unpriced_kwh,
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
            total_ere_credits=_as_float(data.get("total_ere_credits")) or 0.0,
            day=data.get("day") if isinstance(data.get("day"), str) else None,
            day_cost=_as_float(data.get("day_cost")) or 0.0,
            day_kwh=_as_float(data.get("day_kwh")) or 0.0,
            last_value=_as_float(data.get("last_value")),
            last_tick=_as_datetime(data.get("last_tick")),
            source=data.get("source") if isinstance(data.get("source"), str) else None,
            untracked_gaps=int(_as_float(data.get("untracked_gaps")) or 0),
            rejected_deltas=int(_as_float(data.get("rejected_deltas")) or 0),
            unpriced_kwh=_as_float(data.get("unpriced_kwh")) or 0.0,
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

    # -- source reading ----------------------------------------------------
    def _read_number(self, entity_id: str | None) -> float | None:
        """Read a plain numeric state without unit conversion."""
        return read_number(self.hass, entity_id)

    def _read_power(self) -> float | None:
        """Read the power entity in watt."""
        return read_power(self.hass, self.config.power_entity)

    def _read_energy(self) -> float | None:
        """Read the energy entity in kWh."""
        return read_energy(self.hass, self.config.energy_entity)

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
            elif credits < self.acc.joulo_base_ere or energy < self.acc.joulo_base_kwh:
                # The Joulo counters restarted. Rebase, otherwise the ratio
                # would mix pre- and post-reset readings and drift silently.
                _LOGGER.info(
                    "%s: Joulo counters went backwards, rebasing the ERE ratio",
                    self.config.name,
                )
                self.acc.joulo_base_ere = credits
                self.acc.joulo_base_kwh = energy
                self._cached_ere_rate = None
                return ere.ere_per_kwh or None
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

        if series is None:
            # Nothing can be priced yet. Keep the window open so the energy is
            # priced once a tariff is available again.
            return

        if self.acc.source != self.source_entity:
            if self.acc.source is not None:
                _LOGGER.info(
                    "%s: accounting source changed from %s to %s, rebasing",
                    self.config.name,
                    self.acc.source,
                    self.source_entity,
                )
            self.acc.source = self.source_entity
            self.acc.last_value = reading
            self.acc.last_tick = now
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

        window_seconds = gap.total_seconds()
        if window_seconds <= 0:
            return

        if self._uses_energy_meter:
            delta_kwh = reading - previous_value
        else:
            delta_kwh = (previous_value + reading) / 2 * window_seconds / 3600 / 1000

        if delta_kwh <= 0:
            return

        implied_kw = delta_kwh / (window_seconds / 3600)
        if implied_kw > MAX_PLAUSIBLE_POWER_KW:
            # Swapping a source entity for one with a larger reading, or a
            # sensor that reported 0 while offline and then recovered, must not
            # be billed as if it were real consumption.
            self.acc.rejected_deltas += 1
            _LOGGER.warning(
                "%s: rejected implausible delta of %.3f kWh in %.0f s "
                "(%.1f kW), rebasing",
                self.config.name,
                delta_kwh,
                window_seconds,
                implied_kw,
            )
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
                acc.unpriced_kwh += kwh

            rate = self.config.reimbursement.rate(point)
            if rate is not None:
                acc.total_reimbursement += kwh * rate

            if self._ere_rate is not None:
                # Credits are accrued at the ratio that applied to this kWh, so
                # the credit counter and the euro counter can never disagree.
                credits = kwh * self._ere_rate
                acc.total_ere_credits += credits
                if self._ere_price is not None:
                    acc.total_ere_value += (
                        ere_value_per_kwh(self._ere_rate, self._ere_price) * kwh
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
            ere_credits=acc.total_ere_credits,
            ere_rate=ere_rate,
            net_cost=acc.total_cost - acc.total_reimbursement - acc.total_ere_value,
            source_available=self._source_ok,
            source_entity=self.source_entity,
            unpriced_kwh=acc.unpriced_kwh,
            last_update=dt_util.as_utc(now),
        )
