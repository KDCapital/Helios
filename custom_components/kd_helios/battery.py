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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum

from homeassistant.util import dt as dt_util

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

#: Start times of the six slots used when releasing control: they only ever
#: charge from the generator, so the grid is never used behind your back.
RESTORE_SLOT_MINUTES = (0, 240, 480, 720, 960, 1200)

#: At most one automatic write per this interval, to spare the Modbus link.
AUTO_APPLY_MIN_INTERVAL = timedelta(minutes=10)

#: A drifting charge start smaller than this is not worth rewriting for.
AUTO_APPLY_TOLERANCE = timedelta(minutes=5)

#: A slot cannot be shorter than this; minute resolution matches the HHMM field.
MIN_SLOT = timedelta(minutes=1)


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
    #: For arbitrage: when the stored energy is sold back to the grid.
    sell_start: datetime | None = None
    sell_end: datetime | None = None
    sell_price: float | None = None


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


def _empty(now: datetime, soc: float | None, reason: str) -> ChargePlan:
    """Return a plan that buys nothing."""
    return ChargePlan(generated_at=now, soc_pct=soc, reason=reason)


def plan_charge(
    series: PriceSeries,
    settings: BatterySettings,
    now: datetime,
    soc_pct: float | None,
) -> ChargePlan:
    """Return the cheapest way to fill the battery over the horizon.

    Each candidate slot is worth the highest price that comes *after* it, times
    the round-trip efficiency: that is the import it displaces when the battery
    discharges into the expensive hours. Slots whose spread does not cover the
    losses are not bought at all.
    """
    now = dt_util.as_utc(now)
    if soc_pct is None:
        return _empty(now, None, "no_soc")

    horizon_end = now + timedelta(hours=max(1, settings.horizon_hours))
    slots = series.priced_slots(now, horizon_end)
    if not slots:
        return _empty(now, soc_pct, "no_prices")

    energy_needed = settings.capacity_kwh * (settings.target_soc_pct - soc_pct) / 100.0
    if energy_needed <= 1e-6:
        return _empty(now, soc_pct, "full")

    efficiency = settings.efficiency
    prices = [price for _, _, price in slots]

    candidates: list[tuple[datetime, datetime, float, float, float]] = []
    for index, (start, end, price) in enumerate(slots):
        later = prices[index + 1 :]
        if not later:
            continue
        discharge_price = max(later)
        profit_per_kwh = discharge_price * efficiency - price
        if profit_per_kwh <= 0:
            continue
        hours = (end - start).total_seconds() / 3600.0
        if hours <= 0:
            continue
        max_kwh = min(settings.max_charge_kw * hours, settings.capacity_kwh)
        if max_kwh <= 0:
            continue
        candidates.append(
            (start, end, price, discharge_price, profit_per_kwh * max_kwh)
        )

    if not candidates:
        return _empty(now, soc_pct, "no_spread")

    candidates.sort(key=lambda item: item[4], reverse=True)

    remaining = energy_needed
    chosen: list[tuple[datetime, datetime, float, float, float]] = []
    for candidate in candidates:
        if remaining <= 1e-9:
            break
        start, end, price, discharge_price, _ = candidate
        hours = (end - start).total_seconds() / 3600.0
        kwh = min(settings.max_charge_kw * hours, remaining)
        if kwh <= 0:
            continue
        chosen.append((start, end, price, discharge_price, kwh))
        remaining -= kwh

    if not chosen:
        return _empty(now, soc_pct, "no_spread")

    chosen.sort(key=lambda item: item[0])
    windows = _merge_windows(chosen, efficiency)
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
    )


def _merge_windows(
    chosen: list[tuple[datetime, datetime, float, float, float]],
    efficiency: float,
) -> list[ChargeWindow]:
    """Merge adjacent chosen slots into windows."""
    windows: list[list] = []
    for start, end, price, discharge_price, kwh in chosen:
        if windows and windows[-1][1] == start:
            windows[-1][1] = end
            windows[-1][2] += kwh
            windows[-1][3] += kwh * price
            windows[-1][4] = max(windows[-1][4], discharge_price)
            continue
        windows.append([start, end, kwh, kwh * price, discharge_price])

    result: list[ChargeWindow] = []
    for start, end, kwh, cost, discharge_price in windows:
        value = kwh * efficiency * discharge_price
        result.append(
            ChargeWindow(
                start=start,
                end=end,
                energy_kwh=kwh,
                cost=cost,
                value=value,
                profit=value - cost,
                average_price=cost / kwh if kwh else 0.0,
                discharge_price=discharge_price,
            )
        )
    return result


def build_tou_programs(
    plan: ChargePlan,
    settings: BatterySettings,
    now: datetime,
    *,
    arbitrage: bool = False,
) -> TouPlan:
    """Turn a charge plan into the inverter's six time-of-use programs.

    A slot that charges starts at the window's start. Every window is followed by
    an idle slot at its end with the reserve SOC, which is what lets the battery
    discharge into the expensive hours instead of sitting at the target.

    Six slots is a hard limit, so at most three windows (each with one idle slot)
    survive; the rest are dropped and reported.
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
        if arbitrage and window.sell_start is not None and window.sell_end is not None:
            # Keep the charge until the expensive hour, then let it go.
            if end_minute != start_minute:
                slots.setdefault(
                    end_minute,
                    TouProgram(end_minute, power_w, target, ChargeSource.OFF),
                )
            sell_minute = _minute_of_day(window.sell_start)
            slots[sell_minute] = TouProgram(
                sell_minute, power_w, reserve, ChargeSource.OFF
            )
            sell_end_minute = _minute_of_day(window.sell_end)
            if sell_end_minute != sell_minute:
                slots.setdefault(
                    sell_end_minute,
                    TouProgram(sell_end_minute, power_w, reserve, ChargeSource.OFF),
                )
            continue
        if end_minute != start_minute:
            slots.setdefault(
                end_minute, TouProgram(end_minute, power_w, reserve, ChargeSource.OFF)
            )

    if not slots:
        # Nothing worth buying: charge nowhere, allow discharge to the reserve.
        for minute in (0, 240, 480, 720, 960, 1200):
            slots[minute] = TouProgram(minute, power_w, reserve, ChargeSource.OFF)

    while len(slots) < TOU_SLOTS:
        minute = _largest_free_minute(slots)
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


def _largest_free_minute(slots: dict[int, TouProgram]) -> int:
    """Return the middle of the largest gap between existing slots."""
    minutes = sorted(slots)
    best_gap = -1
    best_minute = 0
    for index, minute in enumerate(minutes):
        following = minutes[(index + 1) % len(minutes)]
        gap = (following - minute) % (24 * 60) or 24 * 60
        if gap > best_gap:
            best_gap = gap
            best_minute = (minute + gap // 2) % (24 * 60)
    if best_minute not in slots:
        return best_minute
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


def should_apply(
    plan: ChargePlan,
    applied: AppliedPlan | None,
    settings: BatterySettings,
    *,
    last_apply: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    """Return True when the proposal is worth writing to the inverter.

    Rewriting on every tick would hammer the Modbus link: the plan's energy
    changes while the battery charges, so the window end moves constantly. The
    decision therefore looks only at what matters for the schedule -- whether
    there is a charge window at all, when the next one starts, and the target
    state of charge.
    """
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


def plan_arbitrage(
    series: PriceSeries,
    settings: BatterySettings,
    now: datetime,
    soc_pct: float | None,
    headroom_kwh: float,
) -> ChargePlan:
    """Buy in the cheap hours and sell into the most expensive one.

    While saldering still applies, energy returned within your own consumption is
    worth the import tariff of that moment, so a cycle earns the spread minus the
    round-trip losses. ``headroom_kwh`` is what is left of the annual netting
    limit: exporting beyond it only pays the feed-in price, which would turn the
    trade into a loss, so it is a hard cap here.
    """
    now = dt_util.as_utc(now)
    if soc_pct is None:
        return _empty(now, None, "no_soc")
    if headroom_kwh <= 0:
        return _empty(now, soc_pct, "no_headroom")

    slots = series.priced_slots(now, now + timedelta(hours=settings.horizon_hours))
    if len(slots) < 2:
        return _empty(now, soc_pct, "no_prices")

    energy_needed = settings.capacity_kwh * (settings.target_soc_pct - soc_pct) / 100.0
    if energy_needed <= 1e-6:
        return _empty(now, soc_pct, "full")

    # The sell moment is the most expensive slot that still has a cheaper slot
    # before it to buy in.
    sell_index = max(range(1, len(slots)), key=lambda index: slots[index][2])
    sell_start, sell_end, sell_price = slots[sell_index]
    if sell_price <= 0:
        return _empty(now, soc_pct, "no_spread")

    efficiency = settings.efficiency
    candidates: list[tuple[datetime, datetime, float, float]] = []
    for start, end, price in slots[:sell_index]:
        profit_per_kwh = sell_price * efficiency - price
        if profit_per_kwh <= 0:
            continue
        hours = (end - start).total_seconds() / 3600.0
        if hours <= 0:
            continue
        max_kwh = min(settings.max_charge_kw * hours, settings.capacity_kwh)
        if max_kwh <= 0:
            continue
        candidates.append((start, end, price, profit_per_kwh))

    if not candidates:
        return _empty(now, soc_pct, "no_spread")

    candidates.sort(key=lambda item: item[2])

    remaining = min(energy_needed, headroom_kwh)
    chosen: list[tuple[datetime, datetime, float, float, float]] = []
    for start, end, price, _profit in candidates:
        if remaining <= 1e-9:
            break
        hours = (end - start).total_seconds() / 3600.0
        kwh = min(settings.max_charge_kw * hours, remaining)
        if kwh <= 0:
            continue
        chosen.append((start, end, price, kwh, sell_price))
        remaining -= kwh

    if not chosen:
        return _empty(now, soc_pct, "no_spread")

    chosen.sort(key=lambda item: item[0])
    windows: list[ChargeWindow] = []
    cursor: list = []
    for start, end, price, kwh, target_price in chosen:
        if cursor and cursor[1] == start:
            cursor[1] = end
            cursor[2] += kwh
            cursor[3] += kwh * price
            continue
        if cursor:
            windows.append(
                _arbitrage_window(cursor, efficiency, sell_start, sell_end, sell_price)
            )
        cursor = [start, end, kwh, kwh * price]
    if cursor:
        windows.append(
            _arbitrage_window(cursor, efficiency, sell_start, sell_end, sell_price)
        )

    total_cost = sum(window.cost for window in windows)
    total_value = sum(window.value for window in windows)
    return ChargePlan(
        generated_at=now,
        soc_pct=soc_pct,
        windows=tuple(windows),
        energy_kwh=sum(window.energy_kwh for window in windows),
        cost=total_cost,
        expected_saving=total_value - total_cost,
        reason="arbitrage",
    )


def _arbitrage_window(
    cursor: list,
    efficiency: float,
    sell_start: datetime,
    sell_end: datetime,
    sell_price: float,
) -> ChargeWindow:
    """Build one arbitrage window from the accumulated slots."""
    start, end, kwh, cost = cursor
    return ChargeWindow(
        start=start,
        end=end,
        energy_kwh=kwh,
        cost=cost,
        value=kwh * efficiency * sell_price,
        profit=kwh * efficiency * sell_price - cost,
        average_price=cost / kwh if kwh else 0.0,
        discharge_price=sell_price,
        sell_start=sell_start,
        sell_end=sell_end,
        sell_price=sell_price,
    )
