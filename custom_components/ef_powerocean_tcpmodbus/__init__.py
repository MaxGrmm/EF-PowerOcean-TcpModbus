"""EF-PowerOcean-TcpModbus – Local Modbus TCP integration for EcoFlow PowerOcean Plus."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_BATTERY_CAPACITY, CONF_PV_STRINGS, CONF_SCAN_INTERVAL, DEFAULT_BATTERY_CAPACITY, DEFAULT_PORT, DEFAULT_PV_STRINGS, DEFAULT_SCAN_INTERVAL, DOMAIN
from .coordinator import EcoflowCoordinator

PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EF-PowerOcean-TcpModbus from a config entry."""
    def _get(key, default):
        return entry.options.get(key, entry.data.get(key, default))

    coordinator = EcoflowCoordinator(
        hass,
        host=entry.data["host"],
        port=entry.data.get("port", DEFAULT_PORT),
        battery_capacity=_get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY),
        scan_interval=_get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        pv_strings=_get(CONF_PV_STRINGS, DEFAULT_PV_STRINGS),
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
