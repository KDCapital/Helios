"""Data update coordinators for KD Helios."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .accounting import DeviceSnapshot, DeviceTracker
from .battery import (
    DEFAULT_BATTERY_VOLTAGE,
    GRID_CHARGE_CURRENT_REGISTER,
    AppliedPlan,
    BatterySettings,
    ChargePlan,
    ChargeSource,
    ChargeWindow,
    ControlPlan,
    TouPlan,
    WorkMode,
    build_control_plan,
    charge_current_a,
    charge_power_caps,
    plan_charge,
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
    InverterNotReadyError,
    InverterWriteError,
    async_control_matches,
    async_write_control_verified,
    async_write_registers,
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
from .resolve import resolve_battery, resolve_ev, resolve_grid, resolve_joulo
from .sources import read_number, read_power
from .storage import HeliosStorage

_LOGGER = logging.getLogger(__name__)

#: Re-assert the control state even when our own key did not change. This
#: detects a manual change on the inverter and rewrites it, just as the proven
#: Deye planner does on its five-minute cycle.
CONTROL_RESYNC_INTERVAL = timedelta(minutes=5)


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
            try:
                config = resolve_joulo(
                    hass, resolve_ev(hass, DeviceConfig.from_subentry(subentry))
                )
            except Exception:
                _LOGGER.exception(
                    "Could not read the configuration of %s", subentry.title
                )
                config = DeviceConfig.from_subentry(subentry)
            self.trackers[config.subentry_id] = DeviceTracker(
                hass,
                config,
                storage.device_state(config.subentry_id),
            )

        battery_subentries = entry.get_subentries_of_type(SUBENTRY_TYPE_BATTERY)
        self.battery_config: BatteryConfig | None = (
            resolve_battery(hass, BatteryConfig.from_subentry(battery_subentries[0]))
            if battery_subentries
            else None
        )
        self.battery_plan: ChargePlan | None = None
        #: The one window currently programmed into the inverter, which is
        #: either the active window or the next one. ``battery_plan`` keeps the
        #: whole proposal for the dashboard.
        self.schedule_plan: ChargePlan | None = None
        self.tou_plan: TouPlan | None = None
        #: The state the inverter should hold now, written to all six slots.
        self.control_plan: ControlPlan | None = None
        self._control_key: tuple | None = None
        self._applied_current: int | None = None
        self._max_charge_current_a: int | None = None
        self._max_current_source: str = "unknown"
        self._last_control_check: datetime | None = None
        #: A window that has started must finish; a mid-window replan must not
        #: turn the charger off just because the next quarter looks better.
        self._held_charge: ChargeWindow | None = None
        #: End of a manual test charge, or None. It forces grid charging for
        #: the rest of the current price quarter.
        self._test_charge_until: datetime | None = None
        #: Recent control decisions, newest last. Exposed through diagnostics
        #: and the status sensor so a power or mode surprise can be explained.
        self.decision_log: deque[dict[str, Any]] = deque(maxlen=200)
        self.applied_plan: AppliedPlan | None = (
            AppliedPlan.from_dict(storage.battery_state())
            if self.battery_config is not None
            else None
        )
        self.control_enabled: bool = bool(storage.control_state().get("enabled", False))
        self.last_control_error: str | None = None
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
    def _refresh_battery_sensors(self) -> None:
        """Retry auto-resolution once the inverter entities exist.

        A cold start can set this entry up before Solarman has registered its
        sensors. Resolution is retried on each tick until the missing fields
        are filled; once found they are kept, because the user's explicit
        choices still win in :func:`resolve_battery`.
        """
        config = self.battery_config
        if config is None:
            return
        if (
            config.soc_entity
            and config.house_load_entity
            and config.battery_power_entity
            and config.battery_voltage_entity
        ):
            return
        subentries = self.entry.get_subentries_of_type(SUBENTRY_TYPE_BATTERY)
        if not subentries:
            return
        resolved = resolve_battery(
            self.hass, BatteryConfig.from_subentry(subentries[0])
        )
        if resolved != config:
            self.battery_config = resolved

    def _refresh_grid_config(self) -> None:
        """Retry grid auto-resolution after a cold-start ordering race."""
        tracker = self.grid_tracker
        if tracker is None:
            return
        config = tracker.config
        if config.import_entity and config.export_entity and config.import_power_entity:
            return
        subentries = self.entry.get_subentries_of_type(SUBENTRY_TYPE_GRID)
        if not subentries:
            return
        resolved = resolve_grid(self.hass, GridConfig.from_subentry(subentries[0]))
        if resolved != config:
            tracker.config = resolved

    def _refresh_device_configs(self) -> None:
        """Retry EV and ERE auto-resolution after a cold-start race."""
        ev_fields = (
            "ev_soc_entity",
            "ev_target_soc_entity",
            "ev_power_entity",
            "ev_departure_entity",
            "ev_capacity_entity",
        )
        ere_fields = ("credits_entity", "kwh_entity", "price_entity")
        for subentry in device_subentries(self.entry):
            tracker = self.trackers.get(subentry.subentry_id)
            if tracker is None:
                continue
            config = tracker.config
            missing_ev = config.ev_device_id is not None and any(
                getattr(config, field, None) is None for field in ev_fields
            )
            missing_ere = config.ere.mode.value == "joulo" and any(
                getattr(config.ere, field, None) is None for field in ere_fields
            )
            if not missing_ev and not missing_ere:
                continue
            try:
                resolved = resolve_joulo(
                    self.hass,
                    resolve_ev(self.hass, DeviceConfig.from_subentry(subentry)),
                )
            except Exception as err:  # noqa: BLE001 - a bad registry read is retried
                _LOGGER.debug("Could not resolve %s yet: %s", subentry.title, err)
                continue
            if resolved != config:
                tracker.config = resolved

    def _persist_control(self) -> None:
        """Store the control state."""
        self.storage.async_set_control({"enabled": self.control_enabled})

    async def async_test_charge(self, minutes: int = 2) -> dict[str, Any]:
        """Force a grid charge for a short test period.

        This is the panel's test button. It runs for two minutes from the
        moment it is pressed, long enough to verify the inverter follows the
        state without a long manual charge. It still respects the current
        ceiling, the fuse cap, EV priority and the temperature derate.
        """
        config = self.battery_config
        if config is None or config.inverter_device_id is None:
            raise ValueError("No inverter is configured for this battery")
        if not config.allow_writes:
            raise ValueError(
                "Writing to the inverter is disabled in the battery settings"
            )
        if not self.control_enabled:
            raise ValueError("Turn inverter control on before starting a test charge")

        now = dt_util.utcnow()
        duration = max(1, min(60, int(minutes)))
        end = now + timedelta(minutes=duration)
        self._test_charge_until = end
        await self.async_refresh()
        return {"until": end.isoformat(), "minutes": duration}

    async def async_set_control(self, enabled: bool) -> None:
        """Take or release control of the inverter.

        Taking control writes the current state. Releasing it never leaves grid
        charging behind: all six programs are written with charging off and the
        reserve SOC, and the work mode goes back to self-consumption.
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
            control = self.control_plan
            if control is None or self.schedule_plan is None or self.tou_plan is None:
                raise ValueError("There is no charge plan to write yet")
            self.last_verification = await async_write_control_verified(
                self.hass, device_id, control
            )
            self._control_key = self._control_key_for(control)
            self._applied_current = control.charge_current_a
            self._last_control_check = dt_util.utcnow()
            self.applied_plan = AppliedPlan.from_plan(
                self.schedule_plan, self.tou_plan, dt_util.utcnow()
            )
            self.storage.async_set_battery(self.applied_plan.as_dict())
            self._clear_issue(ISSUE_INVERTER_WRITE_FAILED)
        else:
            release = self._release_control(config)
            self.last_verification = await async_write_control_verified(
                self.hass, device_id, release
            )
            self.applied_plan = None
            self._control_key = None
            self._applied_current = 0
            self._last_control_check = None
            self._test_charge_until = None
            self._held_charge = None
            self.storage.async_set_battery({})

        self.control_enabled = enabled
        self._persist_control()
        await self.storage.async_save()
        self.async_set_updated_data(self.data)
        _LOGGER.info("Inverter control %s", "enabled" if enabled else "released")

    def _current_plan(
        self, plan: ChargePlan, window: ChargeWindow | None
    ) -> ChargePlan:
        """Return the plan snapshot for the window active right now."""
        if window is None:
            return replace(
                plan,
                windows=(),
                energy_kwh=0.0,
                cost=0.0,
                expected_saving=0.0,
            )
        return replace(
            plan,
            windows=(window,),
            energy_kwh=window.energy_kwh,
            cost=window.cost,
            expected_saving=window.profit,
        )

    def _record_decision(self, event: str, **details: Any) -> None:
        """Remember one control decision for diagnostics."""
        entry: dict[str, Any] = {
            "time": dt_util.utcnow().isoformat(),
            "event": event,
        }
        entry.update(details)
        self.decision_log.append(entry)
        _LOGGER.debug("KD Helios %s: %s", event, details)

    def _control_key_for(self, control: ControlPlan) -> tuple:
        """Return the values a write has to change before it is repeated."""
        return (
            tuple(
                (program.power_w, program.soc_pct, int(program.source))
                for program in control.programs
            ),
            control.charge_current_a,
            int(control.work_mode),
        )

    def _discharge_power_w(self, config: BatteryConfig) -> int:
        """Return the discharge ceiling written to the TOU power block."""
        kw = config.max_discharge_kw or config.max_charge_kw
        return max(0, round(kw * 1000))

    def _max_grid_charge_current(self) -> int:
        """Return the grid-charge current ceiling the controller may use.

        The SG04LP3 register stops at 185 A, while the SG05LP3-SM2 accepts
        240 A. A user-selected model wins; otherwise the inverter device model
        is used. The Solarman number entity's own maximum is only a fallback,
        because some profiles expose a generic 350 A for a 240 A inverter.
        """
        config = self.battery_config
        override = config.inverter_model if config is not None else "auto"
        if override == "sg04lp3":
            self._max_current_source = "selected SG04LP3"
            return 185
        if override == "sg05lp3":
            self._max_current_source = "selected SG05LP3"
            return 240

        if self._max_charge_current_a is not None:
            return self._max_charge_current_a

        device_id = config.inverter_device_id if config is not None else None
        model = ""
        discovered: int | None = None
        if device_id is not None:
            device = dr.async_get(self.hass).async_get(device_id)
            if device is not None:
                parts = [
                    device.manufacturer,
                    device.model,
                    device.name,
                    device.sw_version,
                ]
                parts += [str(value) for _, value in device.identifiers]
                model = " ".join(part for part in parts if part).upper()

            registry = er.async_get(self.hass)
            for item in registry.entities.values():
                if item.device_id != device_id or item.domain != "number":
                    continue
                label = " ".join(
                    part
                    for part in (
                        item.original_name,
                        item.entity_id,
                        item.translation_key,
                    )
                    if part
                ).lower()
                if "grid" not in label or "current" not in label:
                    continue
                state = self.hass.states.get(item.entity_id)
                if state is not None:
                    maximum = state.attributes.get("max")
                    try:
                        if maximum is not None:
                            discovered = max(1, min(400, int(float(maximum))))
                    except (TypeError, ValueError):
                        pass
                break

        if "SG05LP3" in model:
            self._max_charge_current_a = 240
            self._max_current_source = f"detected {model.strip()}"
        elif "SG04LP3" in model:
            self._max_charge_current_a = 185
            self._max_current_source = f"detected {model.strip()}"
        elif discovered is not None:
            # Unknown model: trust a plausible entity maximum, but not the
            # generic 350 A profile value.
            self._max_charge_current_a = min(discovered, 240)
            self._max_current_source = "entity maximum"
        else:
            self._max_charge_current_a = 185
            self._max_current_source = "fallback"
        return self._max_charge_current_a

    def _battery_voltage(self) -> float:
        """Return the pack voltage used to turn charge power into current."""
        config = self.battery_config
        if config is not None and config.battery_voltage_entity is not None:
            voltage = read_number(self.hass, config.battery_voltage_entity)
            if voltage is not None and voltage > 0:
                return voltage
        return DEFAULT_BATTERY_VOLTAGE

    def _release_control(self, config: BatteryConfig) -> ControlPlan:
        """Return the six self-consumption programs written on release."""
        return build_control_plan(
            config.settings,
            charge=False,
            hold_soc=None,
            power_w=self._discharge_power_w(config),
            charge_current_a=0,
            work_mode=WorkMode(config.restore_work_mode),
        )

    async def _async_sync_inverter(self, now: datetime) -> list[Blocker]:
        """Write the current control state when it changed.

        Returns the reasons the write did not happen, so they can be shown next
        to the plan instead of only living in a log line.
        """
        config = self.battery_config
        control = self.control_plan
        if config is None or control is None:
            return []
        if not config.allow_writes:
            return [Blocker(SOURCE_WRITE, "writes_disabled")]
        if config.inverter_device_id is None:
            return [Blocker(SOURCE_WRITE, "no_inverter")]

        charging = any(
            program.source is ChargeSource.GRID for program in control.programs
        )
        if not self.control_enabled:
            return [Blocker(SOURCE_WRITE, "control_off")] if charging else []

        key = self._control_key_for(control)
        if key == self._control_key:
            due = (
                self._last_control_check is None
                or now - self._last_control_check >= CONTROL_RESYNC_INTERVAL
            )
            if not due:
                return []
            # Compare and re-write the current the cap actually applied, not
            # the uncapped plan value. Otherwise every resync would briefly
            # raise the charge current back to full power.
            resync_control = control
            if charging and self._applied_current is not None:
                resync_control = replace(
                    control, charge_current_a=self._applied_current
                )
            matches = await async_control_matches(
                self.hass, config.inverter_device_id, resync_control
            )
            if matches:
                self._last_control_check = now
                return []
            if matches is None:
                self._record_decision("resync_unreadable")
            else:
                self._record_decision("resync_drift")
            control = resync_control

        try:
            result = await async_write_control_verified(
                self.hass, config.inverter_device_id, control
            )
        except InverterNotReadyError as err:
            # Solarman has not loaded yet (custom integrations are set up
            # concurrently). Retry on the next tick instead of raising a Repair
            # for what is only a startup ordering issue.
            self.last_control_error = str(err)
            self._control_key = None
            return [Blocker(SOURCE_WRITE, "inverter_not_ready")]
        except InverterWriteError as err:
            return self._write_failed(err)
        except Exception as err:  # noqa: BLE001 - never break accounting
            return self._write_failed(err)

        self.last_control_error = None
        self.last_verification = result
        self._clear_issue(ISSUE_INVERTER_WRITE_FAILED)
        self._control_key = key
        self._applied_current = control.charge_current_a
        self._last_control_check = now
        self._record_decision(
            "write",
            charge=charging,
            current_a=control.charge_current_a,
            work_mode=int(control.work_mode),
            verified=result.get("verified"),
        )
        _LOGGER.info(
            "KD Helios control state: %s, SOC %s%%, current %s A, work mode %s",
            "charging" if charging else "idle",
            control.programs[0].soc_pct,
            control.charge_current_a,
            int(control.work_mode),
        )
        if self.schedule_plan is not None and self.tou_plan is not None:
            self.applied_plan = AppliedPlan.from_plan(
                self.schedule_plan, self.tou_plan, now
            )
            self.storage.async_set_battery(self.applied_plan.as_dict())
        return []

    def _write_failed(self, err: Exception) -> list[Blocker]:
        """Record a refused or unconfirmed write and raise a Repair."""
        message = str(err)
        self.last_control_error = message
        self._record_decision("write_failed", error=message)
        _LOGGER.error("Could not write the inverter control state: %s", err)
        self._raise_issue(
            ISSUE_INVERTER_WRITE_FAILED,
            placeholders={"error": message},
        )
        # The inverter may hold a partially written state; write it again on
        # the next tick instead of trusting the last key.
        self._control_key = None
        self._applied_current = None
        self._last_control_check = None
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
        # More than one car can be configured; use the first one that actually
        # has everything needed, so an idle or unreadable car does not hide the
        # other one's window.
        for tracker in self.trackers.values():
            ev = tracker.config
            if not ev.has_ev_schedule:
                continue

            soc = read_number(self.hass, ev.ev_soc_entity)
            if soc is None:
                continue
            capacity = ev.ev_pack_kwh
            if ev.ev_capacity_entity is not None:
                capacity = read_number(self.hass, ev.ev_capacity_entity) or capacity
            power_w = read_power(self.hass, ev.ev_power_entity)
            if power_w is None:
                # The charger's own power sensor is a good proxy for the car's.
                power_w = read_power(self.hass, ev.power_entity)
            power_kw = (power_w or 0.0) / 1000.0
            if capacity <= 0 or power_kw <= 0:
                continue

            departure = self._ev_departure(now, ev)
            if departure is None:
                continue

            target_soc = ev.ev_target_soc
            if ev.ev_target_soc_entity is not None:
                # A sensor tracks a target that changes per trip; otherwise the
                # fixed value from the form is used.
                target_soc = (
                    read_number(self.hass, ev.ev_target_soc_entity) or target_soc
                )
            needed = energy_needed_kwh(capacity, target_soc, soc)
            if needed <= 0:
                continue

            window = ev_charge_window(
                series.priced_slots(now, departure), departure, needed, power_kw
            )
            if window is None:
                continue
            return (window.start, window.end, soc)
        return None

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
        control = self.control_plan
        if config is None or control is None:
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
        # The grid meter also measures the battery while it charges. Using that
        # total as the house load makes the cap throttle the battery because of
        # its own draw. The house-load sensor is the load the battery still has
        # to fit beside; fall back to the grid total when it is not configured.
        load_kw = grid_kw
        if config.house_load_entity is not None:
            house_w = read_power(self.hass, config.house_load_entity)
            if house_w is not None:
                load_kw = house_w / 1000.0
        elif config.battery_power_entity is not None:
            battery_w = read_power(self.hass, config.battery_power_entity)
            if battery_w is not None:
                # No house sensor: remove the battery's own contribution from
                # the meter total. Sign conventions differ, so use magnitude.
                load_kw = max(0.0, grid_kw - abs(battery_w) / 1000.0)
        temperature = (
            self._read_temperature(config.battery_temp_entity)
            if config.temp_charge_limit_enabled
            else None
        )
        limit, blockers = charge_power_caps(
            config.settings.max_charge_kw,
            config.effective_import_cap,
            load_kw,
            ev_kw,
            config.priority,
            config.effective_inverter_cap,
            load_kw if config.house_behind_inverter else 0.0,
            temperature_c=temperature,
            temp_limit_c=config.temp_charge_limit_c,
            temp_band_c=config.temp_charge_limit_band_c,
            temp_floor_pct=config.temp_charge_limit_floor_pct,
        )

        voltage = self._battery_voltage()
        max_current = self._max_grid_charge_current()
        current = charge_current_a(limit, voltage, max_current)
        charging = any(
            program.source is ChargeSource.GRID for program in control.programs
        )
        self._record_decision(
            "cap",
            charging=charging,
            limit_kw=round(limit, 3),
            import_cap_kw=round(config.effective_import_cap, 3),
            inverter_cap_kw=round(config.effective_inverter_cap, 3),
            inverter_load_kw=(
                round(load_kw, 2) if config.house_behind_inverter else 0.0
            ),
            house_behind_inverter=config.house_behind_inverter,
            fuse_amps=config.fuse_amps,
            inverter_fuse_amps=config.inverter_fuse_amps,
            phases=config.phases,
            load_kw=round(load_kw, 2),
            ev_kw=round(ev_kw, 2),
            grid_kw=round(grid_kw, 2),
            voltage_v=round(voltage, 2),
            current_a=current,
            max_current_a=max_current,
            max_current_source=self._max_current_source,
            effective_kw=round(current * voltage / 1000.0, 2) if charging else 0.0,
            reasons=[blocker.reason for blocker in blockers],
        )
        if (
            not self.control_enabled
            or not config.allow_writes
            or config.inverter_device_id is None
        ):
            return blockers
        if not charging:
            self._applied_current = 0
            return blockers

        if self._applied_current is not None and current == self._applied_current:
            return blockers
        try:
            await async_write_registers(
                self.hass,
                config.inverter_device_id,
                {GRID_CHARGE_CURRENT_REGISTER: [current]},
            )
        except InverterNotReadyError:
            # Same startup race as the control write; try again next tick.
            return blockers
        except Exception as err:  # noqa: BLE001 - never break accounting
            self.last_control_error = str(err)
            _LOGGER.error("Could not cap the charge current: %s", err)
            return blockers
        self._applied_current = current
        _LOGGER.info(
            "KD Helios grid charge current set to %s A (%.2f kW at %.1f V); "
            "cap %.2f kW reasons %s",
            current,
            current * voltage / 1000.0,
            voltage,
            limit,
            [blocker.reason for blocker in blockers] or "none",
        )
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

        self._refresh_grid_config()
        grid_snapshot: GridSnapshot | None = None
        if self.grid_tracker is not None:
            self.grid_tracker.update(now, available_series)
            grid_snapshot = self.grid_tracker.snapshot(now, available_series)
            self.storage.async_set_grid(self.grid_tracker.acc.as_dict())

        self._learn_load(now)

        self._refresh_battery_sensors()
        self._refresh_device_configs()
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
            planned_window = battery_plan.active_window(now)
            hold_active = hold is not None and hold[0] <= now < hold[1]
            if self._held_charge is not None:
                held = self._held_charge
                if (
                    now >= held.end
                    or hold_active
                    or (
                        soc_pct is not None and soc_pct >= settings.target_soc_pct - 0.5
                    )
                    or battery_plan.reason in {"prices_stale", "no_prices", "no_soc"}
                ):
                    self._held_charge = None
            if self._held_charge is None and planned_window is not None:
                self._held_charge = planned_window
            active_window = self._held_charge or planned_window
            if self._test_charge_until is not None and now >= self._test_charge_until:
                self._test_charge_until = None
            test_charge = (
                self._test_charge_until is not None and now < self._test_charge_until
            )
            charge = test_charge or active_window is not None
            hold_soc = None
            if not charge and hold_active:
                hold_soc = hold[2]

            voltage = self._battery_voltage()
            requested_a = (
                round(settings.max_charge_kw * 1000.0 / voltage)
                if charge and voltage > 0
                else 0
            )
            max_current = self._max_grid_charge_current()
            current_a = charge_current_a(
                settings.max_charge_kw if charge else 0.0, voltage, max_current
            )
            effective_kw = round(current_a * voltage / 1000.0, 2) if charge else 0.0
            control_settings = (
                replace(settings, target_soc_pct=100.0) if test_charge else settings
            )
            control = build_control_plan(
                control_settings,
                charge=charge,
                hold_soc=hold_soc,
                power_w=self._discharge_power_w(self.battery_config),
                charge_current_a=current_a,
            )
            self.control_plan = control
            self.tou_plan = TouPlan(programs=control.programs, truncated=False)
            self.schedule_plan = self._current_plan(battery_plan, active_window)
            changed = self._control_key_for(control) != self._control_key

            self._record_decision(
                "plan",
                reason=battery_plan.reason,
                soc_pct=soc_pct,
                windows=len(battery_plan.windows),
                active_start=(
                    active_window.start.isoformat()
                    if active_window is not None
                    else None
                ),
                active_end=(
                    active_window.end.isoformat() if active_window is not None else None
                ),
                prices_stale=battery_plan.prices_stale,
            )
            self._record_decision(
                "control",
                charge=charge,
                test_charge=test_charge,
                test_until=(
                    self._test_charge_until.isoformat()
                    if self._test_charge_until is not None
                    else None
                ),
                target_soc=control_settings.target_soc_pct,
                reserve_soc=settings.reserve_soc_pct,
                program_soc=control.programs[0].soc_pct,
                program_source=int(control.programs[0].source),
                program_power_w=control.programs[0].power_w,
                hold_soc=hold_soc,
                voltage_v=round(voltage, 2),
                voltage_source=(
                    self.battery_config.battery_voltage_entity or "default"
                ),
                requested_current_a=requested_a,
                current_a=current_a,
                max_current_a=max_current,
                max_current_source=self._max_current_source,
                effective_kw=effective_kw,
                power_w=self._discharge_power_w(self.battery_config),
            )
            if charge and requested_a > current_a and changed:
                _LOGGER.info(
                    "KD Helios charge current limited to %s A by the inverter "
                    "(requested %s A at %.1f V); %.2f kW instead of %.2f kW",
                    current_a,
                    requested_a,
                    voltage,
                    effective_kw,
                    settings.max_charge_kw,
                )

            blockers: list[Blocker] = list(battery_plan.blockers)
            if credit.blocker is not None:
                blockers.append(credit.blocker)
            if weekly.blocker is not None:
                blockers.append(weekly.blocker)
            hold_active = hold is not None and hold[0] <= now < hold[1]
            if (
                not charge
                and not hold_active
                and soc_pct is not None
                and soc_pct <= settings.reserve_soc_pct + 0.5
            ):
                blockers.append(
                    Blocker(
                        SOURCE_PLAN,
                        "at_reserve",
                        {
                            "soc": round(soc_pct, 1),
                            "reserve": settings.reserve_soc_pct,
                        },
                    )
                )

            blockers.extend(await self._async_sync_inverter(now))
            blockers.extend(
                await self._async_update_power_cap(now, snapshots, grid_snapshot)
            )
            blockers.extend(
                self._check_not_delivering(
                    now, self.schedule_plan or battery_plan, soc_pct
                )
            )
            self.blockers.update(blockers, now)
        else:
            self.blockers.clear()

        return EngineData(devices=snapshots, grid=grid_snapshot, battery=battery_plan)
