"""Tests for the blocker registry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.kd_helios.decisions import (
    SOURCE_CAP,
    SOURCE_PLAN,
    SOURCE_WRITE,
    Blocker,
    BlockerRegistry,
)

START = datetime(2026, 11, 20, 6, 0, tzinfo=UTC)


def test_a_persisting_condition_keeps_its_start_time() -> None:
    """A reason that is still the same keeps the moment it first appeared."""
    registry = BlockerRegistry()
    blocker = Blocker(SOURCE_PLAN, "no_spread")

    registry.update([blocker], START)
    registry.update([Blocker(SOURCE_PLAN, "no_spread")], START + timedelta(minutes=5))

    current = registry.get(SOURCE_PLAN)
    assert current is not None
    assert current.since == START


def test_a_changed_reason_gets_a_new_start_time() -> None:
    """A new reason is a new condition and starts now."""
    registry = BlockerRegistry()
    registry.update([Blocker(SOURCE_PLAN, "no_prices")], START)
    later = START + timedelta(minutes=5)
    registry.update([Blocker(SOURCE_PLAN, "no_spread")], later)

    current = registry.get(SOURCE_PLAN)
    assert current is not None
    assert current.reason == "no_spread"
    assert current.since == later


def test_clearing_a_condition_forgets_it() -> None:
    """A blocker that is not handed in again is gone, not stale."""
    registry = BlockerRegistry()
    registry.update([Blocker(SOURCE_PLAN, "no_spread")], START)
    assert registry.is_blocked()

    registry.update([], START)
    assert not registry.is_blocked()
    assert registry.get(SOURCE_PLAN) is None


def test_two_caps_coexist_instead_of_hiding_each_other() -> None:
    """The car and the connection can hold a charge back at the same time."""
    registry = BlockerRegistry()
    registry.update(
        [
            Blocker(SOURCE_CAP, "ev_charging", {"ev_kw": 11.0}),
            Blocker(SOURCE_CAP, "import_cap", {"headroom_kw": 0.0}),
            Blocker(SOURCE_WRITE, "writes_disabled"),
        ],
        START,
    )

    reasons = {reason for _source, reason in registry.reason_pairs()}
    assert reasons == {"ev_charging", "import_cap", "writes_disabled"}
    assert len(registry.as_list()) == 3


def test_the_list_is_stable_and_serialisable() -> None:
    """Attributes keep a fixed order so a diff means a real change."""
    registry = BlockerRegistry()
    registry.update(
        [
            Blocker(SOURCE_WRITE, "control_off"),
            Blocker(SOURCE_PLAN, "full"),
        ],
        START,
    )

    first = registry.as_list()
    second = registry.as_list()
    assert first == second
    assert [item["source"] for item in first] == sorted(
        item["source"] for item in first
    )
    assert first[0]["since"] == START.isoformat()


def test_details_are_copied_not_shared() -> None:
    """Mutating the caller's details must not rewrite history."""
    details = {"soc": 40}
    registry = BlockerRegistry()
    registry.update([Blocker(SOURCE_PLAN, "full", details)], START)

    details["soc"] = 99
    current = registry.get(SOURCE_PLAN)
    assert current is not None
    assert current.details["soc"] == 40
