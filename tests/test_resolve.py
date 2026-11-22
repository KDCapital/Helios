"""Tests for deriving entity ids from the device the user picked.

The explicit value wins; the registry is only a fallback, and a device the user
already linked is enough to find its sensors.
"""


def test_the_inverter_supplies_soc_and_house_load(hass) -> None:
    """Picking the inverter is enough; the sensors are derived from it."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.kd_helios.models import BatteryConfig
    from custom_components.kd_helios.resolve import resolve_battery

    owner = MockConfigEntry(domain="solarman", data={}, entry_id="solarman-entry")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("solarman", "77")},
        name="Deye",
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
    registry.async_get_or_create(
        "sensor",
        "solarman",
        "solarman-load",
        suggested_object_id="deye_load_power",
        original_name="Load Power",
        device_id=device.id,
    )

    resolved = resolve_battery(
        hass, BatteryConfig(subentry_id="b", inverter_device_id=device.id)
    )
    assert resolved.soc_entity == "sensor.deye_battery_soc"
    assert resolved.house_load_entity == "sensor.deye_load_power"


def test_an_explicit_sensor_beats_the_derived_one(hass) -> None:
    """A value the user set is never overwritten by a guess."""
    from custom_components.kd_helios.models import BatteryConfig
    from custom_components.kd_helios.resolve import resolve_battery

    config = BatteryConfig(
        subentry_id="b", inverter_device_id="some-device", soc_entity="sensor.mine"
    )
    assert resolve_battery(hass, config).soc_entity == "sensor.mine"


def test_the_car_is_filled_in_from_evcc(hass) -> None:
    """Picking the EVCC device supplies the car sensors."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.kd_helios.models import DeviceConfig
    from custom_components.kd_helios.resolve import resolve_ev

    owner = MockConfigEntry(domain="evcc", data={}, entry_id="evcc-entry")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("evcc", "lp1")},
        name="Laadpaal",
    )
    registry = er.async_get(hass)
    for unique, object_id, name in (
        ("soc", "evcc_car_soc", "Vehicle SOC"),
        ("power", "evcc_charge_power", "Charge power"),
        ("dep", "evcc_plan_end", "Plan projected end"),
    ):
        registry.async_get_or_create(
            "sensor",
            "evcc",
            unique,
            suggested_object_id=object_id,
            original_name=name,
            device_id=device.id,
        )

    resolved = resolve_ev(
        hass, DeviceConfig(subentry_id="ev", name="Car", ev_device_id=device.id)
    )
    assert resolved.ev_soc_entity == "sensor.evcc_car_soc"
    assert resolved.ev_power_entity == "sensor.evcc_charge_power"
    assert resolved.ev_departure_entity == "sensor.evcc_plan_end"


def test_joulo_supplies_the_ere_sensors(hass) -> None:
    """ERE mode joulo finds its counters without asking for ids."""
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.kd_helios.const import EreMode
    from custom_components.kd_helios.models import DeviceConfig, EreConfig
    from custom_components.kd_helios.resolve import resolve_joulo

    owner = MockConfigEntry(domain="joulo", data={}, entry_id="joulo-entry")
    owner.add_to_hass(hass)
    registry = er.async_get(hass)
    for unique, object_id, name in (
        ("credits", "joulo_total_ere_credits", "Total ERE credits"),
        ("kwh", "joulo_total_all_kwh", "Total all kWh"),
    ):
        registry.async_get_or_create(
            "sensor",
            "joulo",
            unique,
            suggested_object_id=object_id,
            original_name=name,
            config_entry=owner,
        )

    config = DeviceConfig(
        subentry_id="ev", name="Car", ere=EreConfig(mode=EreMode.JOULO)
    )
    resolved = resolve_joulo(hass, config)
    assert resolved.ere.credits_entity == "sensor.joulo_total_ere_credits"
    assert resolved.ere.kwh_entity == "sensor.joulo_total_all_kwh"


def test_the_grid_is_filled_in_from_the_meter(hass) -> None:
    """The derived field names must be the ones GridConfig actually has."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.kd_helios.models import GridConfig
    from custom_components.kd_helios.resolve import resolve_grid

    owner = MockConfigEntry(domain="homewizard", data={}, entry_id="hw-entry")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("homewizard", "p1")},
        name="P1 meter",
    )
    registry = er.async_get(hass)
    for unique, object_id, name in (
        ("import", "p1_total_energy_import_kwh", "Total energy import"),
        ("export", "p1_total_energy_export_kwh", "Total energy export"),
        ("power", "p1_active_power", "Active power"),
    ):
        registry.async_get_or_create(
            "sensor",
            "homewizard",
            unique,
            suggested_object_id=object_id,
            original_name=name,
            device_id=device.id,
        )

    resolved = resolve_grid(
        hass, GridConfig(subentry_id="g", meter_device_id=device.id)
    )
    assert resolved.import_entity == "sensor.p1_total_energy_import_kwh"
    assert resolved.export_entity == "sensor.p1_total_energy_export_kwh"
    assert resolved.import_power_entity == "sensor.p1_active_power"
    # A signed power sensor serves both directions.
    assert resolved.export_power_entity == resolved.import_power_entity
