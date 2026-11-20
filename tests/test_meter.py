"""Tests for recognising the metering entities of a device."""

from __future__ import annotations

import pytest
from homeassistant.components.sensor import SensorDeviceClass

from custom_components.kd_helios.const import (
    CONF_EXPORT_ENTITY,
    CONF_GRID_POWER_ENTITY,
    CONF_IMPORT_ENTITY,
)
from custom_components.kd_helios.meter import async_detect_meter_entities
from tests.helpers import add_meter_device as add_device
from tests.helpers import add_meter_sensor as add_sensor


async def test_homewizard_p1_meter_is_recognised(hass) -> None:
    """The combined HomeWizard counters are found by translation key."""
    device = add_device(hass)
    add_sensor(
        hass,
        device,
        "p1_meter_energy_import",
        translation_key="total_energy_import_kwh",
    )
    add_sensor(
        hass,
        device,
        "p1_meter_energy_export",
        translation_key="total_energy_export_kwh",
    )
    # Per-tariff registers must not be picked over the combined counter.
    add_sensor(
        hass,
        device,
        "p1_meter_energy_import_t1",
        translation_key="total_energy_import_tariff_kwh",
    )

    detection = async_detect_meter_entities(hass, device.id)

    assert detection.matched_by == "signature"
    assert detection.import_entity == "sensor.p1_meter_energy_import"
    assert detection.export_entity == "sensor.p1_meter_energy_export"
    assert detection.as_suggestions() == {
        CONF_IMPORT_ENTITY: "sensor.p1_meter_energy_import",
        CONF_EXPORT_ENTITY: "sensor.p1_meter_energy_export",
    }


async def test_disabled_entities_are_ignored(hass) -> None:
    """A disabled counter cannot be metered, so it is not suggested."""
    device = add_device(hass)
    add_sensor(
        hass,
        device,
        "p1_meter_energy_import",
        translation_key="total_energy_import_kwh",
        disabled=True,
    )

    detection = async_detect_meter_entities(hass, device.id)

    assert detection.is_empty
    assert detection.as_suggestions() == {}


async def test_unknown_meter_falls_back_to_naming(hass) -> None:
    """An unknown integration is matched on class and naming."""
    device = add_device(hass, "Smart meter")
    add_sensor(
        hass,
        device,
        "electricity_import",
        platform="dsmr",
        device_class=SensorDeviceClass.ENERGY,
    )
    add_sensor(
        hass,
        device,
        "electricity_export",
        platform="dsmr",
        device_class=SensorDeviceClass.ENERGY,
    )

    detection = async_detect_meter_entities(hass, device.id)

    assert detection.matched_by == "heuristic"
    assert detection.import_entity == "sensor.electricity_import"
    assert detection.export_entity == "sensor.electricity_export"


async def test_device_without_meters_is_empty(hass) -> None:
    """A device with no energy sensors yields nothing to suggest."""
    device = add_device(hass, "Thermostat")
    add_sensor(
        hass,
        device,
        "temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        unit="°C",
    )

    assert async_detect_meter_entities(hass, device.id).is_empty


async def test_no_device_selected_is_empty(hass) -> None:
    """Skipping the device picker is allowed."""
    assert async_detect_meter_entities(hass, None).is_empty


async def test_power_sensors_are_detected_too(hass) -> None:
    """Live power sensors are suggested when the meter exposes them."""
    device = add_device(hass, "Smart meter")
    add_sensor(
        hass,
        device,
        "electricity_import",
        platform="dsmr",
        device_class=SensorDeviceClass.ENERGY,
    )
    add_sensor(
        hass,
        device,
        "power_import",
        platform="dsmr",
        device_class=SensorDeviceClass.POWER,
        unit="W",
    )
    add_sensor(
        hass,
        device,
        "power_export",
        platform="dsmr",
        device_class=SensorDeviceClass.POWER,
        unit="W",
    )

    detection = async_detect_meter_entities(hass, device.id)

    assert detection.import_power_entity == "sensor.power_import"
    assert detection.export_power_entity == "sensor.power_export"


@pytest.mark.parametrize("device_id", [None, "does-not-exist"])
async def test_missing_device_is_empty(hass, device_id) -> None:
    """An unknown device id does not raise."""
    assert async_detect_meter_entities(hass, device_id).is_empty


async def test_homewizard_signed_power_is_detected(hass) -> None:
    """The P1 meter's single signed power sensor is offered as net power."""
    device = add_device(hass)
    add_sensor(
        hass,
        device,
        "p1_meter_energy_import",
        translation_key="total_energy_import_kwh",
    )
    # active_power_w carries no translation key; the per-phase sensors do.
    add_sensor(
        hass,
        device,
        "p1_meter_active_power",
        device_class=SensorDeviceClass.POWER,
        unit="W",
    )
    add_sensor(
        hass,
        device,
        "p1_meter_active_power_l1",
        device_class=SensorDeviceClass.POWER,
        unit="W",
        translation_key="active_power_phase_w",
    )

    detection = async_detect_meter_entities(hass, device.id)

    assert detection.power_entity == "sensor.p1_meter_active_power"
    assert detection.as_suggestions()[CONF_GRID_POWER_ENTITY] == (
        "sensor.p1_meter_active_power"
    )


async def test_ambiguous_signed_power_is_not_guessed(hass) -> None:
    """Two unkeyed power sensors means no safe choice."""
    device = add_device(hass)
    add_sensor(
        hass,
        device,
        "meter_power_a",
        device_class=SensorDeviceClass.POWER,
        unit="W",
    )
    add_sensor(
        hass,
        device,
        "meter_power_b",
        device_class=SensorDeviceClass.POWER,
        unit="W",
    )

    assert async_detect_meter_entities(hass, device.id).power_entity is None
