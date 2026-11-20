"""Typed configuration models for KD Helios devices."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

from homeassistant.config_entries import ConfigSubentry

from .const import (
    CONF_ENERGY_ENTITY,
    CONF_ERE_CREDITS_ENTITY,
    CONF_ERE_KWH_ENTITY,
    CONF_ERE_MODE,
    CONF_ERE_PER_KWH,
    CONF_ERE_PRICE,
    CONF_ERE_PRICE_ENTITY,
    CONF_EXPORT_DEDUCTION,
    CONF_EXPORT_ENTITY,
    CONF_EXPORT_POWER_ENTITY,
    CONF_EXPORT_PRICE_SOURCE,
    CONF_GRID_FEE_MONTHLY,
    CONF_IMPORT_ENTITY,
    CONF_IMPORT_POWER_ENTITY,
    CONF_KIND,
    CONF_NAME,
    CONF_POWER_ENTITY,
    CONF_REIMBURSEMENT_BASIS,
    CONF_REIMBURSEMENT_MODE,
    CONF_REIMBURSEMENT_PERCENT,
    CONF_REIMBURSEMENT_RATE,
    CONF_REIMBURSEMENT_SURCHARGE,
    CONF_SALDERING,
    CONF_SUPPLY_COST_MONTHLY,
    DEFAULT_DEVICE_KIND,
    DEFAULT_ERE_MODE,
    DEFAULT_MARKET_PRICE_BASIS,
    DEFAULT_REIMBURSEMENT_MODE,
    DEFAULT_REIMBURSEMENT_PERCENT,
    DeviceKind,
    EreMode,
    ExportPriceSource,
    MarketPriceBasis,
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
        return cls(
            subentry_id=subentry.subentry_id,
            name=name,
            kind=_as_enum(DeviceKind, data.get(CONF_KIND), DEFAULT_DEVICE_KIND),
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
    import_entity: str | None = None
    export_entity: str | None = None
    import_power_entity: str | None = None
    export_power_entity: str | None = None
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
            import_entity=as_entity_id(data.get(CONF_IMPORT_ENTITY)),
            export_entity=as_entity_id(data.get(CONF_EXPORT_ENTITY)),
            import_power_entity=as_entity_id(data.get(CONF_IMPORT_POWER_ENTITY)),
            export_power_entity=as_entity_id(data.get(CONF_EXPORT_POWER_ENTITY)),
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
