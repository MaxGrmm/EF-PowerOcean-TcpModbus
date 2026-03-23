"""DataUpdateCoordinator for EcoFlow PowerOcean Plus."""
from __future__ import annotations

import logging
import struct
from datetime import timedelta

from pymodbus.client import ModbusTcpClient

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN, DEFAULT_SCAN_INTERVAL,
    REG_STATUS, REG_SOC,
    REG_BAT_VOLTAGE, REG_BAT_CURRENT, REG_BAT_TEMP,
    REG_VOLTAGE_L1, REG_VOLTAGE_L2, REG_VOLTAGE_L3,
    REG_CURRENT_L1, REG_CURRENT_L2, REG_CURRENT_L3,
    REG_FREQUENCY, REG_APPARENT_POWER,
    REG_INV_TEMP, REG_PV1_CURRENT, REG_PV2_CURRENT, REG_PV3_CURRENT,
    REG_GRID_IMPORT_TODAY, REG_GRID_EXPORT_TODAY,
    REG_PV1_TODAY, REG_PV2_TODAY,
    REG_BAT_CHARGE_TODAY, REG_BAT_DISCHARGE_TODAY,
    REG_BAT_NET_ENERGY, REG_GRID_IMPORT_TOTAL, REG_GRID_EXPORT_TOTAL,
    REG_PV1_TOTAL, REG_PV2_TOTAL,
    REG_BAT_CHARGED_TOTAL, REG_BAT_REMAINING,
    REG_BAT_DISCHARGED_TOTAL, REG_TOTAL_ENERGY,
)

_LOGGER = logging.getLogger(__name__)


class EcoflowCoordinator(DataUpdateCoordinator):
    """Fetches data from EcoFlow PowerOcean Plus via Modbus TCP."""

    def __init__(self, hass: HomeAssistant, host: str, port: int) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.host = host
        self.port = port
        self._client: ModbusTcpClient | None = None

    def _get_client(self) -> ModbusTcpClient:
        if self._client is None or not self._client.connected:
            self._client = ModbusTcpClient(self.host, port=self.port, timeout=5)
            self._client.unit_id = 1
            self._client.connect()
        return self._client

    def _read_uint16(self, addr: int) -> int | None:
        try:
            r = self._get_client().read_holding_registers(addr, count=1)
            return r.registers[0] if not r.isError() else None
        except Exception as e:
            _LOGGER.debug("uint16 error at %s: %s", addr, e)
            return None

    def _read_float(self, addr: int, scale: float = 1.0) -> float | None:
        try:
            r = self._get_client().read_holding_registers(addr, count=2)
            if r.isError():
                return None
            raw = struct.pack(">HH", r.registers[1], r.registers[0])
            value = struct.unpack(">f", raw)[0]
            if abs(value) > 1e9 or value != value:
                return None
            return round(value * scale, 3)
        except Exception as e:
            _LOGGER.debug("float error at %s: %s", addr, e)
            return None

    async def _async_update_data(self) -> dict:
        try:
            data = await self.hass.async_add_executor_job(self._fetch_all)
            return data
        except Exception as err:
            raise UpdateFailed(f"Modbus read error: {err}") from err

    def _fetch_all(self) -> dict:

        bat_voltage = self._read_float(REG_BAT_VOLTAGE)
        bat_current = self._read_float(REG_BAT_CURRENT)
        bat_power = (
            round(bat_voltage * bat_current, 1)
            if bat_voltage is not None and bat_current is not None
            else None
        )

        u_l1 = self._read_float(REG_VOLTAGE_L1)
        u_l2 = self._read_float(REG_VOLTAGE_L2)
        u_l3 = self._read_float(REG_VOLTAGE_L3)
        i_l1 = self._read_float(REG_CURRENT_L1)
        i_l2 = self._read_float(REG_CURRENT_L2)
        i_l3 = self._read_float(REG_CURRENT_L3)

        # Inverter AC output power = sum of phase power (house + grid combined)
        # This is what the inverter pushes to the AC side in total.
        # NOTE: Grid power is NOT available as a dedicated Modbus register.
        # It is only accessible via the EcoFlow Cloud API.
        inverter_ac_power = None
        if all(v is not None for v in [u_l1, u_l2, u_l3, i_l1, i_l2, i_l3]):
            inverter_ac_power = round(
                (u_l1 * i_l1) + (u_l2 * i_l2) + (u_l3 * i_l3), 1
            )

        return {
            # Status
            "status":                self._read_uint16(REG_STATUS),
            "battery_soc":           self._read_uint16(REG_SOC),

            # Battery (all confirmed via Modbus)
            "battery_voltage":       bat_voltage,
            "battery_current":       bat_current,
            "battery_power":         bat_power,
            "battery_temperature":   self._read_float(REG_BAT_TEMP),
            "bat_remaining":         self._read_float(REG_BAT_REMAINING),

            # Solar (string currents confirmed, total via AC balance)
            "pv1_current":           self._read_float(REG_PV1_CURRENT),
            "pv2_current":           self._read_float(REG_PV2_CURRENT),
            "pv3_current":           self._read_float(REG_PV3_CURRENT),

            # AC Grid (voltages + currents confirmed, total AC power calculated)
            "voltage_l1":            u_l1,
            "voltage_l2":            u_l2,
            "voltage_l3":            u_l3,
            "current_l1":            i_l1,
            "current_l2":            i_l2,
            "current_l3":            i_l3,
            "frequency":             self._read_float(REG_FREQUENCY),
            "apparent_power":        self._read_float(REG_APPARENT_POWER, scale=10),
            "inverter_ac_power":     inverter_ac_power,

            # Inverter
            "inverter_temperature":  self._read_float(REG_INV_TEMP),

            # Energy today
            "grid_import_today":     self._read_float(REG_GRID_IMPORT_TODAY),
            "grid_export_today":     self._read_float(REG_GRID_EXPORT_TODAY),
            "pv1_today":             self._read_float(REG_PV1_TODAY),
            "pv2_today":             self._read_float(REG_PV2_TODAY),
            "bat_charge_today":      self._read_float(REG_BAT_CHARGE_TODAY),
            "bat_discharge_today":   self._read_float(REG_BAT_DISCHARGE_TODAY),

            # Energy lifetime
            "grid_import_total":     self._read_float(REG_GRID_IMPORT_TOTAL, scale=0.001),
            "grid_export_total":     self._read_float(REG_GRID_EXPORT_TOTAL, scale=0.001),
            "pv1_total":             self._read_float(REG_PV1_TOTAL),
            "pv2_total":             self._read_float(REG_PV2_TOTAL),
            "bat_charged_total":     self._read_float(REG_BAT_CHARGED_TOTAL),
            "bat_discharged_total":  self._read_float(REG_BAT_DISCHARGED_TOTAL),
            "bat_net_energy":        self._read_float(REG_BAT_NET_ENERGY),
            "total_energy":          self._read_float(REG_TOTAL_ENERGY),
        }
