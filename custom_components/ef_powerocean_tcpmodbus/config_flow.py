"""Config flow for EF-PowerOcean-TcpModbus integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_BATTERY_COUNT,
    CONF_BATTERY_RESERVE,
    CONF_CALC_SOLAR_POWER,
    CONF_ENERGY_OFFSETS,
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
from .util import parse_kwh, portal_history_url

if TYPE_CHECKING:
    from .coordinator import EcoflowCoordinator

_LOGGER = logging.getLogger(__name__)

# The collapsed section of the parameters step that corrects the lifetime totals.
ENERGY_TOTALS: Final = "energy_totals"
# Its toggle that removes every correction, offered while there is one.
REMOVE_CORRECTIONS: Final = "remove_corrections"

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


def _connection_schema() -> vol.Schema:
    """Return the form for the inverter's address, shared by setup and reconfigure."""
    return vol.Schema(
        {
            vol.Required(CONF_HOST): str,
            vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
        }
    )


def _default_title(host: str) -> str:
    return f"EcoFlow PowerOcean ({host})"


def _is_keyed_by_address(entry: ConfigEntry) -> bool:
    """Return whether the entry still has a host:port unique id, not a serial.

    Entries made before the serial number could be read are keyed by address
    until the coordinator claims the serial. A serial number has no colon.
    """
    return ":" in (entry.unique_id or "")


class EcoflowConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for EF-PowerOcean-TcpModbus."""

    VERSION = 1
    # 2: the settings are read from the entry's options. They are also kept in its
    # data, as they were at setup, for an earlier release after a downgrade.
    MINOR_VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._user_input: dict[str, Any] = {}
        self._device_settings: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,  # noqa: ARG004 - required by the flow API
    ) -> EcoflowOptionsFlow:
        return EcoflowOptionsFlow()

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
            data_schema=self.add_suggested_values_to_schema(
                _connection_schema(), user_input
            ),
            errors=errors,
        )

    async def async_step_parameters(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            return self.async_create_entry(
                title=_default_title(self._user_input[CONF_HOST]),
                data={**self._user_input, **user_input},
                options=user_input,
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

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Move an existing entry to a new address of the same inverter.

        Only the connection lives here. Everything else about the inverter is in
        the options flow, which works without the inverter answering.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            if self._address_taken(entry, host, port):
                return self.async_abort(reason="already_configured")
            device_settings = await async_read_device_settings(self.hass, host, port)
            if device_settings is None:
                errors["base"] = "cannot_connect"
            else:
                serial = device_settings.get(DEVICE_SERIAL_NUMBER)
                if serial and not _is_keyed_by_address(entry):
                    # A different inverter answering here would take over this
                    # entry's history and controls, so refuse it.
                    await self.async_set_unique_id(serial)
                    self._abort_if_unique_id_mismatch(reason="wrong_device")
                # Not async_update_reload_and_abort: the entry's update listener
                # reloads it already, and Home Assistant flags the second reload
                # as an error from 2026.12.
                return self.async_update_and_abort(
                    entry,
                    data_updates={CONF_HOST: host, CONF_PORT: port},
                    # A title the user chose is kept; the default names the host.
                    title=(
                        _default_title(host)
                        if entry.title == _default_title(entry.data[CONF_HOST])
                        else entry.title
                    ),
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _connection_schema(),
                user_input
                or {
                    CONF_HOST: entry.data[CONF_HOST],
                    CONF_PORT: entry.data.get(CONF_PORT, DEFAULT_PORT),
                },
            ),
            errors=errors,
            description_placeholders={"title": entry.title},
        )

    def _address_taken(self, entry: ConfigEntry, host: str, port: int) -> bool:
        """Return whether another entry already polls this address."""
        return any(
            other.entry_id != entry.entry_id
            and other.data.get(CONF_HOST) == host
            and other.data.get(CONF_PORT, DEFAULT_PORT) == port
            for other in self._async_current_entries(include_ignore=False)
        )


class EcoflowOptionsFlow(OptionsFlow):
    """Change how the integration treats the inverter, without reconnecting.

    The address is not here: it is changed with the reconfigure flow, which tests
    the connection and checks that the same inverter answers.
    """

    def __init__(self) -> None:
        # The totals as the form showed them, so a field left alone, while the
        # total kept counting, is not taken for a correction.
        self._shown_totals: dict[str, float] = {}
        # The lowered totals last warned about; submitting them again confirms.
        self._warned_lower: dict[str, float] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            entered = user_input.pop(ENERGY_TOTALS, {})
            try:
                corrected, lowered = self._energy_offsets(entered)
            except ValueError:
                errors["base"] = "invalid_energy_total"
            else:
                if lowered and lowered != self._warned_lower:
                    self._warned_lower = lowered
                    errors["base"] = "energy_total_lowered"
            if not errors:
                return self.async_create_entry(
                    data={**self.config_entry.options, **user_input, **corrected}
                )

        fields: dict[Any, Any] = {
            vol.Required(
                CONF_INVERTER_MODEL,
                default=self.config_entry.options.get(
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
                default=self.config_entry.options.get(
                    CONF_BATTERY_COUNT, DEFAULT_BATTERY_COUNT
                ),
            ): vol.All(int, vol.Range(min=0, max=MAX_BATTERY_COUNT)),
            vol.Required(
                CONF_MAX_SOLAR_POWER,
                default=self.config_entry.options.get(
                    CONF_MAX_SOLAR_POWER, DEFAULT_MAX_SOLAR_POWER
                ),
            ): vol.All(int, vol.Range(min=MIN_CONFIG_POWER, max=MAX_CONFIG_POWER)),
            vol.Required(
                CONF_MAX_GRID_POWER,
                default=self.config_entry.options.get(
                    CONF_MAX_GRID_POWER, DEFAULT_MAX_GRID_POWER
                ),
            ): vol.All(int, vol.Range(min=MIN_CONFIG_POWER, max=MAX_CONFIG_POWER)),
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=self.config_entry.options.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_S
                ),
            ): vol.All(int, vol.Range(min=2, max=30)),
            vol.Required(
                CONF_CALC_SOLAR_POWER,
                default=self.config_entry.options.get(CONF_CALC_SOLAR_POWER, False),
            ): BooleanSelector({}),
            vol.Required(
                CONF_BATTERY_RESERVE,
                default=battery_reserve_for(
                    InverterModel(
                        self.config_entry.options.get(
                            CONF_INVERTER_MODEL, DEFAULT_INVERTER_MODEL
                        )
                    ),
                    self.config_entry.options.get(CONF_BATTERY_RESERVE),
                ).value,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[support.value for support in ReserveSupport],
                    translation_key=CONF_BATTERY_RESERVE,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
        if energy_totals := self._energy_totals_section(collapsed=not errors):
            fields[vol.Required(ENERGY_TOTALS)] = energy_totals
        schema = vol.Schema(fields)
        if user_input is not None:
            # Shown again with an error: keep what the user typed.
            schema = self.add_suggested_values_to_schema(
                schema, {**user_input, ENERGY_TOTALS: entered}
            )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "portal_url": portal_history_url(self.hass.config.language)
            },
        )

    def _coordinator(self) -> EcoflowCoordinator | None:
        coordinator: EcoflowCoordinator | None = getattr(
            self.config_entry, "runtime_data", None
        )
        return coordinator

    def _energy_totals_section(self, *, collapsed: bool) -> section | None:
        """Return the section to correct the lifetime totals.

        Holds the totals EcoFlow's portal shows, to correct them to it, and is left
        out until the inverter has reported them, which needs the entry to be
        loaded. The totals are text fields: a number field on an iPhone set
        to a region with a decimal comma offers only the comma, which the field
        then refuses.
        """
        coordinator = self._coordinator()
        if coordinator is None:
            return None
        if not self._shown_totals:
            # Kept when the form is shown again, so the comparison stays with
            # what the user saw.
            self._shown_totals = {
                key: round(value, 2)
                for key, value in coordinator.correctable_totals.items()
            }
        if not self._shown_totals:
            return None
        field = TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, suffix="kWh")
        )
        fields: dict[Any, Any] = {
            vol.Required(key, default=str(value)): field
            for key, value in self._shown_totals.items()
        }
        if self.config_entry.options.get(CONF_ENERGY_OFFSETS):
            fields[vol.Required(REMOVE_CORRECTIONS, default=False)] = BooleanSelector()
        return section(vol.Schema(fields), {"collapsed": collapsed})

    def _energy_offsets(
        self, entered: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, float]]:
        """Return the entry data for the section, and the totals it would lower.

        A changed total becomes an offset to what the inverter reports, and the
        entry's reload continues it from the value entered. A field left as shown,
        while the total kept counting, changes nothing. The remove toggle drops
        every correction, whatever was entered, returning each total to the
        inverter's value. Raises ValueError for a
        value that is not a total.
        """
        coordinator = self._coordinator()
        if coordinator is None:
            return {}, {}
        if entered.get(REMOVE_CORRECTIONS):
            data = coordinator.data or {}
            shown = coordinator.correctable_totals
            device = {key: data[f"{key}_raw"] for key in shown}
            return {CONF_ENERGY_OFFSETS: {}}, _lowered(device, shown)
        values = {
            key: parse_kwh(text)
            for key, text in entered.items()
            if key in self._shown_totals
        }
        changed = {
            key: value
            for key, value in values.items()
            if abs(value - self._shown_totals[key]) >= ENERGY_RESOLUTION_KWH / 2
        }
        if not changed:
            return {}, {}
        return (
            {CONF_ENERGY_OFFSETS: coordinator.energy_offsets_for(changed)},
            _lowered(changed, self._shown_totals),
        )


def _lowered(totals: dict[str, float], shown: dict[str, float]) -> dict[str, float]:
    """Return the totals that would end up below the value shown for them.

    Home Assistant's statistics record a drop as negative energy, or as a new
    meter cycle adding the whole value when it is over 10%, so it is confirmed
    first.
    """
    return {
        key: round(value, 2)
        for key, value in totals.items()
        if value < shown[key] - ENERGY_RESOLUTION_KWH / 2
    }
