"""Tests for configuring KD Helios from the panel instead of the flows."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.kd_helios.const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_HORIZON_HOURS,
    CONF_EFFICIENCY,
    CONF_ENERGY_ENTITY,
    CONF_MAX_CHARGE_KW,
    CONF_NAME,
    CONF_PRICE_INTERVAL,
    CONF_PROVIDER,
    CONF_RESERVE_SOC,
    CONF_SOC_ENTITY,
    CONF_TARGET_SOC,
    DOMAIN,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_DEVICE,
    SUBENTRY_TYPE_EV_CHARGER,
)
from custom_components.kd_helios.management import (
    ConfigError,
    async_advance_setup,
    create_subentry,
    delete_subentry,
    detect,
    detect_entities,
    list_sections,
    setup_state,
    update_contract,
    update_subentry,
)
from tests.test_panel import FROZEN, _setup_entry

USER_INPUT = {CONF_PROVIDER: "zonneplan", CONF_PRICE_INTERVAL: "quarterly"}


async def _entry(hass: HomeAssistant, tmp_path=None):
    """Return a loaded entry without subentries."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="zonneplan_quarterly",
        title="Zonneplan",
    )
    with freeze_time(FROZEN):
        await _setup_entry(hass, entry)
    return entry


def test_a_device_needs_a_name(hass) -> None:
    """The panel validates in one place, and a blank name is refused."""
    entry = _entry_sync(hass)
    with pytest.raises(ConfigError) as err:
        create_subentry(hass, entry, SUBENTRY_TYPE_DEVICE, {CONF_NAME: "  "})
    assert err.value.code == "invalid"


def _entry_sync(hass):
    """Build an unsaved entry, which is enough for validation tests."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT, title="Zonneplan")
    entry.add_to_hass(hass)
    return entry


def test_a_device_is_stored_as_a_plain_device(hass) -> None:
    """The panel's device form cannot accidentally create an EV charger."""
    entry = _entry_sync(hass)
    subentry = create_subentry(
        hass,
        entry,
        SUBENTRY_TYPE_DEVICE,
        {CONF_NAME: "Heat pump", CONF_ENERGY_ENTITY: "sensor.hp"},
    )
    assert subentry.subentry_type == SUBENTRY_TYPE_DEVICE
    assert subentry.data["kind"] == "device"
    assert subentry.title == "Heat pump"


def test_an_ev_charger_is_stored_as_one(hass) -> None:
    """The kind follows the section it was created from."""
    entry = _entry_sync(hass)
    subentry = create_subentry(
        hass, entry, SUBENTRY_TYPE_EV_CHARGER, {CONF_NAME: "Car"}
    )
    assert subentry.data["kind"] == "ev_charger"


def test_only_one_battery_is_allowed(hass) -> None:
    """Two batteries would double-count the plan."""
    entry = _entry_sync(hass)
    battery = {
        CONF_SOC_ENTITY: "sensor.soc",
        CONF_BATTERY_CAPACITY_KWH: 10.0,
        CONF_MAX_CHARGE_KW: 3.0,
        CONF_RESERVE_SOC: 10.0,
        CONF_TARGET_SOC: 100.0,
        CONF_EFFICIENCY: 90.0,
        CONF_BATTERY_HORIZON_HOURS: 24,
    }
    create_subentry(hass, entry, SUBENTRY_TYPE_BATTERY, battery)
    with pytest.raises(ConfigError):
        create_subentry(hass, entry, SUBENTRY_TYPE_BATTERY, battery)


def test_updating_keeps_the_keys_the_form_did_not_send(hass) -> None:
    """A partial edit must not wipe settings the form does not show."""
    entry = _entry_sync(hass)
    subentry = create_subentry(
        hass,
        entry,
        SUBENTRY_TYPE_DEVICE,
        {CONF_NAME: "Heat pump", CONF_ENERGY_ENTITY: "sensor.hp"},
    )
    update_subentry(hass, entry, subentry.subentry_id, {CONF_NAME: "Heat pump 2"})

    stored = entry.subentries[subentry.subentry_id]
    assert stored.title == "Heat pump 2"
    assert stored.data[CONF_ENERGY_ENTITY] == "sensor.hp"


def test_deleting_removes_it_from_the_sections(hass) -> None:
    """The list the panel renders follows the storage."""
    entry = _entry_sync(hass)
    subentry = create_subentry(
        hass, entry, SUBENTRY_TYPE_DEVICE, {CONF_NAME: "Heat pump"}
    )
    assert list_sections(hass, entry)[0]["items"]

    delete_subentry(hass, entry, subentry.subentry_id)
    assert list_sections(hass, entry)[0]["items"] == []


def test_deleting_something_that_is_gone_is_reported(hass) -> None:
    """A stale panel must get a clear error, not a crash."""
    entry = _entry_sync(hass)
    with pytest.raises(ConfigError) as err:
        delete_subentry(hass, entry, "01NOTAREALSUBENTRY")
    assert err.value.code == "not_found"


async def test_the_wizard_remembers_where_it_stopped(hass) -> None:
    """Closing the panel must not lose the wizard's position."""
    entry = await _entry(hass)
    assert setup_state(entry)["step"] is None

    await async_advance_setup(hass, entry, step="battery")
    assert setup_state(entry)["step"] == "battery"

    await async_advance_setup(hass, entry, skipped="car")
    assert setup_state(entry)["skipped"] == ["car"]

    await async_advance_setup(hass, entry, step="control", completed=True)
    assert setup_state(entry)["completed"] is True


async def test_an_unknown_step_is_refused(hass) -> None:
    """The wizard is a fixed sequence, not free text."""
    entry = await _entry(hass)
    with pytest.raises(ConfigError):
        await async_advance_setup(hass, entry, step="nonsense")


def test_detection_reports_what_is_installed(hass) -> None:
    """The wizard offers what exists instead of asking for it."""
    result = detect(hass)
    assert set(result) >= {"evcc", "solarman", "homewizard", "recorder"}
    assert result["evcc"] in (True, False)


def test_a_non_admin_cannot_change_configuration(hass) -> None:
    """The panel is admin-only, and the commands check again."""
    from custom_components.kd_helios.websocket import _admin_error

    sent = []
    connection = SimpleNamespace(
        user=SimpleNamespace(is_admin=False),
        send_error=lambda *args: sent.append(args),
    )
    assert _admin_error(connection, {"id": 7}) is True
    assert sent and sent[0][1] == "unauthorized"

    admin = SimpleNamespace(
        user=SimpleNamespace(is_admin=True), send_error=lambda *a: None
    )
    assert _admin_error(admin, {"id": 7}) is False


async def test_the_contract_can_be_changed_from_the_panel(hass) -> None:
    """Provider and interval are entry data, not a subentry."""
    entry = await _entry(hass)
    result = update_contract(hass, entry, "vandebron", "hourly")

    assert result == {"provider": "vandebron", "interval": "hourly"}
    assert entry.data[CONF_PROVIDER] == "vandebron"
    assert entry.data[CONF_PRICE_INTERVAL] == "hourly"


async def test_an_unknown_provider_is_refused(hass) -> None:
    """The catalogue is the authority, not the panel."""
    entry = await _entry(hass)
    with pytest.raises(ConfigError):
        update_contract(hass, entry, "not-a-provider", "hourly")
    with pytest.raises(ConfigError):
        update_contract(hass, entry, "zonneplan", "yearly")


def test_detection_offers_what_it_recognises(hass) -> None:
    """Sensors are offered by name and never forced on the user."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        "evcc",
        "evcc-car-soc",
        suggested_object_id="evcc_car_soc",
        original_name="Car SOC",
    )

    suggestions = detect_entities(hass)
    assert "ev_charger" in suggestions
    assert suggestions["ev_charger"]["ev_soc_entity"].startswith("sensor.")


def test_detection_offers_nothing_when_it_recognises_nothing(hass) -> None:
    """No guesses: an empty result is a valid answer."""
    assert detect_entities(hass) == {}


def test_detection_offers_the_solarman_device_for_the_inverter(hass) -> None:
    """The inverter field is a device, so an entity id would be wrong."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    owner = MockConfigEntry(domain="solarman", data={}, entry_id="solarman-entry")
    owner.add_to_hass(hass)

    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("solarman", "12345")},
        name="Deye SUN-12K",
    )
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        "solarman",
        "solarman-soc",
        suggested_object_id="deye_battery_soc",
        original_name="Battery SOC",
        device_id=device.id,
    )

    suggestions = detect_entities(hass)["battery"]
    assert suggestions["inverter_device"] == device.id
    assert suggestions["soc_entity"].startswith("sensor.")
