"""Render the panel's JavaScript against a real payload.

The panel is the one part of the integration the Python tests cannot execute, so
this runs it in Node with a minimal DOM stub. It proves the render path does not
throw and that the sections built from real data are present.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from freezegun import freeze_time

from custom_components.kd_helios import panel
from custom_components.kd_helios.websocket import build_dashboard
from tests.test_panel import FROZEN, _setup_entry, full_entry

HARNESS = Path(__file__).parent / "panel_harness.js"
BUNDLE = Path(panel.ASSET_DIR / panel.PANEL_JS)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
async def test_panel_renders_a_real_payload(hass, tmp_path) -> None:
    """The real dashboard payload renders without errors."""
    entry = full_entry()
    with freeze_time(FROZEN):
        await _setup_entry(hass, entry)
        payload = build_dashboard(hass, entry)

    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    result = await asyncio.to_thread(
        subprocess.run,
        ["node", str(HARNESS), str(BUNDLE), str(payload_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    html = result.stdout

    for needle in (
        "Energy overview",
        "Net electricity cost",
        "Battery plan saving",
        "EV charging balance",
        "Net grid balance",
        "Price &amp; planned charging",
        "EV charging &amp; reimbursement",
        "Device cost breakdown",
        "Battery &amp; plan",
        "Tariff",
        "Grid connection",
    ):
        assert needle in html, needle

    # Real values, not placeholders: today's grid cost and the battery SOC.
    assert "€" in html
    assert "25%" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
async def test_panel_renders_without_grid_or_battery(hass, tmp_path) -> None:
    """A tariff-only install renders its empty states instead of crashing."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.kd_helios.const import (
        CONF_PRICE_INTERVAL,
        CONF_PROVIDER,
        DOMAIN,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PROVIDER: "zonneplan", CONF_PRICE_INTERVAL: "quarterly"},
        unique_id="zonneplan_quarterly",
        title="Zonneplan",
    )
    with freeze_time(FROZEN):
        await _setup_entry(hass, entry)
        payload = build_dashboard(hass, entry)

    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    result = await asyncio.to_thread(
        subprocess.run,
        ["node", str(HARNESS), str(BUNDLE), str(payload_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    html = result.stdout
    assert "No grid connection" in html
    assert "No battery configured" in html
    assert "No EV charger configured" in html
