"""EF-PowerOcean-TcpModbus – Local Modbus TCP integration for EcoFlow PowerOcean Plus."""

from __future__ import annotations

import logging
from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .control import ControlInputs
from .coordinator import EcoflowCoordinator
from .modbus import async_prepare
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

type EcoflowConfigEntry = ConfigEntry[EcoflowCoordinator]


def _modbus_disabled_issue_id(entry: ConfigEntry) -> str:
    """Return the stable id of the repairs issue raised while Modbus is disabled."""
    return f"modbus_disabled_{entry.entry_id}"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions, which serve every configured inverter."""
    async_setup_services(hass)
    return True


@callback
def _async_update_modbus_disabled_issue(
    hass: HomeAssistant, entry: EcoflowConfigEntry, coordinator: EcoflowCoordinator
) -> None:
    """Raise a repairs issue while telemetry appears disabled, and clear it after."""
    issue_id = _modbus_disabled_issue_id(entry)
    if not coordinator.is_modbus_disabled:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return

    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="modbus_disabled",
        translation_placeholders={"host": coordinator.host},
        learn_more_url="https://github.com/MaxGrmm/EF-PowerOcean-TcpModbus#prerequisites",
    )


@callback
def _async_claim_serial_number(
    hass: HomeAssistant, entry: EcoflowConfigEntry, coordinator: EcoflowCoordinator
) -> None:
    """Make the serial number the entry's unique id once the device has reported it.

    Entries made before the serial was read are keyed by host and port, which
    changes with the network while the inverter does not. Entities and the device
    are keyed by the entry id, so the entry's unique id can change under them.
    """
    serial = coordinator.identity.serial_number
    if not serial or serial == "unknown" or entry.unique_id == serial:
        return
    if any(
        other.unique_id == serial and other.entry_id != entry.entry_id
        for other in hass.config_entries.async_entries(DOMAIN)
    ):
        _LOGGER.warning(
            "Serial number %s is already the unique id of another entry; "
            "keeping %s for this one",
            serial,
            entry.unique_id,
        )
        return
    _LOGGER.info(
        "Unique id of %s changes from %s to %s", entry.title, entry.unique_id, serial
    )
    hass.config_entries.async_update_entry(entry, unique_id=serial)


@callback
def _async_make_device_physical(hass: HomeAssistant, entry: EcoflowConfigEntry) -> None:
    """Drop the service entry type the device was once registered with.

    The device info no longer sets one, which leaves what the registry has.
    """
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if device.entry_type is dr.DeviceEntryType.SERVICE:
            registry.async_update_device(device.id, entry_type=None)


async def async_setup_entry(hass: HomeAssistant, entry: EcoflowConfigEntry) -> bool:
    """Set up EF-PowerOcean-TcpModbus from a config entry."""

    await async_prepare(hass)
    coordinator = EcoflowCoordinator(
        hass,
        config_entry=entry,
    )
    await coordinator.async_load_persisted_state()
    await coordinator.async_connect_client()
    # Before the update listener below, which would reload the entry over it.
    _async_claim_serial_number(hass, entry, coordinator)
    await coordinator.async_config_entry_first_refresh()

    # The control loop reads its inputs whatever entities are enabled.
    entry.async_on_unload(coordinator.async_require(ControlInputs.keys()))
    _async_update_modbus_disabled_issue(hass, entry, coordinator)
    entry.async_on_unload(
        coordinator.async_add_listener(
            lambda: _async_update_modbus_disabled_issue(hass, entry, coordinator)
        )
    )

    entry.runtime_data = coordinator
    _async_make_device_physical(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload integration when config entry data changes
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    return True


async def _async_reload_entry(hass: HomeAssistant, entry: EcoflowConfigEntry) -> None:
    """Reload the integration when the config entry is updated."""
    _LOGGER.debug("Config entry updated — reloading EF-PowerOcean-TcpModbus")
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EcoflowConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    ir.async_delete_issue(hass, DOMAIN, _modbus_disabled_issue_id(entry))
    # close connection and shutdown
    await entry.runtime_data.async_client_shutdown()

    return True
