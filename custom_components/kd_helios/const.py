"""Constants for the KD Helios integration."""

from __future__ import annotations

from datetime import date, timedelta
from enum import StrEnum
from typing import Final

DOMAIN: Final = "kd_helios"
NAME: Final = "KD Helios"
VERSION: Final = "0.1.0"

# ---------------------------------------------------------------------------
# Config entry
# ---------------------------------------------------------------------------
CONF_PROVIDER: Final = "provider"
CONF_PRICE_INTERVAL: Final = "price_interval"

# ---------------------------------------------------------------------------
# Subentries
# ---------------------------------------------------------------------------
SUBENTRY_TYPE_DEVICE: Final = "device"
SUBENTRY_TYPE_EV_CHARGER: Final = "ev_charger"
SUBENTRY_TYPE_GRID: Final = "grid"
SUBENTRY_TYPE_BATTERY: Final = "battery"

CONF_NAME: Final = "name"
CONF_KIND: Final = "kind"
CONF_POWER_ENTITY: Final = "power_entity"
CONF_ENERGY_ENTITY: Final = "energy_entity"

CONF_REIMBURSEMENT_MODE: Final = "reimbursement_mode"
CONF_REIMBURSEMENT_RATE: Final = "reimbursement_rate"
CONF_REIMBURSEMENT_SURCHARGE: Final = "reimbursement_surcharge"
CONF_REIMBURSEMENT_PERCENT: Final = "reimbursement_percent"
CONF_REIMBURSEMENT_BASIS: Final = "reimbursement_basis"
CONF_REIMBURSEMENT_INTERVAL: Final = "reimbursement_interval"

CONF_ERE_MODE: Final = "ere_mode"
CONF_ERE_PER_KWH: Final = "ere_per_kwh"
CONF_ERE_PRICE: Final = "ere_price"
CONF_ERE_PRICE_ENTITY: Final = "ere_price_entity"
CONF_ERE_CREDITS_ENTITY: Final = "ere_credits_entity"
CONF_ERE_KWH_ENTITY: Final = "ere_kwh_entity"

# Grid connection
CONF_IMPORT_ENTITY: Final = "import_entity"
CONF_EXPORT_ENTITY: Final = "export_entity"
CONF_GRID_POWER_ENTITY: Final = "grid_power_entity"
CONF_IMPORT_POWER_ENTITY: Final = "import_power_entity"
CONF_EXPORT_POWER_ENTITY: Final = "export_power_entity"
CONF_SALDERING: Final = "saldering"
CONF_EXPORT_DEDUCTION: Final = "export_deduction"
CONF_EXPORT_DEDUCTION_CONFIRMED: Final = "export_deduction_confirmed"
CONF_SUPPLY_COST_MONTHLY: Final = "supply_cost_monthly"
CONF_GRID_FEE_MONTHLY: Final = "grid_fee_monthly"
CONF_EXPORT_PRICE_SOURCE: Final = "export_price_source"
CONF_METER_DEVICE: Final = "meter_device"

# Battery charge planning
CONF_SOC_ENTITY: Final = "soc_entity"
CONF_BATTERY_CAPACITY_KWH: Final = "battery_capacity_kwh"
CONF_MAX_CHARGE_KW: Final = "max_charge_kw"
CONF_RESERVE_SOC: Final = "reserve_soc"
CONF_TARGET_SOC: Final = "target_soc"
CONF_EFFICIENCY: Final = "efficiency"
CONF_BATTERY_HORIZON_HOURS: Final = "battery_horizon_hours"
CONF_INVERTER_DEVICE: Final = "inverter_device"
CONF_ALLOW_INVERTER_WRITES: Final = "allow_inverter_writes"
CONF_RESTORE_WORK_MODE: Final = "restore_work_mode"
CONF_PRIORITY: Final = "priority"
CONF_IMPORT_CAP_KW: Final = "import_cap_kw"


class DeviceKind(StrEnum):
    """Kind of metered device."""

    DEVICE = "device"
    EV_CHARGER = "ev_charger"


class ReimbursementMode(StrEnum):
    """How the employer reimburses energy used by a device."""

    NONE = "none"
    FIXED = "fixed"
    DYNAMIC = "dynamic"


class PriceInterval(StrEnum):
    """Price resolution used to value energy."""

    QUARTERLY = "quarterly"
    HOURLY = "hourly"


class MarketPriceBasis(StrEnum):
    """Market price used by the dynamic reimbursement formula."""

    # EPEX day-ahead spot, excluding VAT and excluding energy tax.
    SPOT = "spot"
    # Provider all-in price, including VAT and energy tax.
    ALL_IN = "all_in"


class Priority(StrEnum):
    """Who gets the cheap energy first when both want it."""

    #: The car charges from the grid; the battery waits. Avoids cycling the
    #: battery to cover a car that is already charging cheaply.
    EV = "ev"
    #: The battery fills first, the car gets what is left.
    BATTERY = "battery"


class ExportPriceSource(StrEnum):
    """Where the feed-in deduction came from."""

    #: Suggested from the epexprijzen.nl provider dataset and left unchanged.
    EPEXPRIJZEN = "epexprijzen"
    #: Typed or corrected by the user.
    MANUAL = "manual"


class EreMode(StrEnum):
    """How ERE certificate value is determined."""

    NONE = "none"
    FIXED = "fixed"
    JOULO = "joulo"


DEFAULT_DEVICE_KIND: Final = DeviceKind.DEVICE
DEFAULT_REIMBURSEMENT_MODE: Final = ReimbursementMode.NONE
DEFAULT_REIMBURSEMENT_PERCENT: Final = 100.0
DEFAULT_MARKET_PRICE_BASIS: Final = MarketPriceBasis.SPOT
DEFAULT_REIMBURSEMENT_INTERVAL: Final = PriceInterval.QUARTERLY
DEFAULT_ERE_MODE: Final = EreMode.NONE

MAX_REIMBURSEMENT_PERCENT: Final = 500.0
MAX_SURCHARGE: Final = 5.0
MAX_ERE_PER_KWH: Final = 100.0
MAX_ERE_PRICE: Final = 1000.0
MAX_FIXED_REIMBURSEMENT_RATE: Final = 10.0
MAX_EXPORT_DEDUCTION: Final = 5.0
MAX_MONTHLY_FIXED_COST: Final = 500.0
MAX_BATTERY_CAPACITY_KWH: Final = 200.0
MAX_CHARGE_POWER_KW: Final = 100.0
MAX_BATTERY_HORIZON_HOURS: Final = 72

#: Service that writes the time-of-use programs, and the Solarman service used
#: to talk to the inverter.
SERVICE_APPLY_TOU: Final = "apply_tou_programs"
SOLARMAN_WRITE_SERVICE: Final = "write_multiple_holding_registers"

#: Net metering (saldering) ends on this date. From it onwards, export is paid
#: at the feed-in price instead of being netted against import.
SALDERING_END_DATE: Final = date(2027, 1, 1)

# ---------------------------------------------------------------------------
# epexprijzen.nl API
# ---------------------------------------------------------------------------
EPEX_BASE_URL: Final = "https://epexprijzen.nl"
PROVIDER_PRICE_URL: Final = EPEX_BASE_URL + "/api/v1/prices/{provider}/{interval}"
SPOT_PRICE_URL: Final = EPEX_BASE_URL + "/api/prices"

PRICE_INTERVAL_QUARTERLY: Final = "quarterly"
PRICE_INTERVAL_HOURLY: Final = "hourly"
PRICE_INTERVALS: Final = (PRICE_INTERVAL_QUARTERLY, PRICE_INTERVAL_HOURLY)
DEFAULT_PRICE_INTERVAL: Final = PRICE_INTERVAL_QUARTERLY

#: Native resolution of each provider price interval, in minutes.
SLOT_MINUTES: Final[dict[str, int]] = {
    PRICE_INTERVAL_QUARTERLY: 15,
    PRICE_INTERVAL_HOURLY: 60,
}

#: Internal price grid resolution, in minutes. The finest resolution the API
#: serves for either provider prices or spot prices.
GRID_MINUTES: Final = 15

#: How far a stale price may be carried forward before a slot is considered
#: unpriced. Carrying prices forward keeps energy accounting complete when the
#: API is briefly unavailable, at the cost of using the last known price.
PRICE_CARRY_FORWARD: Final = timedelta(hours=36)

# ---------------------------------------------------------------------------
# Provider catalogue (superset of https://epexprijzen.nl/api/v1/providers)
# ---------------------------------------------------------------------------
PROVIDERS: Final[dict[str, str]] = {
    "anwb-energie": "ANWB Energie",
    "budget-energie": "Budget Energie",
    "coolblue-energie": "Coolblue Energie",
    "delta-energie": "DELTA Energie",
    "easyenergy": "easyEnergy",
    "eneco": "Eneco",
    "energie-vanons": "Energie VanOns",
    "energiedirect": "Energiedirect",
    "energiek": "Energiek",
    "energyzero": "EnergyZero",
    "engie": "Engie",
    "essent": "Essent",
    "frank-energie": "Frank Energie",
    "frank-energie-slim": "Frank Energie (Slim terugleveren)",
    "greenchoice": "Greenchoice",
    "groenpand": "Groenpand",
    "hegg": "Hegg",
    "innova": "Innova",
    "liveenergy": "LiveEnergy",
    "mega": "Mega",
    "nextenergy": "NextEnergy",
    "noord-energie": "Noord Energie",
    "oxxio": "Oxxio",
    "powerpeers": "PowerPeers",
    "pure-energie": "Pure Energie",
    "quatt-energy": "Quatt Energy",
    "samenom": "SamenOm",
    "samsam": "SamSam",
    "sunergy": "Sunergy",
    "tibber": "Tibber",
    "tinqe": "TinQE",
    "vandebron": "VandeBron",
    "vattenfall": "Vattenfall",
    "zonneplan": "Zonneplan",
}

# ---------------------------------------------------------------------------
# Runtime behaviour
# ---------------------------------------------------------------------------
#: Price polling interval. Provider prices change at most every 15 minutes.
PRICE_UPDATE_INTERVAL: Final = timedelta(minutes=10)

#: Energy accounting tick. Every tick samples each device, integrates energy and
#: prices that energy against the tariff of the elapsed period.
ACCOUNTING_UPDATE_INTERVAL: Final = timedelta(minutes=1)

#: A gap between two accounting ticks larger than this is treated as downtime.
#: Energy consumed while Home Assistant was off cannot be priced reliably, so a
#: fresh baseline is taken instead of pricing the backlog at the current price.
MAX_TRACKING_GAP: Final = timedelta(minutes=10)

#: A single accounting window may never imply more power than this. Protects
#: against phantom energy when a source entity is swapped for one with a
#: larger reading, or recovers from a bogus 0 while offline.
MAX_PLAUSIBLE_POWER_KW: Final = 500.0

#: Minimum energy, in kWh, needed before a Joulo-derived ERE/kWh ratio is
#: trusted. Below this the ratio is dominated by measurement noise.
MIN_ERE_RATIO_WINDOW_KWH: Final = 1.0

#: Fallback ERE/kWh used when a Joulo-derived ratio is not available yet.
MIN_ERE_PER_KWH: Final = 0.0

#: Repair issue shown while the entry has no tracked devices yet.
ISSUE_NO_DEVICES: Final = "no_devices"

STORAGE_VERSION: Final = 1
STORAGE_SAVE_DELAY: Final = 15

CURRENCY: Final = "EUR"
UNIT_EUR_PER_KWH: Final = "EUR/kWh"
UNIT_KWH: Final = "kWh"
UNIT_W: Final = "W"
UNIT_ERE: Final = "ERE"
