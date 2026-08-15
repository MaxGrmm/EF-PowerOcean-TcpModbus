"""DataUpdateCoordinator for EcoFlow PowerOcean Plus."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
    DEFAULT_MAX_POWER,
    DEFAULT_MAX_SOLAR_POWER,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SLAVE,
    DOMAIN,
    ENERGY_SENSOR_MAP,
    MAX_BATTERY_CHARGED_POWER,
    MAX_BATTERY_DISCHARGED_POWER,
    MOD_REGISTER_MAP,
    InverterModel,
)
from .telemetry import TelemetryData, calculate_derived_values, decode_register

_LOGGER = logging.getLogger(__name__)

SLEEP_TIME_AFTER_RECONNECT: Final = 1
SLEEP_TIME_AFTER_BATTERY_CHECK_FAILED: Final = 15


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
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
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
        self._client: AsyncModbusTcpClient = AsyncModbusTcpClient(
            host=self.host, port=self.port, timeout=20, reconnect_delay=0, retries=0
        )
        self._client_slave_id = DEFAULT_SLAVE
        self._lock = asyncio.Lock()
        self._last_checked_data: dict[str, Any] = {}
        self._last_checked_time: datetime = None
        self._check_monotonic: bool = True
        self._count_reset_energy_sensor: int = 0
        for sensor in ENERGY_SENSOR_MAP:
            if sensor.reset_at_midnight:
                self._count_reset_energy_sensor += 1
        self._count_reset_energy_finished: int = self._count_reset_energy_sensor

    @property
    def connected(self) -> bool:
        return self._client.connected

    def get_pymodbus_version(self) -> str:
        return pyModbusVersion

    async def async_client_shutdown(self) -> None:
        """Integration-Shutdown, closing connection"""
        _LOGGER.info("PowerOcean Shutdown. Closing Connection!")
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

        sn = "".join(chr((r >> 8) & 0xFF) + chr(r & 0xFF) for r in raw)
        return sn.strip().replace("\x00", "")

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
                    await asyncio.sleep(SLEEP_TIME_AFTER_RECONNECT)
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

            if data["battery_count"] != self.limits[CONF_BATTERY_COUNT]:
                _LOGGER.debug(
                    f"Read battery count {data['battery_count']} is unequal -> Skip data! Wait {SLEEP_TIME_AFTER_BATTERY_CHECK_FAILED}s."
                )
                await asyncio.sleep(SLEEP_TIME_AFTER_BATTERY_CHECK_FAILED)
                return None

            return data
        except ModbusException as err:
            _LOGGER.debug(f"{err.string}. Connection closing...")
            self._client.close()
            return None
        except Exception as err:
            _LOGGER.error(f"Unexpected error during data fetch: {repr(err)}")
            return data

    def _sanitize_energy_values(self, data: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = dict(data)
        self._check_monotonic = True

        now = dt.now()
        if self._last_checked_time is None or self._last_checked_data is None:
            _LOGGER.debug(
                "Last checked time or last checked data is None. Return current data."
            )
            return result

        elapsed_seconds = (now - self._last_checked_time).total_seconds()
        if elapsed_seconds < 1:
            _LOGGER.debug(
                f"dt is less than one second. Return last data. Delta-t: {elapsed_seconds}"
            )
            return dict(self._last_checked_data)

        dt_hours = elapsed_seconds / 3600
        for energy_sensor in ENERGY_SENSOR_MAP:
            if energy_sensor.is_calculated:
                continue

            current_energy = result.get(energy_sensor.key, None)
            last_energy = self._last_checked_data.get(energy_sensor.key, None)
            if current_energy is None or last_energy is None:
                _LOGGER.debug(
                    f"Current energy or last energy is None of entity {energy_sensor.key}"
                )
                continue

            is_midnight_reset = (
                energy_sensor.reset_at_midnight
                and current_energy == 0
                and last_energy > 0
                and now.hour == 0
                and now.minute < 1
            )
            if is_midnight_reset:
                # Reset nur zwischen 00:00 und 00:01 erlauben
                _LOGGER.debug(f"Reset of entity {energy_sensor.key}")
                if self._count_reset_energy_finished == self._count_reset_energy_sensor:
                    # first counter reset after midnight
                    self._count_reset_energy_finished = 0
                result[energy_sensor.key] = 0
                self._check_monotonic = False
                self._count_reset_energy_finished += 1
                continue

            energy_delta = current_energy - last_energy
            calculated_power = energy_delta / dt_hours
            if dt_hours > 1:
                _LOGGER.debug(
                    f"Time window is too large of entity {energy_sensor.key}! (raw energy: {current_energy} last energy: {last_energy} delta energy: {round(energy_delta, 4)} dt: {dt_hours} power: {int(calculated_power)} limit: {energy_sensor.max_power} last check: {self._last_checked_time.time()})"
                )
                continue

            limit = self.limits.get(energy_sensor.max_power, DEFAULT_MAX_POWER)
            if calculated_power > limit:
                _LOGGER.warning(
                    f"Skip entire data. Reason: {energy_sensor.key}! (raw energy: {current_energy} last energy: {last_energy} delta energy: {round(energy_delta, 2)} dt: {dt_hours} power: {int(calculated_power)} limit: {limit} last check: {self._last_checked_time.time()})"
                )
                return dict(self._last_checked_data)

            if current_energy == 0 and last_energy > 0:
                _LOGGER.warning(
                    f"Skip entire data. Reason: 0 kWh of {energy_sensor.key}! (raw energy: {current_energy} last energy: {last_energy} delta energy: {round(energy_delta, 2)} dt: {dt_hours} power: {int(calculated_power)} limit: {limit} last check: {self._last_checked_time.time()})"
                )
                return dict(self._last_checked_data)

            # Rückgabe des aktuellen Wertes nur wenn der neue Wert > letzter Wert ist
            result[energy_sensor.key] = max(current_energy, last_energy)

        return result

    def _enforced_monotonic(self, data: dict[str, Any]) -> dict[str, Any]:
        for energy_senser in ENERGY_SENSOR_MAP:
            last = self._last_checked_data.get(energy_senser.key, None)
            current = data.get(energy_senser.key, None)
            if last is not None and current is not None and current < last:
                data[energy_senser.key] = last

        return data

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            if (raw_data := await self.async_get_raw_data()) is None:
                return None

            result = self._sanitize_energy_values(raw_data)
            calculated_results = calculate_derived_values(
                TelemetryData.from_mapping(result),
                calculate_solar_power=self._ena_calc_solar_power,
                daily_reset_complete=(
                    self._count_reset_energy_finished == self._count_reset_energy_sensor
                ),
                startup_voltage=self.inverter_model.startup_voltage,
                max_battery_charge_power=MAX_BATTERY_CHARGED_POWER,
                max_battery_discharge_power=MAX_BATTERY_DISCHARGED_POWER,
            )
            result.update(calculated_results)

            if self._check_monotonic:
                result = self._enforced_monotonic(result)

            self._last_checked_data = dict(result)
            self._last_checked_time = dt.now()

            return dict(result)
        except UpdateFailed:  # noqa: BLE001
            raise UpdateFailed(
                "Reconnect attempts failed! Integration stopped. Retry after 120s.",
                retry_after=120,
            )
        except Exception as err:
            _LOGGER.error(f"Unexpected error during data fetch: {repr(err)}")
            return None

    async def async_write_modbus_register(
        self, register_address: int, key: str, value: int
    ) -> None:
        """Universal method to write a 16-bit unsigned integer to any Modbus register."""
        if not self._client:
            _LOGGER.error("Modbus client is not initialized")
            return

        async with self._lock:
            try:
                from homeassistant.exceptions import HomeAssistantError

                target_value = int(value)

                _LOGGER.debug(
                    "Sending Modbus write command [FC6]: value %s to address %s (Key: %s, Device ID: %s)",
                    target_value,
                    register_address,
                    key,
                    self._client_slave_id,
                )

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

                _LOGGER.info(
                    "Register %s [%s] successfully updated to value: %s",
                    register_address,
                    key,
                    target_value,
                )

                # Update the coordinator cache data locally to update the user interface immediately
                if self.data is None:
                    self.data = {}

                if isinstance(self.data, dict):
                    self.data[key] = target_value

                # Notify entity platforms to refresh states across Home Assistant
                self.async_update_listeners()

            except Exception as err:
                _LOGGER.error(
                    "Failed to write to register %s via Modbus TCP: %s",
                    register_address,
                    err,
                )
                raise HomeAssistantError(f"Error writing data to inverter: {err}")
