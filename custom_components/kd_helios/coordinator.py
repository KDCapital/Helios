"""Data update coordinators for KD Helios."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .accounting import DeviceSnapshot, DeviceTracker
from .battery import (
    TOU_REGISTERS,
    AppliedPlan,
    ChargePlan,
    ChargeSource,
    TouPlan,
    WorkMode,
    build_restore_programs,
    build_tou_programs,
    charge_power_limit,
    plan_charge,
    should_apply,
)
from .const import (
    ACCOUNTING_UPDATE_INTERVAL,
    DOMAIN,
    PRICE_UPDATE_INTERVAL,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_GRID,
    DeviceKind,
)
from .grid import GridSnapshot, GridTracker
from .inverter import async_write_registers, async_write_tou
from .models import BatteryConfig, DeviceConfig, GridConfig, device_subentries
from .prices import (
    PriceApiError,
    PriceSeries,
    UnknownProviderError,
    async_fetch_provider_prices,
    async_fetch_spot_prices,
    build_price_series,
)
from .sources import read_number
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


@dataclass(frozen=True, slots=True)
class EngineData:
    """Everything one accounting tick produces."""

    devices: dict[str, DeviceSnapshot]
    grid: GridSnapshot | None
    battery: ChargePlan | None = None


class HeliosEngine(DataUpdateCoordinator[EngineData]):
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
        for subentry in device_subentries(entry):
            config = DeviceConfig.from_subentry(subentry)
            self.trackers[config.subentry_id] = DeviceTracker(
                hass,
                config,
                storage.device_state(config.subentry_id),
            )

        battery_subentries = entry.get_subentries_of_type(SUBENTRY_TYPE_BATTERY)
        self.battery_config: BatteryConfig | None = (
            BatteryConfig.from_subentry(battery_subentries[0])
            if battery_subentries
            else None
        )
        self.battery_plan: ChargePlan | None = None
        self.tou_plan: TouPlan | None = None
        self.applied_plan: AppliedPlan | None = (
            AppliedPlan.from_dict(storage.battery_state())
            if self.battery_config is not None
            else None
        )
        self.control_enabled: bool = bool(storage.control_state().get("enabled", False))
        self.last_control_error: str | None = None
        self._last_apply: datetime | None = None
        self._applied_cap: float | None = None

        grid_subentries = entry.get_subentries_of_type(SUBENTRY_TYPE_GRID)
        self.grid_tracker: GridTracker | None = (
            GridTracker(
                hass,
                GridConfig.from_subentry(grid_subentries[0]),
                storage.grid_state(),
            )
            if grid_subentries
            else None
        )

    # -- inverter control ---------------------------------------------------
    def _persist_control(self) -> None:
        """Store the control state."""
        self.storage.async_set_control({"enabled": self.control_enabled})

    async def async_set_control(self, enabled: bool) -> None:
        """Take or release control of the inverter.

        Taking control writes the current plan. Releasing it never leaves grid
        charging behind: the programs are rewritten so only the generator may
        charge, and the work mode goes back to self-consumption.
        """
        config = self.battery_config
        if config is None or config.inverter_device_id is None:
            raise ValueError("No inverter is configured for this battery")
        if not config.allow_writes:
            raise ValueError(
                "Writing to the inverter is disabled in the battery settings"
            )

        device_id = config.inverter_device_id
        if enabled:
            if self.battery_plan is None or self.tou_plan is None:
                raise ValueError("There is no charge plan to write yet")
            await async_write_tou(self.hass, device_id, self.tou_plan)
            self.applied_plan = AppliedPlan.from_plan(
                self.battery_plan, self.tou_plan, dt_util.utcnow()
            )
            self._last_apply = dt_util.utcnow()
            self.storage.async_set_battery(self.applied_plan.as_dict())
        else:
            restore = build_restore_programs(config.settings)
            await async_write_tou(
                self.hass,
                device_id,
                restore,
                work_mode=WorkMode(config.restore_work_mode),
            )
            self.applied_plan = None
            self.storage.async_set_battery({})

        self.control_enabled = enabled
        self._persist_control()
        await self.storage.async_save()
        self.async_set_updated_data(self.data)
        _LOGGER.info("Inverter control %s", "enabled" if enabled else "released")

    async def _async_sync_inverter(self, now: datetime) -> None:
        """Apply the plan when it changed enough to matter."""
        config = self.battery_config
        plan = self.battery_plan
        tou = self.tou_plan
        if (
            config is None
            or plan is None
            or tou is None
            or not self.control_enabled
            or not config.allow_writes
            or config.inverter_device_id is None
        ):
            return
        if not should_apply(
            plan,
            self.applied_plan,
            config.settings,
            last_apply=self._last_apply,
            now=now,
        ):
            return
        try:
            await async_write_tou(self.hass, config.inverter_device_id, tou)
        except Exception as err:  # noqa: BLE001 - never break accounting
            self.last_control_error = str(err)
            _LOGGER.error("Could not write the time-of-use programs: %s", err)
            return
        self.last_control_error = None
        self._last_apply = now
        self.applied_plan = AppliedPlan.from_plan(plan, tou, now)
        self.storage.async_set_battery(self.applied_plan.as_dict())

    async def _async_update_power_cap(self, now, snapshots, grid_snapshot) -> None:
        """Hold the battery back while the car charges, and under the cap.

        The plan says "charge at full power", but the car may already be using
        that capacity. Rewriting only the power block leaves the schedule intact
        and reacts within a tick, which is what stops the battery from swallowing
        the capacity the car frees when it pauses for grid rewards.
        """
        config = self.battery_config
        if (
            config is None
            or self.tou_plan is None
            or not self.control_enabled
            or not config.allow_writes
            or config.inverter_device_id is None
        ):
            return

        ev_kw = (
            sum(
                (snapshots[sid].power or 0.0)
                for sid, tracker in self.trackers.items()
                if tracker.config.kind is DeviceKind.EV_CHARGER and sid in snapshots
            )
            / 1000.0
        )
        grid_kw = (grid_snapshot.import_power or 0.0) / 1000.0 if grid_snapshot else 0.0
        limit = charge_power_limit(
            config.settings.max_charge_kw,
            config.import_cap_kw,
            grid_kw,
            ev_kw,
            config.priority,
        )

        has_grid_slot = any(
            program.source is ChargeSource.GRID for program in self.tou_plan.programs
        )
        if not has_grid_slot:
            return
        if self._applied_cap is not None and abs(limit - self._applied_cap) < 0.25:
            return

        values = [
            round(limit * 1000)
            if program.source is ChargeSource.GRID
            else program.power_w
            for program in self.tou_plan.programs
        ]
        if values == [program.power_w for program in self.tou_plan.programs]:
            # The plan already asks for exactly this, nothing to change.
            self._applied_cap = limit
            return
        try:
            await async_write_registers(
                self.hass,
                config.inverter_device_id,
                {TOU_REGISTERS["power"]: values},
            )
        except Exception as err:  # noqa: BLE001 - never break accounting
            self.last_control_error = str(err)
            _LOGGER.error("Could not cap the charge power: %s", err)
            return
        self._applied_cap = limit
        _LOGGER.debug("Charge power limited to %s kW", limit)

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

    async def _async_update_data(self) -> EngineData:
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

        grid_snapshot: GridSnapshot | None = None
        if self.grid_tracker is not None:
            self.grid_tracker.update(now, available_series)
            grid_snapshot = self.grid_tracker.snapshot(now, available_series)
            self.storage.async_set_grid(self.grid_tracker.acc.as_dict())

        battery_plan: ChargePlan | None = None
        if self.battery_config is not None:
            series_for_plan = available_series or PriceSeries.empty(
                self.prices.provider, self.prices.interval
            )
            battery_plan = plan_charge(
                series_for_plan,
                self.battery_config.settings,
                now,
                read_number(self.hass, self.battery_config.soc_entity),
            )
            self.battery_plan = battery_plan
            self.tou_plan = build_tou_programs(
                battery_plan, self.battery_config.settings, now
            )
            await self._async_sync_inverter(now)
            await self._async_update_power_cap(now, snapshots, grid_snapshot)

        return EngineData(devices=snapshots, grid=grid_snapshot, battery=battery_plan)
