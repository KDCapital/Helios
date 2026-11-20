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
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENERGY_ENTITY,
    CONF_ERE_CREDITS_ENTITY,
    CONF_ERE_KWH_ENTITY,
    CONF_ERE_MODE,
    CONF_ERE_PER_KWH,
    CONF_ERE_PRICE,
    CONF_ERE_PRICE_ENTITY,
    CONF_EXPORT_DEDUCTION,
    CONF_EXPORT_DEDUCTION_CONFIRMED,
    CONF_EXPORT_ENTITY,
    CONF_EXPORT_POWER_ENTITY,
    CONF_EXPORT_PRICE_SOURCE,
    CONF_GRID_FEE_MONTHLY,
    CONF_IMPORT_ENTITY,
    CONF_IMPORT_POWER_ENTITY,
    CONF_KIND,
    CONF_NAME,
    CONF_POWER_ENTITY,
    CONF_PRICE_INTERVAL,
    CONF_PROVIDER,
    CONF_REIMBURSEMENT_BASIS,
    CONF_REIMBURSEMENT_INTERVAL,
    CONF_REIMBURSEMENT_MODE,
    CONF_REIMBURSEMENT_PERCENT,
    CONF_REIMBURSEMENT_RATE,
    CONF_REIMBURSEMENT_SURCHARGE,
    CONF_SALDERING,
    CONF_SUPPLY_COST_MONTHLY,
    DEFAULT_ERE_MODE,
    DEFAULT_MARKET_PRICE_BASIS,
    DEFAULT_PRICE_INTERVAL,
    DEFAULT_REIMBURSEMENT_INTERVAL,
    DEFAULT_REIMBURSEMENT_MODE,
    DEFAULT_REIMBURSEMENT_PERCENT,
    DOMAIN,
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
    SUBENTRY_TYPE_DEVICE,
    SUBENTRY_TYPE_GRID,
    UNIT_EUR_PER_KWH,
    DeviceKind,
    EreMode,
    ExportPriceSource,
    MarketPriceBasis,
    PriceInterval,
    ReimbursementMode,
)
from .feedin import FEED_IN_SNAPSHOT_DATE, feed_in_deduction
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


def _device_schema() -> vol.Schema:
    """Schema for the basic device settings."""
    return vol.Schema(
        {
            vol.Required(CONF_NAME): selector.TextSelector(),
            vol.Required(
                CONF_KIND, default=DeviceKind.DEVICE.value
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[kind.value for kind in DeviceKind],
                    mode=selector.SelectSelectorMode.LIST,
                    translation_key="device_kind",
                )
            ),
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


def _device_settings_schema() -> vol.Schema:
    """Schema for a device with every optional section expanded."""
    return vol.Schema(
        {
            **_device_schema().schema,
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


def _grid_connection_schema() -> vol.Schema:
    """Schema for the meters feeding the grid connection."""
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
            vol.Required(
                CONF_SALDERING, default=_default_saldering()
            ): selector.BooleanSelector(),
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


def _grid_settings_schema(suggested: float | None) -> vol.Schema:
    """Schema for a grid connection with every field expanded."""
    return vol.Schema(
        {
            **_grid_connection_schema().schema,
            **_grid_tariff_schema(suggested).schema,
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
            SUBENTRY_TYPE_GRID: GridSubentryFlow,
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
    """Add or edit a tracked device."""

    def __init__(self) -> None:
        """Initialise the flow state."""
        self._device: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle the basic device step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_device(user_input)
            if not errors:
                self._device = dict(user_input)
                if user_input[CONF_KIND] == DeviceKind.EV_CHARGER:
                    return await self.async_step_reimbursement()
                return await self.async_step_ere()

        return self.async_show_form(
            step_id="user", data_schema=_device_schema(), errors=errors
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
        """Handle the ERE certificate step and create the device."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_ere(user_input)
            if not errors:
                data = {**self._device, **user_input}
                return self.async_create_entry(title=data[CONF_NAME], data=data)

        return self.async_show_form(
            step_id="ere", data_schema=_ere_schema(), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit every setting of an existing device."""
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
                return self.async_update_and_abort(
                    entry,
                    subentry,
                    title=str(user_input[CONF_NAME]).strip(),
                    data=dict(user_input),
                )

        schema = _device_settings_schema()
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

    The feed-in deduction is prefilled from the provider dataset of
    epexprijzen.nl, because their JSON API does not expose it. That dataset can
    drift, so the user has to confirm or correct the value before it is used:
    a wrong deduction would silently distort every euro of export revenue.
    """

    def __init__(self) -> None:
        """Initialise the flow state."""
        self._connection: dict[str, Any] = {}

    @staticmethod
    def _placeholders(provider: str, suggested: float | None) -> dict[str, str]:
        """Return the text placeholders for the tariff step."""
        return {
            "provider": PROVIDERS.get(provider, provider),
            "suggested": "unknown" if suggested is None else f"{suggested:+.5f}",
            "snapshot": FEED_IN_SNAPSHOT_DATE,
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle the meters step."""
        entry = self._get_entry()
        if entry.get_subentries_of_type(SUBENTRY_TYPE_GRID):
            return self.async_abort(reason="single_instance")

        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_grid_connection(user_input)
            if not errors:
                self._connection = dict(user_input)
                return await self.async_step_tariff()

        return self.async_show_form(
            step_id="user", data_schema=_grid_connection_schema(), errors=errors
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

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit every setting of the grid connection."""
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        provider = entry.data.get(CONF_PROVIDER, "")
        suggested = feed_in_deduction(provider)

        errors: dict[str, str] = {}
        if user_input is not None:
            errors = {
                **_validate_grid_connection(user_input),
                **_validate_grid_tariff(user_input),
            }
            if not errors:
                data = dict(user_input)
                data[CONF_EXPORT_PRICE_SOURCE] = _export_price_source(
                    user_input.get(CONF_EXPORT_DEDUCTION), suggested
                )
                return self.async_update_and_abort(entry, subentry, data=data)

        schema = _grid_settings_schema(suggested)
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
            description_placeholders=self._placeholders(provider, suggested),
        )
