"""Diagnostics support for EcoFlow PowerOcean Plus."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HOST, DOMAIN
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
            "pymodbus": coordinator.get_pymodbus_version(),
        },
        TO_REDACT,
    )
