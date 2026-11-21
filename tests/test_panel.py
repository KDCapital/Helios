"""Tests for the sidebar panel: its WebSocket payload and its registration."""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kd_helios import panel
from custom_components.kd_helios.const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_HORIZON_HOURS,
    CONF_EFFICIENCY,
    CONF_ENERGY_ENTITY,
    CONF_EXPORT_DEDUCTION,
    CONF_GRID_FEE_MONTHLY,
    CONF_IMPORT_ENTITY,
    CONF_KIND,
    CONF_MAX_CHARGE_KW,
    CONF_NAME,
    CONF_PRICE_INTERVAL,
    CONF_PROVIDER,
    CONF_RESERVE_SOC,
    CONF_SALDERING,
    CONF_SOC_ENTITY,
    CONF_SUPPLY_COST_MONTHLY,
    CONF_TARGET_SOC,
    DOMAIN,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_DEVICE,
    SUBENTRY_TYPE_GRID,
)
from custom_components.kd_helios.websocket import (
    async_register_websocket_api,
    build_dashboard,
)

FROZEN = "2026-11-20 12:00:00+00:00"

USER_INPUT = {CONF_PROVIDER: "zonneplan", CONF_PRICE_INTERVAL: "quarterly"}

DEVICE_SUBENTRY = {
    "subentry_type": SUBENTRY_TYPE_DEVICE,
    "data": {
        CONF_NAME: "EV charger",
        CONF_KIND: "ev_charger",
        CONF_ENERGY_ENTITY: "sensor.ev_energy",
    },
    "title": "EV charger",
    "unique_id": None,
}

GRID_SUBENTRY = {
    "subentry_type": SUBENTRY_TYPE_GRID,
    "data": {
        CONF_IMPORT_ENTITY: "sensor.p1_import",
        CONF_SALDERING: True,
        CONF_EXPORT_DEDUCTION: 0.01361,
        CONF_SUPPLY_COST_MONTHLY: 6.0,
        CONF_GRID_FEE_MONTHLY: 20.0,
    },
    "title": "Grid connection",
    "unique_id": None,
}

BATTERY_SUBENTRY = {
    "subentry_type": SUBENTRY_TYPE_BATTERY,
    "data": {
        CONF_SOC_ENTITY: "sensor.battery_soc",
        CONF_BATTERY_CAPACITY_KWH: 10.0,
        CONF_MAX_CHARGE_KW: 3.0,
        CONF_RESERVE_SOC: 10.0,
        CONF_TARGET_SOC: 100.0,
        CONF_EFFICIENCY: 90.0,
        CONF_BATTERY_HORIZON_HOURS: 24,
    },
    "title": "Home battery",
    "unique_id": None,
}


def full_entry() -> MockConfigEntry:
    """Return an entry with a device, a grid connection and a battery."""
    return MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="zonneplan_quarterly",
        title="Zonneplan",
        subentries_data=[DEVICE_SUBENTRY, GRID_SUBENTRY, BATTERY_SUBENTRY],
    )


async def _setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set up an entry with the sensor states the payload reads."""
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.battery_soc", "25", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.ev_energy", "100", {"unit_of_measurement": "kWh"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_dashboard_returns_everything_the_panel_renders(hass) -> None:
    """One payload carries prices, grid, battery and devices."""
    entry = full_entry()
    with freeze_time(FROZEN):
        await _setup_entry(hass, entry)
        result = build_dashboard(hass, entry)

    assert result["entry"]["provider"] == "zonneplan"
    assert result["entry"]["interval"] == "quarterly"
    assert result["entry"]["currency"] == hass.config.currency

    prices = result["prices"]
    assert prices["current"] is not None
    assert prices["spot"] is not None
    assert prices["next"] is not None
    assert prices["min"] <= prices["max"]
    assert len(prices["points"]) > 80
    assert all("t" in point and "price" in point for point in prices["points"])

    grid = result["grid"]
    assert grid is not None
    assert grid["billing_year"] == 2026
    assert grid["fixed_monthly"] == 26.0
    assert grid["saldering_active"] is True
    assert isinstance(grid["total_cost"], float)

    battery = result["battery"]
    assert battery is not None
    assert battery["soc_pct"] == 25.0
    assert len(battery["programs"]) == 6
    assert battery["plan"] is not None
    assert battery["plan"]["reason"] in {"planned", "no_spread"}
    assert battery["applied"] is None

    assert len(result["devices"]) == 1
    device = result["devices"][0]
    assert device["name"] == "EV charger"
    assert device["kind"] == "ev_charger"
    assert "energy_kwh_today" in device

    summary = result["summary"]
    assert summary["grid_cost_today"] == pytest.approx(grid["total_cost_today"])
    assert summary["grid_cost_year"] == pytest.approx(grid["total_cost"])
    assert summary["ev_count"] == 1
    assert summary["control_enabled"] is False
    assert summary["battery_saving_planned"] is not None
    # The EV balance is reimbursement minus cost to date.
    assert summary["ev_balance"] == pytest.approx(
        summary["ev_reimbursed"] - device["cost"]
    )


async def test_dashboard_without_grid_or_battery(hass) -> None:
    """A tariff-only install still renders a usable payload."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="zonneplan_quarterly",
        title="Zonneplan",
    )
    with freeze_time(FROZEN):
        await _setup_entry(hass, entry)
        result = build_dashboard(hass, entry)

    assert result["grid"] is None
    assert result["battery"] is None
    assert result["devices"] == []
    assert result["prices"]["points"]


async def test_dashboard_command_is_registered(hass) -> None:
    """The command the panel calls is registered on the websocket API."""
    from homeassistant.components import websocket_api

    async_register_websocket_api(hass)

    commands = hass.data[websocket_api.DOMAIN]
    assert f"{DOMAIN}/dashboard" in commands


async def test_dashboard_reports_an_unknown_entry(hass) -> None:
    """Asking for an entry that is not loaded resolves to nothing."""
    from custom_components.kd_helios.websocket import _loaded_entry

    assert _loaded_entry(hass, "does-not-exist") is None


async def test_panel_is_wired_into_the_sidebar(hass, monkeypatch) -> None:
    """The bundle is served and the panel is registered with the right names."""
    calls: dict[str, object] = {}

    class FakeHttp:
        async def async_register_static_paths(self, configs) -> None:
            calls["static"] = configs

    register = AsyncMock(
        side_effect=lambda hass, **kwargs: calls.setdefault("panel", kwargs)
    )
    monkeypatch.setattr(hass, "http", FakeHttp(), raising=False)
    monkeypatch.setattr(panel.panel_custom, "async_register_panel", register)

    await panel.async_register_panel(hass)

    # The real function is a coroutine; forgetting to await it is what made the
    # panel silently never register once already.
    register.assert_awaited_once()

    static = calls["static"]
    assert static[0].url_path == f"/{DOMAIN}/{panel.PANEL_JS}"
    assert static[0].path.endswith(panel.PANEL_JS)

    registered = calls["panel"]
    assert registered["frontend_url_path"] == "helios"
    assert registered["webcomponent_name"] == "kd-helios-panel"
    assert registered["sidebar_title"] == "KD Helios"
    assert registered["sidebar_icon"].startswith("mdi:")
    # The version query is what stops a browser running a stale bundle.
    assert registered["module_url"].startswith(f"/{DOMAIN}/{panel.PANEL_JS}?v=")


async def test_panel_registration_is_idempotent(hass, monkeypatch) -> None:
    """Setting up twice does not register the sidebar entry twice."""
    calls: list[dict] = []

    class FakeHttp:
        async def async_register_static_paths(self, configs) -> None:
            return None

    monkeypatch.setattr(hass, "http", FakeHttp(), raising=False)
    monkeypatch.setattr(
        panel.panel_custom,
        "async_register_panel",
        AsyncMock(side_effect=lambda hass, **kwargs: calls.append(kwargs)),
    )

    await panel.async_register_panel(hass)
    # Simulate the frontend registry already holding the panel.
    hass.data.setdefault("frontend_panels", {})["helios"] = object()
    await panel.async_register_panel(hass)

    assert len(calls) == 1


def test_panel_bundle_defines_the_custom_element() -> None:
    """The served file exists and registers the element the panel points at."""
    bundle = Path(panel.ASSET_DIR / panel.PANEL_JS)
    assert bundle.is_file()
    source = bundle.read_text(encoding="utf-8")
    assert f'customElements.define("{panel.PANEL_COMPONENT}"' in source
    assert f"{DOMAIN}/dashboard" in source


def test_panel_bundle_asks_before_writing() -> None:
    """The apply button must not write to the inverter without a confirmation."""
    source = Path(panel.ASSET_DIR / panel.PANEL_JS).read_text(encoding="utf-8")
    assert "confirm: true" in source
    assert "window.confirm" in source


def test_panel_registration_is_awaited() -> None:
    """Guard against calling the coroutine registration without awaiting it.

    Home Assistant's ``panel_custom.async_register_panel`` is a coroutine. An
    un-awaited call raises no error at the call site: it logs a RuntimeWarning
    and the panel simply never appears, which is exactly how this broke once.
    """
    assert inspect.iscoroutinefunction(panel.panel_custom.async_register_panel)
    source = Path(panel.__file__).read_text(encoding="utf-8")
    assert "await panel_custom.async_register_panel(" in source
    assert "\n    panel_custom.async_register_panel(" not in source


async def test_history_command_is_registered(hass) -> None:
    """The panel's range selector depends on this command existing."""
    from homeassistant.components import websocket_api

    from custom_components.kd_helios.websocket import async_register_websocket_api

    async_register_websocket_api(hass)
    handlers = hass.data[websocket_api.DOMAIN]
    assert "kd_helios/history" in handlers
    assert "kd_helios/dashboard" in handlers


async def test_the_version_does_not_touch_the_disk(hass, monkeypatch) -> None:
    """Reading the manifest on the event loop is what HA warns about."""
    from custom_components.kd_helios import panel

    def forbid(*args, **kwargs):
        raise AssertionError("the panel must not read files on the event loop")

    monkeypatch.setattr(Path, "read_text", forbid)
    version = await panel._version(hass)
    assert isinstance(version, str) and version
