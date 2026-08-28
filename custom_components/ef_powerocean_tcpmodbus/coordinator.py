"""DataUpdateCoordinator for EcoFlow PowerOcean Plus."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt
from pymodbus import __version__ as pyModbusVersion
from pymodbus.client import AsyncModbusTcpClient
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
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    DEFAULT_BATTERY_COUNT,
    DEFAULT_INVERTER_MODEL,
    DEFAULT_MAX_GRID_POWER,
    DEFAULT_MAX_SOLAR_POWER,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL_S,
    DEFAULT_SLAVE,
    DOMAIN,
    MAX_BATTERY_CHARGED_POWER,
    MAX_BATTERY_DISCHARGED_POWER,
    MOD_REGISTER_MAP,
    SLEEP_TIME_AFTER_BATTERY_CHECK_FAILED_S,
    SLEEP_TIME_AFTER_RECONNECT_S,
    STATE_SAVE_DELAY_S,
    STORAGE_VERSION,
    CoordinatorStatus,
    InverterModel,
    NumberWritableDef,
)
from .energy import EnergyProcessor, parse_datetime
from .telemetry import (
    TelemetryData,
    calculate_derived_values,
    decode_register,
    decode_serial_number,
    is_modbus_disabled,
)

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
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=self.scan_interval),
        )

        self.serial_number: str | None = None
        self._last_inverter_temperature: float | None = None
        self._client: AsyncModbusTcpClient = AsyncModbusTcpClient(
            host=self.host, port=self.port, timeout=20, reconnect_delay=0, retries=0
        )
        self._client_slave_id = DEFAULT_SLAVE
        self._lock = asyncio.Lock()
        self._last_checked_data: dict[str, Any] = {}
        self._last_checked_time: datetime | None = None
        self._energy = EnergyProcessor(self.limits)
        self._status: CoordinatorStatus | None = None
        self._store: Store[dict[str, Any]] | None = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{config_entry.entry_id}.state"
        )

    @property
    def connected(self) -> bool:
        return self._client.connected

    @property
    def status(self) -> CoordinatorStatus | None:
        return self._status

    @property
    def is_modbus_disabled(self) -> bool:
        """Return whether the last telemetry read indicates Modbus is disabled."""
        return is_modbus_disabled(
            self.serial_number,
            self._last_inverter_temperature,
        )

    def get_pymodbus_version(self) -> str:
        return pyModbusVersion

    def _persisted_state(self) -> dict[str, Any]:
        """Return the state in a JSON-serializable form."""
        return {
            "last_checked_data": self._last_checked_data,
            "last_checked_time": self._last_checked_time.isoformat()
            if self._last_checked_time is not None
            else None,
            **self._energy.dump_state(),
        }

    async def async_load_persisted_state(self) -> None:
        """Seed the state from disk so the first poll is validated."""
        if self._store is None or (stored := await self._store.async_load()) is None:
            return

        self._last_checked_data = stored.get("last_checked_data") or {}
        self._last_checked_time = parse_datetime(stored.get("last_checked_time"))
        self._energy.load_state(stored)

    async def async_client_shutdown(self) -> None:
        """Integration-Shutdown, closing connection"""
        _LOGGER.info("PowerOcean Shutdown. Closing Connection!")
        if self._store is not None:
            await self._store.async_save(self._persisted_state())
        async with self._lock:
            self._client.close()
        await super().async_shutdown()

    async def async_connect_client(self) -> None:
        """First Client-Connect"""
        await self._client.connect()

        if not self._client.connected:
            _LOGGER.error(f"Modbus TCP not connected to {self.host}:{self.port}")
            return

        self.serial_number = await self.async_get_serial_number()
        _LOGGER.info(
            f"Modbus TCP is connected to {self.host}:{self.port} (SN: {self.serial_number})"
        )

    async def async_get_serial_number(self) -> str:
        """Read serial number"""
        try:
            raw = await self.async_read_block(MOD_REGISTER_MAP["serial_number"], 8)
        except ModbusException as err:
            _LOGGER.error(f"Can not read serial number. {err.string}.")
            self._client.close()
            return "unknown"

        return decode_serial_number(raw) or "unknown"

    async def async_reconnect(self) -> bool:
        """Client-Reconnect"""
        delays = [0, 5, 30, 120]
        _LOGGER.debug(
            f"PowerOcean (SN: {self.serial_number}) is not connected. Start reconnect!"
        )

        for i, delay in enumerate(delays):
            async with self._lock:
                if delay > 0:
                    _LOGGER.debug(
                        f"Reconnect failed! Wait {delay}s until next attempt."
                    )
                    await asyncio.sleep(delay)

                _LOGGER.debug(f"Modbus TCP reconnect (Attempt {i + 1}/4)...")
                if await self._client.connect() and self._client.connected:
                    _LOGGER.debug(
                        f"Reconnect successful! (SN: {self.serial_number}) Atempts: {i + 1}/4"
                    )
                    await asyncio.sleep(SLEEP_TIME_AFTER_RECONNECT_S)
                    return True
                self._client.close()

        _LOGGER.error(
            "EF-Modbus-TCP: All reconnect attempts failed! – will retry next poll"
        )
        return False

    async def async_read_block(self, addr: int, count: int) -> list[int] | None:
        """Read *count* holding registers starting at *addr*.  Returns None on error."""
        async with self._lock:
            res = await self._client.read_holding_registers(
                address=addr, count=count, device_id=self._client_slave_id
            )
            if res.isError():
                # Modbus error response – connection may be stale
                raise ModbusException(
                    f"Modbus error response at 0x{addr:04X} with Exception-Code {res.exception_code}"
                )
            return res.registers

    async def async_get_raw_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {}

        # ── Check Connection, if not -> start reconnection ──
        if not self._client.connected and not await self.async_reconnect():
            raise UpdateFailed("Reconnect failed!")

        try:
            # Read all register blocks
            for register_block in MOD_REGISTER_MAP["blocks"]:
                raw = await self.async_read_block(
                    register_block.start_register, register_block.num_read_regs
                )
                for register in register_block.content:
                    decode_value = decode_register(
                        raw,
                        register.block_index_for(self.inverter_model),
                        register.size,
                    )
                    data[register.key] = decode_value

            # Store the inverter temperature used for the modbus tcp disabled check, before we do any data validations.
            self._last_inverter_temperature = data.get("inverter_temperature")

            if data["battery_count"] != self.limits[CONF_BATTERY_COUNT]:
                _LOGGER.debug(
                    f"Read battery count {data['battery_count']} is unequal -> Skip data! Wait {SLEEP_TIME_AFTER_BATTERY_CHECK_FAILED_S}s."
                )
                await asyncio.sleep(SLEEP_TIME_AFTER_BATTERY_CHECK_FAILED_S)
                return None

            return data
        except ModbusException as err:
            _LOGGER.debug(f"{err.string}. Connection closing...")
            self._client.close()
            return None
        except Exception as err:
            _LOGGER.error(f"Unexpected error during data fetch: {repr(err)}")
            return data

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            if (raw_data := await self.async_get_raw_data()) is None:
                self._status = CoordinatorStatus.READ_FAILED
                return dict(self._last_checked_data)

            result = self._energy.validate_totals(
                raw_data, self._last_checked_data, self._last_checked_time
            )
            result, is_daily_reset = self._energy.derive_daily(result)
            result.update(self._energy.raw_daily_values(raw_data))
            calculated_results = calculate_derived_values(
                TelemetryData.from_mapping(result),
                calculate_solar_power=self._ena_calc_solar_power,
                startup_voltage=self.inverter_model.startup_voltage,
                max_battery_charge_power=MAX_BATTERY_CHARGED_POWER,
                max_battery_discharge_power=MAX_BATTERY_DISCHARGED_POWER,
            )
            result.update(calculated_results)
            result = self._energy.clamp_calculated(
                result, self._last_checked_data, is_daily_reset=is_daily_reset
            )

            self._last_checked_data = dict(result)
            self._last_checked_time = dt.now()
            self._status = CoordinatorStatus.SUCCESS
            if self._store is not None:
                self._store.async_delay_save(self._persisted_state, STATE_SAVE_DELAY_S)

            return dict(result)
        except UpdateFailed:  # noqa: BLE001
            self._status = CoordinatorStatus.RECONNECT_FAILED
            raise UpdateFailed(
                "Reconnect attempts failed! Integration stopped. Retry after 120s.",
                retry_after=120,
            )
        except Exception as err:
            self._status = CoordinatorStatus.PROCESSING_FAILED
            _LOGGER.error(f"Unexpected error during data fetch: {repr(err)}")
            return None

    async def async_write_modbus_register(
        self, entity_def: NumberWritableDef, value: int
    ) -> None:
        """Universal method to write a 16-bit unsigned integer to any Modbus register."""
        if not self._client or not self.connected:
            _LOGGER.error("Modbus client is not initialized")
            return

        target_value = int(value)

        register_address = entity_def.register
        key = entity_def.read_key

        _LOGGER.debug(
            "Sending Modbus write command [FC6]: value %s to address %s (Key: %s, Device ID: %s)",
            target_value,
            register_address,
            key,
            self._client_slave_id,
        )

        try:
            async with self._lock:
                # Execute write single register operation
                response = await self._client.write_register(
                    address=register_address,
                    value=target_value,
                    device_id=self._client_slave_id,
                )

                if response.isError():
                    _LOGGER.error(
                        "Modbus error response when writing to register %s: %s",
                        register_address,
                        response,
                    )
                    raise HomeAssistantError(
                        f"Modbus rejected write operation for register {register_address}: {response}"
                    )

                readback_response = await self._client.read_holding_registers(
                    address=register_address,
                    count=1,
                    device_id=self._client_slave_id,
                )
                if readback_response.isError():
                    raise HomeAssistantError(
                        f"Could not verify write to register {register_address}: {readback_response}"
                    )

                readback_value = readback_response.registers[0]

            if readback_value != target_value:
                raise HomeAssistantError(
                    f"Register {register_address} acknowledged value {target_value}, "
                    f"but read back {readback_value}"
                )

            _LOGGER.info(
                "Register %s [%s] successfully updated to value: %s",
                register_address,
                key,
                target_value,
            )

            updated_data = {**(self.data or {}), key: target_value}
            self.async_set_updated_data(updated_data)
        except Exception as err:
            _LOGGER.error(
                "Failed to write to register %s via Modbus TCP: %s",
                entity_def.register,
                err,
            )
            raise HomeAssistantError(f"Error writing data to inverter: {err}")
