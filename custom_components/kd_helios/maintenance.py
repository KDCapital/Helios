"""One deliberate full charge a week, for the battery's own sake.

A battery that is never taken to the top of its range cannot balance its cells:
a lithium-iron-phosphate pack only separates its cell voltages inside the upper
knee, which is the only region where the management system can see which cell is
leading. A pack held at the reserve and the mid-range drifts, quietly, for years.

The cycle therefore raises the target to 100 percent on one chosen day, lets the
normal plan charge towards it, and only reports itself complete once the battery
has actually arrived. The previous target comes back the moment it does.

Everything here is side-effect free: the caller owns the persisted state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

from .decisions import SOURCE_MAINTENANCE, Blocker

#: At or above this state of charge the cycle counts as complete. A full charge
#: that ends at 99 percent has still balanced the cells; insisting on 100 can
#: leave the cycle open forever on a pack whose counter reads low.
COMPLETE_SOC_PCT = 99.0

#: The day the full charge runs, in Python weekday order (Monday is 0).
DEFAULT_WEEKDAY = 6


@dataclass(frozen=True, slots=True)
class WeeklyResult:
    """What the weekly cycle wants from this control cycle."""

    active: bool = False
    #: The target to plan against while the cycle runs, or None to leave it alone.
    target_soc_pct: float | None = None
    #: The state to persist. Empty when no cycle is running.
    state: Mapping[str, Any] = field(default_factory=dict)
    blocker: Blocker | None = None


class WeeklyFullCharge:
    """Run a full charge once a week and confirm the battery got there."""

    def __init__(
        self,
        enabled: bool = False,
        weekday: int = DEFAULT_WEEKDAY,
        target_soc_pct: float = 100.0,
        complete_soc_pct: float = COMPLETE_SOC_PCT,
    ) -> None:
        """Initialise the schedule."""
        self.enabled = enabled
        self.weekday = max(0, min(6, int(weekday)))
        self.target_soc_pct = max(0.0, min(100.0, float(target_soc_pct)))
        self.complete_soc_pct = max(0.0, min(100.0, float(complete_soc_pct)))

    def evaluate(
        self,
        now: datetime,
        soc_pct: float | None,
        stored: Mapping[str, Any] | None = None,
    ) -> WeeklyResult:
        """Return the target and state for this cycle.

        ``stored`` is whatever was persisted last time. It is replaced, so a
        cycle that ran yesterday cannot make today look complete.
        """
        if not self.enabled:
            return WeeklyResult()

        local = dt_util.as_local(dt_util.as_utc(now))
        if local.weekday() != self.weekday:
            # Not the day: clear the marker so the next run starts from scratch.
            return WeeklyResult()

        today = local.date().isoformat()
        state: dict[str, Any] = {"date": today, "completed": False}
        if isinstance(stored, Mapping) and stored.get("date") == today:
            state["completed"] = bool(stored.get("completed"))

        if soc_pct is not None and soc_pct >= self.complete_soc_pct:
            state["completed"] = True
            state["soc_at_completion"] = round(float(soc_pct), 1)

        if state["completed"]:
            return WeeklyResult(active=False, state=state)

        return WeeklyResult(
            active=True,
            target_soc_pct=self.target_soc_pct,
            state=state,
            blocker=Blocker(
                SOURCE_MAINTENANCE,
                "weekly_full_charge",
                {
                    "weekday": self.weekday,
                    "target_soc_pct": self.target_soc_pct,
                    "soc_pct": None if soc_pct is None else round(float(soc_pct), 1),
                },
            ),
        )
