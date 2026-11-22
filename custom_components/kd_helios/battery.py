"""Battery charge planning and Deye time-of-use programs.

The inverter (Deye SUN-*K-SG04LP3 / SG05LP3 family) has six time-of-use slots.
Each slot has a start time, a power, a SOC target and a charge source, and stays
active until the next slot starts. Grid charging happens in a slot when its
source includes the grid and the battery is below that slot's SOC target; in a
slot whose source is off, the inverter serves the house from the battery down to
the slot's SOC target.

That means the optimisation is only about **when to buy** energy. Pick the cheap
hours, charge to the target there, and leave the rest of the day at the reserve
SOC so the battery discharges into the expensive hours on its own.

Everything here is side-effect free: no Home Assistant state, no Modbus.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import IntEnum

from homeassistant.util import dt as dt_util

from .caps import apply_caps, charge_caps
from .const import (
    DEFAULT_TEMP_CHARGE_LIMIT_BAND_C,
    DEFAULT_TEMP_CHARGE_LIMIT_C,
    DEFAULT_TEMP_CHARGE_LIMIT_FLOOR_PCT,
    Priority,
)
from .decisions import SOURCE_PLAN, Blocker
from .prices import PriceSeries

_LOGGER = logging.getLogger(__name__)

#: Deye holding registers of the six time-of-use slots. Source: Deye "MODBUS
#: RTU three-phase energy storage communication protocol", which documents the
#: SG04LP3/SG05LP3 family.
TOU_REGISTERS: dict[str, int] = {
    "times": 148,
    "power": 154,
    "soc": 166,
    "sources": 172,
}

#: Number of time-of-use slots in the inverter.
TOU_SLOTS = 6

#: Holding register of the inverter's work mode.
WORK_MODE_REGISTER = 142

#: Master switch for charging the battery from the grid. The TOU source alone
#: is not enough: with this off, the inverter ignores every grid source.
GRID_CHARGE_REGISTER = 130

#: Battery grid-charging current. On a Deye this, not the TOU power block,
#: sets how fast the battery charges from the grid.
GRID_CHARGE_CURRENT_REGISTER = 128

#: The inverter accepts 0..185 A on that register.
GRID_CHARGE_CURRENT_MAX = 240

#: Nominal pack voltage used when no battery voltage sensor is configured.
DEFAULT_BATTERY_VOLTAGE = 51.2

#: Weekly bitmask that enables the TOU schedule. 0x00FF enables every day.
TOU_ENABLE_REGISTER = 146
TOU_ENABLE_ALL_DAYS = 0x00FF

#: Start times of the six slots used when releasing control: they only ever
#: charge from the generator, so the grid is never used behind your back.
RESTORE_SLOT_MINUTES = (0, 240, 480, 720, 960, 1200)

#: At most one automatic write per this interval, to spare the Modbus link.
AUTO_APPLY_MIN_INTERVAL = timedelta(minutes=10)

#: Prices within this fraction of the highest later price count as the same
#: expensive period. Quarter-hour tariffs can spike for one slot; sizing the
#: discharge budget on that slot alone would cap a whole plan to 15 minutes.
PEAK_PLATEAU_RATIO = 0.9

#: A drifting charge start smaller than this is not worth rewriting for.
AUTO_APPLY_TOLERANCE = timedelta(minutes=5)

#: A slot cannot be shorter than this; minute resolution matches the HHMM field.
MIN_SLOT = timedelta(minutes=1)

#: Expected house energy between two instants, in kWh, or None when unknown.
LoadForecast = Callable[[datetime, datetime], float | None]


class ChargeSource(IntEnum):
    """Charge sources the inverter accepts per time-of-use slot."""

    OFF = 0
    GRID = 1
    GENERATOR = 2
    GRID_AND_GENERATOR = 3


class WorkMode(IntEnum):
    """Work modes the inverter accepts."""

    SELLING_FIRST = 0
    ZERO_EXPORT_TO_LOAD = 1
    ZERO_EXPORT_TO_CT = 2


@dataclass(frozen=True, slots=True)
class BatterySettings:
    """Battery and inverter parameters needed to plan a charge."""

    capacity_kwh: float
    max_charge_kw: float
    reserve_soc_pct: float = 10.0
    target_soc_pct: float = 100.0
    #: Round-trip efficiency in percent.
    efficiency_pct: float = 90.0
    horizon_hours: int = 36

    @property
    def efficiency(self) -> float:
        """Return the round-trip efficiency as a fraction."""
        return max(0.0, min(1.0, self.efficiency_pct / 100.0))

    @property
    def usable_kwh(self) -> float:
        """Return the energy between reserve and target."""
        span = max(0.0, self.target_soc_pct - self.reserve_soc_pct)
        return self.capacity_kwh * span / 100.0


@dataclass(frozen=True, slots=True)
class ChargeWindow:
    """A contiguous period worth buying energy in."""

    start: datetime
    end: datetime
    energy_kwh: float
    cost: float
    value: float
    profit: float
    average_price: float
    discharge_price: float
    #: Part of ``energy_kwh`` the house is expected to consume before the peak
    #: it was bought for. That energy saves the tariff of the hours it is used
    #: in, not the peak price, so counting it at the peak overstates the saving.
    drain_kwh: float = 0.0

    @property
    def discharge_kwh(self) -> float:
        """Return the energy still stored when the expensive hours arrive."""
        return max(0.0, self.energy_kwh - self.drain_kwh)


@dataclass(frozen=True, slots=True)
class ChargePlan:
    """What to buy, when, and what it is expected to be worth."""

    generated_at: datetime
    soc_pct: float | None
    windows: tuple[ChargeWindow, ...] = ()
    energy_kwh: float = 0.0
    cost: float = 0.0
    expected_saving: float = 0.0
    reason: str = "planned"
    #: Every condition that shaped this plan, including the ones that lost.
    blockers: tuple[Blocker, ...] = ()
    #: Energy the sun is expected to deliver, so not bought.
    solar_credit_kwh: float = 0.0
    #: True when the prices came from the last good fetch rather than this one.
    prices_stale: bool = False

    @property
    def has_windows(self) -> bool:
        """Return True when there is something worth charging."""
        return bool(self.windows)

    def active_window(self, when: datetime) -> ChargeWindow | None:
        """Return the window covering ``when``, if any."""
        when = dt_util.as_utc(when)
        for window in self.windows:
            if window.start <= when < window.end:
                return window
        return None

    def next_window(self, when: datetime) -> ChargeWindow | None:
        """Return the next window starting after ``when``."""
        when = dt_util.as_utc(when)
        upcoming = [window for window in self.windows if window.start > when]
        return upcoming[0] if upcoming else None


@dataclass(frozen=True, slots=True)
class TouProgram:
    """One inverter time-of-use slot."""

    start_minute: int
    power_w: int
    soc_pct: int
    source: ChargeSource

    @property
    def hhmm(self) -> int:
        """Return the start time in the HHMM form the inverter expects."""
        return (self.start_minute // 60) * 100 + self.start_minute % 60


@dataclass(frozen=True, slots=True)
class TouPlan:
    """The six programs to write, plus what had to be dropped."""

    programs: tuple[TouProgram, ...]
    truncated: bool = False

    def registers(self) -> dict[int, list[int]]:
        """Return register address to values for a multi-register write."""
        return {
            TOU_REGISTERS["times"]: [program.hhmm for program in self.programs],
            TOU_REGISTERS["power"]: [program.power_w for program in self.programs],
            TOU_REGISTERS["soc"]: [program.soc_pct for program in self.programs],
            TOU_REGISTERS["sources"]: [
                int(program.source) for program in self.programs
            ],
        }


@dataclass(frozen=True, slots=True)
class ControlPlan:
    """The state Deye should hold now, written to all six TOU slots.

    Deye applies a change to the six program values immediately, but a change
    to their start times is only picked up after a manual confirmation on the
    inverter. The controller therefore leaves the times alone and gives every
    slot the same SOC, power and charging state. Time of Use stays enabled and
    the slots are fixed around the day, so whichever slot is active does the
    same thing.
    """

    programs: tuple[TouProgram, ...]
    charge_current_a: int
    work_mode: WorkMode = WorkMode.ZERO_EXPORT_TO_CT

    def registers(self) -> dict[int, list[int]]:
        """Return the register blocks to write, without the static times."""
        return {
            TOU_ENABLE_REGISTER: [TOU_ENABLE_ALL_DAYS],
            WORK_MODE_REGISTER: [int(self.work_mode)],
            GRID_CHARGE_REGISTER: [1],
            GRID_CHARGE_CURRENT_REGISTER: [self.charge_current_a],
            TOU_REGISTERS["power"]: [program.power_w for program in self.programs],
            TOU_REGISTERS["soc"]: [program.soc_pct for program in self.programs],
            TOU_REGISTERS["sources"]: [
                int(program.source) for program in self.programs
            ],
        }


@dataclass(frozen=True, slots=True)
class AppliedPlan:
    """The plan that was actually written to the inverter.

    The live plan is recomputed on every tick, so it drifts as prices and the
    state of charge move. This is the snapshot that is in force on the inverter,
    kept so the saving it was expected to deliver stays auditable instead of
    being overwritten a minute later.
    """

    applied_at: datetime
    soc_pct: float | None
    energy_kwh: float
    cost: float
    expected_saving: float
    truncated: bool
    programs: tuple[TouProgram, ...]
    windows: tuple[ChargeWindow, ...]

    @classmethod
    def from_plan(
        cls,
        plan: ChargePlan,
        tou: TouPlan,
        applied_at: datetime,
    ) -> AppliedPlan:
        """Snapshot a plan and the programs derived from it."""
        return cls(
            applied_at=dt_util.as_utc(applied_at),
            soc_pct=plan.soc_pct,
            energy_kwh=plan.energy_kwh,
            cost=plan.cost,
            expected_saving=plan.expected_saving,
            truncated=tou.truncated,
            programs=tou.programs,
            windows=plan.windows,
        )

    def as_dict(self) -> dict[str, object]:
        """Return a JSON serialisable representation."""
        return {
            "applied_at": self.applied_at.isoformat(),
            "soc_pct": self.soc_pct,
            "energy_kwh": self.energy_kwh,
            "cost": self.cost,
            "expected_saving": self.expected_saving,
            "truncated": self.truncated,
            "programs": [
                {
                    "start": program.hhmm,
                    "power_w": program.power_w,
                    "soc_pct": program.soc_pct,
                    "source": int(program.source),
                }
                for program in self.programs
            ],
            "windows": [
                {
                    "start": window.start.isoformat(),
                    "end": window.end.isoformat(),
                    "energy_kwh": window.energy_kwh,
                    "cost": window.cost,
                    "value": window.value,
                    "average_price": window.average_price,
                    "discharge_price": window.discharge_price,
                    "expected_profit": window.profit,
                }
                for window in self.windows
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object] | None) -> AppliedPlan | None:
        """Restore a snapshot, returning None when there is nothing usable."""
        if not data:
            return None
        applied_at = _parse_datetime(data.get("applied_at"))
        if applied_at is None:
            return None

        programs: list[TouProgram] = []
        for raw in data.get("programs") or []:
            if not isinstance(raw, Mapping):
                continue
            start = _as_int(raw.get("start"))
            if start is None:
                continue
            programs.append(
                TouProgram(
                    start_minute=(start // 100) * 60 + start % 100,
                    power_w=_as_int(raw.get("power_w")) or 0,
                    soc_pct=_as_int(raw.get("soc_pct")) or 0,
                    source=ChargeSource(_as_int(raw.get("source")) or 0),
                )
            )

        windows: list[ChargeWindow] = []
        for raw in data.get("windows") or []:
            if not isinstance(raw, Mapping):
                continue
            start = _parse_datetime(raw.get("start"))
            end = _parse_datetime(raw.get("end"))
            if start is None or end is None:
                continue
            profit = _as_float(raw.get("expected_profit")) or 0.0
            energy = _as_float(raw.get("energy_kwh")) or 0.0
            cost = _as_float(raw.get("cost")) or 0.0
            value = _as_float(raw.get("value")) or (cost + profit)
            windows.append(
                ChargeWindow(
                    start=start,
                    end=end,
                    energy_kwh=energy,
                    cost=cost,
                    value=value,
                    profit=profit,
                    average_price=_as_float(raw.get("average_price")) or 0.0,
                    discharge_price=_as_float(raw.get("discharge_price")) or 0.0,
                )
            )

        return cls(
            applied_at=applied_at,
            soc_pct=_as_float(data.get("soc_pct")),
            energy_kwh=_as_float(data.get("energy_kwh")) or 0.0,
            cost=_as_float(data.get("cost")) or 0.0,
            expected_saving=_as_float(data.get("expected_saving")) or 0.0,
            truncated=bool(data.get("truncated")),
            programs=tuple(programs),
            windows=tuple(windows),
        )

    def active_window(self, when: datetime) -> ChargeWindow | None:
        """Return the window of the applied plan covering ``when``."""
        when = dt_util.as_utc(when)
        for window in self.windows:
            if window.start <= when < window.end:
                return window
        return None


def _parse_datetime(value: object) -> datetime | None:
    """Parse a stored timestamp."""
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    return dt_util.as_utc(parsed) if parsed else None


def _as_float(value: object) -> float | None:
    """Coerce a stored value to float."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    """Coerce a stored value to int."""
    number = _as_float(value)
    return int(number) if number is not None else None


def _minute_of_day(when: datetime) -> int:
    """Return the local minute of the day, 0..1439."""
    local = dt_util.as_local(dt_util.as_utc(when))
    return local.hour * 60 + local.minute


def _plan_reason(
    windows: tuple[ChargeWindow, ...],
    blockers: tuple[Blocker, ...],
) -> str:
    """Return the one-word reason shown to the user.

    A plan with windows succeeds; otherwise the first blocker explains why not.
    The full list stays available in ``ChargePlan.blockers``; this keeps the
    long-standing single-string contract of the sensor and the panel.
    """
    if windows:
        return "planned"
    for blocker in blockers:
        return blocker.reason
    return "planned"


def _empty(
    now: datetime,
    soc: float | None,
    blockers: tuple[Blocker, ...] = (),
    *,
    solar_credit_kwh: float = 0.0,
    prices_stale: bool = False,
) -> ChargePlan:
    """Return a plan that buys nothing, with the reasons why."""
    return ChargePlan(
        generated_at=now,
        soc_pct=soc,
        reason=_plan_reason((), blockers),
        blockers=tuple(blockers),
        solar_credit_kwh=solar_credit_kwh,
        prices_stale=prices_stale,
    )


def plan_charge(
    series: PriceSeries,
    settings: BatterySettings,
    now: datetime,
    soc_pct: float | None,
    *,
    load_forecast: LoadForecast | None = None,
    solar_credit_kwh: float = 0.0,
    max_discharge_kw: float = 0.0,
    stale_prices: bool = False,
    suppressed: tuple[datetime, datetime] | None = None,
) -> ChargePlan:
    """Return the cheapest way to fill the battery over the horizon.

    Each candidate slot is worth the highest price that comes *after* it, times
    the round-trip efficiency: that is the import it displaces when the battery
    discharges into the expensive hours. Slots whose spread does not cover the
    losses are not bought at all.

    Three refinements keep the estimate honest rather than merely optimistic:

    * ``solar_credit_kwh`` is energy the sun is expected to deliver, so it is
      subtracted from what the plan buys.
    * ``load_forecast`` says how much the house will draw between a window and
      the peak it is bought for. That energy still displaces import, but at the
      prices of the hours it is used in, not at the peak price ``drain_kwh``.
    * ``max_discharge_kw`` caps what can leave the battery in the peak, so a
      slow inverter is not credited with a peak it cannot serve.

    ``suppressed`` is an interval the battery must not charge in (the car's
    window). Buying and spending energy inside one interval only pays the
    round-trip losses, so those candidates are dropped and the conflict is
    reported rather than silently planned.
    """
    now = dt_util.as_utc(now)
    blockers: list[Blocker] = []

    if soc_pct is None:
        return _empty(
            now,
            None,
            (Blocker(SOURCE_PLAN, "no_soc"),),
            prices_stale=stale_prices,
        )

    horizon_end = now + timedelta(hours=max(1, settings.horizon_hours))
    slots = series.priced_slots(now, horizon_end)
    if not slots:
        reason = "prices_stale" if stale_prices else "no_prices"
        return _empty(
            now,
            soc_pct,
            (Blocker(SOURCE_PLAN, reason),),
            prices_stale=stale_prices,
        )

    efficiency = settings.efficiency
    energy_needed = settings.capacity_kwh * (settings.target_soc_pct - soc_pct) / 100.0
    if energy_needed <= 1e-6:
        return _empty(
            now,
            soc_pct,
            (Blocker(SOURCE_PLAN, "full"),),
            prices_stale=stale_prices,
        )

    credit = max(0.0, min(float(solar_credit_kwh), energy_needed))
    shortfall = energy_needed - credit
    if shortfall <= 1e-6:
        return _empty(
            now,
            soc_pct,
            (
                Blocker(
                    SOURCE_PLAN,
                    "waiting_for_solar",
                    {"needed_kwh": round(energy_needed, 2)},
                ),
            ),
            solar_credit_kwh=credit,
            prices_stale=stale_prices,
        )

    prices = [price for _, _, price in slots]

    candidates: list[_Candidate] = []
    suppressed_slots = 0
    for index, (start, end, price) in enumerate(slots):
        later = prices[index + 1 :]
        if not later:
            continue
        peak_price = max(later)
        if peak_price * efficiency - price <= 0:
            continue
        hours = (end - start).total_seconds() / 3600.0
        if hours <= 0:
            continue
        if suppressed is not None and _overlaps(
            start, end, suppressed[0], suppressed[1]
        ):
            suppressed_slots += 1
            continue
        max_kwh = min(settings.max_charge_kw * hours, settings.capacity_kwh)
        if max_kwh <= 0:
            continue

        peak_index = index + 1 + later.index(peak_price)
        peak_start = slots[peak_index][0]
        # What the battery can serve is the expensive period, not one slot in
        # it: a volatile quarter-hour price can spike once and fall back to
        # prices that are still within a tenth of it. Count every later slot
        # in that band, so the plan is not capped to the spike's 15 minutes.
        threshold = peak_price * PEAK_PLATEAU_RATIO
        peak_hours = sum(
            max(0.0, (slot_end - slot_start).total_seconds() / 3600.0)
            for slot_start, slot_end, slot_price in slots[index + 1 :]
            if slot_price >= threshold
        )
        candidates.append(
            _Candidate(
                start=start,
                end=end,
                price=price,
                peak_price=peak_price,
                kwh=max_kwh,
                peak_hours=peak_hours,
                peak_start=peak_start,
            )
        )

    if suppressed_slots:
        blockers.append(
            Blocker(SOURCE_PLAN, "hold_conflict", {"slots": suppressed_slots})
        )

    if not candidates:
        if not suppressed_slots:
            blockers.append(Blocker(SOURCE_PLAN, "no_spread"))
        return _empty(
            now,
            soc_pct,
            tuple(blockers),
            solar_credit_kwh=credit,
            prices_stale=stale_prices,
        )

    candidates.sort(key=lambda item: item.profit(efficiency), reverse=True)

    remaining = shortfall
    chosen: list[_Candidate] = []
    # One budget per peak run: several cheap slots can feed the same expensive
    # evening, and the peak can only absorb max_discharge_kw for its length.
    budgets: dict[datetime, float] = {}
    for candidate in candidates:
        if remaining <= 1e-9:
            break
        kwh = min(candidate.kwh, remaining)
        if kwh <= 0:
            continue
        if max_discharge_kw > 0 and candidate.peak_hours > 0:
            budget = budgets.setdefault(
                candidate.peak_start,
                max_discharge_kw * candidate.peak_hours,
            )
            # The peak cannot absorb more than this, so do not buy more: an
            # overfilled battery is energy bought and never returned.
            kwh = min(kwh, budget)
            budgets[candidate.peak_start] = max(0.0, budget - kwh)
        if kwh <= 0:
            continue
        chosen.append(
            _Candidate(
                start=candidate.start,
                end=candidate.end,
                price=candidate.price,
                peak_price=candidate.peak_price,
                kwh=kwh,
                peak_hours=candidate.peak_hours,
                peak_start=candidate.peak_start,
            )
        )
        remaining -= kwh

    if not chosen:
        blockers.append(Blocker(SOURCE_PLAN, "no_spread"))
        return _empty(
            now,
            soc_pct,
            tuple(blockers),
            solar_credit_kwh=credit,
            prices_stale=stale_prices,
        )

    chosen.sort(key=lambda item: item.start)
    windows = _windows_from_candidates(chosen, efficiency, slots, load_forecast)
    total_cost = sum(window.cost for window in windows)
    total_value = sum(window.value for window in windows)
    return ChargePlan(
        generated_at=now,
        soc_pct=soc_pct,
        windows=tuple(windows),
        energy_kwh=sum(window.energy_kwh for window in windows),
        cost=total_cost,
        expected_saving=total_value - total_cost,
        reason="planned",
        blockers=tuple(blockers),
        solar_credit_kwh=credit,
        prices_stale=stale_prices,
    )


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One priced slot being considered for a charge."""

    start: datetime
    end: datetime
    price: float
    peak_price: float
    kwh: float
    peak_hours: float
    peak_start: datetime

    @property
    def cost(self) -> float:
        """Return what the energy costs to buy."""
        return self.kwh * self.price

    def profit(self, efficiency: float) -> float:
        """Return what the energy is expected to earn at the peak."""
        return self.kwh * self.peak_price * efficiency - self.cost


def _overlaps(
    start: datetime,
    end: datetime,
    other_start: datetime,
    other_end: datetime,
) -> bool:
    """Return True when two half-open intervals share any time."""
    return start < other_end and other_start < end


def _expected_drain(
    load_forecast: LoadForecast | None,
    from_when: datetime,
    until_when: datetime,
    slots: list[tuple[datetime, datetime, float]],
    kwh: float,
    peak_price: float,
) -> tuple[float, float]:
    """Return the house energy expected before the peak, and at what price.

    The drain belongs to the window as a whole, not to each quarter hour of it:
    charging earlier in the same window does not make the house consume twice.
    Without a forecast there is nothing to say, and the plan keeps valuing
    everything at the peak exactly as it did before.
    """
    if load_forecast is None or until_when <= from_when:
        return 0.0, peak_price
    try:
        energy = load_forecast(from_when, until_when)
    except Exception:  # noqa: BLE001 - a broken forecast must not stop planning
        _LOGGER.debug("Load forecast failed; valuing at the peak price")
        return 0.0, peak_price
    if energy is None:
        return 0.0, peak_price
    try:
        energy = max(0.0, float(energy))
    except (TypeError, ValueError):
        return 0.0, peak_price
    if energy <= 0.0:
        return 0.0, peak_price

    between = [
        price for start, end, price in slots if start >= from_when and end <= until_when
    ]
    price = sum(between) / len(between) if between else peak_price
    return min(energy, kwh), price


def _windows_from_candidates(
    chosen: list[_Candidate],
    efficiency: float,
    slots: list[tuple[datetime, datetime, float]],
    load_forecast: LoadForecast | None,
) -> tuple[ChargeWindow, ...]:
    """Merge adjacent chosen slots into windows.

    Values are accumulated per slot rather than recomputed from the merged
    totals, because two adjacent slots can serve two different peaks and the
    drained energy is worth what the hours it is used in cost.
    """
    merged: list[list] = []
    for candidate in chosen:
        if merged and merged[-1][1] == candidate.start:
            merged[-1][1] = candidate.end
            merged[-1][2] += candidate.kwh
            merged[-1][3] += candidate.cost
            if candidate.peak_price > merged[-1][4]:
                merged[-1][4] = candidate.peak_price
                merged[-1][5] = candidate.peak_start
            continue
        merged.append(
            [
                candidate.start,
                candidate.end,
                candidate.kwh,
                candidate.cost,
                candidate.peak_price,
                candidate.peak_start,
            ]
        )

    result: list[ChargeWindow] = []
    for start, end, kwh, cost, peak_price, peak_start in merged:
        drain, drain_price = _expected_drain(
            load_forecast, end, peak_start, slots, kwh, peak_price
        )
        delivered = max(0.0, kwh - drain)
        value = efficiency * (delivered * peak_price + drain * drain_price)
        result.append(
            ChargeWindow(
                start=start,
                end=end,
                energy_kwh=kwh,
                cost=cost,
                value=value,
                profit=value - cost,
                average_price=cost / kwh if kwh else 0.0,
                discharge_price=peak_price,
                drain_kwh=drain,
            )
        )
    return tuple(result)


def schedule_window_plan(plan: ChargePlan, now: datetime) -> ChargePlan:
    """Return a plan holding only the window the inverter should run now.

    The inverter has six slots, enough for three windows with their idle
    boundaries, but a day can contain more profitable windows than that. Rather
    than dropping the extra windows, the scheduler writes only the active
    window (or the next one when none is active) and rewrites when that window
    ends. The other slots stay idle at the reserve, so the slot count no longer
    limits the plan.

    The end slot is always part of what is written, which is what keeps the
    inverter from charging past the window if Home Assistant goes away.
    """
    window = plan.active_window(now) or plan.next_window(now)
    if window is None:
        return replace(
            plan,
            windows=(),
            energy_kwh=0.0,
            cost=0.0,
            expected_saving=0.0,
        )
    return replace(
        plan,
        windows=(window,),
        energy_kwh=window.energy_kwh,
        cost=window.cost,
        expected_saving=window.profit,
    )


def charge_current_a(
    power_kw: float,
    voltage: float,
    max_current_a: int = GRID_CHARGE_CURRENT_MAX,
) -> int:
    """Return the battery grid-charge current for a charge power."""
    if power_kw <= 0 or voltage <= 0:
        return 0
    limit = max(1, int(max_current_a))
    return max(1, min(limit, round(power_kw * 1000.0 / voltage)))


def build_control_plan(
    settings: BatterySettings,
    *,
    charge: bool,
    hold_soc: float | None,
    power_w: int,
    charge_current_a: int,
    work_mode: WorkMode = WorkMode.ZERO_EXPORT_TO_CT,
) -> ControlPlan:
    """Return the six identical TOU programs for the current state.

    The times are only for display and diagnostics; the controller never
    writes them. The inverter keeps its six existing, day-covering slots.
    """
    reserve = max(0, min(100, round(settings.reserve_soc_pct)))
    target = max(reserve, min(100, round(settings.target_soc_pct)))
    soc = target if charge else reserve
    if not charge and hold_soc is not None:
        soc = max(reserve, min(100, round(hold_soc)))
    programs = tuple(
        TouProgram(
            minute,
            max(0, int(power_w)),
            soc,
            ChargeSource.GRID if charge else ChargeSource.OFF,
        )
        for minute in RESTORE_SLOT_MINUTES
    )
    return ControlPlan(
        programs=programs,
        charge_current_a=max(0, int(charge_current_a)),
        work_mode=work_mode,
    )


def build_tou_programs(
    plan: ChargePlan,
    settings: BatterySettings,
    now: datetime,
    *,
    hold: tuple[datetime, datetime, float] | None = None,
) -> TouPlan:
    """Turn a charge plan into the inverter's six time-of-use programs.

    A slot that charges starts at the window's start. Every window is followed by
    an idle slot at its end with the reserve SOC, which is what lets the battery
    discharge into the expensive hours instead of sitting at the target.

    The coordinator passes a single-window plan from :func:`schedule_window_plan`
    so the six slots are never a limit in normal operation. This function still
    trims a multi-window plan if another caller hands it one.
    """
    power_w = max(0, round(settings.max_charge_kw * 1000))
    reserve = max(0, min(100, round(settings.reserve_soc_pct)))
    target = max(reserve, min(100, round(settings.target_soc_pct)))

    ordered = sorted(plan.windows, key=lambda window: window.start)
    max_windows = TOU_SLOTS // 2
    truncated = len(ordered) > max_windows
    if truncated:
        _LOGGER.warning(
            "Battery plan has %s windows, only %s fit in the inverter's %s slots",
            len(ordered),
            max_windows,
            TOU_SLOTS,
        )
        ordered = sorted(ordered, key=lambda window: window.profit, reverse=True)[
            :max_windows
        ]
        ordered.sort(key=lambda window: window.start)

    slots: dict[int, TouProgram] = {}
    for window in ordered:
        start_minute = _minute_of_day(window.start)
        slots[start_minute] = TouProgram(
            start_minute, power_w, target, ChargeSource.GRID
        )
        end_minute = _minute_of_day(window.end)
        if end_minute != start_minute:
            slots.setdefault(
                end_minute, TouProgram(end_minute, power_w, reserve, ChargeSource.OFF)
            )

    if hold is not None:
        slots = _apply_hold(slots, hold, power_w, reserve)

    protected: list[tuple[int, int]] = [
        (_minute_of_day(window.start), _minute_of_day(window.end)) for window in ordered
    ]
    if hold is not None:
        protected.append((_minute_of_day(hold[0]), _minute_of_day(hold[1])))

    if not slots:
        # Nothing worth buying: charge nowhere, allow discharge to the reserve.
        for minute in (0, 240, 480, 720, 960, 1200):
            slots[minute] = TouProgram(minute, power_w, reserve, ChargeSource.OFF)

    while len(slots) < TOU_SLOTS:
        minute = _largest_free_minute(slots, tuple(protected))
        slots[minute] = TouProgram(minute, power_w, reserve, ChargeSource.OFF)

    while len(slots) > TOU_SLOTS:
        # Drop idle slots first: a charging slot's SOC target already stops it.
        idle = [
            minute
            for minute, program in slots.items()
            if program.source is ChargeSource.OFF
        ]
        if not idle:
            break
        slots.pop(max(idle))
        truncated = True

    programs = tuple(slots[minute] for minute in sorted(slots))
    return TouPlan(programs=programs, truncated=truncated)


def _protected_segments(
    intervals: tuple[tuple[int, int], ...],
) -> list[tuple[int, int]]:
    """Split minute-of-day intervals into non-wrapping segments."""
    segments: list[tuple[int, int]] = []
    for start, end in intervals:
        if start == end:
            continue
        if start < end:
            segments.append((start, end))
        else:
            segments.append((start, 24 * 60))
            segments.append((0, end))
    return segments


def _is_protected(minute: int, segments: list[tuple[int, int]]) -> bool:
    """Return True when a slot at ``minute`` would split a window."""
    return any(start <= minute < end for start, end in segments)


def _largest_free_minute(
    slots: dict[int, TouProgram],
    protected: tuple[tuple[int, int], ...] = (),
) -> int:
    """Return the middle of the largest gap that does not split a window.

    Idle fillers have to stay outside the charging and hold windows: a filler
    inside one would cut that window in two and turn charging off halfway.
    """
    segments = _protected_segments(protected)
    minutes = sorted(slots)
    best_gap = -1
    best_minute: int | None = None
    for index, minute in enumerate(minutes):
        following = minutes[(index + 1) % len(minutes)]
        gap = (following - minute) % (24 * 60) or 24 * 60
        candidate = (minute + gap // 2) % (24 * 60)
        if (
            gap > best_gap
            and candidate not in slots
            and not _is_protected(candidate, segments)
        ):
            best_gap = gap
            best_minute = candidate
    if best_minute is not None:
        return best_minute
    for candidate in range(24 * 60):
        if candidate not in slots and not _is_protected(candidate, segments):
            return candidate
    for candidate in range(24 * 60):
        if candidate not in slots:
            return candidate
    return 0


def build_restore_programs(
    settings: BatterySettings,
    *,
    charge_source: ChargeSource = ChargeSource.GENERATOR,
    soc_pct: int = 100,
) -> TouPlan:
    """Return the programs written when control is released.

    Grid charging is never allowed here: every slot only accepts the generator
    as its source, so releasing control cannot silently keep buying power. The
    work mode written alongside puts the inverter back into self-consumption.
    """
    power_w = max(0, round(settings.max_charge_kw * 1000))
    programs = tuple(
        TouProgram(minute, power_w, soc_pct, charge_source)
        for minute in RESTORE_SLOT_MINUTES
    )
    return TouPlan(programs=programs, truncated=False)


def settings_fingerprint(settings: BatterySettings) -> tuple[float, ...]:
    """Return the settings a plan depends on, in one comparable tuple.

    The live plan is compared against this so that a moved knob rewrites the
    inverter immediately, while the many ticks that change nothing about the
    settings keep honouring the write throttle.
    """
    return (
        round(float(settings.capacity_kwh), 3),
        round(float(settings.max_charge_kw), 3),
        round(float(settings.reserve_soc_pct), 2),
        round(float(settings.target_soc_pct), 2),
        round(float(settings.efficiency_pct), 2),
        float(settings.horizon_hours),
    )


def should_apply(
    plan: ChargePlan,
    applied: AppliedPlan | None,
    settings: BatterySettings,
    *,
    last_apply: datetime | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> bool:
    """Return True when the proposal is worth writing to the inverter.

    Rewriting on every tick would hammer the Modbus link: the plan's energy
    changes while the battery charges, so the window end moves constantly. The
    decision therefore looks only at what matters for the schedule -- whether
    there is a charge window at all, when the next one starts, and the target
    state of charge.

    ``force`` bypasses the throttle: a setting the plan reads has changed, so the
    schedule in force was computed for a different question and must be replaced
    even if the plan it replaced looked similar.
    """
    if force:
        return True
    if applied is None:
        return True

    if (
        now is not None
        and last_apply is not None
        and now - last_apply < AUTO_APPLY_MIN_INTERVAL
    ):
        return False

    if not plan.has_windows:
        # Nothing to buy any more: the idle program has to replace the one that
        # is in force, otherwise the old window would still grid-charge.
        return bool(applied.windows)

    if not applied.windows:
        return True

    if abs(plan.windows[0].start - applied.windows[0].start) > AUTO_APPLY_TOLERANCE:
        return True

    target = round(settings.target_soc_pct)
    # Compare against the slot that actually charges, not the first slot of the
    # day: slots sorted by time-of-day usually start with an idle one at the
    # reserve SOC, which would look like a change on every tick.
    charging = [
        program for program in applied.programs if program.source is ChargeSource.GRID
    ]
    if not charging:
        return True
    return charging[0].soc_pct != target


def charge_power_caps(
    max_charge_kw: float,
    import_cap_kw: float,
    grid_import_kw: float,
    ev_power_kw: float,
    priority: Priority,
    inverter_cap_kw: float = 0.0,
    inverter_load_kw: float = 0.0,
    *,
    temperature_c: float | None = None,
    temp_limit_c: float = DEFAULT_TEMP_CHARGE_LIMIT_C,
    temp_band_c: float = DEFAULT_TEMP_CHARGE_LIMIT_BAND_C,
    temp_floor_pct: float = DEFAULT_TEMP_CHARGE_LIMIT_FLOOR_PCT,
) -> tuple[float, tuple[Blocker, ...]]:
    """Return the charge ceiling and every cap that lowered it."""
    caps = charge_caps(
        max_charge_kw,
        import_cap_kw,
        grid_import_kw,
        ev_power_kw,
        priority,
        inverter_cap_kw=inverter_cap_kw,
        inverter_load_kw=inverter_load_kw,
        temperature_c=temperature_c,
        temp_limit_c=temp_limit_c,
        temp_band_c=temp_band_c,
        temp_floor_pct=temp_floor_pct,
    )
    limit, blockers = apply_caps(max_charge_kw, caps)
    return round(limit, 3), blockers


def charge_power_limit(
    max_charge_kw: float,
    import_cap_kw: float,
    grid_import_kw: float,
    ev_power_kw: float,
    priority: Priority,
    inverter_cap_kw: float = 0.0,
    inverter_load_kw: float = 0.0,
    *,
    temperature_c: float | None = None,
    temp_limit_c: float = DEFAULT_TEMP_CHARGE_LIMIT_C,
    temp_band_c: float = DEFAULT_TEMP_CHARGE_LIMIT_BAND_C,
    temp_floor_pct: float = DEFAULT_TEMP_CHARGE_LIMIT_FLOOR_PCT,
) -> float:
    """Return how much power the battery may take from the grid right now.

    A chain of caps rather than one formula, because the reasons differ: a car
    that is charging, a connection with no room left and a hot battery all
    lower the same number, and the user is entitled to know which one did.

    With the EV first, a car that is charging gets the cheap hours to itself:
    filling the battery at the same time doubles the import, and when the car
    pauses for grid rewards the battery would swallow the freed capacity,
    undoing the pause it is being paid for. The import cap keeps the combined
    draw under what the connection can carry.
    """
    limit, _ = charge_power_caps(
        max_charge_kw,
        import_cap_kw,
        grid_import_kw,
        ev_power_kw,
        priority,
        inverter_cap_kw=inverter_cap_kw,
        inverter_load_kw=inverter_load_kw,
        temperature_c=temperature_c,
        temp_limit_c=temp_limit_c,
        temp_band_c=temp_band_c,
        temp_floor_pct=temp_floor_pct,
    )
    return limit


def _apply_hold(
    slots: dict[int, TouProgram],
    hold: tuple[datetime, datetime, float],
    power_w: int,
    reserve: int,
) -> dict[int, TouProgram]:
    """Hold the battery through the car's window instead of discharging.

    Slots inside the window that do not charge are rewritten to the SOC the
    battery has right now, so the car is served by the grid. That is what stops
    a car resuming after a pause from draining the battery, while the car's
    expensive hours outside the window still run off the battery.
    """
    start, end, soc_pct = hold
    hold_soc = round(min(100.0, max(0.0, soc_pct)))
    if hold_soc <= reserve:
        return slots

    start_minute = _minute_of_day(start)
    end_minute = _minute_of_day(end)
    result = dict(slots)
    # The window needs a slot of its own, otherwise the inverter keeps whatever
    # the previous slot told it to do.
    result[start_minute] = TouProgram(start_minute, power_w, hold_soc, ChargeSource.OFF)
    if end_minute != start_minute:
        result.setdefault(
            end_minute, TouProgram(end_minute, power_w, reserve, ChargeSource.OFF)
        )
    for minute, program in slots.items():
        inside = (
            start_minute <= minute < end_minute
            if start_minute <= end_minute
            else minute >= start_minute or minute < end_minute
        )
        if not inside or program.source is ChargeSource.GRID:
            continue
        result[minute] = TouProgram(minute, power_w, hold_soc, ChargeSource.OFF)
    return result
