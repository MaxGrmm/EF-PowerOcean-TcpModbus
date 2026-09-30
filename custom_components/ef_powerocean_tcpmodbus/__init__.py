"""EF-PowerOcean-TcpModbus – Local Modbus TCP integration for EcoFlow PowerOcean Plus."""

from __future__ import annotations

import logging
from typing import Final

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.translation import async_get_translations
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import EcoflowCoordinator
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA: Final = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: Final = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
]
WARNING_TRANSLATION_PREFIX: Final = f"component.{DOMAIN}.config.step.warning"


def _modbus_warning_notification_id(entry: ConfigEntry) -> str:
    """Return the stable Modbus warning notification ID."""
    return f"{DOMAIN}_{entry.entry_id}_modbus_warning"


def _command_expired_issue_id(entry: ConfigEntry) -> str:
    return f"command_expired_{entry.entry_id}"


@callback
def _sync_command_expired_issue(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: EcoflowCoordinator
) -> None:
    """Raise a repair while a timed-out command's Charge Limit still applies."""
    charge_limit_soc = coordinator.control.expired_charge_limit
    if charge_limit_soc is None:
        ir.async_delete_issue(hass, DOMAIN, _command_expired_issue_id(entry))
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        _command_expired_issue_id(entry),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="command_expired",
        translation_placeholders={
            "device": entry.title,
            "charge_limit_soc": f"{charge_limit_soc:g}",
        },
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions, which serve every configured inverter."""
    async_setup_services(hass)
    return True


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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EF-PowerOcean-TcpModbus from a config entry."""

    coordinator = EcoflowCoordinator(
        hass,
        config_entry=entry,
    )
    await coordinator.async_load_persisted_state()
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
    entry.async_on_unload(
        coordinator.async_add_listener(
            lambda: _sync_command_expired_issue(hass, entry, coordinator)
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
    # A reload starts without a command, so nothing it left behind is known any more.
    ir.async_delete_issue(hass, DOMAIN, _command_expired_issue_id(entry))

    return True
