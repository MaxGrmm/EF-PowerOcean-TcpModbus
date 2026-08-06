"""EF-PowerOcean-TcpModbus – Local Modbus TCP integration for EcoFlow PowerOcean Plus."""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import EcoflowCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
]
# CONFIG_VERSION = 2


# async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
#     """Migrate old config entries to current schema."""

#     if config_entry.version < CONFIG_VERSION:
#         _LOGGER.info(
#             f"Migrating config entry {config_entry.entry_id} from version {CONFIG_VERSION} to {config_entry.version}."
#         )
#         new_data = {**config_entry.data}
#         hass.config_entries.async_update_entry(
#             config_entry,
#             data=new_data,
#             version=CONFIG_VERSION,
#         )
#         _LOGGER.info(
#             f"Migration of config entry {config_entry.entry_id} to version {CONFIG_VERSION} successful!"
#         )

#     return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EF-PowerOcean-TcpModbus from a config entry."""

    coordinator = EcoflowCoordinator(
        hass,
        config_entry=entry,
    )
    await coordinator.async_connect_client()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload integration when config entry data changes
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when the config entry is updated."""
    _LOGGER.debug("Config entry updated — reloading EF-PowerOcean-TcpModbus")
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    # close connection and shutdown
    coordinator: EcoflowCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
    await coordinator.async_client_shutdown()

    return True
