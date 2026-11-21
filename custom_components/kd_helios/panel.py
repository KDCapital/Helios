"""Sidebar panel for KD Helios.

Registered the same way other integrations add a panel: a custom element served
as a static file, wired into the sidebar through ``panel_custom``. The element
gets everything it shows from the ``kd_helios/dashboard`` WebSocket command, so
there is no template or REST plumbing in between.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from homeassistant.components import panel_custom
from homeassistant.components.frontend import DATA_PANELS
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PANEL_URL = "helios"
PANEL_COMPONENT = "kd-helios-panel"
PANEL_JS = "kd-helios-panel.js"
PANEL_TITLE = "KD Helios"
PANEL_ICON = "mdi:home-lightning-bolt-outline"

ASSET_DIR = Path(__file__).parent / "frontend"


def _version() -> str:
    """Return the integration version, used to bypass the module cache."""
    path = Path(__file__).with_name("manifest.json")
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("version", "0"))
    except (OSError, ValueError):
        return "0"


async def async_register_panel(hass: HomeAssistant) -> None:
    """Serve the panel bundle and put it in the sidebar."""
    if PANEL_URL in hass.data.get(DATA_PANELS, {}):
        return
    if hass.http is None:
        _LOGGER.debug("HTTP is not set up, skipping the Helios panel")
        return

    # The query string is what makes the browser fetch a new bundle after an
    # update; without it an upgraded integration keeps running old JavaScript.
    module_url = f"/{DOMAIN}/{PANEL_JS}?v={_version()}"
    asset_path = f"/{DOMAIN}/{PANEL_JS}"
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(asset_path, str(ASSET_DIR / PANEL_JS), False)]
        )
    except RuntimeError:
        # The path is already registered from an earlier setup attempt.
        _LOGGER.debug("%s is already served", asset_path)

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name=PANEL_COMPONENT,
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        module_url=module_url,
        config={"domain": DOMAIN},
    )
