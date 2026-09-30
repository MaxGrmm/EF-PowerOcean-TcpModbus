"""Diagnostics support for EcoFlow PowerOcean Plus."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HOST, CONTROL_FEATURES, DOMAIN
from .coordinator import EcoflowCoordinator
from .shared_modbus import SHARED_CONNECTION

TO_REDACT = (CONF_HOST, "title", "unique_id")

# Raw readings worth having in a model report, to compare how each model fills them.
PROTOCOL_REPORT_KEYS = (
    "system_modes_hex",
    "active_control_mode",
    "device_modbus_control",
    "bms_connected",
    "grid_feed_mode",
    "feed_in_power_max_setting",
    "feed_in_power_max_effective",
    "feed_in_power_max_percent",
    "limit_inv_power",
    "limit_inv_max",
    "inverter_rated_power",
)


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
            "inverter_model": coordinator.inverter_model,
            "protocol_version": coordinator.protocol_version,
            "device_address": coordinator.device_address,
            "pymodbus": coordinator.get_pymodbus_version(),
            "modbus_connection": "shared" if SHARED_CONNECTION else "own",
            "heartbeat_supported": coordinator.control.heartbeat_supported,
            "modbus_control_enabled": coordinator.control.enabled,
            "last_heartbeat_time": coordinator.control.last_heartbeat_time,
            "in_control": coordinator.control.in_control,
            "selected_feature": str(coordinator.control.selected_feature),
            "command_expires_at": coordinator.control.expires_at,
            "control_status": str(coordinator.control.status),
            "control_guard": (
                str(guard) if (guard := coordinator.control.active_guard) else None
            ),
            "feature_power": {
                str(feature): coordinator.control.feature_power(feature)
                for feature in CONTROL_FEATURES
            },
            "charge_limit_soc": coordinator.control.charge_limit_soc,
            "battery_reserve_soc": coordinator.control.battery_reserve_soc,
            "control_method": str(coordinator.control.method),
            "control_power": coordinator.control.power,
            "control_command": f"0x{coordinator.control.command:08X}",
            "unsupported_registers": sorted(coordinator.unsupported_registers),
            "protocol_report": {
                key: (coordinator.data or {}).get(key) for key in PROTOCOL_REPORT_KEYS
            },
        },
        TO_REDACT,
    )
