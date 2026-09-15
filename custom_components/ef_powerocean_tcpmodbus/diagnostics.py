"""Diagnostics support for EcoFlow PowerOcean Plus."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HOST, CONTROL_FEATURES, DOMAIN
from .coordinator import EcoflowCoordinator

TO_REDACT = (CONF_HOST, "title", "unique_id")


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: EcoflowCoordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})

    serial_number = coordinator.serial_number

    if serial_number != "unknown":
        serial_number = serial_number[:4]

    return async_redact_data(
        {
            "entry": entry.as_dict(),
            "domain": DOMAIN,
            "serial_number": serial_number,
            "firmware_version": coordinator.firmware_version,
            "detected_model": coordinator.detected_model,
            "pymodbus": coordinator.get_pymodbus_version(),
            "heartbeat_supported": coordinator.heartbeat_supported,
            "last_heartbeat_time": coordinator.last_heartbeat_time,
            "in_control": coordinator.in_control,
            "selected_feature": str(coordinator.selected_feature),
            "control_status": str(coordinator.control_status),
            "feature_power": {
                str(feature): coordinator.feature_power(feature)
                for feature in CONTROL_FEATURES
            },
            "control_method": str(coordinator.control_method),
            "control_power": coordinator.control_power,
            "control_command": f"0x{coordinator.control_command:08X}",
        },
        TO_REDACT,
    )
