"""Data update coordinators for KD Helios."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .accounting import DeviceSnapshot, DeviceTracker
from .battery import (
    TOU_REGISTERS,
    AppliedPlan,
    BatterySettings,
    ChargePlan,
    ChargeSource,
    TouPlan,
    WorkMode,
    build_restore_programs,
    build_tou_programs,
    charge_power_caps,
    plan_charge,
    settings_fingerprint,
    should_apply,
)
from .const import (
    ACCOUNTING_UPDATE_INTERVAL,
    DOMAIN,
    ISSUE_BATTERY_NOT_DELIVERING,
    ISSUE_INVERTER_WRITE_FAILED,
    NOT_DELIVERING_GRACE,
    NOT_DELIVERING_POWER_W,
    NOT_DELIVERING_STRIKES,
    PRICE_UPDATE_INTERVAL,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_GRID,
    DeviceKind,
)
from .decisions import (
    SOURCE_DELIVERY,
    SOURCE_PLAN,
    SOURCE_WRITE,
    Blocker,
    BlockerRegistry,
)
from .ev import energy_needed_kwh, ev_charge_window
from .forecast import SolarCredit, remaining_consumption_kwh, solar_credit
from .grid import GridSnapshot, GridTracker
from .inverter import (
    InverterWriteError,
    async_write_registers,
    async_write_tou_verified,
)
from .maintenance import WeeklyFullCharge, WeeklyResult
from .models import BatteryConfig, DeviceConfig, GridConfig, device_subentries
from .prices import (
    PriceApiError,
    PriceSeries,
    UnknownProviderError,
    async_fetch_provider_prices,
    async_fetch_spot_prices,
    build_price_series,
)
from .profile import LoadProfile
from .resolve import resolve_ev, resolve_grid, resolve_joulo
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
        #: True while the series in use is the last good one, not a fresh fetch.
        self.stale = False
        #: Why the last fetch failed, when it did.
        self.last_error: str | None = None

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
                # Keep planning against the last good prices, but say so: a plan
                # built on a stale tariff is not the same as one built on this
                # hour's, and the difference must be visible rather than silent.
                self.stale = True
                self.last_error = str(err)
                _LOGGER.warning(
                    "Keeping the last known prices for %s: %s", self.provider, err
                )
                return self._last_good
            self.stale = True
            self.last_error = str(err)
            raise UpdateFailed(str(err)) from err

        spot_payload = await async_fetch_spot_prices(self.hass)
        series = build_price_series(
            provider_payload,
            spot_payload,
            provider=self.provider,
            interval=self.interval,
        )
        if not series:
            self.stale = True
            self.last_error = (
                f"No usable prices returned for provider '{self.provider}'"
            )
            raise UpdateFailed(self.last_error)
        self.stale = False
        self.last_error = None
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
            config = resolve_joulo(
                hass, resolve_ev(hass, DeviceConfig.from_subentry(subentry))
            )
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
        self.load_profile = LoadProfile.from_dict(
            storage.profile_state().get("buckets")
        )

        # -- supervision ---------------------------------------------------
        self.entry = entry
        #: Everything that currently shapes the plan, including why it lost.
        self.blockers = BlockerRegistry()
        #: Outcome of the last verified write (None when unreadable).
        self.last_verification: dict[str, Any] | None = None
        self._weekly = WeeklyFullCharge(
            enabled=bool(self.battery_config.weekly_full_charge_enabled)
            if self.battery_config is not None
            else False,
            weekday=(
                self.battery_config.weekly_full_charge_day
                if self.battery_config is not None
                else 0
            ),
        )
        #: What the weekly full charge wanted from the last tick.
        self.weekly_result: WeeklyResult = WeeklyResult()
        self._plan_fingerprint: tuple[float | bool, ...] | None = None
        integrity = storage.integrity_state()
        self._not_delivering_since: datetime | None = None
        self._delivery_strikes = int(integrity.get("strikes", 0) or 0)

        grid_subentries = entry.get_subentries_of_type(SUBENTRY_TYPE_GRID)
        self.grid_tracker: GridTracker | None = (
            GridTracker(
                hass,
                resolve_grid(hass, GridConfig.from_subentry(grid_subentries[0])),
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
            self.last_verification = await async_write_tou_verified(
                self.hass, device_id, self.tou_plan
            )
            self.applied_plan = AppliedPlan.from_plan(
                self.battery_plan, self.tou_plan, dt_util.utcnow()
            )
            self._last_apply = dt_util.utcnow()
            self._applied_cap = None
            self.storage.async_set_battery(self.applied_plan.as_dict())
            self._clear_issue(ISSUE_INVERTER_WRITE_FAILED)
        else:
            restore = build_restore_programs(config.settings)
            self.last_verification = await async_write_tou_verified(
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

    async def _async_sync_inverter(
        self,
        now: datetime,
        settings: BatterySettings,
        *,
        force: bool = False,
    ) -> list[Blocker]:
        """Apply the plan when it changed enough to matter.

        Returns the reasons the write did not happen, so they can be shown next
        to the plan instead of only living in a log line.
        """
        config = self.battery_config
        plan = self.battery_plan
        tou = self.tou_plan
        if config is None or plan is None or tou is None:
            return []
        if not config.allow_writes:
            return [Blocker(SOURCE_WRITE, "writes_disabled")]
        if config.inverter_device_id is None:
            return [Blocker(SOURCE_WRITE, "no_inverter")]
        if not self.control_enabled:
            # Nothing is being written because the user has not switched control
            # on. That is a reason worth showing, but only when there was
            # something to write in the first place.
            return [Blocker(SOURCE_WRITE, "control_off")] if plan.has_windows else []

        if not should_apply(
            plan,
            self.applied_plan,
            settings,
            last_apply=self._last_apply,
            now=now,
            force=force,
        ):
            return []

        try:
            result = await async_write_tou_verified(
                self.hass, config.inverter_device_id, tou
            )
        except InverterWriteError as err:
            return self._write_failed(err)
        except Exception as err:  # noqa: BLE001 - never break accounting
            return self._write_failed(err)

        self.last_control_error = None
        self.last_verification = result
        self._clear_issue(ISSUE_INVERTER_WRITE_FAILED)
        self._last_apply = now
        # The plan that was just written carries the uncapped charge power, so
        # the power cap has to be re-applied on this cycle.
        self._applied_cap = None
        self.applied_plan = AppliedPlan.from_plan(plan, tou, now)
        self.storage.async_set_battery(self.applied_plan.as_dict())
        return []

    def _write_failed(self, err: Exception) -> list[Blocker]:
        """Record a refused or unconfirmed write and raise a Repair."""
        message = str(err)
        self.last_control_error = message
        _LOGGER.error("Could not write the time-of-use programs: %s", err)
        self._raise_issue(
            ISSUE_INVERTER_WRITE_FAILED,
            placeholders={"error": message},
        )
        # The schedule in force is not the one that was planned, so forget the
        # snapshot: the next successful write must not be throttled against it.
        self._last_apply = None
        return [Blocker(SOURCE_WRITE, "write_failed", {"error": message})]

    def _issue_id(self, key: str) -> str:
        """Return the per-entry repair issue id."""
        return f"{key}_{self.entry.entry_id}"

    def _raise_issue(
        self,
        key: str,
        *,
        placeholders: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Raise a non-fixable Repair for this entry."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id(key),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=key,
            translation_placeholders=placeholders or {},
            data=data or {},
        )

    def _clear_issue(self, key: str) -> None:
        """Clear a Repair this entry raised, if it is still open."""
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(key))

    def _learn_load(self, now) -> None:
        """Fold the house load into the profile, if a sensor is configured."""
        config = self.battery_config
        if config is None or config.house_load_entity is None:
            return
        state = self.hass.states.get(config.house_load_entity)
        if state is None or state.state in ("unknown", "unavailable"):
            return
        try:
            watts = float(state.state)
        except (TypeError, ValueError):
            return
        unit = state.attributes.get("unit_of_measurement")
        kw = watts / 1000.0 if unit in (None, "W") else watts
        self.load_profile.add(now, kw)
        self.storage.async_set_profile({"buckets": self.load_profile.as_dict()})

    def _ev_hold(self, now, series) -> tuple[datetime, datetime, float] | None:
        """Return the car's window and its SOC, if the battery must hold there."""
        config = self.battery_config
        if config is None:
            return None
        ev = next(
            (
                tracker.config
                for tracker in self.trackers.values()
                if tracker.config.has_ev_schedule
            ),
            None,
        )
        if ev is None:
            return None

        soc = read_number(self.hass, ev.ev_soc_entity)
        if soc is None:
            return None
        capacity = ev.ev_pack_kwh
        if ev.ev_capacity_entity is not None:
            capacity = read_number(self.hass, ev.ev_capacity_entity) or capacity
        power_kw = read_number(self.hass, ev.ev_power_entity) or 0.0
        if capacity <= 0 or power_kw <= 0:
            return None

        departure = self._ev_departure(now, ev)
        if departure is None:
            return None

        needed = energy_needed_kwh(capacity, ev.ev_target_soc, soc)
        if needed <= 0:
            return None

        window = ev_charge_window(
            series.priced_slots(now, departure), departure, needed, power_kw
        )
        if window is None:
            return None
        return (window.start, window.end, soc)

    def _ev_departure(self, now, config) -> datetime | None:
        """Return the next departure time of the car."""
        if config.ev_departure_entity is not None:
            state = self.hass.states.get(config.ev_departure_entity)
            if state is not None and state.state not in ("unknown", "unavailable"):
                parsed = dt_util.parse_datetime(state.state)
                if parsed is None:
                    # EVCC may publish a clock time rather than a timestamp.
                    clock = dt_util.parse_time(state.state)
                    if clock is not None:
                        day = dt_util.as_local(now).replace(
                            hour=clock.hour,
                            minute=clock.minute,
                            second=0,
                            microsecond=0,
                        )
                        if day <= dt_util.as_local(now):
                            day += timedelta(days=1)
                        return dt_util.as_utc(day)
                if parsed is not None:
                    return dt_util.as_utc(parsed)

        local = dt_util.as_local(now)
        is_weekend = local.weekday() >= 5
        raw = config.ev_departure_weekend if is_weekend else config.ev_departure_weekday
        if not raw:
            return None
        parsed_time = dt_util.parse_time(raw)
        if parsed_time is None:
            return None
        departure = local.replace(
            hour=parsed_time.hour, minute=parsed_time.minute, second=0, microsecond=0
        )
        if departure <= local:
            departure += timedelta(days=1)
        return dt_util.as_utc(departure)

    async def _async_update_power_cap(
        self,
        now: datetime,
        snapshots: dict[str, DeviceSnapshot],
        grid_snapshot: GridSnapshot | None,
    ) -> list[Blocker]:
        """Hold the battery back while the car charges, and under the cap.

        The plan says "charge at full power", but the car may already be using
        that capacity, the connection may have no room left, or the battery may
        be hot. Rewriting only the power block leaves the schedule intact and
        reacts within a tick, which is what stops the battery from swallowing
        the capacity the car frees when it pauses for grid rewards.
        """
        config = self.battery_config
        tou = self.tou_plan
        if config is None or tou is None:
            return []

        ev_kw = (
            sum(
                (snapshots[sid].power or 0.0)
                for sid, tracker in self.trackers.items()
                if tracker.config.kind is DeviceKind.EV_CHARGER and sid in snapshots
            )
            / 1000.0
        )
        grid_kw = (grid_snapshot.import_power or 0.0) / 1000.0 if grid_snapshot else 0.0
        temperature = (
            self._read_temperature(config.battery_temp_entity)
            if config.temp_charge_limit_enabled
            else None
        )
        limit, blockers = charge_power_caps(
            config.settings.max_charge_kw,
            config.effective_import_cap,
            grid_kw,
            ev_kw,
            config.priority,
            temperature_c=temperature,
            temp_limit_c=config.temp_charge_limit_c,
            temp_band_c=config.temp_charge_limit_band_c,
            temp_floor_pct=config.temp_charge_limit_floor_pct,
        )

        if (
            not self.control_enabled
            or not config.allow_writes
            or config.inverter_device_id is None
        ):
            return blockers
        if not any(program.source is ChargeSource.GRID for program in tou.programs):
            return blockers
        if self._applied_cap is not None and abs(limit - self._applied_cap) < 0.25:
            return blockers

        values = [
            round(limit * 1000)
            if program.source is ChargeSource.GRID
            else program.power_w
            for program in tou.programs
        ]
        if values == [program.power_w for program in tou.programs]:
            # The plan already asks for exactly this, nothing to change.
            self._applied_cap = limit
            return blockers
        try:
            await async_write_registers(
                self.hass,
                config.inverter_device_id,
                {TOU_REGISTERS["power"]: values},
            )
        except Exception as err:  # noqa: BLE001 - never break accounting
            self.last_control_error = str(err)
            _LOGGER.error("Could not cap the charge power: %s", err)
            return blockers
        self._applied_cap = limit
        _LOGGER.debug("Charge power limited to %s kW", limit)
        return blockers

    def _read_temperature(self, entity_id: str | None) -> float | None:
        """Return a battery temperature in °C, or None when it cannot be read."""
        if entity_id is None:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        value = read_number(self.hass, entity_id)
        if value is None:
            return None
        if state.attributes.get("unit_of_measurement") == "°F":
            return (value - 32.0) * 5.0 / 9.0
        return value

    def _end_of_local_day(self, now: datetime) -> datetime:
        """Return the next local midnight, in UTC."""
        local = dt_util.as_local(dt_util.as_utc(now))
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return dt_util.as_utc(midnight + timedelta(days=1))

    def _read_forecast_kwh(self, entity_id: str | None) -> float | None:
        """Return a solar forecast in kWh, or None when it is not an energy value.

        A power sensor in W is refused rather than guessed at: scaling a
        kilowatt reading as if it were kilowatt-hours would credit the battery
        with an afternoon of sun that does not exist.
        """
        if entity_id is None:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        value = read_number(self.hass, entity_id)
        if value is None:
            return None
        unit = state.attributes.get("unit_of_measurement")
        if unit == "Wh":
            return value / 1000.0
        if unit == "MWh":
            return value * 1000.0
        if unit in (None, "", "kWh"):
            return value
        return None

    def _solar_credit(
        self,
        now: datetime,
        settings: BatterySettings,
        soc_pct: float | None,
    ) -> SolarCredit:
        """Return how much of today's charge the sun is expected to deliver."""
        config = self.battery_config
        if config is None or not config.solar_hold_enabled:
            return SolarCredit()
        if config.solar_forecast_entity is None:
            return SolarCredit()

        target = max(0.0, settings.target_soc_pct - (soc_pct or 0.0))
        needed = config.capacity_kwh * target / 100.0
        forecast = self._read_forecast_kwh(config.solar_forecast_entity)
        consumption = remaining_consumption_kwh(
            self.load_profile, now, self._end_of_local_day(now)
        )
        return solar_credit(forecast, consumption, needed)

    def _load_forecast(self, start: datetime, end: datetime) -> float | None:
        """Return the house energy the learned profile expects between two instants."""
        return self.load_profile.energy_kwh(start, end)

    def _effective_settings(
        self,
        now: datetime,
        soc_pct: float | None,
    ) -> tuple[BatterySettings, WeeklyResult]:
        """Return the settings to plan with, including a weekly full charge.

        The weekly cycle raises the target rather than adding a second planner:
        one plan, one target, and the reason the target moved is on record.
        """
        config = self.battery_config
        if config is None:
            raise ValueError("No battery is configured")
        settings = config.settings
        weekly = self._weekly.evaluate(now, soc_pct, self.storage.weekly_state())
        self.weekly_result = weekly
        if weekly.state:
            self.storage.async_set_weekly(dict(weekly.state))
        if weekly.active and weekly.target_soc_pct is not None:
            settings = replace(settings, target_soc_pct=weekly.target_soc_pct)
        return settings, weekly

    def _persist_integrity(self) -> None:
        """Store the non-delivery strike counter."""
        self.storage.async_set_integrity({"strikes": self._delivery_strikes})

    def _check_not_delivering(
        self,
        now: datetime,
        plan: ChargePlan | None,
        soc_pct: float | None,
    ) -> list[Blocker]:
        """Notice a schedule that is in force but not running.

        A battery that acknowledges the schedule and then delivers nothing looks
        identical to a working one in every entity: SOC stops falling, the plan
        still reads "planned". It gets a grace period, then a strike per grace
        period, then a Repair.
        """
        config = self.battery_config
        if config is None or config.battery_power_entity is None:
            return []
        if not self.control_enabled:
            self._not_delivering_since = None
            return []

        window = plan.active_window(now) if plan is not None else None
        if window is None:
            self._not_delivering_since = None
            return []

        power = read_number(self.hass, config.battery_power_entity)
        if power is None:
            return []

        if abs(power) >= NOT_DELIVERING_POWER_W:
            self._not_delivering_since = None
            if self._delivery_strikes:
                self._delivery_strikes = 0
                self._persist_integrity()
            self._clear_issue(ISSUE_BATTERY_NOT_DELIVERING)
            return []

        details: dict[str, Any] = {
            "power_w": round(power, 1),
            "soc_pct": None if soc_pct is None else round(soc_pct, 1),
            "strikes": self._delivery_strikes,
        }
        if self._not_delivering_since is None:
            self._not_delivering_since = now
            return [Blocker(SOURCE_DELIVERY, "not_delivering", details)]

        if now - self._not_delivering_since < NOT_DELIVERING_GRACE:
            return [Blocker(SOURCE_DELIVERY, "not_delivering", details)]

        self._delivery_strikes += 1
        self._not_delivering_since = now
        self._persist_integrity()
        details["strikes"] = self._delivery_strikes
        if self._delivery_strikes >= NOT_DELIVERING_STRIKES:
            self._raise_issue(
                ISSUE_BATTERY_NOT_DELIVERING,
                placeholders={
                    "power": f"{power:.0f}",
                    "soc": f"{soc_pct:.0f}" if soc_pct is not None else "?",
                },
                data=details,
            )
        return [Blocker(SOURCE_DELIVERY, "not_delivering", details)]

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

        self._learn_load(now)

        battery_plan: ChargePlan | None = None
        if self.battery_config is not None:
            series_for_plan = available_series or PriceSeries.empty(
                self.prices.provider, self.prices.interval
            )
            soc_pct = read_number(self.hass, self.battery_config.soc_entity)
            settings, weekly = self._effective_settings(now, soc_pct)
            credit = self._solar_credit(now, settings, soc_pct)
            hold = self._ev_hold(now, series_for_plan)

            battery_plan = plan_charge(
                series_for_plan,
                settings,
                now,
                soc_pct,
                load_forecast=self._load_forecast,
                solar_credit_kwh=credit.credit_kwh,
                max_discharge_kw=self.battery_config.max_discharge_kw,
                stale_prices=self.prices.stale,
                suppressed=(hold[0], hold[1]) if hold is not None else None,
            )
            self.battery_plan = battery_plan
            self.tou_plan = build_tou_programs(battery_plan, settings, now, hold=hold)

            blockers: list[Blocker] = list(battery_plan.blockers)
            if credit.blocker is not None:
                blockers.append(credit.blocker)
            if weekly.blocker is not None:
                blockers.append(weekly.blocker)
            if self.tou_plan.truncated:
                blockers.append(
                    Blocker(
                        SOURCE_PLAN,
                        "slots_truncated",
                        {"windows": len(battery_plan.windows)},
                    )
                )

            fingerprint = settings_fingerprint(settings) + (weekly.active,)
            force = (
                self._plan_fingerprint is not None
                and fingerprint != self._plan_fingerprint
            )
            self._plan_fingerprint = fingerprint

            blockers.extend(await self._async_sync_inverter(now, settings, force=force))
            blockers.extend(
                await self._async_update_power_cap(now, snapshots, grid_snapshot)
            )
            blockers.extend(self._check_not_delivering(now, battery_plan, soc_pct))
            self.blockers.update(blockers, now)
        else:
            self.blockers.clear()

        return EngineData(devices=snapshots, grid=grid_snapshot, battery=battery_plan)
