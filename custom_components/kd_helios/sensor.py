"""Sensor platform for KD Helios."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import HeliosConfigEntry
from .accounting import DeviceSnapshot
from .const import (
    CURRENCY,
    DOMAIN,
    GRID_MINUTES,
    NAME,
    SLOT_MINUTES,
    SUBENTRY_TYPE_DEVICE,
    UNIT_ERE,
    UNIT_EUR_PER_KWH,
    UNIT_KWH,
    UNIT_W,
)
from .coordinator import HeliosEngine
from .models import DeviceConfig
from .prices import PriceSeries

PARALLEL_UPDATES = 0

PRICE_PRECISION = 5


# ---------------------------------------------------------------------------
# Hub (tariff) sensors
# ---------------------------------------------------------------------------
def _current_price(series: PriceSeries, now: datetime) -> float | None:
    """Return the all-in price right now."""
    return series.all_in_at(now)


def _next_price(series: PriceSeries, now: datetime) -> float | None:
    """Return the all-in price for the next price slot.

    The step follows the provider's billing interval, so an hourly contract
    looks one hour ahead instead of repeating the current quarter.
    """
    step = SLOT_MINUTES.get(series.interval, GRID_MINUTES)
    return series.all_in_at(now + timedelta(minutes=step))


def _spot_price(series: PriceSeries, now: datetime) -> float | None:
    """Return the raw EPEX spot price right now."""
    return series.spot_at(now)


def _average_price_today(series: PriceSeries, now: datetime) -> float | None:
    """Return the average all-in price for the local day."""
    start = dt_util.as_local(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return series.average_all_in(start, start + timedelta(days=1))


def _energy_tax(series: PriceSeries, _now: datetime) -> float | None:
    """Return the energy tax component of the tariff."""
    return series.meta.energy_tax


def _provider_charge(series: PriceSeries, _now: datetime) -> float | None:
    """Return the provider charge component of the tariff."""
    return series.meta.provider_charge


@dataclass(frozen=True, kw_only=True)
class HeliosPriceSensorDescription(SensorEntityDescription):
    """Describes a tariff sensor."""

    value_fn: Callable[[PriceSeries, datetime], StateType]


HUB_SENSORS: tuple[HeliosPriceSensorDescription, ...] = (
    HeliosPriceSensorDescription(
        key="current_price",
        translation_key="current_price",
        native_unit_of_measurement=UNIT_EUR_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=PRICE_PRECISION,
        icon="mdi:currency-eur",
        value_fn=_current_price,
    ),
    HeliosPriceSensorDescription(
        key="next_price",
        translation_key="next_price",
        native_unit_of_measurement=UNIT_EUR_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=PRICE_PRECISION,
        icon="mdi:clock-outline",
        value_fn=_next_price,
    ),
    HeliosPriceSensorDescription(
        key="spot_price",
        translation_key="spot_price",
        native_unit_of_measurement=UNIT_EUR_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=PRICE_PRECISION,
        icon="mdi:chart-line",
        value_fn=_spot_price,
    ),
    HeliosPriceSensorDescription(
        key="average_price_today",
        translation_key="average_price_today",
        native_unit_of_measurement=UNIT_EUR_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=PRICE_PRECISION,
        icon="mdi:chart-timeline-variant",
        value_fn=_average_price_today,
    ),
    HeliosPriceSensorDescription(
        key="energy_tax",
        translation_key="energy_tax",
        native_unit_of_measurement=UNIT_EUR_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=PRICE_PRECISION,
        icon="mdi:bank",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_energy_tax,
    ),
    HeliosPriceSensorDescription(
        key="provider_charge",
        translation_key="provider_charge",
        native_unit_of_measurement=UNIT_EUR_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=PRICE_PRECISION,
        icon="mdi:storefront",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_provider_charge,
    ),
)


# ---------------------------------------------------------------------------
# Per-device sensors
# ---------------------------------------------------------------------------
@dataclass(frozen=True, kw_only=True)
class HeliosDeviceSensorDescription(SensorEntityDescription):
    """Describes a device cost sensor."""

    value_fn: Callable[[DeviceSnapshot], StateType]


DEVICE_SENSORS: tuple[HeliosDeviceSensorDescription, ...] = (
    HeliosDeviceSensorDescription(
        key="energy",
        translation_key="energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UNIT_KWH,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        icon="mdi:lightning-bolt",
        value_fn=lambda snapshot: snapshot.energy_kwh,
    ),
    HeliosDeviceSensorDescription(
        key="cost",
        translation_key="cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:cash-minus",
        value_fn=lambda snapshot: snapshot.cost,
    ),
    HeliosDeviceSensorDescription(
        key="cost_today",
        translation_key="cost_today",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:calendar-today",
        value_fn=lambda snapshot: snapshot.cost_today,
    ),
    HeliosDeviceSensorDescription(
        key="current_price",
        translation_key="current_price",
        native_unit_of_measurement=UNIT_EUR_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=PRICE_PRECISION,
        icon="mdi:currency-eur",
        value_fn=lambda snapshot: snapshot.price,
    ),
    HeliosDeviceSensorDescription(
        key="power",
        translation_key="power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UNIT_W,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:flash",
        value_fn=lambda snapshot: snapshot.power,
    ),
    HeliosDeviceSensorDescription(
        key="reimbursement",
        translation_key="reimbursement",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:cash-plus",
        value_fn=lambda snapshot: snapshot.reimbursement,
    ),
    HeliosDeviceSensorDescription(
        key="reimbursement_rate",
        translation_key="reimbursement_rate",
        native_unit_of_measurement=UNIT_EUR_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=PRICE_PRECISION,
        icon="mdi:briefcase-account",
        value_fn=lambda snapshot: snapshot.reimbursement_rate,
    ),
    HeliosDeviceSensorDescription(
        key="ere_value",
        translation_key="ere_value",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:certificate",
        value_fn=lambda snapshot: snapshot.ere_value,
    ),
    HeliosDeviceSensorDescription(
        key="ere_credits",
        translation_key="ere_credits",
        native_unit_of_measurement=UNIT_ERE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:certificate-outline",
        value_fn=lambda snapshot: snapshot.ere_credits,
    ),
    HeliosDeviceSensorDescription(
        key="net_cost",
        translation_key="net_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:cash-check",
        value_fn=lambda snapshot: snapshot.net_cost,
    ),
)


def _descriptions_for(config: DeviceConfig) -> list[HeliosDeviceSensorDescription]:
    """Return the sensor descriptions that apply to a device."""
    descriptions: list[HeliosDeviceSensorDescription] = []
    for description in DEVICE_SENSORS:
        if description.key == "power" and not config.power_entity:
            continue
        if description.key in ("reimbursement", "reimbursement_rate") and (
            not config.has_reimbursement
        ):
            continue
        if description.key in ("ere_value", "ere_credits") and not config.has_ere:
            continue
        if description.key == "net_cost" and not config.has_offsets:
            continue
        descriptions.append(description)
    return descriptions


class HeliosPriceSensor(CoordinatorEntity[HeliosEngine], SensorEntity):
    """A tariff sensor for the configured provider.

    These listen to the accounting engine rather than to the price
    coordinator: the engine ticks every minute, so ``current price`` flips at
    the quarter hour boundary instead of waiting for the next API poll. The
    engine reads the price coordinator's cached series, so no extra requests
    are made.
    """

    _attr_has_entity_name = True
    entity_description: HeliosPriceSensorDescription

    def __init__(
        self,
        coordinator: HeliosEngine,
        entry: HeliosConfigEntry,
        description: HeliosPriceSensorDescription,
    ) -> None:
        """Initialise the tariff sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=NAME,
            manufacturer=NAME,
            model="Dynamic energy cost tracker",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://epexprijzen.nl",
        )

    @property
    def native_value(self) -> StateType:
        """Return the current sensor value."""
        series = self.coordinator.prices.series
        if not series:
            return None
        return self.entity_description.value_fn(series, dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Expose the tariff source on the current price sensor."""
        if self.entity_description.key != "current_price":
            return None
        return {
            "provider": self.coordinator.prices.provider,
            "price_interval": self.coordinator.prices.interval,
            "source": "https://epexprijzen.nl",
        }


class HeliosDeviceSensor(CoordinatorEntity[HeliosEngine], SensorEntity):
    """A cost or energy sensor belonging to a tracked device."""

    _attr_has_entity_name = True
    entity_description: HeliosDeviceSensorDescription

    def __init__(
        self,
        coordinator: HeliosEngine,
        config: DeviceConfig,
        description: HeliosDeviceSensorDescription,
    ) -> None:
        """Initialise the device sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._subentry_id = config.subentry_id
        self._attr_unique_id = f"{config.subentry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config.subentry_id)},
            name=config.name,
            manufacturer=NAME,
            model=config.model_name,
        )
        self._config = config

    @property
    def _snapshot(self) -> DeviceSnapshot | None:
        """Return the snapshot for this device."""
        return (self.coordinator.data or {}).get(self._subentry_id)

    @property
    def native_value(self) -> StateType:
        """Return the current sensor value."""
        snapshot = self._snapshot
        if snapshot is None:
            return None
        return self.entity_description.value_fn(snapshot)

    @property
    def last_reset(self) -> datetime | None:
        """Return the local midnight that the daily cost counter restarts at.

        Without this the statistics engine would read every daily reset as a
        loss, so the long term sum of ``cost today`` would drift.
        """
        if self.entity_description.key != "cost_today":
            return None
        snapshot = self._snapshot
        if snapshot is None:
            return None
        return dt_util.as_local(snapshot.last_update).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    @property
    def extra_state_attributes(self) -> dict[str, str | float | None] | None:
        """Expose accounting health on the cost sensor."""
        if self.entity_description.key != "cost":
            return None
        snapshot = self._snapshot
        tracker = self.coordinator.trackers.get(self._subentry_id)
        if snapshot is None or tracker is None:
            return None
        return {
            "source_entity": snapshot.source_entity,
            "source_available": snapshot.source_available,
            "unpriced_energy_kwh": round(snapshot.unpriced_kwh, 4),
            "untracked_gaps": tracker.acc.untracked_gaps,
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HeliosConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up KD Helios sensors."""
    runtime = entry.runtime_data

    async_add_entities(
        HeliosPriceSensor(runtime.engine, entry, description)
        for description in HUB_SENSORS
    )

    for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE):
        config = DeviceConfig.from_subentry(subentry)
        async_add_entities(
            (
                HeliosDeviceSensor(runtime.engine, config, description)
                for description in _descriptions_for(config)
            ),
            config_subentry_id=subentry.subentry_id,
        )
