"""Config flow for EF-PowerOcean-TcpModbus integration."""

from __future__ import annotations

import logging
from typing import Any, Final

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_BATTERY_COUNT,
    CONF_BATTERY_RESERVE,
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
    DEVICE_INFO_BLOCK,
    DOMAIN,
    ECOFLOW_PORTAL_HISTORY_URL,
    ENERGY_RESOLUTION_KWH,
    MAX_BATTERY_COUNT,
    PRODUCT_CATEGORY,
    PRODUCT_NUMBER,
    REGISTERS_BY_KEY,
    SERIAL_NUMBER,
)
from .modbus import TRANSPORT_ERRORS, ModbusClient, async_temporary_client
from .models import InverterModel, ReserveSupport, battery_reserve_for
from .telemetry import decode_register, decode_serial_number

_LOGGER = logging.getLogger(__name__)

MIN_CONFIG_POWER: Final = 1000
MAX_CONFIG_POWER: Final = 60000

# Key of the serial number in the settings the device reports. Not a config key:
# it becomes the entry's unique id rather than part of its data.
DEVICE_SERIAL_NUMBER: Final = "serial_number"

# Config key -> register that suggests it, and the range a suggestion must fall in.
# Maximum grid power is left out: it is the grid connection, which the house alone
# can take beyond the inverter's rating, and no register reports it reliably.
DEVICE_SUGGESTED_SETTINGS: Final = {
    CONF_BATTERY_COUNT: (REGISTERS_BY_KEY["battery_count"], 1, MAX_BATTERY_COUNT),
    CONF_MAX_SOLAR_POWER: (
        REGISTERS_BY_KEY["limit_inv_power"],
        MIN_CONFIG_POWER,
        MAX_CONFIG_POWER,
    ),
}


async def async_read_device_settings(
    hass: HomeAssistant, host: str, port: int
) -> dict[str, Any] | None:
    """Connect and return the settings the device reports, or None if unreachable.

    The values only pre-fill the form, so a register that cannot be read or holds
    something implausible is left out rather than failing the setup.
    """
    try:
        async with async_temporary_client(hass, host, port) as client:
            if not await client.async_connect():
                return None
            return await _async_read_settings(client)
    except Exception as e:
        _LOGGER.warning("EF-PowerOcean connection test failed: %s", e)
        return None


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
        if serial := decode_serial_number(
            DEVICE_INFO_BLOCK.registers_for(raw, SERIAL_NUMBER)
        ):
            settings[DEVICE_SERIAL_NUMBER] = serial

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
        self._user_input: dict[str, Any] = {}
        self._device_settings: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EcoflowOptionsFlow:
        return EcoflowOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            device_settings = await async_read_device_settings(
                self.hass, user_input[CONF_HOST], user_input[CONF_PORT]
            )
            if device_settings is not None:
                self._device_settings = device_settings
                self._user_input = user_input
                # The serial number identifies the inverter whatever address it is
                # at; host and port stand in only until it can be read.
                await self.async_set_unique_id(
                    device_settings.get(DEVICE_SERIAL_NUMBER)
                    or f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
                )
                # An inverter already set up that has moved on the network gets
                # its new address rather than a second entry.
                self._abort_if_unique_id_configured(
                    updates={
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_PORT: user_input[CONF_PORT],
                    }
                )
                return await self.async_step_parameters()
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

    async def async_step_parameters(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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
                }
            ),
            errors=errors,
        )


class EcoflowOptionsFlow(OptionsFlow):
    """Handle options (reconfiguration after setup)."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._user_input: dict[str, Any] = {}
        # The totals as the form showed them, so a field left alone, while the
        # total kept counting, is not taken for a correction.
        self._shown_totals: dict[str, float] = {}

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002 - a menu
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["connection", "energy_totals"]
        )

    async def async_step_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]

            # Only a new address needs the connection tested again.
            moved = (host, port) != (
                self._config_entry.data.get(CONF_HOST),
                self._config_entry.data.get(CONF_PORT, DEFAULT_PORT),
            )
            if (
                moved
                and await async_read_device_settings(self.hass, host, port) is None
            ):
                errors["base"] = "cannot_connect"

            if not errors:
                self._user_input = user_input
                return await self.async_step_parameters()

        return self.async_show_form(
            step_id="connection",
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

    async def async_step_parameters(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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
                        CONF_BATTERY_RESERVE,
                        default=battery_reserve_for(
                            InverterModel(
                                self._config_entry.data.get(
                                    CONF_INVERTER_MODEL, DEFAULT_INVERTER_MODEL
                                )
                            ),
                            self._config_entry.data.get(CONF_BATTERY_RESERVE),
                        ).value,
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[support.value for support in ReserveSupport],
                            translation_key=CONF_BATTERY_RESERVE,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_energy_totals(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Correct lifetime totals to the values in EcoFlow's portal.

        A changed field becomes an offset to what the inverter reports; the entry's
        reload then continues the total from the value entered.
        """
        coordinator = getattr(self._config_entry, "runtime_data", None)
        if coordinator is None or not coordinator.correctable_totals:
            return self.async_abort(reason="no_energy_totals")

        if user_input is not None:
            changed = {
                key: float(value)
                for key, value in user_input.items()
                if key in self._shown_totals
                and abs(value - self._shown_totals[key]) >= ENERGY_RESOLUTION_KWH / 2
            }
            if changed:
                coordinator.async_set_energy_offsets(
                    coordinator.energy_offsets_for(changed)
                )
            return self.async_create_entry(title="", data={})

        self._shown_totals = {
            key: round(value, 2)
            for key, value in coordinator.correctable_totals.items()
        }
        field = NumberSelector(
            NumberSelectorConfig(
                min=0,
                step=ENERGY_RESOLUTION_KWH,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="kWh",
            )
        )
        language = "de" if self.hass.config.language.startswith("de") else "en"
        return self.async_show_form(
            step_id="energy_totals",
            data_schema=vol.Schema(
                {
                    vol.Required(key, default=value): field
                    for key, value in self._shown_totals.items()
                }
            ),
            description_placeholders={
                "portal_url": ECOFLOW_PORTAL_HISTORY_URL.format(language=language)
            },
        )
