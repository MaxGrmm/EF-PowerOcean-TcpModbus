"""DataUpdateCoordinator for EcoFlow PowerOcean Plus."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt
from pymodbus import __version__ as pyModbusVersion
from pymodbus.exceptions import ModbusException

from .const import (
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
    DEVICE_INFO_BLOCK,
    DOMAIN,
    FIRMWARE_VERSION,
    MAX_BATTERY_CHARGED_POWER,
    MAX_BATTERY_DISCHARGED_POWER,
    MODBUS_DISABLED_READ_THRESHOLD,
    PRODUCT_CATEGORY,
    PRODUCT_NUMBER,
    SERIAL_NUMBER,
    STATE_SAVE_DELAY_S,
    STORAGE_VERSION,
    register_blocks_for,
)
from .control import ControlManager
from .energy_processor import EnergyProcessor
from .modbus import ModbusClient
from .models import (
    CoordinatorStatus,
    InverterModel,
    NumberWritableDef,
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
            name=DOMAIN,
            update_interval=timedelta(seconds=self.scan_interval),
        )

        self.serial_number: str | None = None
        self.firmware_version: str | None = None
        self.detected_model: InverterModel | None = None
        self._last_inverter_temperature: float | None = None
        self._consecutive_modbus_disabled_reads = 0
        self._modbus_client = ModbusClient(self.host, self.port)
        self._last_checked_data: dict[str, Any] = {}
        self._last_checked_time: datetime | None = None

        self.control = ControlManager(
            self._modbus_client,
            registers_by_key=self._registers_by_key,
            limits=self.limits,
            inverter_model=self.inverter_model,
            enabled=config_entry.data.get(CONF_MODBUS_CONTROL, False),
            on_update=self.async_update_listeners,
            on_refresh=self.async_refresh,
        )
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
        await self._modbus_client.async_close()
        await super().async_shutdown()

    async def async_connect_client(self) -> None:
        """First Client-Connect"""
        if not await self._modbus_client.async_connect():
            _LOGGER.error(f"Modbus TCP not connected to {self.host}:{self.port}")
            return

        await self.async_read_device_info()
        _LOGGER.info(
            f"Modbus TCP is connected to {self.host}:{self.port} (SN: {self.serial_number})"
        )

    async def async_read_device_info(self) -> None:
        """Populate the serial number, firmware and detected model from the device."""
        self.serial_number = "unknown"

        try:
            raw = await self._modbus_client.async_read(
                DEVICE_INFO_BLOCK.start, DEVICE_INFO_BLOCK.count
            )
        except ModbusException as err:
            _LOGGER.error(f"Can not read device information. {err.string}.")
            self._modbus_client.close()
            return

        if not raw or len(raw) < DEVICE_INFO_BLOCK.count:
            return

        registers_for = partial(DEVICE_INFO_BLOCK.registers_for, raw)

        self.serial_number = (
            decode_serial_number(registers_for(SERIAL_NUMBER)) or "unknown"
        )

        if firmware := decode_firmware_version(registers_for(FIRMWARE_VERSION)):
            self.firmware_version = firmware

        self.detected_model = InverterModel.from_product_info(
            registers_for(PRODUCT_NUMBER)[0], registers_for(PRODUCT_CATEGORY)[0]
        )
        if self.detected_model and self.detected_model != self.inverter_model:
            _LOGGER.warning(
                "Inverter reports %s but %s is configured. Update the integration "
                "options if this is wrong; the model affects PV startup voltage.",
                self.detected_model.display_name,
                self.inverter_model.display_name,
            )

    async def async_reconnect(self) -> bool:
        """Reconnect, and assume the device stopped following us while we were away."""
        if not await self._modbus_client.async_reconnect():
            return False
        self.control.mark_stale()
        return True

    async def async_get_raw_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {}

        # ── Check Connection, if not -> start reconnection ──
        if not self._modbus_client.connected and not await self.async_reconnect():
            raise UpdateFailed("Reconnect failed!")

        await self.control.async_send_heartbeat()

        try:
            for register_block in self._register_blocks:
                raw = await self._modbus_client.async_read(
                    register_block.start, register_block.count
                )
                for register in register_block.registers:
                    data[register.key] = decode_register(
                        register_block.registers_for(raw, register),
                        register.data_type,
                    )

            if is_modbus_disabled(
                self.serial_number,
                data.get("inverter_rated_power"),
                data.get("limit_inv_max"),
            ):
                self._consecutive_modbus_disabled_reads += 1
            else:
                self._consecutive_modbus_disabled_reads = 0

            configured_battery_count = self.limits[CONF_BATTERY_COUNT]
            if data["battery_count"] != configured_battery_count:
                _LOGGER.debug(
                    "Inverter reported battery count %s, but %s is configured; "
                    "using the configured count for this update",
                    data["battery_count"],
                    configured_battery_count,
                )
                data["battery_count"] = configured_battery_count

            return data
        except ModbusException as err:
            _LOGGER.debug(f"{err.string}. Connection closing...")
            self._modbus_client.close()
            return None
        except Exception as err:
            _LOGGER.error(f"Unexpected error during data fetch: {repr(err)}")
            return data

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
            result, is_daily_reset = self._energy_processor.derive_daily(result)
            calculated_results = calculate_derived_values(
                TelemetryData.from_mapping(result),
                calculate_solar_power=self._ena_calc_solar_power,
                startup_voltage=self.inverter_model.startup_voltage,
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
        """Write a device setting and verify it by reading it back.

        Settings apply without Modbus control authority, unlike the control word and
        its setpoints, so this never takes control away from the EcoFlow app.
        """
        if not self.connected:
            raise HomeAssistantError("Modbus client is not connected")

        target_value = int(value)
        register_address = entity_def.register
        key = entity_def.read_key

        try:
            words = encode_register(target_value, entity_def.data_type)
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

        readback_value = decode_register(readback_words, entity_def.data_type)
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

        updated_data = {**(self.data or {}), key: target_value}
        self.async_set_updated_data(updated_data)
