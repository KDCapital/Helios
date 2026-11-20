"""Unit tests for price retrieval, normalisation and slot splitting."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta

import pytest

from custom_components.kd_helios.prices import (
    PriceMeta,
    PriceSeries,
    build_price_series,
)
from custom_components.kd_helios.tariff import all_in_from_components
from tests.helpers import FROZEN_TIME, UTC, build_series, load_fixture


@pytest.fixture(name="series")
def series_fixture() -> PriceSeries:
    """Return the recorded Zonneplan quarterly series."""
    return build_price_series(
        load_fixture("provider_zonneplan_quarterly.json"),
        load_fixture("spot_prices.json"),
        provider="zonneplan",
        interval="quarterly",
    )


@pytest.fixture(name="hourly_series")
def hourly_series_fixture() -> PriceSeries:
    """Return the recorded Tibber hourly series."""
    return build_price_series(
        load_fixture("provider_tibber_hourly.json"),
        load_fixture("spot_prices.json"),
        provider="tibber",
        interval="hourly",
    )


def test_series_covers_today_and_tomorrow(series: PriceSeries) -> None:
    """Two days of quarter hour prices give 192 grid points."""
    assert series.points == 192
    covered = series.covered
    assert covered is not None
    assert covered[0] == datetime(2026, 11, 19, 23, 0, tzinfo=UTC)
    assert covered[1] == datetime(2026, 11, 21, 22, 45, tzinfo=UTC)


def test_series_exposes_tariff_metadata(series: PriceSeries) -> None:
    """Tax and provider charge are carried along with the prices."""
    assert series.meta.energy_tax == pytest.approx(0.11085)
    assert series.meta.provider_charge == pytest.approx(0.02)
    assert series.meta.spot_coefficient == pytest.approx(1.0)


def test_known_quarter_price(series: PriceSeries) -> None:
    """A recorded quarter resolves to the published all-in and spot price."""
    point = series.point_at(datetime(2026, 11, 19, 23, 0, tzinfo=UTC))
    assert point is not None
    assert point.all_in == pytest.approx(0.328322)
    assert point.spot == pytest.approx(0.1632)


def test_every_price_satisfies_the_api_formula(series: PriceSeries) -> None:
    """The published price always equals the published components."""
    meta = series.meta
    checked = 0
    for key in sorted(series._grid):
        point = series._grid[key]
        if point.all_in is None or point.spot is None:
            continue
        expected = all_in_from_components(
            point.spot,
            meta.spot_coefficient,
            meta.provider_charge,
            meta.energy_tax,
        )
        assert point.all_in == pytest.approx(expected, abs=1e-9)
        checked += 1
    assert checked == 192


def test_spot_price_varies_within_an_hourly_slot(hourly_series: PriceSeries) -> None:
    """Hourly billing still resolves the quarter hour spot price."""
    early = hourly_series.point_at(datetime(2026, 11, 20, 12, 0, tzinfo=UTC))
    late = hourly_series.point_at(datetime(2026, 11, 20, 12, 30, tzinfo=UTC))
    assert early is not None and late is not None
    assert early.all_in == pytest.approx(late.all_in)
    assert early.spot != late.spot


def test_segments_split_on_price_boundaries(series: PriceSeries) -> None:
    """Energy spanning a tariff change is split at the boundary."""
    start = datetime(2026, 11, 20, 12, 10, tzinfo=UTC)
    end = datetime(2026, 11, 20, 12, 30, tzinfo=UTC)
    segments = series.segments(start, end)

    assert len(segments) == 2
    assert segments[0][0] == start
    assert segments[0][1] == datetime(2026, 11, 20, 12, 15, tzinfo=UTC)
    assert segments[1][0] == datetime(2026, 11, 20, 12, 15, tzinfo=UTC)
    assert segments[1][1] == end
    assert segments[0][2].all_in != segments[1][2].all_in


def test_segments_cover_the_window_without_gaps(series: PriceSeries) -> None:
    """Segments are contiguous and complete."""
    start = datetime(2026, 11, 20, 11, 52, tzinfo=UTC)
    end = datetime(2026, 11, 20, 13, 7, tzinfo=UTC)
    segments = series.segments(start, end)
    assert segments[0][0] == start
    assert segments[-1][1] == end
    for (_, previous_end, _), (next_start, _, _) in itertools.pairwise(segments):
        assert previous_end == next_start


def test_empty_window_yields_no_segments(series: PriceSeries) -> None:
    """A zero length window is rejected rather than returning zero prices."""
    when = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    assert series.segments(when, when) == []


def test_price_is_carried_forward_after_the_end(series: PriceSeries) -> None:
    """The last known price survives a short API outage."""
    just_after = series.covered[1] + timedelta(minutes=30)  # type: ignore[index]
    assert series.all_in_at(just_after) is not None


def test_price_expires_after_the_carry_forward_window(series: PriceSeries) -> None:
    """Stale prices are not used forever."""
    far_future = datetime(2026, 12, 10, tzinfo=UTC)
    assert series.point_at(far_future) is None


def test_average_all_in_is_time_weighted() -> None:
    """The daily average weights each price by its duration."""
    start = FROZEN_TIME
    series = build_series(
        {
            start: (0.10, None),
            start + timedelta(minutes=15): (0.30, None),
            start + timedelta(minutes=30): (0.30, None),
            start + timedelta(minutes=45): (0.30, None),
        }
    )
    # Four quarter slots: one at 0.10, three at 0.30.
    assert series.average_all_in(start, start + timedelta(hours=1)) == pytest.approx(
        0.25
    )


def test_backfills_a_leading_gap() -> None:
    """A series that starts with a missing component is still usable."""
    start = FROZEN_TIME
    series = build_price_series(
        {
            "today": [{"t": "2026-11-20T12:00:00Z", "price": 0.20}],
            "energy_tax": 0.0,
            "provider_charge": 0.0,
            "spot_coefficient": 1.0,
        },
        {"today": [{"date": "2026-11-20T12:15:00Z", "price": 0.10}]},
        provider="test",
        interval="quarterly",
    )
    # The spot value is backfilled from the first known point.
    assert series.spot_at(start) == pytest.approx(0.10)
    assert series.all_in_at(start) == pytest.approx(0.20)


def test_empty_payload_yields_empty_series() -> None:
    """No prices means an empty series, not a crash."""
    series = build_price_series({}, None, provider="zonneplan", interval="quarterly")
    assert not series
    assert series.points == 0
    assert series.point_at(FROZEN_TIME) is None
    assert not series.segments(FROZEN_TIME, FROZEN_TIME + timedelta(hours=1))


def test_missing_spot_prices_leave_all_in_intact() -> None:
    """Cost tracking works without the spot endpoint."""
    series = build_price_series(
        load_fixture("provider_zonneplan_quarterly.json"),
        None,
        provider="zonneplan",
        interval="quarterly",
    )
    assert series.all_in_at(FROZEN_TIME) is not None
    assert series.spot_at(FROZEN_TIME) is None


def test_empty_series_is_falsy() -> None:
    """Empty series are falsy so callers can fall back cleanly."""
    assert not PriceSeries.empty("zonneplan", "quarterly")
    assert PriceMeta() == PriceMeta(0.0, 0.0, 1.0)
