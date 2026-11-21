"""A learned profile of what the house consumes, per weekday and hour.

The optimiser cannot plan for a load it does not expect. Before there is a
forecast API or a model, there is this: a rolling average per (weekday, hour)
bucket, fed by a measured house-load sensor.

Two buckets levels of fallback keep it useful early on:

1. the exact weekday and hour, once it has samples;
2. the hour across all days, which fills in the first week;
3. nothing, which the caller must treat as "unknown", not as zero.

Everything here is pure: timestamps and kilowatts in, kilowatts out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import homeassistant.util.dt as dt_util

WEEK_HOURS = 7 * 24

#: How strongly a new sample moves the average. Low enough that one odd evening
#: does not rewrite the profile, high enough that a changed routine shows up
#: within days.
SMOOTHING = 0.15

#: Fraction of a sample's weight that is "trusted" on the first observation.
_INITIAL_WEIGHT = 0.35


def _bucket(when: datetime) -> int:
    """Return the weekday-and-hour bucket of a moment."""
    local = dt_util.as_local(dt_util.as_utc(when))
    return local.weekday() * 24 + local.hour


@dataclass
class LoadProfile:
    """Rolling average house load per weekday and hour."""

    #: bucket -> [average kW, sample count]
    buckets: dict[int, list[float]] = field(default_factory=dict)

    def add(self, when: datetime, kw: float) -> None:
        """Fold one measurement into the profile."""
        if kw < 0 or math.isnan(kw):
            return
        bucket = _bucket(when)
        entry = self.buckets.setdefault(bucket, [0.0, 0.0])
        count = entry[1]
        if count == 0:
            entry[0] = kw
            entry[1] = _INITIAL_WEIGHT
            return
        alpha = max(SMOOTHING, 1.0 / (count + 2.0))
        entry[0] = entry[0] * (1.0 - alpha) + kw * alpha
        entry[1] = count + 1.0

    def forecast_kw(self, when: datetime) -> float | None:
        """Return the expected load for a moment, or None when nothing is known."""
        bucket = _bucket(when)
        entry = self.buckets.get(bucket)
        if entry is not None and entry[1] > 0:
            return round(entry[0], 3)

        # Same hour on other weekdays, which covers the first days of learning.
        hour = dt_util.as_local(dt_util.as_utc(when)).hour
        same_hour = [
            value[0] for key, value in self.buckets.items() if key % 24 == hour
        ]
        if same_hour:
            return round(sum(same_hour) / len(same_hour), 3)
        return None

    def forecast_series(
        self, start: datetime, hours: int
    ) -> list[tuple[datetime, float | None]]:
        """Return the expected load per hour from a moment onwards."""
        start = dt_util.as_utc(start)
        return [
            (
                start + timedelta(hours=offset),
                self.forecast_kw(start + timedelta(hours=offset)),
            )
            for offset in range(hours)
        ]

    def energy_kwh(self, start: datetime, end: datetime) -> float | None:
        """Return the expected energy between two moments, or None if unknown."""
        start = dt_util.as_utc(start)
        end = dt_util.as_utc(end)
        if end <= start:
            return 0.0
        total = 0.0
        known = False
        moment = start
        while moment < end:
            step_end = min(moment + timedelta(hours=1), end)
            value = self.forecast_kw(moment)
            if value is None:
                return None
            total += value * (step_end - moment).total_seconds() / 3600.0
            known = True
            moment = step_end
        return round(total, 3) if known else None

    def filled_buckets(self) -> int:
        """Return how many of the 168 buckets have learned anything."""
        return sum(1 for entry in self.buckets.values() if entry[1] > 0)

    def coverage_pct(self) -> float:
        """Return how much of the week is known, as a percentage."""
        return round(100.0 * self.filled_buckets() / WEEK_HOURS, 1)

    def as_dict(self) -> dict[str, list[float]]:
        """Return a JSON-friendly form."""
        return {
            str(bucket): [round(entry[0], 4), round(entry[1], 2)]
            for bucket, entry in self.buckets.items()
        }

    @classmethod
    def from_dict(cls, data: dict[str, list[float]] | None) -> LoadProfile:
        """Build a profile from stored data, ignoring anything malformed."""
        profile = cls()
        for key, entry in (data or {}).items():
            try:
                bucket = int(key)
                average, count = float(entry[0]), float(entry[1])
            except (TypeError, ValueError, IndexError):
                continue
            if 0 <= bucket < WEEK_HOURS:
                profile.buckets[bucket] = [average, count]
        return profile
