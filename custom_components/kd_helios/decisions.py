"""Named reasons for a decision, instead of a log line nobody can query.

Every layer that can hold a charge back records a :class:`Blocker` rather than
computing its own explanation: the planner (no prices, no spread, capped by the
connection), the solar forecast (the sun will deliver this), the maintenance
schedule (a weekly full charge owns the target) and the write path (writes
disabled, inverter unreachable, write refused).

A blocker carries a ``source``, a machine-readable ``reason``, the numbers the
decision was made on, and since when it holds. The registry keeps one blocker
per source, so two conditions coexist without overwriting each other and the
user sees *all* the reasons, not whichever was evaluated last.

Everything here is side-effect free apart from the registry's own memory.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: Sources, so a caller can clear or inspect one condition specifically.
SOURCE_PLAN = "plan"
SOURCE_SOLAR = "solar"
SOURCE_MAINTENANCE = "maintenance"
SOURCE_CAP = "cap"
SOURCE_WRITE = "write"
SOURCE_DELIVERY = "delivery"


@dataclass(frozen=True, slots=True)
class Blocker:
    """One reason a charge is not happening, with the numbers behind it."""

    source: str
    reason: str
    details: Mapping[str, Any] = field(default_factory=dict)
    #: When this condition started. Stamped by the registry, not the caller.
    since: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON/state-attribute friendly representation."""
        payload: dict[str, Any] = {
            "source": self.source,
            "reason": self.reason,
            "details": dict(self.details),
        }
        if self.since is not None:
            payload["since"] = self.since.isoformat()
        return payload

    def replaced_by(self, other: Blocker, since: datetime | None) -> Blocker:
        """Return ``other`` keeping this blocker's start time when it persists.

        A condition that stays the same from one tick to the next keeps the
        moment it first appeared, so "why is it waiting?" can be answered with
        "since 06:12" rather than "since one minute ago".
        """
        details = dict(other.details)
        if other.since is not None:
            return Blocker(other.source, other.reason, details, other.since)
        if self.reason == other.reason and self.since is not None:
            return Blocker(other.source, other.reason, details, self.since)
        return Blocker(other.source, other.reason, details, since)


class BlockerRegistry:
    """The blockers that hold right now, one per (source, reason) pair.

    Keying on the pair rather than the source alone is what lets three caps
    (the car, the connection and the battery's temperature) hold at once without
    hiding each other.
    """

    def __init__(self) -> None:
        """Initialise an empty registry."""
        self._current: dict[tuple[str, str], Blocker] = {}

    def update(
        self,
        blockers: Iterable[Blocker],
        now: datetime | None = None,
    ) -> None:
        """Replace the set, preserving the start time of a condition that persists.

        A condition that is absent from ``blockers`` is cleared: the caller is
        expected to hand in everything that holds for this tick, which is what
        keeps a stale reason from outliving its cause.
        """
        fresh: dict[tuple[str, str], Blocker] = {}
        for blocker in blockers:
            key = (blocker.source, blocker.reason)
            incoming = Blocker(
                blocker.source,
                blocker.reason,
                dict(blocker.details),
                blocker.since,
            )
            previous = self._current.get(key)
            if previous is not None:
                fresh[key] = previous.replaced_by(incoming, now)
            else:
                fresh[key] = Blocker(
                    incoming.source,
                    incoming.reason,
                    dict(incoming.details),
                    incoming.since or now,
                )
        self._current = fresh

    def get(self, source: str) -> Blocker | None:
        """Return the first blocker a given source contributes, if any."""
        for (owner, _reason), blocker in self._current.items():
            if owner == source:
                return blocker
        return None

    def is_blocked(self) -> bool:
        """Return True while anything holds a charge back."""
        return bool(self._current)

    def reason_pairs(self) -> list[tuple[str, str]]:
        """Return the (source, reason) pairs in force, for a log line or a test."""
        return sorted(self._current)

    def as_list(self) -> list[dict[str, Any]]:
        """Return the blockers in a stable order, ready for attributes."""
        return [self._current[key].as_dict() for key in sorted(self._current)]

    def clear(self) -> None:
        """Forget every blocker."""
        self._current = {}

    def __bool__(self) -> bool:
        """Return True while anything holds a charge back."""
        return bool(self._current)
