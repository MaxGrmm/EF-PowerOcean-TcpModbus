"""Diagnostics support for EcoFlow PowerOcean Plus."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HOST, CONTROL_FEATURES, DOMAIN
from .coordinator import EcoflowCoordinator
from .modbus import is_shared

TO_REDACT = (CONF_HOST, "title", "unique_id")

# Raw readings worth having in a model report, to compare how each model fills them.
PROTOCOL_REPORT_KEYS = (
    "system_modes_hex",
    "system_state_2_hex",
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
    coordinator: EcoflowCoordinator = entry.runtime_data

    identity = asdict(coordinator.identity)
    if identity["serial_number"] not in (None, "unknown"):
        identity["serial_number"] = identity["serial_number"][:4]

    return async_redact_data(
        {
            "entry": entry.as_dict(),
            "domain": DOMAIN,
            "inverter_model": coordinator.inverter_model,
            **identity,
            "pymodbus": coordinator.get_pymodbus_version(),
            "modbus_connection": "shared" if is_shared() else "own",
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
            # The last control test run here, the report to attach to an issue.
            "control_test": {
                "state": str(coordinator.control_test.state),
                "last_report": coordinator.control_test.last_report,
            },
            "protocol_report": {
                key: (coordinator.data or {}).get(key) for key in PROTOCOL_REPORT_KEYS
            },
        },
        TO_REDACT,
    )
