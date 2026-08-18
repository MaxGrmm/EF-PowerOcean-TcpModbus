"""EF-PowerOcean-TcpModbus – Local Modbus TCP integration for EcoFlow PowerOcean Plus."""

from __future__ import annotations

import logging
from typing import Final

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN
from .coordinator import EcoflowCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: Final = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
]
# CONFIG_VERSION = 2
WARNING_TRANSLATION_PREFIX: Final = f"component.{DOMAIN}.config.step.warning"


def _modbus_warning_notification_id(entry: ConfigEntry) -> str:
    """Return the stable Modbus warning notification ID."""
    return f"{DOMAIN}_{entry.entry_id}_modbus_warning"


async def _async_show_modbus_warning(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: EcoflowCoordinator
) -> None:
    """Create a persistent notification when telemetry appears disabled."""
    if not coordinator.is_modbus_disabled:
        persistent_notification.async_dismiss(
            hass,
            _modbus_warning_notification_id(entry),
        )
        return

    translations = await async_get_translations(
        hass,
        hass.config.language,
        "config",
        integrations={DOMAIN},
        config_flow=True,
    )
    persistent_notification.async_create(
        hass,
        translations[f"{WARNING_TRANSLATION_PREFIX}.description"],
        title=translations[f"{WARNING_TRANSLATION_PREFIX}.title"],
        notification_id=_modbus_warning_notification_id(entry),
    )


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

    await _async_show_modbus_warning(hass, entry, coordinator)
    entry.async_on_unload(
        coordinator.async_add_listener(
            lambda: hass.async_create_task(
                _async_show_modbus_warning(hass, entry, coordinator)
            )
        )
    )

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
