"""Packaging checks: manifest, HACS metadata and translation consistency.

These guard the things Home Assistant, HACS and hassfest silently trip over,
and stop a placeholder URL or a version drift from reaching a release.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from custom_components.kd_helios.const import DOMAIN, VERSION

ROOT = Path(__file__).parent.parent
COMPONENT = ROOT / "custom_components" / DOMAIN
REPOSITORY = "https://github.com/KDCapital/Helios"


@pytest.fixture(name="manifest")
def manifest_fixture() -> dict:
    """Return the integration manifest."""
    return json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))


def test_domain_matches_the_folder(manifest: dict) -> None:
    """The domain must match the directory the integration lives in."""
    assert manifest["domain"] == DOMAIN
    assert COMPONENT.name == DOMAIN


def test_required_manifest_fields(manifest: dict) -> None:
    """Every field Home Assistant and HACS require is present."""
    for field in (
        "domain",
        "name",
        "version",
        "documentation",
        "issue_tracker",
        "codeowners",
        "config_flow",
        "integration_type",
        "iot_class",
    ):
        assert field in manifest, field

    assert manifest["config_flow"] is True
    assert manifest["integration_type"] == "hub"
    assert manifest["iot_class"] == "cloud_polling"
    assert isinstance(manifest["requirements"], list)


def test_version_is_semver_and_matches_const(manifest: dict) -> None:
    """The manifest version is a valid version and not out of sync."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert manifest["version"] == VERSION


def test_links_point_at_the_project_repository(manifest: dict) -> None:
    """Documentation and the issue tracker point at the real repository."""
    assert manifest["documentation"] == REPOSITORY
    assert manifest["issue_tracker"] == f"{REPOSITORY}/issues"
    assert "example" not in manifest["documentation"]
    assert "placeholder" not in manifest["documentation"].lower()


def test_codeowners_are_real_handles(manifest: dict) -> None:
    """Codeowners are GitHub handles, not a placeholder."""
    assert manifest["codeowners"] == ["@KDCapital"]


def test_hacs_metadata() -> None:
    """HACS needs a name and a minimum Home Assistant version."""
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert hacs["name"] == "KD Helios"
    assert re.fullmatch(r"\d{4}\.\d{1,2}\.\d+", hacs["homeassistant"])


def test_english_translation_matches_strings() -> None:
    """translations/en.json must stay identical to the source strings."""
    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    english = json.loads(
        (COMPONENT / "translations" / "en.json").read_text(encoding="utf-8")
    )
    assert english == strings


def test_translations_cover_the_same_keys() -> None:
    """The Dutch translation offers the same structure as the English one."""
    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    dutch = json.loads(
        (COMPONENT / "translations" / "nl.json").read_text(encoding="utf-8")
    )

    def keys(node: object, prefix: str = "") -> set[str]:
        if not isinstance(node, dict):
            return {prefix}
        found: set[str] = set()
        for key, value in node.items():
            found |= keys(value, f"{prefix}.{key}" if prefix else key)
        return found

    missing = keys(strings) - keys(dutch)
    assert not missing, f"Dutch translation is missing: {sorted(missing)}"


def test_every_subentry_type_has_translations() -> None:
    """Each supported subentry type is described in the UI strings."""
    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    subentries = strings.get("config_subentries", {})
    assert "device" in subentries
    device = subentries["device"]
    assert device["initiate_flow"]["user"]
    assert device["entry_type"]
    for step in ("user", "reimbursement", "ere", "reconfigure"):
        assert step in device["step"], step


def test_every_sensor_has_a_translated_name() -> None:
    """No entity is left showing a raw translation key."""
    from custom_components.kd_helios.sensor import DEVICE_SENSORS, HUB_SENSORS

    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    names = strings["entity"]["sensor"]
    for description in (*HUB_SENSORS, *DEVICE_SENSORS):
        key = description.translation_key or description.key
        assert key in names, key
        assert names[key]["name"]
