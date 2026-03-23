"""Config flow for EF-PowerOcean-TcpModbus integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from pymodbus.client import ModbusTcpClient

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, DEFAULT_PORT

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("host"): str,
        vol.Optional("port", default=DEFAULT_PORT): int,
    }
)


def _test_connection(host: str, port: int) -> bool:
    """Try to connect and read status register."""
    try:
        client = ModbusTcpClient(host, port=port, timeout=5)
        client.unit_id = 1
        if not client.connect():
            return False
        result = client.read_holding_registers(42081, count=1)
        client.close()
        return not result.isError()
    except Exception as e:
        _LOGGER.warning("EF-PowerOcean connection test failed: %s", e)
        return False


class EcoflowConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for EF-PowerOcean-TcpModbus."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input["host"]
            port = user_input.get("port", DEFAULT_PORT)

            ok = await self.hass.async_add_executor_job(_test_connection, host, port)

            if ok:
                await self.async_set_unique_id(f"{host}:{port}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"PowerOcean Plus ({host})",
                    data={"host": host, "port": port},
                )
            else:
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
