from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]

from tests.helpers import load_fixture  # noqa: E402


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def patch_price_api():
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
