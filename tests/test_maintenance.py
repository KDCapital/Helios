"""Tests for the weekly full charge schedule."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.kd_helios.maintenance import WeeklyFullCharge

#: Friday 20 November 2026, mid-morning.
FRIDAY = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
SATURDAY = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)


def _weekly(**overrides) -> WeeklyFullCharge:
    defaults = {"enabled": True, "weekday": 4, "target_soc_pct": 100.0}
    defaults.update(overrides)
    return WeeklyFullCharge(**defaults)


def test_disabled_never_asks_for_anything() -> None:
    """Off means the normal target stands."""
    result = _weekly(enabled=False).evaluate(FRIDAY, 40.0, None)
    assert not result.active
    assert result.target_soc_pct is None
    assert result.blocker is None


def test_the_cycle_runs_on_its_day() -> None:
    """On the chosen day the target is raised and the reason is on record."""
    result = _weekly().evaluate(FRIDAY, 40.0, None)

    assert result.active
    assert result.target_soc_pct == 100.0
    assert result.state == {"date": "2026-11-20", "completed": False}
    assert result.blocker is not None
    assert result.blocker.reason == "weekly_full_charge"
    assert result.blocker.details["soc_pct"] == 40.0


def test_a_full_battery_completes_the_cycle() -> None:
    """Reaching the top ends the cycle and records the SOC it reached."""
    result = _weekly().evaluate(FRIDAY, 99.4, None)

    assert not result.active
    assert result.target_soc_pct is None
    assert result.state["completed"] is True
    assert result.state["soc_at_completion"] == 99.4


def test_a_completed_cycle_stays_complete_all_day() -> None:
    """The marker survives the battery dropping again the same day."""
    stored = {"date": "2026-11-20", "completed": True}
    result = _weekly().evaluate(FRIDAY, 60.0, stored)

    assert not result.active
    assert result.state["completed"] is True


def test_yesterdays_completion_does_not_count_today() -> None:
    """A cycle that ran last week cannot make today look finished."""
    stored = {"date": "2026-11-13", "completed": True}
    result = _weekly().evaluate(FRIDAY, 60.0, stored)

    assert result.active
    assert result.state == {"date": "2026-11-20", "completed": False}


def test_another_day_plans_normally() -> None:
    """Off-day means no weekly state, so nothing stale is persisted."""
    result = _weekly().evaluate(SATURDAY, 40.0, None)

    assert not result.active
    assert dict(result.state) == {}


def test_an_unknown_soc_does_not_complete_the_cycle() -> None:
    """A missing reading is not evidence of a full battery."""
    result = _weekly().evaluate(FRIDAY, None, None)

    assert result.active
    assert result.blocker is not None
    assert result.blocker.details["soc_pct"] is None


def test_the_weekday_is_clamped_to_a_real_day() -> None:
    """A malformed setting cannot schedule a cycle that never runs."""
    assert WeeklyFullCharge(enabled=True, weekday=99).weekday == 6
    assert WeeklyFullCharge(enabled=True, weekday=-3).weekday == 0
