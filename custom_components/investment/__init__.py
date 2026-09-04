"""HA Investment integration."""
from __future__ import annotations

import logging
from pathlib import Path
from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, PANEL_ASSET_REVISION, PANEL_ICON, PANEL_NAME, PANEL_URL, STATIC_URL, VERSION, sidebar_title
from .manager import InvestmentManager
from .websocket import async_register_commands

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up command registrations."""
    async_register_commands(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HA Investment."""
    manager = InvestmentManager(hass)
    await manager.async_initialize()
    hass.data[DOMAIN] = {"entry_id": entry.entry_id, "manager": manager}
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])

    www_path = Path(__file__).parent / "www"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(www_path), False)]
    )

    if frontend.async_panel_exists(hass, PANEL_URL):
        frontend.async_remove_panel(hass, PANEL_URL)

    await panel_custom.async_register_panel(
        hass=hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name=PANEL_NAME,
        module_url=f"{STATIC_URL}/investment-panel.js?v={PANEL_ASSET_REVISION}",
        sidebar_title=sidebar_title(hass.config.language),
        sidebar_icon=PANEL_ICON,
        require_admin=False,
        config={},
        config_panel_domain=DOMAIN,
    )
    _LOGGER.info("HA Investment %s initialized", VERSION)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload HA Investment."""
    if not await hass.config_entries.async_unload_platforms(entry, ["sensor"]):
        return False
    if frontend.async_panel_exists(hass, PANEL_URL):
        frontend.async_remove_panel(hass, PANEL_URL)
    hass.data.pop(DOMAIN, None)
    return True
