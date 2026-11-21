"""Tests for the chain of charge-power caps."""

from __future__ import annotations

import pytest

from custom_components.kd_helios.caps import (
    EnvelopeCap,
    EvPriorityCap,
    ImportHeadroomCap,
    TemperatureCap,
    apply_caps,
    charge_caps,
)
from custom_components.kd_helios.const import Priority


def test_the_envelope_is_the_widest_bound() -> None:
    """The battery's own rating caps a limit that asks for more."""
    limit, blockers = apply_caps(11.0, [EnvelopeCap(5.0)])
    assert limit == 5.0
    assert blockers == ()


def test_the_car_gets_the_cheap_hours() -> None:
    """A charging car with priority stops the battery from buying as well."""
    limit, blockers = apply_caps(11.0, [EvPriorityCap(11.0, Priority.EV)])
    assert limit == 0.0
    assert [blocker.reason for blocker in blockers] == ["ev_charging"]
    assert blockers[0].details["ev_kw"] == 11.0


def test_a_trickle_does_not_count_as_charging() -> None:
    """A connected car drawing 200 W is not charging."""
    limit, blockers = apply_caps(11.0, [EvPriorityCap(0.2, Priority.EV)])
    assert limit == 11.0
    assert blockers == ()


def test_battery_priority_ignores_the_car() -> None:
    """With battery priority the car does not block anything."""
    limit, _ = apply_caps(11.0, [EvPriorityCap(11.0, Priority.BATTERY)])
    assert limit == 11.0


def test_the_import_headroom_shrinks_the_limit() -> None:
    """The connection has the final say over the battery's rating."""
    limit, blockers = apply_caps(11.0, [ImportHeadroomCap(17.0, 15.0)])
    assert limit == pytest.approx(2.0)
    assert blockers[0].reason == "import_cap"
    assert blockers[0].details["headroom_kw"] == 2.0


def test_no_room_left_means_zero() -> None:
    """A connection already at its limit cannot take a charge."""
    limit, blockers = apply_caps(11.0, [ImportHeadroomCap(17.0, 20.0)])
    assert limit == 0.0
    assert blockers[0].reason == "import_cap"


def test_a_cap_never_raises_a_limit() -> None:
    """No cap may undo a ceiling another one imposed."""
    limit, _ = apply_caps(
        1.0,
        [
            EnvelopeCap(5.0),
            EvPriorityCap(0.0, Priority.EV),
            ImportHeadroomCap(0.0, 0.0),
        ],
    )
    assert limit == 1.0


def test_a_missing_temperature_passes_the_limit_through() -> None:
    """A sensor gap must never be the reason a battery stops charging."""
    limit, blockers = apply_caps(5.0, [TemperatureCap(None)])
    assert limit == 5.0
    assert blockers == ()


def test_the_thermal_ramp_is_continuous() -> None:
    """Full power at the limit, the floor at the top of the band, and back up."""
    assert TemperatureCap(40.0, 40.0, 10.0, 40.0).factor == 1.0
    assert TemperatureCap(45.0, 40.0, 10.0, 40.0).factor == pytest.approx(0.7)
    assert TemperatureCap(50.0, 40.0, 10.0, 40.0).factor == pytest.approx(0.4)
    assert TemperatureCap(60.0, 40.0, 10.0, 40.0).factor == pytest.approx(0.4)


def test_a_hot_battery_derates_the_limit_and_says_so() -> None:
    """The derate is visible as a reason, with the temperature behind it."""
    limit, blockers = apply_caps(5.0, [TemperatureCap(45.0)])
    assert limit == pytest.approx(3.5)
    assert blockers[0].reason == "temperature"
    assert blockers[0].details["temperature_c"] == 45.0


def test_the_chain_reports_every_cap_that_bit() -> None:
    """Two ceilings at once are two reasons, not one."""
    _, blockers = apply_caps(
        11.0,
        [EvPriorityCap(11.0, Priority.EV), TemperatureCap(50.0)],
    )
    # The EV cap zeroes the limit; the thermal cap still reports its factor.
    assert {blocker.reason for blocker in blockers} == {"ev_charging", "temperature"}


def test_the_default_chain_is_ordered_widest_first() -> None:
    """The chain is assembled predictably for one control cycle."""
    caps = charge_caps(5.0, 17.0, 3.0, 0.0, Priority.EV, temperature_c=45.0)
    assert [type(cap).__name__ for cap in caps] == [
        "EnvelopeCap",
        "EvPriorityCap",
        "ImportHeadroomCap",
        "TemperatureCap",
    ]
