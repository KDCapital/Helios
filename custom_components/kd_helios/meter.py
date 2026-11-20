"""Recognise the metering entities of a grid connection device.

Picking a meter device in the config flow is friendlier than hunting for four
entity ids, so KD Helios recognises well known meters and prefills the entity
fields. The user always reviews and can override the result: guessing wrong
here would silently meter the wrong counter.

Detection is keyed on the entity registry's ``platform`` and ``translation_key``
rather than on entity ids or names, because those are stable across languages
and renames. A heuristic on device class and naming is the fallback for meters
that are not in the signature table.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_EXPORT_ENTITY,
    CONF_EXPORT_POWER_ENTITY,
    CONF_GRID_POWER_ENTITY,
    CONF_IMPORT_ENTITY,
    CONF_IMPORT_POWER_ENTITY,
)

_LOGGER = logging.getLogger(__name__)

_ENERGY = SensorDeviceClass.ENERGY.value
_POWER = SensorDeviceClass.POWER.value

#: A tariff register suffix such as ``t1``/``t2``; a combined counter is
#: preferred over the per-tariff registers when both are present.
_TARIFF_SUFFIX = re.compile(r"(?:^|[_\.])t[1-4](?:$|[_\.])")


@dataclass(frozen=True, slots=True)
class MeterSignature:
    """Translation keys that identify a known meter's counters."""

    import_energy: tuple[str, ...] = ()
    export_energy: tuple[str, ...] = ()
    import_power: tuple[str, ...] = ()
    export_power: tuple[str, ...] = ()
    #: Translation keys of a single signed net power sensor.
    signed_power: tuple[str, ...] = ()
    #: True when the net power sensor carries no translation key (HomeWizard).
    signed_power_without_key: bool = False


#: HomeWizard Wi-Fi P1 meter. Its combined counters are enabled by default, so
#: no per-tariff register has to be summed.
HOME_WIZARD = MeterSignature(
    import_energy=("total_energy_import_kwh",),
    export_energy=("total_energy_export_kwh",),
    # ``active_power_w`` is one signed sensor and has no translation key.
    signed_power_without_key=True,
)

SIGNATURES: dict[str, MeterSignature] = {"homewizard": HOME_WIZARD}


@dataclass(frozen=True, slots=True)
class MeterDetection:
    """Entities found for a meter device."""

    import_entity: str | None = None
    export_entity: str | None = None
    import_power_entity: str | None = None
    export_power_entity: str | None = None
    power_entity: str | None = None
    matched_by: str = "none"

    @property
    def found(self) -> tuple[str, ...]:
        """Return the entities that were found."""
        return tuple(
            entity_id
            for entity_id in (
                self.import_entity,
                self.export_entity,
                self.import_power_entity,
                self.export_power_entity,
                self.power_entity,
            )
            if entity_id
        )

    @property
    def is_empty(self) -> bool:
        """Return True when nothing was recognised."""
        return not self.found

    def as_suggestions(self) -> dict[str, str]:
        """Return the detected entities keyed by config field."""
        candidates = {
            CONF_IMPORT_ENTITY: self.import_entity,
            CONF_EXPORT_ENTITY: self.export_entity,
            CONF_IMPORT_POWER_ENTITY: self.import_power_entity,
            CONF_EXPORT_POWER_ENTITY: self.export_power_entity,
            CONF_GRID_POWER_ENTITY: self.power_entity,
        }
        return {key: value for key, value in candidates.items() if value}


@dataclass(frozen=True, slots=True)
class _Candidate:
    """An energy or power sensor of the selected device."""

    entity_id: str
    device_class: str | None
    translation_key: str | None
    platform: str | None
    label: str


def _candidates(hass: HomeAssistant, device_id: str) -> list[_Candidate]:
    """Return the usable energy and power sensors of a device."""
    registry = er.async_get(hass)
    result: list[_Candidate] = []
    for entry in er.async_entries_for_device(registry, device_id):
        if not entry.entity_id.startswith("sensor."):
            continue
        if entry.disabled_by is not None:
            continue
        if hass.states.get(entry.entity_id) is None:
            continue
        device_class = entry.device_class or entry.original_device_class
        if device_class not in (_ENERGY, _POWER):
            continue
        label = entry.original_name or entry.entity_id
        result.append(
            _Candidate(
                entity_id=entry.entity_id,
                device_class=device_class,
                translation_key=entry.translation_key,
                platform=entry.platform,
                label=label,
            )
        )
    return result


def _pick(candidates: list[_Candidate], keys: tuple[str, ...]) -> str | None:
    """Return the first entity matching one of the translation keys."""
    for key in keys:
        for candidate in candidates:
            if candidate.translation_key == key:
                return candidate.entity_id
    return None


def _signed_power_without_key(candidates: list[_Candidate]) -> str | None:
    """Return the only power sensor that has no translation key, if any.

    HomeWizard's ``active_power_w`` is such an entity: one signed value, no
    translation key, and the per-phase sensors do carry one.
    """
    unsigned = [
        candidate
        for candidate in candidates
        if candidate.device_class == _POWER and candidate.translation_key is None
    ]
    return unsigned[0].entity_id if len(unsigned) == 1 else None


def _from_signature(
    candidates: list[_Candidate], signature: MeterSignature
) -> MeterDetection:
    """Match a known meter by its translation keys."""
    power_entity = _pick(candidates, signature.signed_power)
    if power_entity is None and signature.signed_power_without_key:
        power_entity = _signed_power_without_key(candidates)
    return MeterDetection(
        import_entity=_pick(candidates, signature.import_energy),
        export_entity=_pick(candidates, signature.export_energy),
        import_power_entity=_pick(candidates, signature.import_power),
        export_power_entity=_pick(candidates, signature.export_power),
        power_entity=power_entity,
        matched_by="signature",
    )


def _rank(candidate: _Candidate) -> tuple[int, int]:
    """Rank candidates so combined counters win over per-tariff registers."""
    per_tariff = 1 if _TARIFF_SUFFIX.search(candidate.entity_id) else 0
    return (per_tariff, len(candidate.entity_id))


def _from_heuristic(candidates: list[_Candidate]) -> MeterDetection:
    """Infer the counters from device class and naming."""

    def find(device_class: str, *, direction: str) -> str | None:
        matches = [
            candidate
            for candidate in candidates
            if candidate.device_class == device_class
            and direction in f"{candidate.entity_id} {candidate.label}".lower()
            and (direction == "import" or "import" not in candidate.entity_id.lower())
        ]
        if not matches:
            return None
        return min(matches, key=_rank).entity_id

    signed = [
        candidate
        for candidate in candidates
        if candidate.device_class == _POWER
        and "power" in candidate.entity_id.lower()
        and "import" not in candidate.entity_id.lower()
        and "export" not in candidate.entity_id.lower()
        and not re.search(r"l[1-3](?:$|[_\\.])", candidate.entity_id)
    ]

    detection = MeterDetection(
        import_entity=find(_ENERGY, direction="import"),
        export_entity=find(_ENERGY, direction="export"),
        import_power_entity=find(_POWER, direction="import"),
        export_power_entity=find(_POWER, direction="export"),
        power_entity=signed[0].entity_id if len(signed) == 1 else None,
        matched_by="heuristic",
    )
    return detection if not detection.is_empty else MeterDetection()


def async_detect_meter_entities(
    hass: HomeAssistant, device_id: str | None
) -> MeterDetection:
    """Return the metering entities of a device, best effort."""
    if not device_id:
        return MeterDetection()

    candidates = _candidates(hass, device_id)
    if not candidates:
        return MeterDetection()

    for platform, signature in SIGNATURES.items():
        if not any(candidate.platform == platform for candidate in candidates):
            continue
        detection = _from_signature(candidates, signature)
        if not detection.is_empty:
            _LOGGER.debug(
                "Grid: recognised %s meter %s as %s",
                platform,
                device_id,
                detection.found,
            )
            return detection

    return _from_heuristic(candidates)
