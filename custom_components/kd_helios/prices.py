"""Price retrieval and price modelling for KD Helios.

Data source: https://epexprijzen.nl
------------------------------
Two endpoints are used:

``/api/v1/prices/{provider}/{interval}``
    Consumer prices for a specific Dutch energy provider. Prices are all-in:
    they include VAT, energy tax and the provider charge. The payload also
    carries the ``energy_tax``, ``provider_charge`` and ``spot_coefficient``
    components, both VAT inclusive.

``/api/prices``
    Raw EPEX day-ahead spot prices, excluding VAT and excluding energy tax,
    at a 15 minute resolution.

Both payloads are normalised onto a single 15 minute UTC grid so that energy
can be priced exactly, even when a provider bills per hour while the spot
market settles per quarter.
"""

from __future__ import annotations

import asyncio
import logging
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aiohttp import ClientError, ClientResponseError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import (
    GRID_MINUTES,
    PRICE_CARRY_FORWARD,
    PROVIDER_PRICE_URL,
    SLOT_MINUTES,
    SPOT_PRICE_URL,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20


class PriceApiError(HomeAssistantError):
    """Raised when the price API cannot deliver usable data."""


class UnknownProviderError(PriceApiError):
    """Raised when the price API does not know the configured provider."""


@dataclass(frozen=True, slots=True)
class PricePoint:
    """Price components for a single point on the price grid."""

    #: All-in consumer price in EUR/kWh, including VAT and energy tax.
    all_in: float | None
    #: EPEX day-ahead spot price in EUR/kWh, excluding VAT and energy tax.
    spot: float | None


@dataclass(frozen=True, slots=True)
class PriceMeta:
    """Provider tariff metadata, VAT inclusive as returned by the API."""

    energy_tax: float = 0.0
    provider_charge: float = 0.0
    spot_coefficient: float = 1.0


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an API timestamp into an aware UTC datetime."""
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    return dt_util.as_utc(parsed)


def _as_float(value: Any) -> float | None:
    """Coerce an API value to float, returning None when unusable."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _grid_floor(when: datetime) -> datetime:
    """Floor a UTC datetime to the price grid."""
    return when.replace(
        minute=when.minute - when.minute % GRID_MINUTES,
        second=0,
        microsecond=0,
    )


async def _async_get_json(hass: HomeAssistant, url: str) -> Any:
    """Fetch JSON from the price API."""
    session = async_get_clientsession(hass)
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT):
            response = await session.get(url)
            response.raise_for_status()
            return await response.json(content_type=None)
    except TimeoutError as err:
        raise PriceApiError(f"Timeout while fetching {url}") from err
    except ClientResponseError as err:
        if err.status == 404:
            raise UnknownProviderError(f"Unknown provider at {url}") from err
        raise PriceApiError(f"Price API returned HTTP {err.status} for {url}") from err
    except (ClientError, ValueError) as err:
        raise PriceApiError(f"Could not fetch {url}: {err}") from err


async def async_fetch_provider_prices(
    hass: HomeAssistant, provider: str, interval: str
) -> Mapping[str, Any]:
    """Fetch all-in consumer prices for a provider."""
    url = PROVIDER_PRICE_URL.format(provider=provider, interval=interval)
    payload = await _async_get_json(hass, url)
    if not isinstance(payload, Mapping):
        raise PriceApiError(f"Unexpected price payload from {url}")
    if not payload.get("today") and not payload.get("tomorrow"):
        raise PriceApiError(f"No prices published yet at {url}")
    return payload


async def async_fetch_spot_prices(hass: HomeAssistant) -> Mapping[str, Any] | None:
    """Fetch raw EPEX spot prices.

    Spot prices are only needed for the dynamic reimbursement formula. A
    failure here must not take down the integration, so errors are logged and
    ``None`` is returned.
    """
    try:
        payload = await _async_get_json(hass, SPOT_PRICE_URL)
    except PriceApiError as err:
        _LOGGER.warning("Could not fetch EPEX spot prices: %s", err)
        return None
    return payload if isinstance(payload, Mapping) else None


class PriceSeries:
    """A normalised, queryable timeline of all-in and spot prices."""

    __slots__ = ("_grid", "_keys", "_meta", "interval", "provider")

    def __init__(
        self,
        grid: Mapping[datetime, PricePoint],
        meta: PriceMeta,
        provider: str,
        interval: str,
    ) -> None:
        """Initialise the series from an already normalised grid."""
        self._grid = dict(grid)
        self._keys = sorted(self._grid)
        self._meta = meta
        self.provider = provider
        self.interval = interval

    def __bool__(self) -> bool:
        """Return True when the series holds at least one priced point."""
        return bool(self._keys)

    def __repr__(self) -> str:
        """Return a debug representation."""
        if not self._keys:
            return f"<PriceSeries {self.provider} empty>"
        return (
            f"<PriceSeries {self.provider} {self.interval} "
            f"{self._keys[0].isoformat()}..{self._keys[-1].isoformat()}>"
        )

    @classmethod
    def empty(cls, provider: str, interval: str) -> PriceSeries:
        """Return an empty series."""
        return cls({}, PriceMeta(), provider, interval)

    @property
    def meta(self) -> PriceMeta:
        """Return the provider tariff metadata."""
        return self._meta

    @property
    def points(self) -> int:
        """Return the number of priced grid points."""
        return len(self._keys)

    @property
    def covered(self) -> tuple[datetime, datetime] | None:
        """Return the first and last grid point."""
        if not self._keys:
            return None
        return self._keys[0], self._keys[-1]

    def point_at(self, when: datetime) -> PricePoint | None:
        """Return the price point governing ``when``.

        The most recent known point is carried forward, so a short API outage
        never leaves consumed energy unpriced. Beyond
        :data:`PRICE_CARRY_FORWARD` the point is considered unknown.
        """
        if not self._keys:
            return None
        key = _grid_floor(dt_util.as_utc(when))
        index = bisect_right(self._keys, key) - 1
        if index < 0:
            return None
        found = self._keys[index]
        if key - found > PRICE_CARRY_FORWARD:
            return None
        return self._grid[found]

    def all_in_at(self, when: datetime) -> float | None:
        """Return the all-in price in EUR/kWh at ``when``."""
        point = self.point_at(when)
        return point.all_in if point else None

    def spot_at(self, when: datetime) -> float | None:
        """Return the spot price in EUR/kWh at ``when``."""
        point = self.point_at(when)
        return point.spot if point else None

    def segments(
        self, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime, PricePoint | None]]:
        """Split ``[start, end)`` into pieces with a constant price.

        Each returned piece is bounded by a price grid boundary, so pricing
        energy across a tariff change is exact.
        """
        start = dt_util.as_utc(start)
        end = dt_util.as_utc(end)
        if end <= start or not self._keys:
            return []

        step = timedelta(minutes=GRID_MINUTES)
        segments: list[tuple[datetime, datetime, PricePoint | None]] = []
        cursor = start
        while cursor < end:
            chunk_end = min(end, _grid_floor(cursor) + step)
            if chunk_end <= cursor:  # pragma: no cover - defensive
                chunk_end = end
            segments.append((cursor, chunk_end, self.point_at(cursor)))
            cursor = chunk_end
        return segments

    def priced_slots(
        self, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime, float]]:
        """Return contiguous slices that all have a known all-in price."""
        slots: list[tuple[datetime, datetime, float]] = []
        for seg_start, seg_end, point in self.segments(start, end):
            if point is None or point.all_in is None:
                continue
            slots.append((seg_start, seg_end, point.all_in))
        return slots

    def highest_all_in(self, start: datetime, end: datetime) -> float | None:
        """Return the highest all-in price in a window."""
        prices = [price for _, _, price in self.priced_slots(start, end)]
        return max(prices) if prices else None

    def hourly_average(self) -> PriceSeries:
        """Return a series where every point holds its hour average.

        Used when a reimbursement settles per hour while the contract settles
        per quarter: the reimbursement then sees one price per hour instead of
        the quarter hour price at the moment of charging.
        """
        if not self._keys:
            return self

        buckets: dict[datetime, list[PricePoint]] = {}
        for key in self._keys:
            hour = key.replace(minute=0, second=0, microsecond=0)
            buckets.setdefault(hour, []).append(self._grid[key])

        averages: dict[datetime, PricePoint] = {}
        for hour, points in buckets.items():
            all_in = [point.all_in for point in points if point.all_in is not None]
            spot = [point.spot for point in points if point.spot is not None]
            averages[hour] = PricePoint(
                all_in=sum(all_in) / len(all_in) if all_in else None,
                spot=sum(spot) / len(spot) if spot else None,
            )

        grid = {
            key: averages[key.replace(minute=0, second=0, microsecond=0)]
            for key in self._keys
        }
        return PriceSeries(grid, self._meta, self.provider, self.interval)

    def average_all_in(self, start: datetime, end: datetime) -> float | None:
        """Return the time weighted average all-in price over a period."""
        total_weight = 0.0
        total = 0.0
        for seg_start, seg_end, point in self.segments(start, end):
            if point is None or point.all_in is None:
                continue
            weight = (seg_end - seg_start).total_seconds()
            total += point.all_in * weight
            total_weight += weight
        if total_weight <= 0:
            return None
        return total / total_weight


def build_price_series(
    provider_payload: Mapping[str, Any],
    spot_payload: Mapping[str, Any] | None,
    *,
    provider: str,
    interval: str,
) -> PriceSeries:
    """Build a :class:`PriceSeries` from raw API payloads."""
    meta = PriceMeta(
        energy_tax=_as_float(provider_payload.get("energy_tax")) or 0.0,
        provider_charge=_as_float(provider_payload.get("provider_charge")) or 0.0,
        spot_coefficient=_as_float(provider_payload.get("spot_coefficient")) or 1.0,
    )

    raw: dict[datetime, dict[str, float]] = {}
    step = timedelta(minutes=GRID_MINUTES)
    slot_span = timedelta(minutes=SLOT_MINUTES.get(interval, GRID_MINUTES))
    slots_per_price = max(1, int(slot_span / step))

    for bucket in ("today", "tomorrow"):
        for item in provider_payload.get(bucket) or []:
            if not isinstance(item, Mapping):
                continue
            start = _parse_timestamp(item.get("t"))
            price = _as_float(item.get("price"))
            if start is None or price is None:
                continue
            cursor = _grid_floor(start)
            for _ in range(slots_per_price):
                raw.setdefault(cursor, {})["all_in"] = price
                cursor += step

    if spot_payload:
        for bucket in ("today", "tomorrow"):
            for item in spot_payload.get(bucket) or []:
                if not isinstance(item, Mapping):
                    continue
                start = _parse_timestamp(item.get("date") or item.get("t"))
                price = _as_float(item.get("price"))
                if start is None or price is None:
                    continue
                raw.setdefault(_grid_floor(start), {})["spot"] = price

    if not raw:
        return PriceSeries.empty(provider, interval)

    keys = sorted(raw)

    # Backfill leading gaps, then carry every value forward so each grid point
    # is fully populated. This keeps accounting complete when the two payloads
    # do not share exactly the same coverage.
    first_all_in = next((raw[k]["all_in"] for k in keys if "all_in" in raw[k]), None)
    first_spot = next((raw[k]["spot"] for k in keys if "spot" in raw[k]), None)
    last_all_in = first_all_in
    last_spot = first_spot

    grid: dict[datetime, PricePoint] = {}
    for key in keys:
        entry = raw[key]
        if "all_in" in entry:
            last_all_in = entry["all_in"]
        if "spot" in entry:
            last_spot = entry["spot"]
        grid[key] = PricePoint(all_in=last_all_in, spot=last_spot)

    return PriceSeries(grid, meta, provider, interval)
