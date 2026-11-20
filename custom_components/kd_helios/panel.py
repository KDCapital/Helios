"""Sidebar panel for KD Helios.

Registered the same way other integrations add a panel: a custom element served
as a static file, wired into the sidebar through ``panel_custom``. The element
gets everything it shows from the ``kd_helios/dashboard`` WebSocket command, so
there is no template or REST plumbing in between.
"""

from __future__ import annotations

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
PANEL_TITLE = "Helios"
PANEL_ICON = "mdi:home-lightning-bolt-outline"

ASSET_DIR = Path(__file__).parent / "frontend"


async def async_register_panel(hass: HomeAssistant) -> None:
    """Serve the panel bundle and put it in the sidebar."""
    if PANEL_URL in hass.data.get(DATA_PANELS, {}):
        return
    if hass.http is None:
        _LOGGER.debug("HTTP is not set up, skipping the Helios panel")
        return

    module_url = f"/{DOMAIN}/{PANEL_JS}"
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(module_url, str(ASSET_DIR / PANEL_JS), False)]
        )
    except RuntimeError:
        # The path is already registered from an earlier setup attempt.
        _LOGGER.debug("%s is already served", module_url)

    panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name=PANEL_COMPONENT,
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        module_url=module_url,
        config={"domain": DOMAIN},
    )
