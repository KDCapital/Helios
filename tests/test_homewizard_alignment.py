"""Alignment checks against the real HomeWizard integration.

These read the metadata the HomeWizard integration actually publishes, so the
signature table in ``kd_helios.meter`` cannot silently drift away from it:
if Home Assistant renames a counter, or flips the combined counter to disabled,
these tests say so instead of the integration quietly metering the wrong sensor.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import homeassistant
import pytest

from custom_components.kd_helios.const import CONF_GRID_POWER_ENTITY
from custom_components.kd_helios.grid import GridTracker
from custom_components.kd_helios.meter import HOME_WIZARD, async_detect_meter_entities
from custom_components.kd_helios.models import GridConfig
from tests.helpers import (
    FROZEN_TIME,
    add_meter_device,
    add_meter_sensor,
    flat_series,
)

COMPONENT = Path(homeassistant.__file__).parent / "components" / "homewizard"
SENSOR_SOURCE = COMPONENT / "sensor.py"

UNIT_MAP = {"KILO_WATT_HOUR": "kWh", "WATT": "W"}


@dataclass(frozen=True, slots=True)
class Described:
    """One sensor description as published by HomeWizard."""

    key: str
    translation_key: str | None
    device_class: str | None
    unit: str | None
    state_class: str | None
    enabled: bool


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    """Return a keyword argument of a call."""
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _name_of(node: ast.expr | None) -> str | None:
    """Return the trailing name of an attribute or a string constant."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


@pytest.fixture(name="descriptions", scope="module")
def descriptions_fixture() -> list[Described]:
    """Parse the sensor descriptions HomeWizard publishes."""
    if not SENSOR_SOURCE.exists():
        pytest.skip("homewizard integration not installed")
    tree = ast.parse(SENSOR_SOURCE.read_text(encoding="utf-8"))
    found: list[Described] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = (
            getattr(node.func, "id", None) or getattr(node.func, "attr", None) or ""
        )
        if "SensorEntityDescription" not in target:
            continue
        key = _keyword(node, "key")
        if key is None:
            continue
        enabled = _keyword(node, "entity_registry_enabled_default")
        found.append(
            Described(
                key=key.value,
                translation_key=_name_of(_keyword(node, "translation_key")),
                device_class=_name_of(_keyword(node, "device_class")),
                unit=_name_of(_keyword(node, "native_unit_of_measurement")),
                state_class=_name_of(_keyword(node, "state_class")),
                enabled=not (
                    isinstance(enabled, ast.Constant) and enabled.value is False
                ),
            )
        )
    assert found, "no sensor descriptions parsed"
    return found


def test_signature_keys_exist_in_homewizard(descriptions: list[Described]) -> None:
    """Every translation key in our signature is published by HomeWizard."""
    published = {item.translation_key for item in descriptions if item.translation_key}
    for key in HOME_WIZARD.import_energy + HOME_WIZARD.export_energy:
        assert key in published, f"{key} is no longer published by HomeWizard"


def test_signature_counters_are_cumulative_kwh(
    descriptions: list[Described],
) -> None:
    """The counters we meter must be cumulative kWh and enabled by default."""
    wanted = set(HOME_WIZARD.import_energy + HOME_WIZARD.export_energy)
    matched = [item for item in descriptions if item.translation_key in wanted]
    assert {item.translation_key for item in matched} == wanted

    for item in matched:
        assert item.device_class == "ENERGY", item.key
        assert item.unit == "KILO_WATT_HOUR", item.key
        assert item.state_class == "TOTAL_INCREASING", item.key
        assert item.enabled, f"{item.key} is disabled by default"


def test_exactly_one_unkeyed_power_sensor(descriptions: list[Described]) -> None:
    """Our signed-power rule assumes a single unkeyed power sensor.

    HomeWizard's ``active_power_w`` is one signed value and is the only power
    sensor without a translation key; the per-phase sensors carry one, which is
    what keeps them from being mistaken for it.
    """
    unkeyed = [
        item
        for item in descriptions
        if item.device_class == "POWER" and item.translation_key is None
    ]
    assert len(unkeyed) == 1, [item.key for item in unkeyed]
    assert unkeyed[0].unit == "WATT"


def _register(hass, device, descriptions: list[Described], *, skip: set[str]):
    """Register the meter entities as HomeWizard would."""
    entities: dict[str, str] = {}
    for item in descriptions:
        if item.device_class not in ("ENERGY", "POWER") or not item.enabled:
            continue
        if item.key in skip:
            continue
        entry = add_meter_sensor(
            hass,
            device,
            item.key,
            translation_key=item.translation_key,
            device_class=item.device_class.lower(),
            unit=UNIT_MAP.get(item.unit or "kWh", "kWh"),
        )
        entities[item.key] = entry.entity_id
    return entities


async def test_detection_matches_the_published_metadata(hass, descriptions) -> None:
    """Against the real descriptions, the right three entities are chosen."""
    device = add_meter_device(hass)
    entities = _register(hass, device, descriptions, skip=set())

    detection = async_detect_meter_entities(hass, device.id)

    assert detection.matched_by == "signature"
    assert detection.import_entity == entities["total_power_import_kwh"]
    assert detection.export_entity == entities["total_power_export_kwh"]
    assert detection.power_entity == entities["active_power_w"]
    # Never a per-tariff register or a per-phase power sensor.
    assert detection.import_entity != entities["total_power_import_t1_kwh"]
    assert detection.power_entity != entities["active_power_l1_w"]


async def test_disabled_combined_counter_is_not_replaced_by_a_register(
    hass, descriptions
) -> None:
    """Without the combined counter we suggest nothing rather than half.

    T1/T2 each measure only part of the flow, so falling back to one of them
    would silently under-meter the connection. The user is left to choose.
    """
    device = add_meter_device(hass)
    _register(
        hass,
        device,
        descriptions,
        skip={"total_power_import_kwh", "total_power_export_kwh"},
    )

    detection = async_detect_meter_entities(hass, device.id)

    # No import or export counter is suggested: half a meter is worse than none.
    assert detection.import_entity is None
    assert detection.export_entity is None
    # The signed power sensor is independent and unambiguous, so it is kept.
    assert set(detection.as_suggestions()) == {CONF_GRID_POWER_ENTITY}


async def test_split_registers_are_not_guessed_for_unknown_meters(hass) -> None:
    """The same guard applies to a meter we do not recognise."""
    device = add_meter_device(hass, "DSMR reader")
    add_meter_sensor(
        hass, device, "electricity_delivered_1", platform="dsmr", device_class="energy"
    )
    add_meter_sensor(
        hass, device, "electricity_delivered_2", platform="dsmr", device_class="energy"
    )

    assert async_detect_meter_entities(hass, device.id).import_entity is None


async def test_a_homewizard_connection_is_metered_end_to_end(
    hass, descriptions
) -> None:
    """Detected HomeWizard entities feed the grid ledger correctly.

    This is the whole chain: real metadata, detection, the tracker reading the
    published units (kWh counters and a signed watt sensor), and the money.
    """
    device = add_meter_device(hass)
    entities = _register(hass, device, descriptions, skip=set())
    detection = async_detect_meter_entities(hass, device.id)

    tracker = GridTracker(
        hass,
        GridConfig(
            subentry_id="grid-1",
            import_entity=detection.import_entity,
            export_entity=detection.export_entity,
            grid_power_entity=detection.power_entity,
            saldering=False,
            export_deduction=0.02,
        ),
    )
    series = flat_series(FROZEN_TIME, all_in=0.30, spot=0.20)
    start = FROZEN_TIME
    tick = timedelta(minutes=1)

    hass.states.async_set(
        entities["total_power_import_kwh"], "1000.0", {"unit_of_measurement": "kWh"}
    )
    hass.states.async_set(
        entities["total_power_export_kwh"], "500.0", {"unit_of_measurement": "kWh"}
    )
    hass.states.async_set(
        entities["active_power_w"], "-1200", {"unit_of_measurement": "W"}
    )
    tracker.update(start, series)

    hass.states.async_set(
        entities["total_power_import_kwh"], "1000.2", {"unit_of_measurement": "kWh"}
    )
    hass.states.async_set(
        entities["total_power_export_kwh"], "500.1", {"unit_of_measurement": "kWh"}
    )
    tracker.update(start + tick, series)

    snapshot = tracker.snapshot(start + tick, series)

    assert snapshot.imported_kwh == pytest.approx(0.2)
    assert snapshot.exported_kwh == pytest.approx(0.1)
    # 0.2 kWh in at 0.30, 0.1 kWh out at spot 0.20 minus the 0.02 deduction.
    assert snapshot.total_cost == pytest.approx(0.2 * 0.30 - 0.1 * 0.18)
    # One signed watt reading becomes an export of 1200 W.
    assert snapshot.import_power == pytest.approx(0.0)
    assert snapshot.export_power == pytest.approx(1200.0)
    assert snapshot.net_power == pytest.approx(-1200.0)
