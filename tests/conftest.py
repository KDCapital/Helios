"""Test fixtures for KD Helios."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.helpers import load_fixture

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading the custom integration in every test."""
    yield


@pytest.fixture(autouse=True)
def fast_inverter_writes(monkeypatch):
    """Drop the pause between register writes.

    The pause protects the inverter's Modbus link in production; in tests it
    would only make every write slow, and it deadlocks under a frozen clock
    because asyncio.sleep would never come back.
    """
    monkeypatch.setattr("custom_components.kd_helios.inverter.WRITE_PAUSE", 0)


@pytest.fixture(autouse=True)
def patch_price_api():
    """Serve the recorded epexprijzen.nl payloads instead of the network."""
    provider = load_fixture("provider_zonneplan_quarterly.json")
    spot = load_fixture("spot_prices.json")
    with (
        patch(
            "custom_components.kd_helios.config_flow.async_fetch_provider_prices",
            new=AsyncMock(return_value=provider),
        ),
        patch(
            "custom_components.kd_helios.coordinator.async_fetch_provider_prices",
            new=AsyncMock(return_value=provider),
        ),
        patch(
            "custom_components.kd_helios.coordinator.async_fetch_spot_prices",
            new=AsyncMock(return_value=spot),
        ),
    ):
        yield
