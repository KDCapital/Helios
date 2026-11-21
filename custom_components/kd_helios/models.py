"""Typed configuration models for KD Helios devices."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

from homeassistant.config_entries import ConfigEntry, ConfigSubentry

from .battery import BatterySettings
from .const import (
    CONF_ALLOW_INVERTER_WRITES,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_HORIZON_HOURS,
    CONF_EFFICIENCY,
    CONF_ENERGY_ENTITY,
    CONF_ERE_CREDITS_ENTITY,
    CONF_ERE_KWH_ENTITY,
    CONF_ERE_MODE,
    CONF_ERE_PER_KWH,
    CONF_ERE_PRICE,
    CONF_ERE_PRICE_ENTITY,
    CONF_EV_CAPACITY_ENTITY,
    CONF_EV_DEPARTURE_ENTITY,
    CONF_EV_DEPARTURE_WEEKDAY,
    CONF_EV_DEPARTURE_WEEKEND,
    CONF_EV_PACK_KWH,
    CONF_EV_POWER_ENTITY,
    CONF_EV_SOC_ENTITY,
    CONF_EV_TARGET_SOC,
    CONF_EXPORT_DEDUCTION,
    CONF_EXPORT_ENTITY,
    CONF_EXPORT_POWER_ENTITY,
    CONF_EXPORT_PRICE_SOURCE,
    CONF_FUSE_AMPS,
    CONF_FUSE_MARGIN_PCT,
    CONF_GRID_FEE_MONTHLY,
    CONF_GRID_POWER_ENTITY,
    CONF_HOUSE_LOAD_ENTITY,
    CONF_IMPORT_CAP_KW,
    CONF_IMPORT_ENTITY,
    CONF_IMPORT_POWER_ENTITY,
    CONF_INVERTER_DEVICE,
    CONF_KIND,
    CONF_MAX_CHARGE_KW,
    CONF_METER_DEVICE,
    CONF_NAME,
    CONF_PHASES,
    CONF_POWER_ENTITY,
    CONF_PRIORITY,
    CONF_REIMBURSEMENT_BASIS,
    CONF_REIMBURSEMENT_INTERVAL,
    CONF_REIMBURSEMENT_MODE,
    CONF_REIMBURSEMENT_PERCENT,
    CONF_REIMBURSEMENT_RATE,
    CONF_REIMBURSEMENT_SURCHARGE,
    CONF_RESERVE_SOC,
    CONF_RESTORE_WORK_MODE,
    CONF_SALDERING,
    CONF_SOC_ENTITY,
    CONF_SUPPLY_COST_MONTHLY,
    CONF_TARGET_SOC,
    DEFAULT_DEVICE_KIND,
    DEFAULT_ERE_MODE,
    DEFAULT_MARKET_PRICE_BASIS,
    DEFAULT_REIMBURSEMENT_INTERVAL,
    DEFAULT_REIMBURSEMENT_MODE,
    DEFAULT_REIMBURSEMENT_PERCENT,
    SUBENTRY_TYPE_DEVICE,
    SUBENTRY_TYPE_EV_CHARGER,
    DeviceKind,
    EreMode,
    ExportPriceSource,
    MarketPriceBasis,
    PriceInterval,
    Priority,
    ReimbursementMode,
)
from .prices import PricePoint
from .tariff import reimbursement_rate

_EnumT = TypeVar("_EnumT", bound=StrEnum)


def as_float(value: Any, default: float = 0.0) -> float:
    """Coerce a config value to float."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_bool(value: Any, default: bool = False) -> bool:
    """Coerce a config value to a boolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    if value is None:
        return default
    return bool(value)


def as_entity_id(value: Any) -> str | None:
    """Coerce a config value to an entity id."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _as_enum(enum_type: type[_EnumT], value: Any, default: _EnumT) -> _EnumT:
    """Coerce a config value to an enum member."""
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(str(value))
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class ReimbursementConfig:
    """How an employer reimburses the energy a device consumes."""

    mode: ReimbursementMode = ReimbursementMode.NONE
    fixed_rate: float = 0.0
    surcharge: float = 0.0
    percent: float = DEFAULT_REIMBURSEMENT_PERCENT
    basis: MarketPriceBasis = MarketPriceBasis.SPOT
    interval: PriceInterval = DEFAULT_REIMBURSEMENT_INTERVAL

    @property
    def enabled(self) -> bool:
        """Return True when reimbursement is active."""
        return self.mode is not ReimbursementMode.NONE

    def rate(self, point: PricePoint | None) -> float | None:
        """Return the reimbursement in EUR/kWh for a price point."""
        if not self.enabled:
            return None
        if point is None:
            return self.fixed_rate if self.mode is ReimbursementMode.FIXED else None
        return reimbursement_rate(
            self.mode,
            fixed_rate=self.fixed_rate,
            surcharge=self.surcharge,
            percent=self.percent,
            basis=self.basis,
            spot=point.spot,
            all_in=point.all_in,
        )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ReimbursementConfig:
        """Build the config from raw subentry data."""
        return cls(
            mode=_as_enum(
                ReimbursementMode,
                data.get(CONF_REIMBURSEMENT_MODE),
                DEFAULT_REIMBURSEMENT_MODE,
            ),
            fixed_rate=as_float(data.get(CONF_REIMBURSEMENT_RATE)),
            surcharge=as_float(data.get(CONF_REIMBURSEMENT_SURCHARGE)),
            percent=as_float(
                data.get(CONF_REIMBURSEMENT_PERCENT), DEFAULT_REIMBURSEMENT_PERCENT
            ),
            basis=_as_enum(
                MarketPriceBasis,
                data.get(CONF_REIMBURSEMENT_BASIS),
                DEFAULT_MARKET_PRICE_BASIS,
            ),
            interval=_as_enum(
                PriceInterval,
                data.get(CONF_REIMBURSEMENT_INTERVAL),
                DEFAULT_REIMBURSEMENT_INTERVAL,
            ),
        )


@dataclass(frozen=True, slots=True)
class EreConfig:
    """How ERE certificate value is determined for a device."""

    mode: EreMode = EreMode.NONE
    ere_per_kwh: float = 0.0
    ere_price: float = 0.0
    price_entity: str | None = None
    credits_entity: str | None = None
    kwh_entity: str | None = None

    @property
    def enabled(self) -> bool:
        """Return True when ERE value tracking is active."""
        return self.mode is not EreMode.NONE

    @property
    def uses_joulo(self) -> bool:
        """Return True when the ERE ratio is derived from Joulo entities."""
        return (
            self.mode is EreMode.JOULO
            and self.credits_entity is not None
            and self.kwh_entity is not None
        )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> EreConfig:
        """Build the config from raw subentry data."""
        return cls(
            mode=_as_enum(EreMode, data.get(CONF_ERE_MODE), DEFAULT_ERE_MODE),
            ere_per_kwh=as_float(data.get(CONF_ERE_PER_KWH)),
            ere_price=as_float(data.get(CONF_ERE_PRICE)),
            price_entity=as_entity_id(data.get(CONF_ERE_PRICE_ENTITY)),
            credits_entity=as_entity_id(data.get(CONF_ERE_CREDITS_ENTITY)),
            kwh_entity=as_entity_id(data.get(CONF_ERE_KWH_ENTITY)),
        )


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    """Everything KD Helios needs to meter one device."""

    subentry_id: str
    name: str
    kind: DeviceKind = DEFAULT_DEVICE_KIND
    power_entity: str | None = None
    energy_entity: str | None = None
    reimbursement: ReimbursementConfig = ReimbursementConfig()
    ere: EreConfig = EreConfig()
    #: Car schedule, only meaningful for an EV charger.
    ev_pack_kwh: float = 0.0
    ev_target_soc: float = 100.0
    ev_departure_weekday: str = ""
    ev_departure_weekend: str = ""
    ev_soc_entity: str | None = None
    ev_capacity_entity: str | None = None
    ev_power_entity: str | None = None
    ev_departure_entity: str | None = None

    @property
    def is_ev_charger(self) -> bool:
        """Return True for electric vehicle chargers."""
        return self.kind is DeviceKind.EV_CHARGER

    @property
    def has_reimbursement(self) -> bool:
        """Return True when the employer reimburses this device."""
        return self.reimbursement.enabled

    @property
    def has_ere(self) -> bool:
        """Return True when ERE certificates are valued for this device."""
        return self.ere.enabled

    @property
    def has_ev_schedule(self) -> bool:
        """Return True when enough is known to plan the car's window."""
        return self.kind is DeviceKind.EV_CHARGER and (
            self.ev_pack_kwh > 0 or self.ev_capacity_entity is not None
        )

    @property
    def has_offsets(self) -> bool:
        """Return True when the device has income offsetting its cost."""
        return self.has_reimbursement or self.has_ere

    @property
    def model_name(self) -> str:
        """Return the model shown in the device registry."""
        return "EV charger" if self.is_ev_charger else "Energy meter"

    @classmethod
    def from_subentry(cls, subentry: ConfigSubentry) -> DeviceConfig:
        """Build the config from a config subentry."""
        data = subentry.data
        name = as_entity_id(data.get(CONF_NAME)) or subentry.title or "Device"
        # The subentry type decides the kind. Older entries kept it in the data
        # instead, so that is still honoured.
        kind = (
            DeviceKind.EV_CHARGER
            if subentry.subentry_type == SUBENTRY_TYPE_EV_CHARGER
            else _as_enum(DeviceKind, data.get(CONF_KIND), DEFAULT_DEVICE_KIND)
        )
        return cls(
            subentry_id=subentry.subentry_id,
            name=name,
            kind=kind,
            ev_pack_kwh=as_float(data.get(CONF_EV_PACK_KWH)),
            ev_target_soc=as_float(data.get(CONF_EV_TARGET_SOC), 100.0),
            ev_departure_weekday=str(data.get(CONF_EV_DEPARTURE_WEEKDAY) or ""),
            ev_departure_weekend=str(data.get(CONF_EV_DEPARTURE_WEEKEND) or ""),
            ev_soc_entity=as_entity_id(data.get(CONF_EV_SOC_ENTITY)),
            ev_capacity_entity=as_entity_id(data.get(CONF_EV_CAPACITY_ENTITY)),
            ev_power_entity=as_entity_id(data.get(CONF_EV_POWER_ENTITY)),
            ev_departure_entity=as_entity_id(data.get(CONF_EV_DEPARTURE_ENTITY)),
            power_entity=as_entity_id(data.get(CONF_POWER_ENTITY)),
            energy_entity=as_entity_id(data.get(CONF_ENERGY_ENTITY)),
            reimbursement=ReimbursementConfig.from_mapping(data),
            ere=EreConfig.from_mapping(data),
        )


@dataclass(frozen=True, slots=True)
class GridConfig:
    """Everything KD Helios needs to meter the grid connection."""

    subentry_id: str
    name: str = "Grid connection"
    meter_device_id: str | None = None
    import_entity: str | None = None
    export_entity: str | None = None
    import_power_entity: str | None = None
    export_power_entity: str | None = None
    #: A single signed power sensor: positive imports, negative exports.
    grid_power_entity: str | None = None
    saldering: bool = False
    export_deduction: float = 0.0
    supply_cost_monthly: float = 0.0
    grid_fee_monthly: float = 0.0
    export_price_source: ExportPriceSource = ExportPriceSource.MANUAL

    @property
    def model_name(self) -> str:
        """Return the model shown in the device registry."""
        return "Grid connection"

    @property
    def signed_power_entity(self) -> str | None:
        """Return the sensor that carries net power as one signed value.

        A P1 meter reports import as positive and export as negative in a single
        sensor. The same entity selected for both power fields is treated the
        same way, which is what a user does when they only have that one sensor.
        """
        if self.grid_power_entity:
            return self.grid_power_entity
        if (
            self.import_power_entity
            and self.import_power_entity == self.export_power_entity
        ):
            return self.import_power_entity
        return None

    @property
    def has_power(self) -> bool:
        """Return True when any power sensor is configured."""
        return bool(
            self.grid_power_entity
            or self.import_power_entity
            or self.export_power_entity
        )

    @property
    def fixed_monthly(self) -> float:
        """Return the total monthly fixed costs."""
        return self.supply_cost_monthly + self.grid_fee_monthly

    @classmethod
    def from_subentry(cls, subentry: ConfigSubentry) -> GridConfig:
        """Build the config from a config subentry."""
        data = subentry.data
        return cls(
            subentry_id=subentry.subentry_id,
            name=as_entity_id(data.get(CONF_NAME)) or subentry.title or "Grid",
            meter_device_id=as_entity_id(data.get(CONF_METER_DEVICE)),
            import_entity=as_entity_id(data.get(CONF_IMPORT_ENTITY)),
            export_entity=as_entity_id(data.get(CONF_EXPORT_ENTITY)),
            import_power_entity=as_entity_id(data.get(CONF_IMPORT_POWER_ENTITY)),
            export_power_entity=as_entity_id(data.get(CONF_EXPORT_POWER_ENTITY)),
            grid_power_entity=as_entity_id(data.get(CONF_GRID_POWER_ENTITY)),
            saldering=as_bool(data.get(CONF_SALDERING)),
            export_deduction=as_float(data.get(CONF_EXPORT_DEDUCTION)),
            supply_cost_monthly=as_float(data.get(CONF_SUPPLY_COST_MONTHLY)),
            grid_fee_monthly=as_float(data.get(CONF_GRID_FEE_MONTHLY)),
            export_price_source=_as_enum(
                ExportPriceSource,
                data.get(CONF_EXPORT_PRICE_SOURCE),
                ExportPriceSource.MANUAL,
            ),
        )


@dataclass(frozen=True, slots=True)
class BatteryConfig:
    """How to plan and, optionally, program a home battery."""

    subentry_id: str
    name: str = "Home battery"
    soc_entity: str | None = None
    capacity_kwh: float = 10.0
    max_charge_kw: float = 3.0
    reserve_soc_pct: float = 10.0
    target_soc_pct: float = 100.0
    efficiency_pct: float = 90.0
    horizon_hours: int = 36
    inverter_device_id: str | None = None
    allow_writes: bool = False
    #: Who may use the cheap hours first.
    priority: Priority = Priority.EV
    #: Maximum total grid import; 0 disables the cap.
    import_cap_kw: float = 0.0
    #: Sensor with the whole-house load in W, used to learn the profile.
    house_load_entity: str | None = None
    #: Connection, used to derive the import cap when it is not set directly.
    phases: int = 3
    fuse_amps: float = 0.0
    fuse_margin_pct: float = 90.0
    #: Work mode written when control is released, back to self-consumption.
    restore_work_mode: int = 1

    @property
    def model_name(self) -> str:
        """Return the model shown in the device registry."""
        return "Battery"

    @property
    def effective_import_cap(self) -> float:
        """Return the import cap, from the connection when not set directly."""
        if self.import_cap_kw > 0:
            return self.import_cap_kw
        if self.fuse_amps <= 0:
            return 0.0
        from .ev import fuse_capacity_kw

        return fuse_capacity_kw(self.phases, self.fuse_amps, self.fuse_margin_pct)

    @property
    def settings(self) -> BatterySettings:
        """Return the parameters the planner needs."""
        return BatterySettings(
            capacity_kwh=self.capacity_kwh,
            max_charge_kw=self.max_charge_kw,
            reserve_soc_pct=self.reserve_soc_pct,
            target_soc_pct=self.target_soc_pct,
            efficiency_pct=self.efficiency_pct,
            horizon_hours=self.horizon_hours,
        )

    @classmethod
    def from_subentry(cls, subentry: ConfigSubentry) -> BatteryConfig:
        """Build the config from a config subentry."""
        data = subentry.data
        return cls(
            subentry_id=subentry.subentry_id,
            name=as_entity_id(data.get(CONF_NAME)) or subentry.title or "Battery",
            soc_entity=as_entity_id(data.get(CONF_SOC_ENTITY)),
            capacity_kwh=as_float(data.get(CONF_BATTERY_CAPACITY_KWH), 10.0),
            max_charge_kw=as_float(data.get(CONF_MAX_CHARGE_KW), 3.0),
            reserve_soc_pct=as_float(data.get(CONF_RESERVE_SOC), 10.0),
            target_soc_pct=as_float(data.get(CONF_TARGET_SOC), 100.0),
            efficiency_pct=as_float(data.get(CONF_EFFICIENCY), 90.0),
            horizon_hours=int(as_float(data.get(CONF_BATTERY_HORIZON_HOURS), 36.0)),
            inverter_device_id=as_entity_id(data.get(CONF_INVERTER_DEVICE)),
            allow_writes=as_bool(data.get(CONF_ALLOW_INVERTER_WRITES)),
            priority=_as_enum(Priority, data.get(CONF_PRIORITY), Priority.EV),
            import_cap_kw=as_float(data.get(CONF_IMPORT_CAP_KW)),
            house_load_entity=as_entity_id(data.get(CONF_HOUSE_LOAD_ENTITY)),
            phases=int(as_float(data.get(CONF_PHASES), 3.0)),
            fuse_amps=as_float(data.get(CONF_FUSE_AMPS)),
            fuse_margin_pct=as_float(data.get(CONF_FUSE_MARGIN_PCT), 90.0),
            restore_work_mode=int(as_float(data.get(CONF_RESTORE_WORK_MODE), 1.0)),
        )


def device_subentries(entry: ConfigEntry) -> list[ConfigSubentry]:
    """Return the device and EV charger subentries of an entry."""
    return [
        *entry.get_subentries_of_type(SUBENTRY_TYPE_DEVICE),
        *entry.get_subentries_of_type(SUBENTRY_TYPE_EV_CHARGER),
    ]
