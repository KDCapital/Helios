"""Tests for the solar credit decision."""

from __future__ import annotations

import pytest

from custom_components.kd_helios.forecast import (
    STATE_CHARGING_ALLOWED,
    STATE_NO_CONSUMPTION,
    STATE_NO_FORECAST,
    STATE_PARTLY_SOLAR,
    STATE_WAITING_FOR_SOLAR,
    remaining_consumption_kwh,
    solar_credit,
)


def test_no_forecast_credits_nothing_and_says_why() -> None:
    """Without a forecast the plan buys exactly what it did before."""
    credit = solar_credit(None, 4.0, 8.0)
    assert credit.credit_kwh == 0.0
    assert credit.state == STATE_NO_FORECAST
    assert credit.blocker is not None
    assert credit.blocker.reason == "no_forecast"


def test_an_unknown_load_is_not_credited() -> None:
    """Sun against an unknown house is how a battery ends up empty at dusk."""
    credit = solar_credit(30.0, None, 8.0)
    assert credit.credit_kwh == 0.0
    assert credit.state == STATE_NO_CONSUMPTION
    assert credit.blocker is not None
    assert credit.blocker.reason == "no_consumption_forecast"


def test_the_forecast_is_discounted_by_the_cushion() -> None:
    """13 kWh of surplus at 1.3 covers the 10 kWh needed, but only just."""
    credit = solar_credit(13.0, 0.0, 10.0)
    assert credit.credit_kwh == pytest.approx(10.0)
    assert credit.state == STATE_WAITING_FOR_SOLAR


def test_a_shortfall_leaves_part_of_the_charge_to_the_grid() -> None:
    """A surplus that only covers half the need is credited halfway."""
    credit = solar_credit(6.5, 0.0, 10.0)
    assert credit.credit_kwh == pytest.approx(5.0)
    assert credit.state == STATE_PARTLY_SOLAR
    assert credit.blocker is not None
    assert credit.blocker.details["credit_kwh"] == 5.0


def test_the_load_comes_off_the_surplus_first() -> None:
    """Sun that has to run the house is not sun that reaches the battery."""
    credit = solar_credit(10.0, 8.0, 5.0)
    # 2 kWh surplus, discounted by 1.3, is 1.54 kWh.
    assert credit.credit_kwh == pytest.approx(1.538, abs=1e-3)
    assert credit.state == STATE_PARTLY_SOLAR


def test_a_load_above_the_forecast_credits_nothing() -> None:
    """A day that eats all the sun gives the battery nothing."""
    credit = solar_credit(4.0, 6.0, 8.0)
    assert credit.credit_kwh == 0.0
    assert credit.state == STATE_CHARGING_ALLOWED
    assert credit.blocker is None


def test_a_negligible_credit_does_not_change_the_plan() -> None:
    """Half a kWh is noise, not a reason to buy less."""
    credit = solar_credit(0.6, 0.0, 8.0)
    assert credit.credit_kwh == 0.0
    assert credit.state == STATE_CHARGING_ALLOWED


def test_a_battery_already_at_target_needs_no_sun() -> None:
    """Nothing to credit when nothing needs charging."""
    credit = solar_credit(20.0, 2.0, 0.0)
    assert credit.credit_kwh == 0.0
    assert credit.state == STATE_CHARGING_ALLOWED


class _Profile:
    """A profile that answers with a fixed energy."""

    def __init__(self, value: float | None) -> None:
        self.value = value

    def energy_kwh(self, start, end):
        return self.value


def test_remaining_consumption_follows_the_profile() -> None:
    """The helper passes the question through to the learned profile."""
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 11, 20, 12, tzinfo=UTC)
    assert (
        remaining_consumption_kwh(_Profile(3.5), now, now + timedelta(hours=6)) == 3.5
    )


def test_an_unknown_or_broken_profile_is_none_not_zero() -> None:
    """Unknown is not the same as nothing, so the caller must not credit."""
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 11, 20, 12, tzinfo=UTC)
    later = now + timedelta(hours=6)
    assert remaining_consumption_kwh(_Profile(None), now, later) is None
    assert remaining_consumption_kwh(object(), now, later) is None

    class _Broken:
        def energy_kwh(self, start, end):
            raise RuntimeError("boom")

    assert remaining_consumption_kwh(_Broken(), now, later) is None
