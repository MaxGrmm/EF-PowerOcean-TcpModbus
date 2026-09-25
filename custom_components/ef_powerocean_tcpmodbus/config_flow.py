"""Config flow for EF-PowerOcean-TcpModbus integration."""

from __future__ import annotations

import logging
from typing import Any, Final

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

from .const import (
    CONF_BATTERY_COUNT,
    CONF_CALC_SOLAR_POWER,
    CONF_HOST,
    CONF_INVERTER_MODEL,
    CONF_MAX_GRID_POWER,
    CONF_MAX_SOLAR_POWER,
    CONF_MODBUS_CONTROL,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    DEFAULT_BATTERY_COUNT,
    DEFAULT_INVERTER_MODEL,
    DEFAULT_MAX_GRID_POWER,
    DEFAULT_MAX_SOLAR_POWER,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL_S,
    DEVICE_INFO_BLOCK,
    DOMAIN,
    MAX_BATTERY_COUNT,
    PRODUCT_CATEGORY,
    PRODUCT_NUMBER,
    REGISTERS_BY_KEY,
)
from .modbus import TRANSPORT_ERRORS, ModbusClient
from .models import InverterModel
from .telemetry import decode_register

_LOGGER = logging.getLogger(__name__)

MIN_CONFIG_POWER: Final = 1000
MAX_CONFIG_POWER: Final = 60000

# Config key -> register that suggests it, and the range a suggestion must fall in.
DEVICE_SUGGESTED_SETTINGS: Final = {
    CONF_BATTERY_COUNT: (REGISTERS_BY_KEY["battery_count"], 1, MAX_BATTERY_COUNT),
    CONF_MAX_SOLAR_POWER: (
        REGISTERS_BY_KEY["limit_inv_power"],
        MIN_CONFIG_POWER,
        MAX_CONFIG_POWER,
    ),
    CONF_MAX_GRID_POWER: (
        REGISTERS_BY_KEY["inverter_rated_power"],
        MIN_CONFIG_POWER,
        MAX_CONFIG_POWER,
    ),
}


async def async_read_device_settings(host: str, port: int) -> dict[str, Any] | None:
    """Connect and return the settings the device reports, or None if unreachable.

    The values only pre-fill the form, so a register that cannot be read or holds
    something implausible is left out rather than failing the setup.
    """
    client = ModbusClient(host, port, timeout=5)
    try:
        if not await client.async_connect():
            return None
        return await _async_read_settings(client)
    except Exception as e:
        _LOGGER.warning("EF-PowerOcean connection test failed: %s", e)
        return None
    finally:
        client.close()


async def _async_read_settings(client: ModbusClient) -> dict[str, Any]:
    settings: dict[str, Any] = {}

    try:
        raw = await client.async_read(DEVICE_INFO_BLOCK.start, DEVICE_INFO_BLOCK.count)
    except TRANSPORT_ERRORS as err:
        _LOGGER.debug("Could not read the product registers: %s", err)
    else:
        model = InverterModel.from_product_info(
            DEVICE_INFO_BLOCK.registers_for(raw, PRODUCT_NUMBER)[0],
            DEVICE_INFO_BLOCK.registers_for(raw, PRODUCT_CATEGORY)[0],
        )
        if model is not None:
            settings[CONF_INVERTER_MODEL] = model.value

    for key, (register, minimum, maximum) in DEVICE_SUGGESTED_SETTINGS.items():
        try:
            raw = await client.async_read(register.address, register.size)
        except TRANSPORT_ERRORS as err:
            _LOGGER.debug("Could not read %s: %s", register.key, err)
            continue
        value = decode_register(raw, register.data_type)
        if value is not None and minimum <= value <= maximum:
            settings[key] = int(value)

    _LOGGER.debug("Settings reported by the device: %s", settings)
    return settings


class EcoflowConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for EF-PowerOcean-TcpModbus."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._user_input: dict = {}
        self._device_settings: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return EcoflowOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            device_settings = await async_read_device_settings(
                user_input[CONF_HOST], user_input[CONF_PORT]
            )
            if device_settings is not None:
                self._device_settings = device_settings
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
                        CONF_INVERTER_MODEL,
                        default=self._device_settings.get(
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
                        default=self._device_settings.get(
                            CONF_BATTERY_COUNT, DEFAULT_BATTERY_COUNT
                        ),
                    ): vol.All(int, vol.Range(min=0, max=MAX_BATTERY_COUNT)),
                    vol.Required(
                        CONF_MAX_SOLAR_POWER,
                        default=self._device_settings.get(
                            CONF_MAX_SOLAR_POWER, DEFAULT_MAX_SOLAR_POWER
                        ),
                    ): vol.All(
                        int, vol.Range(min=MIN_CONFIG_POWER, max=MAX_CONFIG_POWER)
                    ),
                    vol.Required(
                        CONF_MAX_GRID_POWER,
                        default=self._device_settings.get(
                            CONF_MAX_GRID_POWER, DEFAULT_MAX_GRID_POWER
                        ),
                    ): vol.All(
                        int, vol.Range(min=MIN_CONFIG_POWER, max=MAX_CONFIG_POWER)
                    ),
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL_S
                    ): vol.All(int, vol.Range(min=2, max=30)),
                    vol.Required(
                        CONF_CALC_SOLAR_POWER,
                        default=False,
                    ): BooleanSelector({}),
                    vol.Required(
                        CONF_MODBUS_CONTROL,
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
                if await async_read_device_settings(host, port) is None:
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
                    ): vol.All(int, vol.Range(min=0, max=MAX_BATTERY_COUNT)),
                    vol.Required(
                        CONF_MAX_SOLAR_POWER,
                        default=self._config_entry.data.get(
                            CONF_MAX_SOLAR_POWER, DEFAULT_MAX_SOLAR_POWER
                        ),
                    ): vol.All(
                        int, vol.Range(min=MIN_CONFIG_POWER, max=MAX_CONFIG_POWER)
                    ),
                    vol.Required(
                        CONF_MAX_GRID_POWER,
                        default=self._config_entry.data.get(
                            CONF_MAX_GRID_POWER, DEFAULT_MAX_GRID_POWER
                        ),
                    ): vol.All(
                        int, vol.Range(min=MIN_CONFIG_POWER, max=MAX_CONFIG_POWER)
                    ),
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
                    vol.Required(
                        CONF_MODBUS_CONTROL,
                        default=self._config_entry.data.get(CONF_MODBUS_CONTROL, False),
                    ): BooleanSelector({}),
                }
            ),
            errors=errors,
        )
