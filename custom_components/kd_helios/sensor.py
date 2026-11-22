"""Sensor platform for KD Helios."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import HeliosConfigEntry
from .accounting import DeviceSnapshot
from .battery import AppliedPlan, ChargePlan
from .const import (
    CURRENCY,
    DOMAIN,
    GRID_MINUTES,
    NAME,
    SLOT_MINUTES,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_GRID,
    UNIT_ERE,
    UNIT_EUR_PER_KWH,
    UNIT_KWH,
    UNIT_W,
)
from .coordinator import HeliosEngine
from .grid import GridSnapshot
from .models import BatteryConfig, DeviceConfig, GridConfig, device_subentries
from .prices import PriceSeries

_LOGGER = logging.getLogger(__name__)

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


@dataclass(frozen=True, kw_only=True)
class HeliosGridSensorDescription(SensorEntityDescription):
    """Describes a grid connection sensor."""

    value_fn: Callable[[GridSnapshot], StateType]


GRID_SENSORS: tuple[HeliosGridSensorDescription, ...] = (
    HeliosGridSensorDescription(
        key="grid_import_energy",
        translation_key="grid_import_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UNIT_KWH,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:transmission-tower-import",
        value_fn=lambda snapshot: snapshot.imported_kwh,
    ),
    HeliosGridSensorDescription(
        key="grid_export_energy",
        translation_key="grid_export_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UNIT_KWH,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:transmission-tower-export",
        value_fn=lambda snapshot: snapshot.exported_kwh,
    ),
    HeliosGridSensorDescription(
        key="grid_import_cost",
        translation_key="grid_import_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:cash-minus",
        value_fn=lambda snapshot: snapshot.imported_cost,
    ),
    HeliosGridSensorDescription(
        key="grid_export_credit",
        translation_key="grid_export_credit",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:cash-plus",
        value_fn=lambda snapshot: snapshot.export_credit,
    ),
    HeliosGridSensorDescription(
        key="grid_fixed_cost",
        translation_key="grid_fixed_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:receipt-text-outline",
        value_fn=lambda snapshot: snapshot.fixed_cost,
    ),
    HeliosGridSensorDescription(
        key="grid_cost",
        translation_key="grid_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:transmission-tower",
        value_fn=lambda snapshot: snapshot.total_cost,
    ),
    HeliosGridSensorDescription(
        key="grid_export_price",
        translation_key="grid_export_price",
        native_unit_of_measurement=UNIT_EUR_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=PRICE_PRECISION,
        icon="mdi:cash-refund",
        value_fn=lambda snapshot: snapshot.export_price,
    ),
    HeliosGridSensorDescription(
        key="grid_import_power",
        translation_key="grid_import_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UNIT_W,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:flash",
        value_fn=lambda snapshot: snapshot.import_power,
    ),
    HeliosGridSensorDescription(
        key="grid_export_power",
        translation_key="grid_export_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UNIT_W,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:flash-outline",
        value_fn=lambda snapshot: snapshot.export_power,
    ),
    HeliosGridSensorDescription(
        key="grid_net_power",
        translation_key="grid_net_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UNIT_W,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:transmission-tower",
        value_fn=lambda snapshot: snapshot.net_power,
    ),
)


def _grid_descriptions_for(config: GridConfig) -> list[HeliosGridSensorDescription]:
    """Return the grid sensors that apply to this connection."""
    descriptions: list[HeliosGridSensorDescription] = []
    for description in GRID_SENSORS:
        if (
            description.key
            in (
                "grid_import_power",
                "grid_export_power",
            )
            and not config.has_power
        ):
            continue
        if description.key == "grid_net_power" and not config.signed_power_entity:
            continue
        descriptions.append(description)
    return descriptions


@dataclass(frozen=True, kw_only=True)
class HeliosBatterySensorDescription(SensorEntityDescription):
    """Describes a battery planning sensor.

    A sensor either reports something about the live plan, or something about
    the plan that was actually written to the inverter.
    """

    value_fn: Callable[[ChargePlan, datetime], StateType] | None = None
    applied_fn: Callable[[AppliedPlan], StateType] | None = None


BATTERY_SENSORS: tuple[HeliosBatterySensorDescription, ...] = (
    HeliosBatterySensorDescription(
        key="battery_charge_energy",
        translation_key="battery_charge_energy",
        native_unit_of_measurement=UNIT_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:battery-arrow-up",
        value_fn=lambda plan, _now: plan.energy_kwh,
    ),
    HeliosBatterySensorDescription(
        key="battery_plan_saving",
        translation_key="battery_plan_saving",
        native_unit_of_measurement=CURRENCY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:piggy-bank-outline",
        value_fn=lambda plan, _now: plan.expected_saving,
    ),
    HeliosBatterySensorDescription(
        key="battery_applied_saving",
        translation_key="battery_applied_saving",
        native_unit_of_measurement=CURRENCY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:cash-check",
        applied_fn=lambda applied: applied.expected_saving,
    ),
    HeliosBatterySensorDescription(
        key="battery_next_charge",
        translation_key="battery_next_charge",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-start",
        value_fn=lambda plan, now: (
            window.start if (window := plan.next_window(now)) else None
        ),
    ),
    HeliosBatterySensorDescription(
        key="battery_status",
        translation_key="battery_status",
        icon="mdi:state-machine",
        value_fn=lambda plan, _now: plan.reason,
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
        data = self.coordinator.data
        if data is None:
            return None
        return data.devices.get(self._subentry_id)

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
            "rejected_deltas": tracker.acc.rejected_deltas,
        }


class HeliosGridSensor(CoordinatorEntity[HeliosEngine], SensorEntity):
    """A sensor belonging to the grid connection."""

    _attr_has_entity_name = True
    entity_description: HeliosGridSensorDescription

    def __init__(
        self,
        coordinator: HeliosEngine,
        config: GridConfig,
        description: HeliosGridSensorDescription,
    ) -> None:
        """Initialise the grid sensor."""
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

    @property
    def _snapshot(self) -> GridSnapshot | None:
        """Return the grid snapshot."""
        data = self.coordinator.data
        return data.grid if data is not None else None

    @property
    def native_value(self) -> StateType:
        """Return the current sensor value."""
        snapshot = self._snapshot
        if snapshot is None:
            return None
        return self.entity_description.value_fn(snapshot)

    @property
    def last_reset(self) -> datetime | None:
        """Return the start of the billing year for the yearly counters.

        Every grid counter restarts on 1 January, so the statistics engine must
        treat each year as its own cycle.
        """
        # Home Assistant only accepts last_reset on totals.
        if self.entity_description.state_class is not SensorStateClass.TOTAL:
            return None
        snapshot = self._snapshot
        if snapshot is None:
            return None
        return datetime(snapshot.year, 1, 1, tzinfo=dt_util.get_default_time_zone())

    @property
    def extra_state_attributes(self) -> dict[str, str | float | bool | None] | None:
        """Expose the saldering state on the total grid cost."""
        if self.entity_description.key != "grid_cost":
            return None
        snapshot = self._snapshot
        if snapshot is None:
            return None
        return {
            "billing_year": snapshot.year,
            "saldering_enabled": snapshot.saldering_enabled,
            "saldering_active": snapshot.saldering_active,
            "netted_kwh": round(snapshot.netted_kwh, 3),
            "open_export_kwh": round(snapshot.open_export_kwh, 3),
            "export_deduction": snapshot.export_deduction,
            "fixed_monthly": snapshot.fixed_monthly,
            "unpriced_energy_kwh": round(snapshot.unpriced_kwh, 4),
            "untracked_gaps": snapshot.untracked_gaps,
            "rejected_deltas": snapshot.rejected_deltas,
        }


class HeliosBatterySensor(CoordinatorEntity[HeliosEngine], SensorEntity):
    """A sensor describing the planned battery charge."""

    _attr_has_entity_name = True
    entity_description: HeliosBatterySensorDescription

    def __init__(
        self,
        coordinator: HeliosEngine,
        config: BatteryConfig,
        description: HeliosBatterySensorDescription,
    ) -> None:
        """Initialise the battery sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._config = config
        self._subentry_id = config.subentry_id
        self._attr_unique_id = f"{config.subentry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config.subentry_id)},
            name=config.name,
            manufacturer=NAME,
            model=config.model_name,
        )

    @property
    def _plan(self) -> ChargePlan | None:
        """Return the current plan."""
        data = self.coordinator.data
        return data.battery if data is not None else None

    @property
    def _applied(self) -> AppliedPlan | None:
        """Return the plan currently programmed into the inverter."""
        return self.coordinator.applied_plan

    @property
    def native_value(self) -> StateType:
        """Return the current sensor value."""
        if self.entity_description.applied_fn is not None:
            applied = self._applied
            if applied is None:
                return None
            return self.entity_description.applied_fn(applied)
        plan = self._plan
        if plan is None or self.entity_description.value_fn is None:
            return None
        return self.entity_description.value_fn(plan, dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose the plan, or the one that is actually in force."""
        if self.entity_description.key == "battery_applied_saving":
            return self._applied_attributes()
        if self.entity_description.key == "battery_status":
            return self._status_attributes()
        if self.entity_description.key != "battery_charge_energy":
            return None
        plan = self._plan
        if plan is None:
            return None
        now = dt_util.utcnow()
        tou = self.coordinator.tou_plan
        applied = self._applied
        schedule = self.coordinator.schedule_plan
        applied_matches = (
            applied is not None
            and schedule is not None
            and [window.start for window in applied.windows]
            == [window.start for window in schedule.windows]
        )
        return {
            "reason": plan.reason,
            "blockers": self.coordinator.blockers.as_list(),
            "solar_credit_kwh": plan.solar_credit_kwh,
            "prices_stale": plan.prices_stale,
            "applied_saving": applied.expected_saving if applied else None,
            "applied_at": applied.applied_at.isoformat() if applied else None,
            "applied_matches_proposal": applied_matches,
            "soc_pct": plan.soc_pct,
            "charge_now": plan.active_window(now) is not None,
            "capacity_kwh": self._config.capacity_kwh,
            "max_charge_kw": self._config.max_charge_kw,
            "reserve_soc_pct": self._config.reserve_soc_pct,
            "target_soc_pct": self._config.target_soc_pct,
            "efficiency_pct": self._config.efficiency_pct,
            "horizon_hours": self._config.horizon_hours,
            "windows": [
                {
                    "start": window.start.isoformat(),
                    "end": window.end.isoformat(),
                    "energy_kwh": round(window.energy_kwh, 3),
                    "drain_kwh": round(window.drain_kwh, 3),
                    "average_price": round(window.average_price, 5),
                    "discharge_price": round(window.discharge_price, 5),
                    "expected_profit": round(window.profit, 2),
                }
                for window in plan.windows
            ],
            "tou_truncated": bool(tou.truncated) if tou else False,
            "tou_programs": [
                {
                    "start": f"{program.hhmm:04d}",
                    "power_w": program.power_w,
                    "soc_pct": program.soc_pct,
                    "source": int(program.source),
                }
                for program in (tou.programs if tou else ())
            ],
        }

    def _status_attributes(self) -> dict[str, object] | None:
        """Explain why the battery is, or is not, charging.

        One row per condition, with the numbers behind it and since when it
        holds: the same registry drives the panel, the log and this sensor.
        """
        plan = self._plan
        coordinator = self.coordinator
        weekly = coordinator.weekly_result
        return {
            "blockers": coordinator.blockers.as_list(),
            "reason": plan.reason if plan else None,
            "solar_credit_kwh": plan.solar_credit_kwh if plan else None,
            "prices_stale": plan.prices_stale if plan else None,
            "weekly_full_charge": bool(weekly.active) if weekly else False,
            "last_control_error": coordinator.last_control_error,
            "write_verified": (coordinator.last_verification or {}).get("verified"),
            "last_decision": (
                coordinator.decision_log[-1] if coordinator.decision_log else None
            ),
            "decisions": list(coordinator.decision_log)[-10:],
        }

    def _applied_attributes(self) -> dict[str, object] | None:
        """Describe the plan that is currently programmed."""
        applied = self._applied
        if applied is None:
            return None
        now = dt_util.utcnow()
        return {
            "applied_at": applied.applied_at.isoformat(),
            "soc_at_apply_pct": applied.soc_pct,
            "energy_kwh": round(applied.energy_kwh, 3),
            "cost": round(applied.cost, 2),
            "truncated": applied.truncated,
            "charging_now": applied.active_window(now) is not None,
            "windows": [
                {
                    "start": window.start.isoformat(),
                    "end": window.end.isoformat(),
                    "energy_kwh": round(window.energy_kwh, 3),
                    "average_price": round(window.average_price, 5),
                    "expected_profit": round(window.profit, 2),
                }
                for window in applied.windows
            ],
            "tou_programs": [
                {
                    "start": f"{program.hhmm:04d}",
                    "power_w": program.power_w,
                    "soc_pct": program.soc_pct,
                    "source": int(program.source),
                }
                for program in applied.programs
            ],
        }


def _async_prune_stale_entities(
    hass: HomeAssistant, entry: HeliosConfigEntry, valid_unique_ids: set[str]
) -> None:
    """Remove registry entries this entry no longer creates.

    Turning off reimbursement or ERE on an existing device would otherwise
    leave its sensors behind as permanently unavailable entities.
    """
    registry = er.async_get(hass)
    for item in list(registry.entities.values()):
        if item.config_entry_id != entry.entry_id:
            continue
        if not item.entity_id.startswith(f"{SENSOR_DOMAIN}."):
            # Other platforms of this integration own their own entities.
            continue
        if item.unique_id in valid_unique_ids:
            continue
        _LOGGER.debug("Removing stale entity %s", item.entity_id)
        registry.async_remove(item.entity_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HeliosConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up KD Helios sensors."""
    runtime = entry.runtime_data

    hub_entities = [
        HeliosPriceSensor(runtime.engine, entry, description)
        for description in HUB_SENSORS
    ]
    valid_unique_ids = {entity.unique_id for entity in hub_entities}
    async_add_entities(hub_entities)

    for subentry in device_subentries(entry):
        config = DeviceConfig.from_subentry(subentry)
        entities = [
            HeliosDeviceSensor(runtime.engine, config, description)
            for description in _descriptions_for(config)
        ]
        valid_unique_ids.update(entity.unique_id for entity in entities)
        async_add_entities(
            entities,
            config_subentry_id=subentry.subentry_id,
        )

    for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_GRID):
        grid_config = GridConfig.from_subentry(subentry)
        entities = [
            HeliosGridSensor(runtime.engine, grid_config, description)
            for description in _grid_descriptions_for(grid_config)
        ]
        valid_unique_ids.update(entity.unique_id for entity in entities)
        async_add_entities(
            entities,
            config_subentry_id=subentry.subentry_id,
        )

    for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_BATTERY):
        battery_config = BatteryConfig.from_subentry(subentry)
        entities = [
            HeliosBatterySensor(runtime.engine, battery_config, description)
            for description in BATTERY_SENSORS
        ]
        valid_unique_ids.update(entity.unique_id for entity in entities)
        async_add_entities(
            entities,
            config_subentry_id=subentry.subentry_id,
        )

    _async_prune_stale_entities(hass, entry, valid_unique_ids)
