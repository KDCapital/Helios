"""WebSocket API for the KD Helios panel.

The panel is a frontend custom element, so it needs one call that returns
everything it renders. Keeping the shape in ``build_dashboard`` means it can be
tested without a browser.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .history import RANGES, async_history

#: How much of the price timeline the panel draws.
CHART_HOURS_AHEAD = 24
CHART_HOURS_BEHIND = 1


def _loaded_entry(
    hass: HomeAssistant, entry_id: str | None = None
) -> ConfigEntry | None:
    """Return a loaded KD Helios entry."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        if entry_id is None or entry.entry_id == entry_id:
            return entry
    return None


def _round(value: float | None, digits: int) -> float | None:
    """Round a number for display."""
    return None if value is None else round(value, digits)


def _battery_payload(runtime: Any, now: Any) -> dict[str, Any] | None:
    """Return the battery plan, the applied plan and the programs."""
    config = runtime.engine.battery_config
    if config is None:
        return None
    plan = runtime.engine.battery_plan
    applied = runtime.engine.applied_plan

    payload: dict[str, Any] = {
        "name": config.name,
        "soc_pct": plan.soc_pct if plan else None,
        "capacity_kwh": config.capacity_kwh,
        "max_charge_kw": config.max_charge_kw,
        "reserve_soc_pct": config.reserve_soc_pct,
        "target_soc_pct": config.target_soc_pct,
        "efficiency_pct": config.efficiency_pct,
        "horizon_hours": config.horizon_hours,
        "allow_writes": config.allow_writes,
        "has_inverter": config.inverter_device_id is not None,
        "control_enabled": runtime.engine.control_enabled,
        "test_charge_until": (
            runtime.engine._test_charge_until.isoformat()
            if runtime.engine._test_charge_until is not None
            else None
        ),
        "restore_work_mode": config.restore_work_mode,
        "blockers": runtime.engine.blockers.as_list(),
        "weekly_full_charge": bool(
            getattr(runtime.engine.weekly_result, "active", False)
        ),
        "write_verified": (
            getattr(runtime.engine, "last_verification", None) or {}
        ).get("verified"),
        "last_control_error": runtime.engine.last_control_error,
        "plan": None,
        "applied": None,
    }

    if plan is not None:
        next_window = plan.next_window(now)
        payload["plan"] = {
            "reason": plan.reason,
            "energy_kwh": _round(plan.energy_kwh, 2),
            "cost": _round(plan.cost, 2),
            "expected_saving": _round(plan.expected_saving, 2),
            "charging_now": plan.active_window(now) is not None,
            "next_window_start": (
                next_window.start.isoformat() if next_window is not None else None
            ),
            "solar_credit_kwh": _round(plan.solar_credit_kwh, 2),
            "prices_stale": plan.prices_stale,
            "windows": [
                {
                    "start": window.start.isoformat(),
                    "end": window.end.isoformat(),
                    "energy_kwh": _round(window.energy_kwh, 2),
                    "drain_kwh": _round(window.drain_kwh, 2),
                    "average_price": _round(window.average_price, 5),
                    "expected_profit": _round(window.profit, 2),
                }
                for window in plan.windows
            ],
        }

    tou = runtime.engine.tou_plan
    if tou is not None:
        payload["programs"] = [
            {
                "start": f"{program.hhmm:04d}",
                "power_w": program.power_w,
                "soc_pct": program.soc_pct,
                "source": int(program.source),
            }
            for program in tou.programs
        ]
        payload["truncated"] = tou.truncated

    if applied is not None:
        payload["applied"] = {
            "applied_at": applied.applied_at.isoformat(),
            "expected_saving": _round(applied.expected_saving, 2),
            "energy_kwh": _round(applied.energy_kwh, 2),
            "soc_pct": applied.soc_pct,
            "charging_now": applied.active_window(now) is not None,
            "windows": [
                {
                    "start": window.start.isoformat(),
                    "end": window.end.isoformat(),
                    "energy_kwh": _round(window.energy_kwh, 2),
                }
                for window in applied.windows
            ],
            "programs": [
                {
                    "start": f"{program.hhmm:04d}",
                    "power_w": program.power_w,
                    "soc_pct": program.soc_pct,
                    "source": int(program.source),
                }
                for program in applied.programs
            ],
        }
    payload["decisions"] = list(runtime.engine.decision_log)
    return payload


def _grid_payload(runtime: Any) -> dict[str, Any] | None:
    """Return the grid connection ledger."""
    snapshot = runtime.engine.data.grid if runtime.engine.data else None
    if snapshot is None:
        return None
    return {
        "imported_kwh": _round(snapshot.imported_kwh, 2),
        "exported_kwh": _round(snapshot.exported_kwh, 2),
        "imported_cost": _round(snapshot.imported_cost, 2),
        "export_credit": _round(snapshot.export_credit, 2),
        "fixed_cost": _round(snapshot.fixed_cost, 2),
        "total_cost": _round(snapshot.total_cost, 2),
        "netted_kwh": _round(snapshot.netted_kwh, 2),
        "open_export_kwh": _round(snapshot.open_export_kwh, 2),
        "export_price": _round(snapshot.export_price, 5),
        "import_price": _round(snapshot.import_price, 5),
        "saldering_enabled": snapshot.saldering_enabled,
        "saldering_active": snapshot.saldering_active,
        "fixed_monthly": _round(snapshot.fixed_monthly, 2),
        "billing_year": snapshot.year,
        "imported_kwh_today": _round(snapshot.imported_kwh_today, 2),
        "imported_cost_today": _round(snapshot.imported_cost_today, 2),
        "exported_kwh_today": _round(snapshot.exported_kwh_today, 2),
        "export_credit_today": _round(snapshot.export_credit_today, 2),
        "fixed_cost_today": _round(snapshot.fixed_cost_today, 2),
        "total_cost_today": _round(snapshot.total_cost_today, 2),
        "unpriced_kwh": _round(snapshot.unpriced_kwh, 3),
        "import_power": _round(snapshot.import_power, 0),
        "export_power": _round(snapshot.export_power, 0),
    }


def _devices_payload(runtime: Any) -> list[dict[str, Any]]:
    """Return one row per tracked device."""
    data = runtime.engine.data
    if data is None:
        return []
    devices: list[dict[str, Any]] = []
    for tracker in runtime.engine.trackers.values():
        config = tracker.config
        subentry_id = config.subentry_id
        snapshot = data.devices.get(subentry_id)
        if snapshot is None:
            continue
        devices.append(
            {
                "name": config.name,
                "kind": config.kind.value,
                "energy_kwh": _round(snapshot.energy_kwh, 2),
                "energy_kwh_today": _round(tracker.acc.day_kwh, 2),
                "cost": _round(snapshot.cost, 2),
                "cost_today": _round(snapshot.cost_today, 2),
                "reimbursement": _round(snapshot.reimbursement, 2),
                "ere_value": _round(snapshot.ere_value, 2),
                "net_cost": _round(snapshot.net_cost, 2),
                "has_offsets": config.has_offsets,
                "power": _round(snapshot.power, 0),
                "price": _round(snapshot.price, 5),
            }
        )
    return sorted(devices, key=lambda row: row["name"].lower())


def _summary(runtime: Any) -> dict[str, Any]:
    """Return the headline figures the KPI cards show."""
    engine = runtime.engine
    grid = engine.data.grid if engine.data else None
    devices = _devices_payload(runtime)
    ev = [row for row in devices if row["kind"] == "ev_charger"]
    ev_cost = sum(row["cost"] for row in ev)
    ev_reimbursed = sum(row["reimbursement"] for row in ev)
    applied = engine.applied_plan
    plan = engine.battery_plan
    return {
        "grid_cost_today": grid.total_cost_today if grid else None,
        "grid_cost_year": grid.total_cost if grid else None,
        "grid_import_today": grid.imported_kwh_today if grid else None,
        "grid_export_today": grid.exported_kwh_today if grid else None,
        "grid_balance_today": (
            _round(grid.imported_kwh_today - grid.exported_kwh_today, 1)
            if grid
            else None
        ),
        "devices_cost_today": _round(sum(row["cost_today"] for row in devices), 2),
        "ev_balance": _round(ev_reimbursed - ev_cost, 2) if ev else None,
        "ev_reimbursed": _round(ev_reimbursed, 2) if ev else None,
        "ev_cost_today": _round(sum(row["cost_today"] for row in ev), 2)
        if ev
        else None,
        "ev_count": len(ev),
        "battery_saving_applied": _round(applied.expected_saving, 2)
        if applied
        else None,
        "battery_saving_planned": _round(plan.expected_saving, 2) if plan else None,
        "control_enabled": engine.control_enabled,
    }


def build_dashboard(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return everything the panel renders."""
    runtime = entry.runtime_data
    prices = runtime.prices
    series = prices.series
    now = dt_util.utcnow()

    points = series.points_between(
        now - timedelta(hours=CHART_HOURS_BEHIND),
        now + timedelta(hours=CHART_HOURS_AHEAD),
    )
    all_in = [
        price
        for _, price, _ in series.points_between(
            now, now + timedelta(hours=CHART_HOURS_AHEAD)
        )
        if price is not None
    ]

    return {
        "entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "provider": prices.provider,
            "interval": prices.interval,
            "currency": hass.config.currency,
        },
        "now": now.isoformat(),
        "prices": {
            "current": _round(series.all_in_at(now), 5),
            "spot": _round(series.spot_at(now), 5),
            "next": _round(series.all_in_at(now + timedelta(minutes=15)), 5),
            "average_today": _round(
                series.average_all_in(
                    dt_util.as_local(now).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    ),
                    dt_util.as_local(now).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    + timedelta(days=1),
                ),
                5,
            ),
            "min": _round(min(all_in), 5) if all_in else None,
            "max": _round(max(all_in), 5) if all_in else None,
            "energy_tax": _round(series.meta.energy_tax, 5),
            "provider_charge": _round(series.meta.provider_charge, 5),
            "points": [
                {"t": when.isoformat(), "price": _round(price, 5)}
                for when, price, _ in points
                if price is not None
            ],
        },
        "grid": _grid_payload(runtime),
        "battery": _battery_payload(runtime, now),
        "devices": _devices_payload(runtime),
        "summary": _summary(runtime),
        "load_profile": {
            "coverage_pct": runtime.engine.load_profile.coverage_pct(),
            "predicted_kw": runtime.engine.load_profile.forecast_kw(now),
        },
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/history",
        vol.Optional("entry_id"): str,
        vol.Optional("range", default="7d"): vol.In(list(RANGES)),
    }
)
@websocket_api.async_response
async def websocket_history(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return historical costs from Home Assistant's long-term statistics."""
    entry = _loaded_entry(hass, msg.get("entry_id"))
    if entry is None:
        connection.send_error(msg["id"], "not_found", "No KD Helios entry is loaded")
        return
    connection.send_result(msg["id"], await async_history(hass, entry, msg["range"]))


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/dashboard",
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def websocket_dashboard(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the panel data."""
    entry = _loaded_entry(hass, msg.get("entry_id"))
    if entry is None:
        connection.send_error(msg["id"], "not_found", "No KD Helios entry is loaded")
        return
    connection.send_result(msg["id"], build_dashboard(hass, entry))


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/test_charge",
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def websocket_test_charge(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Start a test charge for the rest of the current price quarter."""
    entry = _loaded_entry(hass, msg.get("entry_id"))
    if entry is None:
        connection.send_error(msg["id"], "not_found", "No KD Helios entry is loaded")
        return
    try:
        result = await entry.runtime_data.engine.async_charge_quarter()
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_state", str(err))
        return
    connection.send_result(msg["id"], result)


def async_register_websocket_api(hass: HomeAssistant) -> None:
    """Register the panel's WebSocket commands."""
    websocket_api.async_register_command(hass, websocket_dashboard)
    websocket_api.async_register_command(hass, websocket_history)
    websocket_api.async_register_command(hass, websocket_test_charge)
