"""Tests for the epexprijzen.nl HTTP client."""

from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.kd_helios.accounting import DeviceTracker
from custom_components.kd_helios.const import (
    PROVIDER_PRICE_URL,
    SPOT_PRICE_URL,
    EreMode,
)
from custom_components.kd_helios.models import DeviceConfig, EreConfig
from custom_components.kd_helios.prices import (
    PriceApiError,
    UnknownProviderError,
    async_fetch_provider_prices,
    async_fetch_spot_prices,
    build_price_series,
)
from tests.helpers import (
    FROZEN_TIME,
    flat_series,
    load_fixture,
    set_energy_state,
)

PROVIDER_URL = PROVIDER_PRICE_URL.format(provider="zonneplan", interval="quarterly")


async def test_provider_prices_are_fetched(hass, aioclient_mock) -> None:
    """A healthy response is parsed into the raw payload."""
    aioclient_mock.get(
        PROVIDER_URL, json=load_fixture("provider_zonneplan_quarterly.json")
    )

    payload = await async_fetch_provider_prices(hass, "zonneplan", "quarterly")

    assert payload["today"]
    assert payload["energy_tax"] == pytest.approx(0.11085)
    assert aioclient_mock.call_count == 1


async def test_unknown_provider_is_reported(hass, aioclient_mock) -> None:
    """A 404 becomes a dedicated error so the UI can explain it."""
    aioclient_mock.get(PROVIDER_URL, status=404, json={"error": "Unknown provider"})

    with pytest.raises(UnknownProviderError):
        await async_fetch_provider_prices(hass, "zonneplan", "quarterly")


async def test_server_error_is_reported(hass, aioclient_mock) -> None:
    """A server error is a connectivity problem, not an unknown provider."""
    aioclient_mock.get(PROVIDER_URL, status=500)

    with pytest.raises(PriceApiError) as err:
        await async_fetch_provider_prices(hass, "zonneplan", "quarterly")

    assert not isinstance(err.value, UnknownProviderError)


async def test_invalid_json_is_reported(hass, aioclient_mock) -> None:
    """Broken JSON does not leak a ValueError out of the coordinator."""
    aioclient_mock.get(PROVIDER_URL, text="<html>maintenance</html>")

    with pytest.raises(PriceApiError):
        await async_fetch_provider_prices(hass, "zonneplan", "quarterly")


async def test_empty_price_payload_is_rejected(hass, aioclient_mock) -> None:
    """A payload without any slots is unusable."""
    aioclient_mock.get(
        PROVIDER_URL, json={"today": [], "tomorrow": [], "energy_tax": 0.11}
    )

    with pytest.raises(PriceApiError):
        await async_fetch_provider_prices(hass, "zonneplan", "quarterly")


async def test_spot_prices_are_fetched(hass, aioclient_mock) -> None:
    """The spot endpoint is fetched independently."""
    aioclient_mock.get(SPOT_PRICE_URL, json=load_fixture("spot_prices.json"))

    payload = await async_fetch_spot_prices(hass)

    assert payload is not None
    assert payload["today"]


async def test_spot_failure_does_not_break_the_integration(
    hass, aioclient_mock
) -> None:
    """Spot prices are optional: a failure yields None rather than an error."""
    aioclient_mock.get(SPOT_PRICE_URL, status=503)

    assert await async_fetch_spot_prices(hass) is None


def test_malformed_slots_are_skipped() -> None:
    """Junk entries do not poison a payload that also holds valid slots."""
    series = build_price_series(
        {
            "today": [
                "not a dict",
                {"t": None, "price": 0.5},
                {"t": "2026-11-20T12:00:00Z", "price": "not a number"},
                {"t": "2026-11-20T12:15:00Z", "price": 0.25},
            ],
        },
        {
            "today": [
                {"date": None, "price": 1.0},
                {"date": "2026-11-20T12:15:00Z", "price": 0.1},
            ]
        },
        provider="test",
        interval="quarterly",
    )

    assert series.points == 1
    # The first known slot is 12:15; nothing exists before it.
    slot = FROZEN_TIME + timedelta(minutes=15)
    assert series.all_in_at(FROZEN_TIME) is None
    assert series.all_in_at(slot) == pytest.approx(0.25)
    assert series.spot_at(slot) == pytest.approx(0.1)


def test_missing_metadata_gets_safe_defaults() -> None:
    """A payload without tariff components still builds a usable series."""
    series = build_price_series(
        {"today": [{"t": "2026-11-20T12:00:00Z", "price": 0.3}]},
        None,
        provider="test",
        interval="quarterly",
    )

    assert series.meta.energy_tax == 0.0
    assert series.meta.provider_charge == 0.0
    # A coefficient of zero would erase the spot price, so it defaults to 1.
    assert series.meta.spot_coefficient == 1.0


async def test_power_sensor_without_a_unit_is_watt(hass) -> None:
    """Sensors that omit a unit are assumed to use the expected unit."""
    tracker = DeviceTracker(
        hass,
        DeviceConfig(
            subentry_id="d",
            name="Plug",
            power_entity="sensor.plug_power",
            energy_entity=None,
        ),
    )
    series = flat_series(FROZEN_TIME, all_in=0.2)

    hass.states.async_set("sensor.plug_power", "1500")
    tracker.update(FROZEN_TIME, series)

    assert tracker.snapshot(FROZEN_TIME, series).power == pytest.approx(1500.0)


async def test_non_numeric_state_is_treated_as_unavailable(hass) -> None:
    """A sensor reporting junk must not crash the accounting tick."""
    tracker = DeviceTracker(
        hass,
        DeviceConfig(subentry_id="d", name="Plug", energy_entity="sensor.plug_energy"),
    )
    series = flat_series(FROZEN_TIME, all_in=0.2)

    hass.states.async_set("sensor.plug_energy", "on")
    tracker.update(FROZEN_TIME, series)

    snapshot = tracker.snapshot(FROZEN_TIME, series)
    assert snapshot.source_available is False
    assert snapshot.energy_kwh == 0.0


async def test_live_ere_price_entity_overrides_the_fixed_price(hass) -> None:
    """A live ERE price sensor replaces the configured price."""
    tracker = DeviceTracker(
        hass,
        DeviceConfig(
            subentry_id="d",
            name="EV",
            energy_entity="sensor.ev_energy",
            ere=EreConfig(
                mode=EreMode.FIXED,
                ere_per_kwh=0.25,
                ere_price=4.0,
                price_entity="sensor.ere_price",
            ),
        ),
    )
    series = flat_series(FROZEN_TIME, all_in=0.2)

    hass.states.async_set("sensor.ere_price", "5.0")
    set_energy_state(hass, "sensor.ev_energy", 10.0)
    tracker.update(FROZEN_TIME, series)
    set_energy_state(hass, "sensor.ev_energy", 12.0)
    tracker.update(FROZEN_TIME + timedelta(minutes=1), series)

    snapshot = tracker.snapshot(FROZEN_TIME + timedelta(minutes=1), series)
    assert snapshot.ere_value == pytest.approx(2.0 * 0.25 * 5.0)
