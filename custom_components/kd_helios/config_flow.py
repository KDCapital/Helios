"""Config and subentry flows for KD Helios."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .battery import WorkMode
from .const import (
    CONF_ALLOW_INVERTER_WRITES,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_HORIZON_HOURS,
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_TEMP_ENTITY,
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
    CONF_EV_DEVICE,
    CONF_EV_PACK_KWH,
    CONF_EV_POWER_ENTITY,
    CONF_EV_SOC_ENTITY,
    CONF_EV_TARGET_SOC,
    CONF_EV_TARGET_SOC_ENTITY,
    CONF_EXPORT_DEDUCTION,
    CONF_EXPORT_DEDUCTION_CONFIRMED,
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
    CONF_MAX_DISCHARGE_KW,
    CONF_METER_DEVICE,
    CONF_NAME,
    CONF_PHASES,
    CONF_POWER_ENTITY,
    CONF_PRICE_INTERVAL,
    CONF_PRIORITY,
    CONF_PROVIDER,
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
    CONF_SOLAR_FORECAST_ENTITY,
    CONF_SOLAR_HOLD_ENABLED,
    CONF_SUPPLY_COST_MONTHLY,
    CONF_TARGET_SOC,
    CONF_TEMP_CHARGE_LIMIT_BAND_C,
    CONF_TEMP_CHARGE_LIMIT_C,
    CONF_TEMP_CHARGE_LIMIT_ENABLED,
    CONF_TEMP_CHARGE_LIMIT_FLOOR_PCT,
    CONF_WEEKLY_FULL_CHARGE_DAY,
    CONF_WEEKLY_FULL_CHARGE_ENABLED,
    DEFAULT_ERE_MODE,
    DEFAULT_MARKET_PRICE_BASIS,
    DEFAULT_PRICE_INTERVAL,
    DEFAULT_REIMBURSEMENT_INTERVAL,
    DEFAULT_REIMBURSEMENT_MODE,
    DEFAULT_REIMBURSEMENT_PERCENT,
    DEFAULT_TEMP_CHARGE_LIMIT_BAND_C,
    DEFAULT_TEMP_CHARGE_LIMIT_C,
    DEFAULT_TEMP_CHARGE_LIMIT_FLOOR_PCT,
    DEFAULT_WEEKLY_FULL_CHARGE_DAY,
    DOMAIN,
    MAX_BATTERY_CAPACITY_KWH,
    MAX_BATTERY_HORIZON_HOURS,
    MAX_CHARGE_POWER_KW,
    MAX_ERE_PER_KWH,
    MAX_ERE_PRICE,
    MAX_EXPORT_DEDUCTION,
    MAX_FIXED_REIMBURSEMENT_RATE,
    MAX_MONTHLY_FIXED_COST,
    MAX_REIMBURSEMENT_PERCENT,
    MAX_SURCHARGE,
    PRICE_INTERVALS,
    PROVIDERS,
    SALDERING_END_DATE,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_DEVICE,
    SUBENTRY_TYPE_EV_CHARGER,
    SUBENTRY_TYPE_GRID,
    UNIT_EUR_PER_KWH,
    UNIT_KWH,
    DeviceKind,
    EreMode,
    ExportPriceSource,
    MarketPriceBasis,
    PriceInterval,
    Priority,
    ReimbursementMode,
)
from .feedin import FEED_IN_SNAPSHOT_DATE, feed_in_deduction
from .meter import MeterDetection, async_detect_meter_entities
from .models import as_bool, as_entity_id, as_float
from .prices import PriceApiError, UnknownProviderError, async_fetch_provider_prices

PRICE_STEP = 0.001
"""Selector step. The value is a hint for the spinner only; the box accepts
any value, so four decimal places remain possible."""


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
def _provider_schema() -> vol.Schema:
    """Schema for choosing the energy provider."""
    return vol.Schema(
        {
            vol.Required(CONF_PROVIDER): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=key, label=label)
                        for key, label in PROVIDERS.items()
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    sort=True,
                )
            ),
            vol.Required(
                CONF_PRICE_INTERVAL, default=DEFAULT_PRICE_INTERVAL
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(PRICE_INTERVALS),
                    mode=selector.SelectSelectorMode.LIST,
                    translation_key="price_interval",
                )
            ),
        }
    )


def _money_selector(
    minimum: float, maximum: float, unit: str
) -> selector.NumberSelector:
    """Return a decimal money selector."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=PRICE_STEP,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement=unit,
        )
    )


def _percent_selector() -> selector.NumberSelector:
    """Return a percentage selector."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=MAX_REIMBURSEMENT_PERCENT,
            step=0.1,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="%",
        )
    )


def _entity_selector(
    device_class: SensorDeviceClass | None = None,
) -> selector.EntitySelector:
    """Return an entity selector limited to sensors."""
    config = selector.EntityFilterSelectorConfig(domain="sensor")
    if device_class is not None:
        config["device_class"] = device_class
    return selector.EntitySelector(selector.EntitySelectorConfig(filter=config))


def _optional_entity(
    device_class: SensorDeviceClass | None = None,
) -> vol.Any:
    """Return an entity selector that also accepts an empty value."""
    return vol.Any(None, _entity_selector(device_class))


def _ev_schedule_schema() -> vol.Schema:
    """Schema for the car and its optional charging schedule."""
    return vol.Schema(
        {
            vol.Optional(CONF_EV_DEVICE): vol.Any(
                None,
                selector.DeviceSelector(
                    selector.DeviceSelectorConfig(integration="evcc")
                ),
            ),
            vol.Optional(CONF_EV_PACK_KWH): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=200,
                    step=0.1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="kWh",
                )
            ),
            vol.Required(CONF_EV_TARGET_SOC, default=100): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=50,
                    max=100,
                    step=5,
                    mode=selector.NumberSelectorMode.SLIDER,
                    unit_of_measurement="%",
                )
            ),
            vol.Optional(CONF_EV_TARGET_SOC_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Optional(
                CONF_EV_DEPARTURE_WEEKDAY, default="07:30"
            ): selector.TimeSelector(),
            vol.Optional(
                CONF_EV_DEPARTURE_WEEKEND, default="09:00"
            ): selector.TimeSelector(),
            vol.Optional(CONF_EV_SOC_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Optional(CONF_EV_CAPACITY_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Optional(CONF_EV_POWER_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Optional(CONF_EV_DEPARTURE_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor", "input_datetime"])
            ),
        }
    )


def _device_schema() -> vol.Schema:
    """Schema for the basic device settings."""
    return vol.Schema(
        {
            vol.Required(CONF_NAME): selector.TextSelector(),
            vol.Optional(CONF_POWER_ENTITY): _optional_entity(SensorDeviceClass.POWER),
            vol.Optional(CONF_ENERGY_ENTITY): _optional_entity(
                SensorDeviceClass.ENERGY
            ),
        }
    )


def _reimbursement_schema() -> vol.Schema:
    """Schema for employer reimbursement."""
    return vol.Schema(
        {
            vol.Required(
                CONF_REIMBURSEMENT_MODE, default=DEFAULT_REIMBURSEMENT_MODE.value
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[mode.value for mode in ReimbursementMode],
                    mode=selector.SelectSelectorMode.LIST,
                    translation_key="reimbursement_mode",
                )
            ),
            vol.Optional(CONF_REIMBURSEMENT_RATE, default=0.0): _money_selector(
                0.0, MAX_FIXED_REIMBURSEMENT_RATE, UNIT_EUR_PER_KWH
            ),
            vol.Optional(CONF_REIMBURSEMENT_SURCHARGE, default=0.0): _money_selector(
                -MAX_SURCHARGE, MAX_SURCHARGE, UNIT_EUR_PER_KWH
            ),
            vol.Optional(
                CONF_REIMBURSEMENT_PERCENT, default=DEFAULT_REIMBURSEMENT_PERCENT
            ): _percent_selector(),
            vol.Required(
                CONF_REIMBURSEMENT_BASIS, default=DEFAULT_MARKET_PRICE_BASIS.value
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[basis.value for basis in MarketPriceBasis],
                    mode=selector.SelectSelectorMode.LIST,
                    translation_key="market_price_basis",
                )
            ),
            vol.Required(
                CONF_REIMBURSEMENT_INTERVAL,
                default=DEFAULT_REIMBURSEMENT_INTERVAL.value,
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[interval.value for interval in PriceInterval],
                    mode=selector.SelectSelectorMode.LIST,
                    translation_key="reimbursement_interval",
                )
            ),
        }
    )


def _ere_schema() -> vol.Schema:
    """Schema for ERE certificate valuation."""
    return vol.Schema(
        {
            vol.Required(
                CONF_ERE_MODE, default=DEFAULT_ERE_MODE.value
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[mode.value for mode in EreMode],
                    mode=selector.SelectSelectorMode.LIST,
                    translation_key="ere_mode",
                )
            ),
            vol.Optional(CONF_ERE_PER_KWH, default=0.0): _money_selector(
                0.0, MAX_ERE_PER_KWH, "ERE/kWh"
            ),
            vol.Optional(CONF_ERE_PRICE, default=0.0): _money_selector(
                0.0, MAX_ERE_PRICE, "EUR/ERE"
            ),
            vol.Optional(CONF_ERE_PRICE_ENTITY): _optional_entity(),
            vol.Optional(CONF_ERE_CREDITS_ENTITY): _optional_entity(),
            vol.Optional(CONF_ERE_KWH_ENTITY): _optional_entity(),
        }
    )


def _ev_settings_schema() -> vol.Schema:
    """Schema for an EV charger with the car, reimbursement and ERE expanded."""
    return vol.Schema(
        {
            **_device_schema().schema,
            **_ev_schedule_schema().schema,
            **_reimbursement_schema().schema,
            **_ere_schema().schema,
        }
    )


def _monthly_cost_selector() -> selector.NumberSelector:
    """Return a selector for a monthly amount in euros."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=MAX_MONTHLY_FIXED_COST,
            step=0.01,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="EUR/month",
        )
    )


def _default_saldering() -> bool:
    """Return whether net metering still applies today."""
    return dt_util.now().date() < SALDERING_END_DATE


def _meter_device_selector() -> selector.DeviceSelector:
    """Return a device picker limited to devices that meter energy."""
    return selector.DeviceSelector(
        selector.DeviceSelectorConfig(
            entity=[
                selector.EntityFilterSelectorConfig(
                    domain="sensor", device_class=[SensorDeviceClass.ENERGY]
                )
            ]
        )
    )


def _optional_device() -> vol.Any:
    """Return a device picker that also accepts an empty value."""
    return vol.Any(None, _meter_device_selector())


def _grid_meter_schema() -> vol.Schema:
    """Schema for picking the meter device and the saldering mode."""
    return vol.Schema(
        {
            vol.Optional(CONF_METER_DEVICE): _optional_device(),
            vol.Required(
                CONF_SALDERING, default=_default_saldering()
            ): selector.BooleanSelector(),
        }
    )


def _grid_entities_schema() -> vol.Schema:
    """Schema for the counters feeding the grid connection."""
    return vol.Schema(
        {
            vol.Optional(CONF_IMPORT_ENTITY): _optional_entity(
                SensorDeviceClass.ENERGY
            ),
            vol.Optional(CONF_EXPORT_ENTITY): _optional_entity(
                SensorDeviceClass.ENERGY
            ),
            vol.Optional(CONF_IMPORT_POWER_ENTITY): _optional_entity(
                SensorDeviceClass.POWER
            ),
            vol.Optional(CONF_EXPORT_POWER_ENTITY): _optional_entity(
                SensorDeviceClass.POWER
            ),
            vol.Optional(CONF_GRID_POWER_ENTITY): _optional_entity(
                SensorDeviceClass.POWER
            ),
        }
    )


def _grid_tariff_schema(suggested: float | None) -> vol.Schema:
    """Schema for the feed-in deduction and the fixed costs."""
    return vol.Schema(
        {
            vol.Required(
                CONF_EXPORT_DEDUCTION,
                default=suggested if suggested is not None else 0.0,
            ): _money_selector(
                -MAX_EXPORT_DEDUCTION, MAX_EXPORT_DEDUCTION, UNIT_EUR_PER_KWH
            ),
            vol.Optional(
                CONF_EXPORT_DEDUCTION_CONFIRMED, default=False
            ): selector.BooleanSelector(),
            vol.Optional(CONF_SUPPLY_COST_MONTHLY): _monthly_cost_selector(),
            vol.Optional(CONF_GRID_FEE_MONTHLY): _monthly_cost_selector(),
        }
    )


def _percent_field(minimum: float, maximum: float) -> selector.NumberSelector:
    """Return a whole-percent selector."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=1,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="%",
        )
    )


def _battery_schema() -> vol.Schema:
    """Schema for the home battery and its inverter."""
    return vol.Schema(
        {
            vol.Optional(CONF_SOC_ENTITY): _optional_entity(),
            vol.Required(
                CONF_BATTERY_CAPACITY_KWH, default=10.0
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=MAX_BATTERY_CAPACITY_KWH,
                    step=0.1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement=UNIT_KWH,
                )
            ),
            vol.Required(CONF_MAX_CHARGE_KW, default=3.0): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1,
                    max=MAX_CHARGE_POWER_KW,
                    step=0.1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="kW",
                )
            ),
            vol.Required(CONF_RESERVE_SOC, default=10.0): _percent_field(0, 100),
            vol.Required(CONF_TARGET_SOC, default=100.0): _percent_field(1, 100),
            vol.Required(CONF_EFFICIENCY, default=90.0): _percent_field(50, 100),
            vol.Required(
                CONF_BATTERY_HORIZON_HOURS, default=36
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=6,
                    max=MAX_BATTERY_HORIZON_HOURS,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="h",
                )
            ),
            vol.Optional(CONF_INVERTER_DEVICE): vol.Any(
                None,
                selector.DeviceSelector(
                    selector.DeviceSelectorConfig(integration="solarman")
                ),
            ),
            vol.Required(
                CONF_ALLOW_INVERTER_WRITES, default=False
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_PRIORITY, default=Priority.EV.value
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[priority.value for priority in Priority],
                    mode=selector.SelectSelectorMode.LIST,
                    translation_key="priority",
                )
            ),
            vol.Optional(CONF_IMPORT_CAP_KW): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=60,
                    step=0.5,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="kW",
                )
            ),
            vol.Optional(CONF_HOUSE_LOAD_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Required(CONF_PHASES, default="3"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["1", "3"],
                    mode=selector.SelectSelectorMode.LIST,
                    translation_key="phases",
                )
            ),
            vol.Optional(CONF_FUSE_AMPS): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=6,
                    max=100,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="A",
                )
            ),
            vol.Required(CONF_FUSE_MARGIN_PCT, default=90): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=50,
                    max=100,
                    step=5,
                    mode=selector.NumberSelectorMode.SLIDER,
                    unit_of_measurement="%",
                )
            ),
            vol.Required(
                CONF_RESTORE_WORK_MODE,
                default=str(WorkMode.ZERO_EXPORT_TO_LOAD.value),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[str(mode.value) for mode in WorkMode],
                    mode=selector.SelectSelectorMode.LIST,
                    translation_key="work_mode",
                )
            ),
            vol.Optional(CONF_MAX_DISCHARGE_KW): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1,
                    max=MAX_CHARGE_POWER_KW,
                    step=0.1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="kW",
                )
            ),
            vol.Optional(CONF_BATTERY_POWER_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Optional(CONF_BATTERY_TEMP_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Required(
                CONF_TEMP_CHARGE_LIMIT_ENABLED, default=False
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_TEMP_CHARGE_LIMIT_C, default=DEFAULT_TEMP_CHARGE_LIMIT_C
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=20,
                    max=80,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="°C",
                )
            ),
            vol.Required(
                CONF_TEMP_CHARGE_LIMIT_BAND_C,
                default=DEFAULT_TEMP_CHARGE_LIMIT_BAND_C,
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=30,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="°C",
                )
            ),
            vol.Required(
                CONF_TEMP_CHARGE_LIMIT_FLOOR_PCT,
                default=DEFAULT_TEMP_CHARGE_LIMIT_FLOOR_PCT,
            ): _percent_field(0, 100),
            vol.Required(
                CONF_WEEKLY_FULL_CHARGE_ENABLED, default=False
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_WEEKLY_FULL_CHARGE_DAY,
                default=str(DEFAULT_WEEKLY_FULL_CHARGE_DAY),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[str(index) for index in range(7)],
                    mode=selector.SelectSelectorMode.LIST,
                    translation_key="weekday",
                )
            ),
            vol.Required(
                CONF_SOLAR_HOLD_ENABLED, default=False
            ): selector.BooleanSelector(),
            vol.Optional(CONF_SOLAR_FORECAST_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
        }
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _validate_device(user_input: Mapping[str, Any]) -> dict[str, str]:
    """Validate the basic device settings."""
    errors: dict[str, str] = {}
    if not str(user_input.get(CONF_NAME) or "").strip():
        errors[CONF_NAME] = "name_required"

    power = as_entity_id(user_input.get(CONF_POWER_ENTITY))
    energy = as_entity_id(user_input.get(CONF_ENERGY_ENTITY))
    if power is None and energy is None:
        errors["base"] = "no_source"
    elif power is not None and energy is not None and power == energy:
        errors["base"] = "duplicate_source"
    return errors


def _validate_reimbursement(user_input: Mapping[str, Any]) -> dict[str, str]:
    """Validate the reimbursement settings."""
    errors: dict[str, str] = {}
    mode = user_input.get(CONF_REIMBURSEMENT_MODE)
    if mode == ReimbursementMode.FIXED:
        if as_float(user_input.get(CONF_REIMBURSEMENT_RATE)) <= 0:
            errors[CONF_REIMBURSEMENT_RATE] = "rate_required"
    elif (
        mode == ReimbursementMode.DYNAMIC
        and as_float(user_input.get(CONF_REIMBURSEMENT_PERCENT)) <= 0
    ):
        errors[CONF_REIMBURSEMENT_PERCENT] = "percent_required"
    return errors


def _validate_ere(user_input: Mapping[str, Any]) -> dict[str, str]:
    """Validate the ERE settings."""
    errors: dict[str, str] = {}
    mode = user_input.get(CONF_ERE_MODE)
    if mode == EreMode.FIXED:
        if as_float(user_input.get(CONF_ERE_PER_KWH)) <= 0:
            errors[CONF_ERE_PER_KWH] = "ere_per_kwh_required"
        if as_float(user_input.get(CONF_ERE_PRICE)) <= 0:
            errors[CONF_ERE_PRICE] = "ere_price_required"
    elif mode == EreMode.JOULO:
        if as_entity_id(user_input.get(CONF_ERE_CREDITS_ENTITY)) is None:
            errors[CONF_ERE_CREDITS_ENTITY] = "entity_required"
        if as_entity_id(user_input.get(CONF_ERE_KWH_ENTITY)) is None:
            errors[CONF_ERE_KWH_ENTITY] = "entity_required"
        # Without a price the credits would be worth zero, silently.
        if (
            as_entity_id(user_input.get(CONF_ERE_PRICE_ENTITY)) is None
            and as_float(user_input.get(CONF_ERE_PRICE)) <= 0
        ):
            errors[CONF_ERE_PRICE] = "ere_price_required"
    return errors


def _validate_grid_connection(user_input: Mapping[str, Any]) -> dict[str, str]:
    """Validate the meters of the grid connection."""
    errors: dict[str, str] = {}
    import_entity = as_entity_id(user_input.get(CONF_IMPORT_ENTITY))
    export_entity = as_entity_id(user_input.get(CONF_EXPORT_ENTITY))
    if import_entity is None:
        errors[CONF_IMPORT_ENTITY] = "import_required"
    elif export_entity is not None and import_entity == export_entity:
        errors[CONF_EXPORT_ENTITY] = "duplicate_source"
    return errors


def _validate_grid_tariff(user_input: Mapping[str, Any]) -> dict[str, str]:
    """Validate the feed-in deduction and the fixed costs."""
    errors: dict[str, str] = {}
    if not as_bool(user_input.get(CONF_EXPORT_DEDUCTION_CONFIRMED)):
        errors[CONF_EXPORT_DEDUCTION_CONFIRMED] = "confirm_required"
    for key in (CONF_SUPPLY_COST_MONTHLY, CONF_GRID_FEE_MONTHLY):
        if user_input.get(key) is None:
            errors[key] = "fixed_cost_required"
    return errors


def _export_price_source(submitted: Any, suggested: float | None) -> str:
    """Return where the confirmed feed-in deduction came from."""
    if suggested is None:
        return ExportPriceSource.MANUAL.value
    if abs(as_float(submitted) - suggested) < 1e-9:
        return ExportPriceSource.EPEXPRIJZEN.value
    return ExportPriceSource.MANUAL.value


def _validate_battery(user_input: Mapping[str, Any]) -> dict[str, str]:
    """Validate the battery settings."""
    errors: dict[str, str] = {}
    if as_entity_id(user_input.get(CONF_SOC_ENTITY)) is None:
        errors[CONF_SOC_ENTITY] = "soc_required"

    reserve = as_float(user_input.get(CONF_RESERVE_SOC))
    target = as_float(user_input.get(CONF_TARGET_SOC))
    if target <= reserve:
        errors[CONF_TARGET_SOC] = "target_above_reserve"

    if as_float(user_input.get(CONF_BATTERY_CAPACITY_KWH)) <= 0:
        errors[CONF_BATTERY_CAPACITY_KWH] = "capacity_required"
    if as_float(user_input.get(CONF_MAX_CHARGE_KW)) <= 0:
        errors[CONF_MAX_CHARGE_KW] = "charge_power_required"

    writes = as_bool(user_input.get(CONF_ALLOW_INVERTER_WRITES))
    if writes and as_entity_id(user_input.get(CONF_INVERTER_DEVICE)) is None:
        errors[CONF_INVERTER_DEVICE] = "inverter_required"

    if as_bool(user_input.get(CONF_TEMP_CHARGE_LIMIT_ENABLED)) and (
        as_entity_id(user_input.get(CONF_BATTERY_TEMP_ENTITY)) is None
    ):
        errors[CONF_BATTERY_TEMP_ENTITY] = "temperature_required"

    if as_bool(user_input.get(CONF_SOLAR_HOLD_ENABLED)) and (
        as_entity_id(user_input.get(CONF_SOLAR_FORECAST_ENTITY)) is None
    ):
        errors[CONF_SOLAR_FORECAST_ENTITY] = "solar_forecast_required"
    return errors


def _unique_id(provider: str, interval: str) -> str:
    """Return the config entry unique id for a provider and interval."""
    return f"{provider}_{interval}"


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------
class HeliosConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a KD Helios tariff."""

    VERSION = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the subentry types this integration supports."""
        return {
            SUBENTRY_TYPE_DEVICE: DeviceSubentryFlow,
            SUBENTRY_TYPE_EV_CHARGER: EvChargerSubentryFlow,
            SUBENTRY_TYPE_GRID: GridSubentryFlow,
            SUBENTRY_TYPE_BATTERY: BatterySubentryFlow,
        }

    async def _async_validate(self, provider: str, interval: str) -> dict[str, str]:
        """Fetch prices to prove the provider works."""
        try:
            await async_fetch_provider_prices(self.hass, provider, interval)
        except UnknownProviderError:
            return {"base": "unknown_provider"}
        except PriceApiError:
            return {"base": "cannot_connect"}
        return {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            provider = user_input[CONF_PROVIDER]
            interval = user_input[CONF_PRICE_INTERVAL]
            await self.async_set_unique_id(_unique_id(provider, interval))
            self._abort_if_unique_id_configured()

            errors = await self._async_validate(provider, interval)
            if not errors:
                return self.async_create_entry(
                    title=PROVIDERS.get(provider, provider),
                    data=dict(user_input),
                )

        return self.async_show_form(
            step_id="user", data_schema=_provider_schema(), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the provider or the price interval."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            provider = user_input[CONF_PROVIDER]
            interval = user_input[CONF_PRICE_INTERVAL]
            new_unique_id = _unique_id(provider, interval)
            if new_unique_id != entry.unique_id:
                await self.async_set_unique_id(new_unique_id)
                self._abort_if_unique_id_configured()

            errors = await self._async_validate(provider, interval)
            if not errors:
                # The entry registers an update listener that reloads it, so
                # updating and reloading explicitly would double up and HA
                # rejects that combination.
                return self.async_update_and_abort(
                    entry,
                    unique_id=new_unique_id,
                    title=PROVIDERS.get(provider, provider),
                    data_updates=dict(user_input),
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _provider_schema(), dict(entry.data)
            ),
            errors=errors,
        )


# ---------------------------------------------------------------------------
# Device subentry flow
# ---------------------------------------------------------------------------
class DeviceSubentryFlow(ConfigSubentryFlow):
    """Add or edit a plain device: an appliance, a circuit, anything metered.

    No reimbursement and no ERE here: those only make sense for an EV charger,
    which has its own entry in the Add menu.
    """

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle the device step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_device(user_input)
            if not errors:
                data = dict(user_input)
                data[CONF_KIND] = DeviceKind.DEVICE.value
                return self.async_create_entry(title=data[CONF_NAME], data=data)

        return self.async_show_form(
            step_id="user", data_schema=_device_schema(), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit an existing device."""
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()

        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_device(user_input)
            if not errors:
                # Merge over the stored data: keys this form does not show, such
                # as an EV charger's reimbursement from before the split, stay.
                data = {**subentry.data, **user_input}
                data[CONF_KIND] = DeviceKind.DEVICE.value
                return self.async_update_and_abort(
                    entry,
                    subentry,
                    title=str(user_input[CONF_NAME]).strip(),
                    data=data,
                )

        schema = _device_schema()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    key: value
                    for key, value in subentry.data.items()
                    if key in schema.schema
                },
            ),
            errors=errors,
        )


# ---------------------------------------------------------------------------
# EV charger subentry flow
# ---------------------------------------------------------------------------
class EvChargerSubentryFlow(ConfigSubentryFlow):
    """Add or edit an EV charger, including its reimbursement and ERE value."""

    def __init__(self) -> None:
        """Initialise the flow state."""
        self._device: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle the metering that the cost calculation needs."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_device(user_input)
            if not errors:
                self._device = dict(user_input)
                return await self.async_step_vehicle()

        return self.async_show_form(
            step_id="user", data_schema=_device_schema(), errors=errors
        )

    async def async_step_vehicle(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle the optional car and charging plan."""
        if user_input is not None:
            self._device |= dict(user_input)
            return await self.async_step_reimbursement()

        return self.async_show_form(
            step_id="vehicle", data_schema=_ev_schedule_schema(), errors={}
        )

    async def async_step_reimbursement(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle the employer reimbursement step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_reimbursement(user_input)
            if not errors:
                self._device |= dict(user_input)
                return await self.async_step_ere()

        return self.async_show_form(
            step_id="reimbursement", data_schema=_reimbursement_schema(), errors=errors
        )

    async def async_step_ere(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle the ERE certificate step and create the charger."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_ere(user_input)
            if not errors:
                data = {**self._device, **user_input}
                data[CONF_KIND] = DeviceKind.EV_CHARGER.value
                return self.async_create_entry(title=data[CONF_NAME], data=data)

        return self.async_show_form(
            step_id="ere", data_schema=_ere_schema(), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit every setting of an existing charger."""
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()

        errors: dict[str, str] = {}
        if user_input is not None:
            errors = {
                **_validate_device(user_input),
                **_validate_reimbursement(user_input),
                **_validate_ere(user_input),
            }
            if not errors:
                data = dict(user_input)
                data[CONF_KIND] = DeviceKind.EV_CHARGER.value
                return self.async_update_and_abort(
                    entry,
                    subentry,
                    title=str(user_input[CONF_NAME]).strip(),
                    data=data,
                )

        schema = _ev_settings_schema()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    key: value
                    for key, value in subentry.data.items()
                    if key in schema.schema
                },
            ),
            errors=errors,
        )


# ---------------------------------------------------------------------------
# Grid connection subentry flow
# ---------------------------------------------------------------------------
class GridSubentryFlow(ConfigSubentryFlow):
    """Add or edit the grid connection.

    Three steps: pick the meter device, review the counters it detected, then
    confirm the feed-in deduction and enter the fixed costs.

    The feed-in deduction is prefilled from the provider dataset of
    epexprijzen.nl, because their JSON API does not expose it. That dataset can
    drift, so the user has to confirm or correct the value before it is used: a
    wrong deduction would silently distort every euro of export revenue.
    """

    def __init__(self) -> None:
        """Initialise the flow state."""
        self._connection: dict[str, Any] = {}
        self._detection: MeterDetection | None = None
        self._suggest_from_detection = False

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _placeholders(provider: str, suggested: float | None) -> dict[str, str]:
        """Return the text placeholders for the tariff step."""
        return {
            "provider": PROVIDERS.get(provider, provider),
            "suggested": "unknown" if suggested is None else f"{suggested:+.5f}",
            "snapshot": FEED_IN_SNAPSHOT_DATE,
        }

    def _detection_placeholders(self) -> dict[str, str]:
        """Describe the selected meter and what was recognised on it."""
        device_id = self._connection.get(CONF_METER_DEVICE)
        name = "the selected device"
        if device_id:
            device = dr.async_get(self.hass).async_get(device_id)
            if device is not None:
                name = device.name_by_user or device.name or name
        found = ", ".join(self._detection.found) if self._detection else ""
        return {"meter": name, "detected": found or "none"}

    def _detect(self, device_id: str | None, *, suggest: bool) -> None:
        """Detect the meter's entities and remember what was found."""
        self._detection = (
            async_detect_meter_entities(self.hass, device_id) if device_id else None
        )
        self._suggest_from_detection = suggest

    # -- add ---------------------------------------------------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle the meter step."""
        entry = self._get_entry()
        if entry.get_subentries_of_type(SUBENTRY_TYPE_GRID):
            return self.async_abort(reason="single_instance")

        if user_input is not None:
            self._connection = dict(user_input)
            self._detect(user_input.get(CONF_METER_DEVICE), suggest=True)
            return await self.async_step_entities()

        return self.async_show_form(step_id="user", data_schema=_grid_meter_schema())

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle the counter review step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_grid_connection(user_input)
            if not errors:
                self._connection |= dict(user_input)
                return await self.async_step_tariff()

        schema = _grid_entities_schema()
        suggestions = (
            self._detection.as_suggestions()
            if self._suggest_from_detection and self._detection
            else {}
        )
        return self.async_show_form(
            step_id="entities",
            data_schema=self.add_suggested_values_to_schema(schema, suggestions),
            errors=errors,
            description_placeholders=self._detection_placeholders(),
        )

    async def async_step_tariff(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle the feed-in deduction and fixed costs step."""
        entry = self._get_entry()
        provider = entry.data.get(CONF_PROVIDER, "")
        suggested = feed_in_deduction(provider)
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = _validate_grid_tariff(user_input)
            if not errors:
                data = {**self._connection, **user_input}
                data[CONF_EXPORT_PRICE_SOURCE] = _export_price_source(
                    user_input.get(CONF_EXPORT_DEDUCTION), suggested
                )
                return self.async_create_entry(title="Grid connection", data=data)

        return self.async_show_form(
            step_id="tariff",
            data_schema=_grid_tariff_schema(suggested),
            errors=errors,
            description_placeholders=self._placeholders(provider, suggested),
        )

    # -- edit --------------------------------------------------------------
    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle the meter step when editing."""
        subentry = self._get_reconfigure_subentry()

        if user_input is None:
            self._connection = dict(subentry.data)
            self._detect(subentry.data.get(CONF_METER_DEVICE), suggest=False)
            schema = _grid_meter_schema()
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=self.add_suggested_values_to_schema(
                    schema,
                    {
                        key: value
                        for key, value in subentry.data.items()
                        if key in schema.schema
                    },
                ),
            )

        previous = self._connection.get(CONF_METER_DEVICE)
        self._connection |= dict(user_input)
        # A different meter gets its counters detected and suggested; the same
        # meter keeps whatever the user had configured.
        changed = user_input.get(CONF_METER_DEVICE) != previous
        self._detect(user_input.get(CONF_METER_DEVICE), suggest=changed)
        return await self.async_step_reconfigure_entities()

    async def async_step_reconfigure_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle the counter review step when editing."""
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_grid_connection(user_input)
            if not errors:
                self._connection |= dict(user_input)
                return await self.async_step_reconfigure_tariff()

        schema = _grid_entities_schema()
        if self._suggest_from_detection and self._detection:
            suggestions = self._detection.as_suggestions()
        else:
            suggestions = {
                key: value
                for key, value in subentry.data.items()
                if key in schema.schema
            }
        return self.async_show_form(
            step_id="reconfigure_entities",
            data_schema=self.add_suggested_values_to_schema(schema, suggestions),
            errors=errors,
            description_placeholders=self._detection_placeholders(),
        )

    async def async_step_reconfigure_tariff(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle the tariff step when editing."""
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        provider = entry.data.get(CONF_PROVIDER, "")
        suggested = feed_in_deduction(provider)
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = _validate_grid_tariff(user_input)
            if not errors:
                data = {**self._connection, **user_input}
                data[CONF_EXPORT_PRICE_SOURCE] = _export_price_source(
                    user_input.get(CONF_EXPORT_DEDUCTION), suggested
                )
                return self.async_update_and_abort(entry, subentry, data=data)

        schema = _grid_tariff_schema(suggested)
        return self.async_show_form(
            step_id="reconfigure_tariff",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    key: value
                    for key, value in subentry.data.items()
                    if key in schema.schema
                },
            ),
            errors=errors,
            description_placeholders=self._placeholders(provider, suggested),
        )


# ---------------------------------------------------------------------------
# Battery subentry flow
# ---------------------------------------------------------------------------
class BatterySubentryFlow(ConfigSubentryFlow):
    """Add or edit the home battery planner.

    Writes stay off until the user enables them and points at the inverter
    device, because writing time-of-use registers changes real behaviour.
    """

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle the battery step."""
        entry = self._get_entry()
        if entry.get_subentries_of_type(SUBENTRY_TYPE_BATTERY):
            return self.async_abort(reason="single_instance")

        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_battery(user_input)
            if not errors:
                return self.async_create_entry(
                    title="Home battery", data=dict(user_input)
                )

        return self.async_show_form(
            step_id="user", data_schema=_battery_schema(), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit the battery settings."""
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()

        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_battery(user_input)
            if not errors:
                return self.async_update_and_abort(
                    entry, subentry, data=dict(user_input)
                )

        schema = _battery_schema()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    key: value
                    for key, value in subentry.data.items()
                    if key in schema.schema
                },
            ),
            errors=errors,
        )
