"""Config flow for EF-PowerOcean-TcpModbus integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from pymodbus.client import AsyncModbusTcpClient

from .const import (
    CONF_BATTERY_COUNT,
    CONF_CALC_SOLAR_POWER,
    CONF_HOST,
    CONF_INVERTER_MODEL,
    CONF_MAX_GRID_POWER,
    CONF_MAX_SOLAR_POWER,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    DEFAULT_BATTERY_COUNT,
    DEFAULT_INVERTER_MODEL,
    DEFAULT_MAX_GRID_POWER,
    DEFAULT_MAX_SOLAR_POWER,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL_S,
    DOMAIN,
    InverterModel,
)

_LOGGER = logging.getLogger(__name__)


async def async_validate_connection(host: str, port: int) -> bool:
    """Try to connect and read status register."""
    try:
        client = AsyncModbusTcpClient(host, port=port, timeout=5)
        await client.connect()
        if not client.connected:
            return False
        else:
            client.close()
            return True
    except Exception as e:
        _LOGGER.warning("EF-PowerOcean connection test failed: %s", e)
        return False


class EcoflowConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for EF-PowerOcean-TcpModbus."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._user_input: dict = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return EcoflowOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            if await async_validate_connection(
                user_input[CONF_HOST], user_input[CONF_PORT]
            ):
                self._user_input = user_input
                await self.async_set_unique_id(
                    f"{self._user_input[CONF_HOST]}:{self._user_input[CONF_PORT]}"
                )
                self._abort_if_unique_id_configured()
                return await self.async_step_parameters()
            else:
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
                }
            ),
            errors=errors,
        )

    async def async_step_parameters(self, user_input=None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._user_input.update(user_input)
            return self.async_create_entry(
                title=f"EcoFlow PowerOcean ({self._user_input[CONF_HOST]})",
                data=self._user_input,
            )

        return self.async_show_form(
            step_id="parameters",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_INVERTER_MODEL, default=DEFAULT_INVERTER_MODEL
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[model.value for model in InverterModel],
                            translation_key=CONF_INVERTER_MODEL,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_BATTERY_COUNT, default=DEFAULT_BATTERY_COUNT
                    ): vol.All(int, vol.Range(min=0, max=9)),
                    vol.Required(
                        CONF_MAX_SOLAR_POWER, default=DEFAULT_MAX_SOLAR_POWER
                    ): vol.All(int, vol.Range(min=1000, max=60000)),
                    vol.Required(
                        CONF_MAX_GRID_POWER, default=DEFAULT_MAX_GRID_POWER
                    ): vol.All(int, vol.Range(min=1000, max=60000)),
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL_S
                    ): vol.All(int, vol.Range(min=2, max=30)),
                    vol.Required(
                        CONF_CALC_SOLAR_POWER,
                        default=False,
                    ): BooleanSelector({}),
                }
            ),
            errors=errors,
        )


class EcoflowOptionsFlow(OptionsFlow):
    """Handle options (reconfiguration after setup)."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._user_input: dict = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]

            # Only re-test connection if host or port changed
            current_host = self._config_entry.data.get(CONF_HOST)
            current_port = self._config_entry.data.get(CONF_PORT, DEFAULT_PORT)
            if host != current_host or port != current_port:
                if not await async_validate_connection(host, port):
                    errors["base"] = "cannot_connect"

            if not errors:
                self._user_input = user_input
                return await self.async_step_parameters()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST, default=self._config_entry.data.get(CONF_HOST, "")
                    ): str,
                    vol.Required(
                        CONF_PORT,
                        default=self._config_entry.data.get(CONF_PORT, DEFAULT_PORT),
                    ): int,
                }
            ),
            errors=errors,
        )

    async def async_step_parameters(self, user_input=None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._user_input.update(user_input)
            self.hass.config_entries.async_update_entry(
                self._config_entry,
                data={
                    **self._config_entry.data,
                    **self._user_input,
                },
            )
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="parameters",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_INVERTER_MODEL,
                        default=self._config_entry.data.get(
                            CONF_INVERTER_MODEL, DEFAULT_INVERTER_MODEL
                        ),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[model.value for model in InverterModel],
                            translation_key=CONF_INVERTER_MODEL,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_BATTERY_COUNT,
                        default=self._config_entry.data.get(
                            CONF_BATTERY_COUNT, DEFAULT_BATTERY_COUNT
                        ),
                    ): vol.All(int, vol.Range(min=0, max=6)),
                    vol.Required(
                        CONF_MAX_SOLAR_POWER,
                        default=self._config_entry.data.get(
                            CONF_MAX_SOLAR_POWER, DEFAULT_MAX_SOLAR_POWER
                        ),
                    ): vol.All(int, vol.Range(min=1000, max=60000)),
                    vol.Required(
                        CONF_MAX_GRID_POWER,
                        default=self._config_entry.data.get(
                            CONF_MAX_GRID_POWER, DEFAULT_MAX_GRID_POWER
                        ),
                    ): vol.All(int, vol.Range(min=1000, max=60000)),
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=self._config_entry.data.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_S
                        ),
                    ): vol.All(int, vol.Range(min=2, max=30)),
                    vol.Required(
                        CONF_CALC_SOLAR_POWER,
                        default=self._config_entry.data.get(
                            CONF_CALC_SOLAR_POWER, False
                        ),
                    ): BooleanSelector({}),
                }
            ),
            errors=errors,
        )
