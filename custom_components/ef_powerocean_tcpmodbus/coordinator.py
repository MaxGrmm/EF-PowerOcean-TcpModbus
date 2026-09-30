"""DataUpdateCoordinator for EcoFlow PowerOcean Plus."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID, Platform
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt
from pymodbus import __version__ as pyModbusVersion
from pymodbus.exceptions import ModbusException

from .const import (
    ATTR_MODE,
    BATTERY_MODE_SELECT,
    CONF_BATTERY_COUNT,
    CONF_CALC_SOLAR_POWER,
    CONF_HOST,
    CONF_INVERTER_MODEL,
    CONF_MAX_BATTERY_CHARGED_POWER,
    CONF_MAX_BATTERY_DISCHARGED_POWER,
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
    DEVICE_ENERGY_KEYS,
    DEVICE_INFO_BLOCK,
    DEVICE_INFO_EXTRA,
    DOMAIN,
    EVENT_COMMAND_EXPIRED,
    FIRMWARE_VERSION,
    MAX_BATTERY_CHARGED_POWER,
    MAX_BATTERY_DISCHARGED_POWER,
    MODBUS_DISABLED_READ_THRESHOLD,
    SERIAL_NUMBER,
    STATE_SAVE_DELAY_S,
    STORAGE_VERSION,
    register_blocks_for,
)
from .control import ControlManager
from .energy_processor import EnergyProcessor
from .modbus import ModbusReadRejected, create_client
from .models import (
    ControlFeature,
    CoordinatorStatus,
    InverterModel,
    NumberWritableDef,
    RegisterBlock,
    RegisterDef,
    encode_register,
)
from .telemetry import (
    TelemetryData,
    calculate_derived_values,
    decode_firmware_version,
    decode_register,
    decode_serial_number,
    is_modbus_disabled,
)
from .util import parse_datetime

_LOGGER = logging.getLogger(__name__)


class EcoflowCoordinator(DataUpdateCoordinator):
    """Fetches data from EcoFlow PowerOcean Plus via Modbus TCP."""

    # Optional registers the device refused to read, which are no longer polled.
    _unsupported_keys: frozenset[str] = frozenset()

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
    ) -> None:
        self.host = config_entry.data.get(CONF_HOST)
        self.port = config_entry.data.get(CONF_PORT, DEFAULT_PORT)
        self.scan_interval = config_entry.data.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_S
        )
        self.limits = {
            CONF_BATTERY_COUNT: config_entry.data.get(
                CONF_BATTERY_COUNT, DEFAULT_BATTERY_COUNT
            ),
            CONF_MAX_GRID_POWER: config_entry.data.get(
                CONF_MAX_GRID_POWER, DEFAULT_MAX_GRID_POWER
            ),
            CONF_MAX_SOLAR_POWER: config_entry.data.get(
                CONF_MAX_SOLAR_POWER, DEFAULT_MAX_SOLAR_POWER
            ),
            CONF_MAX_BATTERY_CHARGED_POWER: config_entry.data.get(
                CONF_MAX_BATTERY_CHARGED_POWER, MAX_BATTERY_CHARGED_POWER
            )
            * config_entry.data.get(CONF_BATTERY_COUNT, DEFAULT_BATTERY_COUNT),
            CONF_MAX_BATTERY_DISCHARGED_POWER: config_entry.data.get(
                CONF_MAX_BATTERY_DISCHARGED_POWER, MAX_BATTERY_DISCHARGED_POWER
            )
            * config_entry.data.get(CONF_BATTERY_COUNT, DEFAULT_BATTERY_COUNT),
        }
        self._ena_calc_solar_power = config_entry.data.get(CONF_CALC_SOLAR_POWER, False)
        self.inverter_model = InverterModel(
            config_entry.data.get(CONF_INVERTER_MODEL, DEFAULT_INVERTER_MODEL)
        )
        self._register_blocks = register_blocks_for(self.inverter_model)
        self._registers_by_key = {
            register.key: register
            for block in self._register_blocks
            for register in block.registers
        }
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=self.scan_interval),
        )

        self.serial_number: str | None = None
        self.firmware_version: str | None = None
        self.protocol_version: int | None = None
        self.device_address: int | None = None
        self._last_inverter_temperature: float | None = None
        self._consecutive_modbus_disabled_reads = 0
        self._modbus_client = create_client(hass, config_entry, self.host, self.port)
        self._last_checked_data: dict[str, Any] = {}
        self._last_checked_time: datetime | None = None

        self.control = ControlManager(
            self._modbus_client,
            registers_by_key=self._registers_by_key,
            limits=self.limits,
            inverter_model=self.inverter_model,
            enabled=config_entry.data.get(CONF_MODBUS_CONTROL, False),
            scan_interval_s=self.scan_interval,
            on_update=self.async_update_listeners,
            on_refresh=self.async_refresh,
            write_setting=self._async_write_register,
            on_command_expired=self._command_expired,
        )
        # Context of the last expiry event, taken once by the Battery Mode select so its
        # change to automatic shows the expiry as the cause.
        self._command_expired_context: Context | None = None
        self._energy_processor = EnergyProcessor(self.limits)
        self._status: CoordinatorStatus | None = None
        self._store: Store[dict[str, Any]] | None = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{config_entry.entry_id}.state"
        )

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._modbus_client.connected

    @property
    def status(self) -> CoordinatorStatus | None:
        return self._status

    @property
    def is_modbus_disabled(self) -> bool:
        """Return whether the last telemetry read indicates Modbus is disabled."""
        return self._consecutive_modbus_disabled_reads >= MODBUS_DISABLED_READ_THRESHOLD

    def get_pymodbus_version(self) -> str:
        return pyModbusVersion

    # ── Command expiry ────────────────────────────────────────────────────────

    @callback
    def _command_expired(self, feature: ControlFeature) -> None:
        """Fire an event for a command that was not renewed in time."""
        entry_id = self.config_entry.entry_id
        devices = device_registry.async_entries_for_config_entry(
            device_registry.async_get(self.hass), entry_id
        )
        select_id = entity_registry.async_get(self.hass).async_get_entity_id(
            Platform.SELECT, DOMAIN, f"{entry_id}_{BATTERY_MODE_SELECT.key}"
        )
        context = Context()
        self.hass.bus.async_fire(
            EVENT_COMMAND_EXPIRED,
            {
                ATTR_DEVICE_ID: devices[0].id if devices else None,
                ATTR_ENTITY_ID: select_id,
                ATTR_MODE: str(feature),
            },
            context=context,
        )
        self._command_expired_context = context

    def pop_command_expired_context(self) -> Context | None:
        """Return the context of the last expiry once, for the mode change it caused."""
        context, self._command_expired_context = self._command_expired_context, None
        return context

    # ── Persistence ───────────────────────────────────────────────────────────

    def _persisted_state(self) -> dict[str, Any]:
        """Return the state in a JSON-serializable form."""
        return {
            "last_checked_data": self._last_checked_data,
            "last_checked_time": self._last_checked_time.isoformat()
            if self._last_checked_time is not None
            else None,
            **self.control.dump_state(),
            **self._energy_processor.dump_state(),
        }

    async def async_load_persisted_state(self) -> None:
        """Seed the state from disk so the first poll is validated."""
        if self._store is None or (stored := await self._store.async_load()) is None:
            return

        self._last_checked_data = stored.get("last_checked_data") or {}
        self._last_checked_time = parse_datetime(stored.get("last_checked_time"))
        self.control.load_state(stored)
        self._energy_processor.load_state(stored)

    # ── Connection ────────────────────────────────────────────────────────────

    async def async_client_shutdown(self) -> None:
        """Integration-Shutdown, closing connection"""
        _LOGGER.info("PowerOcean Shutdown. Closing Connection!")
        if self._store is not None:
            await self._store.async_save(self._persisted_state())
        await self.control.async_stop()
        await self._modbus_client.async_close()
        await super().async_shutdown()

    async def async_connect_client(self) -> None:
        """First Client-Connect"""
        # Started before the connect can fail: the heartbeat skips beats while the
        # client is down and is beating again the moment a reconnect succeeds.
        self.control.start()

        if not await self._modbus_client.async_connect():
            _LOGGER.error(f"Modbus TCP not connected to {self.host}:{self.port}")
            return

        await self.async_read_device_info()
        _LOGGER.info(
            f"Modbus TCP is connected to {self.host}:{self.port} (SN: {self.serial_number})"
        )

    async def async_read_device_info(self) -> None:
        """Populate the serial number and firmware version from the device.

        Run on every connect, not only the first, because a firmware update reboots
        the inverter and drops the connection. A failed read keeps what an earlier
        one found and leaves the connection open; if it is dead, the poll finds out.
        The product registers in the same block are left alone: the configured
        model decides how the device is read.
        """
        if self.serial_number is None:
            self.serial_number = "unknown"

        try:
            raw = await self._modbus_client.async_read(
                DEVICE_INFO_BLOCK.start, DEVICE_INFO_BLOCK.count
            )
        except ModbusException as err:
            _LOGGER.warning(f"Can not read device information. {err.string}.")
            return

        if not raw or len(raw) < DEVICE_INFO_BLOCK.count:
            return

        registers_for = partial(DEVICE_INFO_BLOCK.registers_for, raw)

        self.serial_number = (
            decode_serial_number(registers_for(SERIAL_NUMBER)) or "unknown"
        )

        if firmware := decode_firmware_version(
            registers_for(FIRMWARE_VERSION), self.inverter_model.traits.high_word_first
        ):
            self.firmware_version = firmware

        await self._async_read_device_info_extra()

    async def _async_read_device_info_extra(self) -> None:
        """Read the protocol version and device address, where the device has them.

        Each on its own read, and a refusal only leaves the value unknown.
        """
        values: dict[str, int | None] = {}
        for register in DEVICE_INFO_EXTRA:
            try:
                raw = await self._modbus_client.async_read(
                    register.address, register.size
                )
            except ModbusException as err:
                _LOGGER.debug(f"Could not read {register.key}: {err.string}")
                values[register.key] = None
                continue
            value = decode_register(
                raw, register.data_type, self.inverter_model.traits.high_word_first
            )
            values[register.key] = int(value) if value is not None else None

        self.protocol_version = values.get("protocol_version")
        self.device_address = values.get("device_address")

    def _device_info_values(self) -> dict[str, Any]:
        """Return the values read on connect, in the form the sensors show."""
        return {
            "protocol_version": self.protocol_version,
            "device_address": self.device_address,
        }

    async def async_reconnect(self) -> bool:
        """Reconnect, and assume the device stopped following us while we were away.

        The device info is read again: the outage may have been a firmware update,
        or the first connect may have failed before it could be read at all.
        """
        if not await self._modbus_client.async_reconnect():
            return False
        self.control.mark_stale()
        await self.async_read_device_info()
        return True

    async def async_get_raw_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {}

        # ── Check Connection, if not -> start reconnection ──
        if not self._modbus_client.connected and not await self.async_reconnect():
            raise UpdateFailed("Reconnect failed!")

        try:
            # A probe may replan the blocks, so the loop runs over this poll's plan.
            for register_block in tuple(self._register_blocks):
                await self._async_read_block(register_block, data)

            if is_modbus_disabled(
                self.serial_number,
                data.get("inverter_rated_power"),
                data.get("limit_inv_max"),
            ):
                self._consecutive_modbus_disabled_reads += 1
            else:
                self._consecutive_modbus_disabled_reads = 0

            return data
        except ModbusException as err:
            _LOGGER.debug(f"{err.string}. Connection closing...")
            self._modbus_client.close()
            return None
        except Exception as err:
            _LOGGER.error(f"Unexpected error during data fetch: {repr(err)}")
            return data

    @property
    def unsupported_registers(self) -> frozenset[str]:
        """Return the optional registers the device refused, which are not polled."""
        return self._unsupported_keys

    async def _async_read_block(
        self, register_block: RegisterBlock, data: dict[str, Any]
    ) -> None:
        """Read one block into data, probing an optional one register by register.

        A required block that fails raises as before. An optional one refused as
        an invalid request is read again one register at a time, and each register
        the device refuses on its own is dropped from polling until the integration
        reloads. A refusal for the moment, such as device busy, only leaves the
        block unread for this poll.
        """
        try:
            raw = await self._modbus_client.async_read(
                register_block.start, register_block.count
            )
        except ModbusReadRejected as err:
            if not register_block.optional:
                raise
            if not err.permanent:
                data.update(dict.fromkeys(r.key for r in register_block.registers))
                return
            await self._async_probe_optional_block(register_block, data)
            return

        for register in register_block.registers:
            data[register.key] = self._decode(
                register_block.registers_for(raw, register), register
            )

    async def _async_probe_optional_block(
        self, register_block: RegisterBlock, data: dict[str, Any]
    ) -> None:
        refused: set[str] = set()
        for register in register_block.registers:
            try:
                raw = await self._modbus_client.async_read(
                    register.address, register.size
                )
            except ModbusReadRejected as err:
                if err.permanent:
                    refused.add(register.key)
                data[register.key] = None
                continue
            data[register.key] = self._decode(list(raw), register)

        if refused:
            _LOGGER.info(
                "The device does not implement %s; they are no longer read.",
                ", ".join(sorted(refused)),
            )
            self._unsupported_keys = self._unsupported_keys | refused
            self._register_blocks = register_blocks_for(
                self.inverter_model, exclude=self._unsupported_keys
            )

    def _decode(self, words: list[int], register: RegisterDef) -> float | None:
        traits = self.inverter_model.traits
        value = decode_register(words, register.data_type, traits.high_word_first)
        if (
            value is not None
            and traits.energy_in_watt_hours
            and register.key in DEVICE_ENERGY_KEYS
        ):
            value = round(value / 1000, 3)
        return value

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            raw_data = await self.async_get_raw_data()
        except UpdateFailed:
            self._status = CoordinatorStatus.RECONNECT_FAILED
            raise UpdateFailed(
                "Reconnect attempts failed! Integration stopped. Retry after 120s.",
                retry_after=120,
            )

        if raw_data is None:
            self._status = CoordinatorStatus.READ_FAILED
            raise UpdateFailed(
                "Read failed; entities stay unavailable until the next successful read."
            )

        try:
            result = self._energy_processor.validate_totals(
                raw_data, self._last_checked_data, self._last_checked_time
            )
            result.update(self._energy_processor.raw_daily_values(raw_data))
            result.update(self._device_info_values())
            result, is_daily_reset = self._energy_processor.derive_daily(result)
            calculated_results = calculate_derived_values(
                TelemetryData.from_mapping(result),
                calculate_solar_power=self._ena_calc_solar_power,
                startup_voltage=self.inverter_model.traits.startup_voltage,
                reports_effective_feed_cap=(
                    self.inverter_model.traits.reports_effective_feed_cap
                ),
            )
            result.update(calculated_results)
            result = self._energy_processor.clamp_calculated(
                result, self._last_checked_data, is_daily_reset=is_daily_reset
            )

            # The poll needs to happen after the derived values are calculated so that
            # the control sees the correct solar power, in case the user has configured
            # them to be calculated.
            await self.control.async_poll(result)

            self._last_checked_data = dict(result)
            self._last_checked_time = dt.now()
            self._status = CoordinatorStatus.SUCCESS
            if self._store is not None:
                self._store.async_delay_save(self._persisted_state, STATE_SAVE_DELAY_S)

            return dict(result)
        except Exception as err:
            self._status = CoordinatorStatus.PROCESSING_FAILED
            _LOGGER.error(f"Unexpected error during data fetch: {repr(err)}")
            return None

    # ── Parameter and setpoint writes ─────────────────────────────────────────

    async def async_write_modbus_register(
        self, entity_def: NumberWritableDef, value: int
    ) -> None:
        """Write a device setting from a number entity."""
        await self._async_write_register(
            RegisterDef(entity_def.read_key, entity_def.register, entity_def.data_type),
            value,
        )

    async def _async_write_register(
        self, register: RegisterDef, value: int, *, publish_as: Any = None
    ) -> None:
        """Write a device setting and verify it by reading it back.

        Settings apply without Modbus control authority, unlike the control word and
        its setpoints, so this never takes control away from the EcoFlow app.
        """
        if not self.connected:
            raise HomeAssistantError("Modbus client is not connected")

        target_value = int(value)
        register_address = register.write_address or register.address
        key = register.key

        try:
            words = encode_register(target_value, register.data_type)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

        await self._modbus_client.async_write(
            register_address, words, what=f"{key} {value}"
        )

        try:
            readback_words = await self._modbus_client.async_read(
                register_address, len(words)
            )
        except ModbusException as err:
            raise HomeAssistantError(
                f"Could not verify write to register {register_address}: {err}"
            ) from err

        readback_value = decode_register(
            readback_words,
            register.data_type,
            self.inverter_model.traits.high_word_first,
        )
        # A 32-bit register echoes the words just written and only swaps them into
        # read order a few seconds later, so either form means the write landed.
        if readback_words != words and (
            readback_value is None or int(readback_value) != target_value
        ):
            raise HomeAssistantError(
                f"Register {register_address} acknowledged value {target_value}, "
                f"but read back {readback_value}"
            )

        _LOGGER.debug(
            "Register %s [%s] acknowledged value: %s (the device may still ignore "
            "it; confirm the effect, not the readback)",
            register_address,
            key,
            target_value,
        )

        published = target_value if publish_as is None else publish_as
        updated_data = {**(self.data or {}), key: published}
        self.async_set_updated_data(updated_data)
