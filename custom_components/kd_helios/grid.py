"""Grid connection accounting: import, export and saldering.

Sign conventions
----------------
``imported_cost`` is money you pay, ``export_credit`` is money credited back to
you, and both are positive amounts. The grid total is::

    total = imported_cost - export_credit + fixed_cost

Saldering
---------
Net metering nets exported kWh against imported kWh within the calendar year, so
an exported kWh is worth the *import* tariff of the moment it was exported, not
the feed-in price. When export exceeds import over the year only the imported
volume can be netted and the surplus is worth the feed-in price.

Both valuations are accumulated independently and the effective mode picks the
mix, which keeps the number honest without having to decide at export time
whether that kWh will eventually be netted::

    ratio = min(1, imported_kwh / exported_kwh)
    credit = saldered_credit * ratio + feedin_credit * (1 - ratio)

The ratio prorates by volume, so when export exceeds import the split is an
average rather than an exact FIFO match. It is exact for the common case where
a household imports more than it exports.
"""

from __future__ import annotations

import calendar
import logging
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    MAX_PLAUSIBLE_POWER_KW,
    MAX_TRACKING_GAP,
    SALDERING_END_DATE,
)
from .models import GridConfig
from .prices import PriceSeries
from .sources import read_energy, read_power

_LOGGER = logging.getLogger(__name__)

_DATETIME_FIELDS = frozenset({"import_last_tick", "export_last_tick", "fixed_tick"})
_INT_FIELDS = frozenset(
    {"year", "import_gaps", "import_rejected", "export_gaps", "export_rejected"}
)
_STR_FIELDS = frozenset({"import_source", "export_source", "day"})


def feed_in_price(spot: float | None, deduction: float) -> float | None:
    """Return the feed-in price for a spot price and a provider deduction.

    A negative deduction is a bonus, which is how some providers return their
    own purchase fee.
    """
    if spot is None:
        return None
    return spot - deduction


def _days_in_month(when: datetime) -> int:
    """Return the number of days in the month of ``when``."""
    return calendar.monthrange(when.year, when.month)[1]


def _as_datetime(value: Any) -> datetime | None:
    """Coerce a stored value to an aware UTC datetime."""
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    return dt_util.as_utc(parsed) if parsed else None


def _as_float(value: Any) -> float | None:
    """Coerce a stored value to float."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class GridAccumulators:
    """Persisted year-to-date grid ledger and per-direction baselines."""

    #: Billing year these totals belong to.
    year: int | None = None
    imported_kwh: float = 0.0
    imported_cost: float = 0.0
    exported_kwh: float = 0.0
    #: Export valued at the import tariff, as saldering does.
    export_credit_saldered: float = 0.0
    #: Export valued at the feed-in price, as it is paid after saldering ends.
    export_credit_feedin: float = 0.0
    fixed_cost: float = 0.0
    #: Energy that was metered but had no tariff to price it against.
    unpriced_kwh: float = 0.0

    # Today, for the dashboard. The ledger itself is yearly.
    day: str | None = None
    imported_kwh_today: float = 0.0
    imported_cost_today: float = 0.0
    exported_kwh_today: float = 0.0
    export_credit_today: float = 0.0
    fixed_cost_today: float = 0.0

    import_source: str | None = None
    import_last_value: float | None = None
    import_last_tick: datetime | None = None
    import_gaps: int = 0
    import_rejected: int = 0

    export_source: str | None = None
    export_last_value: float | None = None
    export_last_tick: datetime | None = None
    export_gaps: int = 0
    export_rejected: int = 0

    fixed_tick: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON serialisable representation."""
        data: dict[str, Any] = {}
        for field in fields(self):
            value = getattr(self, field.name)
            data[field.name] = (
                value.isoformat() if isinstance(value, datetime) else value
            )
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GridAccumulators:
        """Restore the ledger from stored data."""
        values: dict[str, Any] = {}
        for field in fields(cls):
            raw = data.get(field.name)
            if field.name in _DATETIME_FIELDS:
                values[field.name] = _as_datetime(raw)
            elif field.name in _INT_FIELDS:
                values[field.name] = (
                    int(_as_float(raw))
                    if _as_float(raw) is not None
                    else None
                    if field.name == "year"
                    else 0
                )
            elif field.name in _STR_FIELDS:
                values[field.name] = raw if isinstance(raw, str) else None
            else:
                values[field.name] = _as_float(raw) or 0.0
        return cls(**values)


@dataclass(frozen=True, slots=True)
class GridSnapshot:
    """Immutable view of the grid connection for one accounting tick."""

    year: int
    imported_kwh: float
    imported_cost: float
    exported_kwh: float
    export_credit: float
    fixed_cost: float
    total_cost: float
    imported_kwh_today: float
    imported_cost_today: float
    exported_kwh_today: float
    export_credit_today: float
    fixed_cost_today: float
    total_cost_today: float
    netted_kwh: float
    open_export_kwh: float
    import_power: float | None
    export_power: float | None
    net_power: float | None
    import_price: float | None
    spot_price: float | None
    export_price: float | None
    saldering_enabled: bool
    saldering_active: bool
    export_deduction: float
    fixed_monthly: float
    unpriced_kwh: float
    untracked_gaps: int
    rejected_deltas: int
    last_update: datetime


class GridTracker:
    """Meters the grid connection and keeps the annual saldering ledger."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: GridConfig,
        state: Mapping[str, Any] | None = None,
    ) -> None:
        """Initialise the tracker and restore the ledger."""
        self.hass = hass
        self.config = config
        self.acc = GridAccumulators.from_dict(state or {})
        self._import_power: float | None = None
        self._export_power: float | None = None
        self._net_power: float | None = None
        self._import_price: float | None = None
        self._spot: float | None = None
        self._export_price: float | None = None

    # -- mode --------------------------------------------------------------
    def saldering_active(self, when: datetime) -> bool:
        """Return whether net metering applies on ``when``.

        The user's toggle is honoured until saldering legally ends; from
        :data:`SALDERING_END_DATE` the connection always settles at the feed-in
        price, whatever the stored preference says.
        """
        if not self.config.saldering:
            return False
        return dt_util.as_local(when).date() < SALDERING_END_DATE

    # -- year ledger -------------------------------------------------------
    def _roll_day(self, now: datetime) -> None:
        """Reset the daily counters when the local date changes."""
        key = dt_util.as_local(now).date().isoformat()
        if self.acc.day == key:
            return
        self.acc.day = key
        self.acc.imported_kwh_today = 0.0
        self.acc.imported_cost_today = 0.0
        self.acc.exported_kwh_today = 0.0
        self.acc.export_credit_today = 0.0
        self.acc.fixed_cost_today = 0.0

    def _roll_year(self, now: datetime) -> None:
        """Reset the billing ledger when the local year changes."""
        year = dt_util.as_local(now).year
        if self.acc.year == year:
            return
        if self.acc.year is not None:
            _LOGGER.info(
                "Grid: closing billing year %s, starting %s", self.acc.year, year
            )
        self.acc.year = year
        self.acc.imported_kwh = 0.0
        self.acc.imported_cost = 0.0
        self.acc.exported_kwh = 0.0
        self.acc.export_credit_saldered = 0.0
        self.acc.export_credit_feedin = 0.0
        self.acc.fixed_cost = 0.0
        self.acc.unpriced_kwh = 0.0

    # -- fixed costs -------------------------------------------------------
    def _accrue_fixed(self, now: datetime) -> None:
        """Accrue the monthly fixed costs over the elapsed time."""
        monthly = self.config.supply_cost_monthly + self.config.grid_fee_monthly
        previous = self.acc.fixed_tick
        self.acc.fixed_tick = now
        if monthly <= 0 or previous is None:
            return
        elapsed = (now - previous).total_seconds()
        if elapsed <= 0 or now - previous > MAX_TRACKING_GAP:
            return
        days = _days_in_month(dt_util.as_local(now))
        share = monthly / (days * 86400) * elapsed
        self.acc.fixed_cost += share
        self.acc.fixed_cost_today += share

    # -- source sampling ---------------------------------------------------
    def _consume(self, direction: str, now: datetime) -> tuple[datetime, float] | None:
        """Return the window and delta kWh for one direction.

        ``None`` means nothing to book: no source, no reading, a rebase, or the
        window is not trustworthy.
        """
        acc = self.acc
        energy_entity: str | None = getattr(self.config, f"{direction}_entity")
        power_entity: str | None = getattr(self.config, f"{direction}_power_entity")
        uses_energy_meter = energy_entity is not None
        entity = energy_entity or power_entity
        if entity is None:
            return None

        reading = (
            read_energy(self.hass, entity)
            if uses_energy_meter
            else read_power(self.hass, entity)
        )
        if reading is None:
            return None

        source_attr = f"{direction}_source"
        if getattr(acc, source_attr) != entity:
            if getattr(acc, source_attr) is not None:
                _LOGGER.info(
                    "Grid %s: source changed to %s, rebasing", direction, entity
                )
            setattr(acc, source_attr, entity)
            setattr(acc, f"{direction}_last_value", reading)
            setattr(acc, f"{direction}_last_tick", now)
            return None

        previous = getattr(acc, f"{direction}_last_value")
        previous_tick = getattr(acc, f"{direction}_last_tick")
        setattr(acc, f"{direction}_last_value", reading)
        setattr(acc, f"{direction}_last_tick", now)

        if previous is None or previous_tick is None:
            return None

        if uses_energy_meter and reading < previous:
            _LOGGER.info(
                "Grid %s: meter went backwards (%.3f -> %.3f), rebasing",
                direction,
                previous,
                reading,
            )
            return None

        gap = now - previous_tick
        if gap > MAX_TRACKING_GAP:
            setattr(acc, f"{direction}_gaps", getattr(acc, f"{direction}_gaps") + 1)
            _LOGGER.debug(
                "Grid %s: %.0f s tracking gap, energy in the gap is not priced",
                direction,
                gap.total_seconds(),
            )
            return None

        seconds = gap.total_seconds()
        if seconds <= 0:
            return None

        if uses_energy_meter:
            delta_kwh = reading - previous
        else:
            delta_kwh = (previous + reading) / 2 * seconds / 3600 / 1000

        if delta_kwh <= 0:
            return None

        implied_kw = delta_kwh / (seconds / 3600)
        if implied_kw > MAX_PLAUSIBLE_POWER_KW:
            setattr(
                acc, f"{direction}_rejected", getattr(acc, f"{direction}_rejected") + 1
            )
            _LOGGER.warning(
                "Grid %s: rejected implausible delta of %.3f kWh in %.0f s "
                "(%.1f kW), rebasing",
                direction,
                delta_kwh,
                seconds,
                implied_kw,
            )
            return None

        return previous_tick, delta_kwh

    def _track_import(self, now: datetime, series: PriceSeries) -> None:
        """Book imported energy at the import tariff."""
        window = self._consume("import", now)
        if window is None:
            return
        start, delta_kwh = window
        total_seconds = (now - start).total_seconds()
        if total_seconds <= 0:
            return
        for seg_start, seg_end, point in series.segments(start, now):
            weight = (seg_end - seg_start).total_seconds() / total_seconds
            kwh = delta_kwh * weight
            self.acc.imported_kwh += kwh
            self.acc.imported_kwh_today += kwh
            if point is not None and point.all_in is not None:
                self.acc.imported_cost += kwh * point.all_in
                self.acc.imported_cost_today += kwh * point.all_in
            else:
                self.acc.unpriced_kwh += kwh

    def _track_export(self, now: datetime, series: PriceSeries) -> None:
        """Book exported energy under both valuations."""
        window = self._consume("export", now)
        if window is None:
            return
        start, delta_kwh = window
        total_seconds = (now - start).total_seconds()
        if total_seconds <= 0:
            return
        deduction = self.config.export_deduction
        for seg_start, seg_end, point in series.segments(start, now):
            weight = (seg_end - seg_start).total_seconds() / total_seconds
            kwh = delta_kwh * weight
            self.acc.exported_kwh += kwh
            self.acc.exported_kwh_today += kwh
            priced = False
            if point is not None and point.all_in is not None:
                self.acc.export_credit_saldered += kwh * point.all_in
                self.acc.export_credit_today += kwh * point.all_in
                priced = True
            if point is not None:
                export_price = feed_in_price(point.spot, deduction)
                if export_price is not None:
                    self.acc.export_credit_feedin += kwh * export_price
                    priced = True
            if not priced:
                self.acc.unpriced_kwh += kwh

    def _read_power(self) -> tuple[float | None, float | None, float | None]:
        """Return import power, export power and signed net power.

        A P1 meter reports one signed value: positive imports, negative
        exports. Separate import and export sensors are used as they are, with
        negative readings clamped away because they would be a sign error.
        """
        signed_entity = self.config.signed_power_entity
        if signed_entity is not None:
            raw = read_power(self.hass, signed_entity)
            if raw is None:
                return None, None, None
            return max(0.0, raw), max(0.0, -raw), raw

        raw_import = read_power(self.hass, self.config.import_power_entity)
        raw_export = read_power(self.hass, self.config.export_power_entity)
        import_power = max(0.0, raw_import) if raw_import is not None else None
        export_power = max(0.0, raw_export) if raw_export is not None else None
        net_power: float | None = None
        if import_power is not None and export_power is not None:
            net_power = import_power - export_power
        return import_power, export_power, net_power

    # -- tick --------------------------------------------------------------
    def update(self, now: datetime, series: PriceSeries | None) -> None:
        """Run one accounting tick for the grid connection."""
        now = dt_util.as_utc(now)
        self._roll_year(now)
        self._roll_day(now)

        if series is not None:
            point = series.point_at(now)
            self._import_price = point.all_in if point else None
            self._spot = point.spot if point else None
            self._export_price = feed_in_price(self._spot, self.config.export_deduction)

        self._import_power, self._export_power, self._net_power = self._read_power()

        self._accrue_fixed(now)

        if series is None:
            return

        self._track_import(now, series)
        self._track_export(now, series)

    # -- reporting ---------------------------------------------------------
    def _export_credit(self, active: bool) -> tuple[float, float, float]:
        """Return credit, netted kWh and open export kWh."""
        acc = self.acc
        exported = acc.exported_kwh
        if not active:
            return acc.export_credit_feedin, 0.0, 0.0
        if exported <= 0:
            return 0.0, 0.0, 0.0
        ratio = min(1.0, acc.imported_kwh / exported)
        credit = acc.export_credit_saldered * ratio + acc.export_credit_feedin * (
            1.0 - ratio
        )
        return (
            credit,
            min(acc.imported_kwh, exported),
            max(0.0, exported - acc.imported_kwh),
        )

    def snapshot(self, now: datetime, series: PriceSeries | None) -> GridSnapshot:
        """Return the current grid values."""
        now = dt_util.as_utc(now)
        active = self.saldering_active(now)
        credit, netted_kwh, open_export_kwh = self._export_credit(active)
        acc = self.acc
        return GridSnapshot(
            year=acc.year or dt_util.as_local(now).year,
            imported_kwh=acc.imported_kwh,
            imported_cost=acc.imported_cost,
            exported_kwh=acc.exported_kwh,
            export_credit=credit,
            fixed_cost=acc.fixed_cost,
            total_cost=acc.imported_cost - credit + acc.fixed_cost,
            imported_kwh_today=acc.imported_kwh_today,
            imported_cost_today=acc.imported_cost_today,
            exported_kwh_today=acc.exported_kwh_today,
            export_credit_today=acc.export_credit_today,
            fixed_cost_today=acc.fixed_cost_today,
            total_cost_today=(
                acc.imported_cost_today - acc.export_credit_today + acc.fixed_cost_today
            ),
            netted_kwh=netted_kwh,
            open_export_kwh=open_export_kwh,
            import_power=self._import_power,
            export_power=self._export_power,
            net_power=self._net_power,
            import_price=self._import_price,
            spot_price=self._spot,
            export_price=self._export_price,
            saldering_enabled=self.config.saldering,
            saldering_active=active,
            export_deduction=self.config.export_deduction,
            fixed_monthly=(
                self.config.supply_cost_monthly + self.config.grid_fee_monthly
            ),
            unpriced_kwh=acc.unpriced_kwh,
            untracked_gaps=acc.import_gaps + acc.export_gaps,
            rejected_deltas=acc.import_rejected + acc.export_rejected,
            last_update=now,
        )
