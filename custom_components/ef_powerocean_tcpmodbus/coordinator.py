"""DataUpdateCoordinator for EcoFlow PowerOcean Plus."""

from __future__ import annotations

import asyncio
import logging
import struct

from typing import Any
from datetime import timedelta
from collections import deque

from datetime import datetime
from pymodbus import __version__ as pyModbusVersion
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from homeassistant.util import dt
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    CONF_HOST,
    CONF_PORT,
    CONF_BATTERY_COUNT,
    CONF_MAX_BATTERY_CHARGED_POWER,
    CONF_MAX_BATTERY_DISCHARGED_POWER,
    CONF_MAX_GRID_POWER,
    CONF_MAX_SOLAR_POWER,
    CONF_SCAN_INTERVAL,
    CONF_CALC_SOLAR_POWER,
    PV_VOLTAGE_THRESHOLD,
    DEFAULT_SLAVE,
    ENERGY_SENSOR_MAP,
    MOD_REGISTER_MAP,
    DEFAULT_PORT,
    DEFAULT_MAX_POWER,
    DEFAULT_BATTERY_COUNT,
    DEFAULT_MAX_GRID_POWER,
    DEFAULT_MAX_SOLAR_POWER,
    DEFAULT_SCAN_INTERVAL,
    MAX_BATTERY_CHARGED_POWER,
    MAX_BATTERY_DISCHARGED_POWER,
)

_LOGGER = logging.getLogger(__name__)

SLEEP_TIME_AFTER_RECONNECT = 1
SLEEP_TIME_AFTER_BATTERY_CHECK_FAILED = 15


def getBit(value: int, bitpos: int) -> bool:
    return (value & (2**bitpos)) == 2**bitpos


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
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=self.scan_interval),
        )

        self.serial_number: str | None = None
        self._client: AsyncModbusTcpClient = AsyncModbusTcpClient(
            host=self.host, port=self.port, timeout=5, reconnect_delay=0, retries=0
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

    @staticmethod
    def _decode_register(
        regs: list[int], register_index: int, register_size: int
    ) -> float:
        """Decode a word-swapped 32-bit IEEE 754 float from two 16-bit registers."""
        if not regs:
            return None
        elif register_size == 1:
            return round(float(regs[register_index]), 2)
        elif len(regs) < register_index + 2:
            return None

        try:
            raw = struct.pack("<HH", regs[register_index], regs[register_index + 1])
            value = struct.unpack("<f", raw)[0]
        except struct.error, TypeError:  # ab Python 3.14 ist ohne Klammern der Standard
            return None

        if abs(value) > 1e9 or value != value:  # guard against NaN / inf
            return None
        return round(value, 2)

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
        else:
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
                    decode_value = self._decode_register(
                        raw, register.block_index, register.size
                    )
                    data[register.key] = decode_value

            if data["battery_count"] != self.limits[CONF_BATTERY_COUNT]:
                _LOGGER.debug(
                    f"Readed battery count {data['battery_count']} is unequal -> Skip data! Wait {SLEEP_TIME_AFTER_BATTERY_CHECK_FAILED}s."
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
                f"Last checked time or last checked data is None. Return current data."
            )
            return result
        elif (now - self._last_checked_time).total_seconds() < 1:
            _LOGGER.debug(
                f"dt is less then one secend. Return last data. Delta-t: {(now - self._last_checked_time).total_seconds()}"
            )
            return dict(self._last_checked_data)

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
            elif (
                energy_sensor.reset_at_midnight
                and current_energy == 0
                and last_energy > 0
                and now.hour == 0
                and now.minute < 1
            ):
                # Reset nur zwischen 00:00 und 00:01 erlauben
                _LOGGER.debug(f"Reset of entity {energy_sensor.key}")
                if self._count_reset_energy_finished == self._count_reset_energy_sensor:
                    # first counter reset after midnight
                    self._count_reset_energy_finished = 0
                result[energy_sensor.key] = 0
                self._check_monotonic = False
                self._count_reset_energy_finished += 1
            else:
                dt_hours = (now - self._last_checked_time).total_seconds() / 3600
                # Nur innerhalb einer 1h Stunde prüfen, danach ist das Gap zu groß
                if 0 < dt_hours <= 1:
                    # Anstieg berechnen
                    energy_delta = current_energy - last_energy
                    calculated_power = energy_delta / dt_hours
                    limit = self.limits.get(energy_sensor.max_power, DEFAULT_MAX_POWER)
                    if calculated_power > limit:
                        # positiver Anstieg und Leistung über Max-Leistung
                        _LOGGER.warning(
                            f"Skip entire data. Reason: {energy_sensor.key}! (raw energy: {current_energy} last energy: {last_energy} delta energy: {round(energy_delta, 2)} dt: {dt_hours} power: {int(calculated_power)} limit: {limit} last check: {self._last_checked_time.time()})"
                        )
                        return dict(self._last_checked_data)
                    else:
                        # negativer Anstieg oder unterhalb der Max-Leistung
                        if current_energy == 0 and last_energy > 0:
                            _LOGGER.warning(
                                f"Skip entire data. Reason: 0 kWh of {energy_sensor.key}! (raw energy: {current_energy} last energy: {last_energy} delta energy: {round(energy_delta, 2)} dt: {dt_hours} power: {int(calculated_power)} limit: {limit} last check: {self._last_checked_time.time()})"
                            )
                            return dict(self._last_checked_data)
                        # Rückgabe des aktuellen Wertes nur wenn der neue Wert > letzter Wert ist
                        result[energy_sensor.key] = (
                            current_energy
                            if current_energy >= last_energy
                            else last_energy
                        )
                else:
                    _LOGGER.debug(
                        f"Time window is too large of entity {energy_sensor.key}! (raw energy: {current_energy} last energy: {last_energy} delta energy: {round(energy_delta, 4)} dt: {dt_hours} power: {int(calculated_power)} limit: {energy_sensor.max_power} last check: {self._last_checked_time.time()})"
                    )

        return result

    def _get_calculated_values(self, data: dict[str, Any]) -> dict[str, Any]:
        calc_data: dict[str, Any] = {}

        battery_soc = data.get("battery_soc", None)
        battery_count = data.get("battery_count", None)
        calc_data["bat_remaining"] = (
            round(battery_count * 5 * battery_soc / 100, 2)
            if battery_soc is not None and battery_count is not None
            else None
        )
        calc_data["limit_discharge"] = (
            round(battery_count * MAX_BATTERY_DISCHARGED_POWER)
            if battery_count is not None
            else None
        )
        calc_data["limit_charge"] = (
            round(battery_count * MAX_BATTERY_CHARGED_POWER)
            if battery_count is not None
            else None
        )
        bat_charged_total = data.get("bat_charged_total", None)
        bat_discharged_total = data.get("bat_discharged_total", None)
        calc_data["bat_net_energy"] = (
            round(bat_charged_total - bat_discharged_total, 2)
            if bat_charged_total is not None and bat_discharged_total is not None
            else None
        )

        # house energy calculation
        if self._count_reset_energy_finished == self._count_reset_energy_sensor:
            # Berechnung erst wenn alle Werte zurückgesetzt wurden
            solar_today = data.get("solar_today", None)
            grid_import_today = data.get("grid_import_today", None)
            grid_export_today = data.get("grid_export_today", None)
            bat_charged_today = data.get("bat_charged_today", None)
            bat_discharged_today = data.get("bat_discharged_today", None)
            calc_data["house_energy_today"] = (
                round(
                    solar_today
                    + grid_import_today
                    + bat_discharged_today
                    - grid_export_today
                    - bat_charged_today,
                    2,
                )
                if solar_today is not None
                and grid_import_today is not None
                and bat_discharged_today is not None
                and grid_export_today is not None
                and bat_charged_today is not None
                else None
            )

        solar_total = data.get("solar_total", None)
        grid_import_total = data.get("grid_import_total", None)
        grid_export_total = data.get("grid_export_total", None)
        calc_data["house_energy_total"] = (
            round(
                solar_total
                + grid_import_total
                + bat_discharged_total
                - grid_export_total
                - bat_charged_total,
                0,
            )
            if solar_total is not None
            and grid_import_total is not None
            and bat_discharged_total is not None
            and grid_export_total is not None
            and bat_charged_total is not None
            else None
        )

        pv1_current = data.get("pv1_current", None)
        pv1_voltage = data.get("pv1_voltage", None)
        pv2_current = data.get("pv2_current", None)
        pv2_voltage = data.get("pv2_voltage", None)
        pv3_current = data.get("pv3_current", None)
        pv3_voltage = data.get("pv3_voltage", None)
        calc_data["pv1_power"] = (
            None
            if pv1_current is None or pv1_voltage is None
            else (
                0
                if pv1_voltage < PV_VOLTAGE_THRESHOLD
                else round(pv1_current * pv1_voltage, 1)
            )
        )
        calc_data["pv2_power"] = (
            None
            if pv2_current is None or pv2_voltage is None
            else (
                0
                if pv2_voltage < PV_VOLTAGE_THRESHOLD
                else round(pv2_current * pv2_voltage, 1)
            )
        )
        calc_data["pv3_power"] = (
            None
            if pv3_current is None or pv3_voltage is None
            else (
                0
                if pv3_voltage < PV_VOLTAGE_THRESHOLD
                else round(pv3_current * pv3_voltage, 1)
            )
        )

        if self._ena_calc_solar_power:
            pv1_power = calc_data.get("pv1_power", None)
            pv2_power = calc_data.get("pv2_power", None)
            pv3_power = calc_data.get("pv3_power", None)

            calc_data["solar_power"] = (
                None
                if pv1_power is None or pv2_power is None or pv3_power is None
                else pv1_power + pv2_power + pv3_power
            )

        system_mode = data.get("system_modes", None)
        if system_mode is not None:
            # Bit 3: Batteriesparmodus
            # Bit 4: Eigenstromversorgung
            # Bit 5: Intelligenter Modus
            calc_data["battery_saver_mode_ena"] = getBit(int(system_mode), 3)
            calc_data["self_use_mode_ena"] = getBit(int(system_mode), 4)
            calc_data["intelligent_mode_ena"] = getBit(int(system_mode), 5)

        return calc_data

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
            calculated_results = self._get_calculated_values(result)
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
