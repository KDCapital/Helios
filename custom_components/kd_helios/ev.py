"""The car's charging window, and how much the connection can carry.

Tibber decides when the car actually charges; KD Helios only has to know *when
that window is* so the battery can behave differently inside and outside it:

* inside the window the battery may charge but must never discharge, so a car
  that resumes after a grid-reward pause is served by the grid rather than by
  the battery;
* outside the window the battery is free to discharge into the car, which is
  what makes charging in the expensive hours cheaper.

Everything here is side-effect free: prices and a schedule in, a window out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import homeassistant.util.dt as dt_util

#: Phase voltage used to turn amps into kilowatts.
PHASE_VOLTAGE = 230.0


@dataclass(frozen=True, slots=True)
class EvWindow:
    """The cheapest hours in which the car can get its energy."""

    start: datetime
    end: datetime
    energy_kwh: float
    cost: float
    average_price: float

    @property
    def duration(self) -> timedelta:
        """Return how long the window lasts."""
        return self.end - self.start


def fuse_capacity_kw(phases: int, amps: float, margin_pct: float) -> float:
    """Return the usable import capacity of a connection.

    ``margin_pct`` is what the user is willing to use of the nominal rating;
    some connections are limited lower by the grid operator than the fuse
    suggests, so this is theirs to choose.
    """
    phases = 3 if int(phases) == 3 else 1
    nominal = phases * max(0.0, amps) * PHASE_VOLTAGE / 1000.0
    margin = min(100.0, max(1.0, margin_pct))
    return round(nominal * margin / 100.0, 2)


def energy_needed_kwh(capacity_kwh: float, target_soc: float, soc: float) -> float:
    """Return how much the car still needs to reach its target."""
    if capacity_kwh <= 0:
        return 0.0
    missing = max(0.0, target_soc - soc)
    return round(capacity_kwh * missing / 100.0, 3)


def ev_charge_window(
    slots: list[tuple[datetime, datetime, float]],
    departure: datetime,
    energy_kwh: float,
    power_kw: float,
) -> EvWindow | None:
    """Return the cheapest slots before departure that deliver the energy.

    The car charges at its own power, so the window is the cheapest set of
    slots that together hold the required energy and end before departure.
    Returns ``None`` when there is not enough time left, in which case the
    caller should not pretend to know the plan.
    """
    departure = dt_util.as_utc(departure)
    if energy_kwh <= 0 or power_kw <= 0:
        return None

    usable = [
        (start, end, price)
        for start, end, price in slots
        if dt_util.as_utc(start) < departure and (end - start) > timedelta(0)
    ]
    if not usable:
        return None

    def slot_kwh(slot: tuple[datetime, datetime, float]) -> float:
        """Return the energy a slot delivers at the car's power."""
        return power_kw * (slot[1] - slot[0]).total_seconds() / 3600.0

    longest = max(slot_kwh(slot) for slot in usable)
    slots_needed = int(-(-energy_kwh // longest)) if longest > 0 else 0
    if slots_needed == 0 or len(usable) < slots_needed:
        return None

    chosen = sorted(usable, key=lambda slot: slot[2])[:slots_needed]
    start = min(slot[0] for slot in chosen)
    end = min(max(slot[1] for slot in chosen), departure)

    cost = sum(slot_kwh(slot) * slot[2] for slot in chosen)
    energy = min(energy_kwh, sum(slot_kwh(slot) for slot in chosen))
    return EvWindow(
        start=dt_util.as_utc(start),
        end=dt_util.as_utc(end),
        energy_kwh=round(energy, 3),
        cost=round(cost, 4),
        average_price=round(cost / energy, 4) if energy else 0.0,
    )


def in_ev_window(moment: datetime, window: EvWindow | None) -> bool:
    """Return True when the moment falls inside the car's charging window."""
    if window is None:
        return False
    moment = dt_util.as_utc(moment)
    return window.start <= moment < window.end
