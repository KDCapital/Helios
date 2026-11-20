"""Data update coordinators for KD Helios."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .accounting import DeviceSnapshot, DeviceTracker
from .const import (
    ACCOUNTING_UPDATE_INTERVAL,
    DOMAIN,
    PRICE_UPDATE_INTERVAL,
    SUBENTRY_TYPE_DEVICE,
)
from .models import DeviceConfig
from .prices import (
    PriceApiError,
    PriceSeries,
    UnknownProviderError,
    async_fetch_provider_prices,
    async_fetch_spot_prices,
    build_price_series,
)
from .storage import HeliosStorage

_LOGGER = logging.getLogger(__name__)


class PriceCoordinator(DataUpdateCoordinator[PriceSeries]):
    """Fetch provider and spot prices and keep them queryable."""

    def __init__(self, hass: HomeAssistant, provider: str, interval: str) -> None:
        """Initialise the price coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} prices ({provider})",
            update_interval=PRICE_UPDATE_INTERVAL,
        )
        self.provider = provider
        self.interval = interval
        self._last_good = PriceSeries.empty(provider, interval)

    @property
    def series(self) -> PriceSeries:
        """Return the best available price series."""
        data: PriceSeries | None = self.data
        if data:
            return data
        return self._last_good

    async def _async_update_data(self) -> PriceSeries:
        """Fetch prices from epexprijzen.nl."""
        try:
            provider_payload = await async_fetch_provider_prices(
                self.hass, self.provider, self.interval
            )
        except UnknownProviderError as err:
            raise UpdateFailed(
                f"Provider '{self.provider}' is not known to epexprijzen.nl"
            ) from err
        except PriceApiError as err:
            if self._last_good:
                _LOGGER.warning(
                    "Keeping the last known prices for %s: %s", self.provider, err
                )
                return self._last_good
            raise UpdateFailed(str(err)) from err

        spot_payload = await async_fetch_spot_prices(self.hass)
        series = build_price_series(
            provider_payload,
            spot_payload,
            provider=self.provider,
            interval=self.interval,
        )
        if not series:
            raise UpdateFailed(
                f"No usable prices returned for provider '{self.provider}'"
            )
        self._last_good = series
        return series


class HeliosEngine(DataUpdateCoordinator[dict[str, DeviceSnapshot]]):
    """Integrate energy and cost for every tracked device."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        prices: PriceCoordinator,
        storage: HeliosStorage,
    ) -> None:
        """Initialise the accounting engine from the entry's subentries."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} accounting",
            update_interval=ACCOUNTING_UPDATE_INTERVAL,
        )
        self.prices = prices
        self.storage = storage
        self.trackers: dict[str, DeviceTracker] = {}
        for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE):
            config = DeviceConfig.from_subentry(subentry)
            self.trackers[config.subentry_id] = DeviceTracker(
                hass,
                config,
                storage.device_state(config.subentry_id),
            )

    @callback
    def async_prices_updated(self) -> None:
        """Recompute snapshots when a new price series arrives.

        Registering this also keeps the price coordinator polling: a
        ``DataUpdateCoordinator`` only reschedules its interval while it has at
        least one listener, so without this the prices would be fetched once at
        setup and never again.
        """
        self.hass.async_create_task(
            self.async_request_refresh(), f"{DOMAIN} price update"
        )

    async def _async_update_data(self) -> dict[str, DeviceSnapshot]:
        """Run one accounting tick."""
        now = dt_util.utcnow()
        series = self.prices.series
        available_series = series if series else None

        snapshots: dict[str, DeviceSnapshot] = {}
        for subentry_id, tracker in self.trackers.items():
            tracker.update(now, available_series)
            snapshots[subentry_id] = tracker.snapshot(now, available_series)

        self.storage.async_set_devices(
            {
                subentry_id: tracker.acc.as_dict()
                for subentry_id, tracker in self.trackers.items()
            }
        )
        return snapshots
