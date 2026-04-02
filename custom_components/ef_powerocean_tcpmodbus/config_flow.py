"""Config flow for EF-PowerOcean-TcpModbus integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from pymodbus.client import ModbusTcpClient

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_PV_STRINGS,
    CONF_SCAN_INTERVAL,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_PORT,
    DEFAULT_PV_STRINGS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    REG_STATUS,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("host"): str,
        vol.Optional("port", default=DEFAULT_PORT): int,
        vol.Optional(CONF_BATTERY_CAPACITY, default=DEFAULT_BATTERY_CAPACITY): vol.Coerce(float),
        vol.Optional(CONF_PV_STRINGS, default=DEFAULT_PV_STRINGS): vol.All(int, vol.Range(min=1, max=3)),
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(int, vol.Range(min=5, max=60)),
    }
)


def _test_connection(host: str, port: int) -> bool:
    """Try to connect and read status register."""
    try:
        client = ModbusTcpClient(host, port=port, timeout=5)
        client.unit_id = 1
        if not client.connect():
            return False
        result = client.read_holding_registers(REG_STATUS, count=1)
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
                    title=f"EcoFlow PowerOcean ({host})",
                    data=user_input,
                )
            else:
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return EcoflowOptionsFlow(config_entry)


class EcoflowOptionsFlow(config_entries.OptionsFlow):
    """Handle options (reconfiguration after setup)."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input["host"]
            port = user_input["port"]

            # Only re-test connection if host or port changed
            current_host = self._config_entry.data.get("host")
            current_port = self._config_entry.data.get("port", DEFAULT_PORT)
            if host != current_host or port != current_port:
                ok = await self.hass.async_add_executor_job(_test_connection, host, port)
                if not ok:
                    errors["base"] = "cannot_connect"

            if not errors:
                # Update entry data for host/port, store rest in options
                self.hass.config_entries.async_update_entry(
                    self._config_entry,
                    data={**self._config_entry.data, "host": host, "port": port},
                )
                return self.async_create_entry(title="", data={
                    CONF_BATTERY_CAPACITY: user_input[CONF_BATTERY_CAPACITY],
                    CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
                })

        # Pre-fill current values – check options first, fall back to data
        def _current(key, default):
            return self._config_entry.options.get(
                key, self._config_entry.data.get(key, default)
            )

        schema = vol.Schema(
            {
                vol.Required("host", default=self._config_entry.data.get("host", "")): str,
                vol.Optional("port", default=self._config_entry.data.get("port", DEFAULT_PORT)): int,
                vol.Optional(
                    CONF_BATTERY_CAPACITY,
                    default=_current(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY),
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_PV_STRINGS,
                    default=_current(CONF_PV_STRINGS, DEFAULT_PV_STRINGS),
                ): vol.All(int, vol.Range(min=1, max=3)),
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=_current(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(int, vol.Range(min=5, max=60)),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

