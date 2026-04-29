"""DataUpdateCoordinator for EcoFlow PowerOcean Plus."""
from __future__ import annotations

import logging
import struct
from datetime import timedelta

from pymodbus.client import ModbusTcpClient

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, PV_CURRENT_THRESHOLD, REG_STATUS

_LOGGER = logging.getLogger(__name__)

# Block start addresses
_REG_SERIAL        = 40004   # Serial number + operation mode
_REG_MAIN          = 40519   # house_con, grid, solar, battery, soc, bat_cap, limits …
_REG_BAT_DETAIL    = 40574   # Battery voltage, current, temperature
_REG_AC_PV         = 40580   # Grid voltages/currents, frequency, apparent power,
                              # PV global voltage, inverter temp, PV string currents
_REG_ENERGY        = 42161   # kWh counters


class EcoflowCoordinator(DataUpdateCoordinator):
    """Fetches data from EcoFlow PowerOcean Plus via Modbus TCP."""

    def __init__(self, hass: HomeAssistant, host: str, port: int, battery_capacity: float, scan_interval: int, pv_strings: int) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.host = host
        self.port = port
        self._battery_capacity = battery_capacity
        self._pv_strings = pv_strings
        self._client: ModbusTcpClient | None = None
        self._last_energy = {}

    # ------------------------------------------------------------------
    # Modbus helpers
    # ------------------------------------------------------------------

    def _disconnect(self) -> None:
        """Close and discard the current client so the next poll reconnects cleanly."""
        try:
            if self._client is not None:
                self._client.close()
        except Exception:  # noqa: BLE001
            pass
        self._client = None

    def _get_client(self) -> ModbusTcpClient:
        if self._client is None or not self._client.connected:
            self._disconnect()
            self._client = ModbusTcpClient(self.host, port=self.port, timeout=5)
            self._client.unit_id = 1
            if not self._client.connect():
                raise ConnectionError(f"Modbus TCP connect failed ({self.host}:{self.port})")
            _LOGGER.debug("Modbus TCP connected to %s:%s", self.host, self.port)
        return self._client

    def _read_block(self, addr: int, count: int) -> list[int] | None:
        """Read *count* holding registers starting at *addr*.  Returns None on error."""
        try:
            res = self._get_client().read_holding_registers(addr, count=count)
            if res and not res.isError():
                _LOGGER.debug("Block 0x%04X(%d): %s", addr, count, res.registers)
                return res.registers
            # Modbus error response – connection may be stale
            _LOGGER.debug("Modbus error response at 0x%04X, resetting connection", addr)
            self._disconnect()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Block read error at 0x%04X: %s – resetting connection", addr, exc)
            self._disconnect()
        return None

    @staticmethod
    def _f(regs: list[int], offset: int) -> float:
        """Decode a word-swapped 32-bit IEEE 754 float from two 16-bit registers."""
        if regs is None or len(regs) < offset + 2:
            return 0.0
        raw = struct.pack("<HH", regs[offset], regs[offset + 1])
        value = struct.unpack("<f", raw)[0]
        if abs(value) > 1e9 or value != value:   # guard against NaN / inf
            return 0.0
        return round(value, 3)


    # ------------------------------------------------------------------
    # Plausibility helpers
    # ------------------------------------------------------------------

    def _safe_energy(self, key: str, new: float, max_delta: float):
        old = self._last_energy.get(key)
        if old is None:
            self._last_energy[key] = new
            return new

        if abs(new - old) > max_delta:
            _LOGGER.debug("Rejecting spike in %s: %s -> %s", key, old, new)
            return old

        self._last_energy[key] = new
        return new

    def _read_block_stable(self, addr: int, count: int, retries: int = 2):
        prev = None
        for _ in range(retries):
            curr = self._read_block(addr, count)
            if curr is None:
                return None
            if prev == curr:
                return curr
            prev = curr
        return prev

    # ------------------------------------------------------------------
    # Data fetch
    # ------------------------------------------------------------------

    def _fetch_all(self) -> dict:
        data: dict = {}

        # ── Heartbeat: verify device is reachable before reading all blocks ──
        try:
            hb = self._get_client().read_holding_registers(REG_STATUS, count=1)
            if hb is None or hb.isError():
                raise ConnectionError("Heartbeat register read failed")
            _LOGGER.debug("Heartbeat OK (reg %s = %s)", REG_STATUS, hb.registers[0])
        except Exception as exc:
            _LOGGER.warning("PowerOcean heartbeat failed: %s – will retry next poll", exc)
            self._disconnect()
            raise

        # ── Block A: Serial number + operation mode (40004, 12 regs) ──────────
        a = self._read_block_stable(_REG_SERIAL, 12)
        if a:
            # Serial number is ASCII-encoded across registers 0-7 (2 chars each)
            sn = "".join(
                chr((r >> 8) & 0xFF) + chr(r & 0xFF) for r in a[0:8]
            )
            data["serial_number"]  = sn.strip().replace("\x00", "")
            data["operation_mode"] = a[9] if len(a) > 9 else None

        # ── Block B: Main power values (40519, 34 regs) ──────────────────────
        b = self._read_block_stable(_REG_MAIN, 30)   # 40519–40548, last needed index = 29
        if b:
            _LOGGER.debug(
                "Block B raw (40519+): house=(%04X,%04X) grid=(%04X,%04X) solar=(%04X,%04X) bat=(%04X,%04X)",
                b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7],
            )
            data["house_power"]       = self._f(b, 0)        # 40519 ✅
            data["grid_power"]        = self._f(b, 2)        # 40521 ✅
            data["solar_power"]       = max(self._f(b, 4), 0.0)  # 40523 ✅
            data["battery_power"]     = self._f(b, 6)        # 40525 ✅
            data["battery_soc"]       = float(b[8])          # 40527 – INT16, % ✅
            data["battery_capacity"]  = self._battery_capacity  # user-configured kWh
            data["bat_remaining"]     = round(
                self._battery_capacity * data["battery_soc"] / 100, 2
            )
            data["inverter_ac_power"] = float(b[11])         # 40530 – INT16, W ✅
            data["min_soc_limit"]     = float(b[17])         # 40536 – INT16, % ✅
            data["bat_temp_warn_max"] = float(b[21])         # 40540 – INT16, °C ✅
            data["bat_temp_warn_min"] = float(b[22])         # 40541 – INT16, °C ✅
            data["limit_inv_power"]   = float(b[27])         # 40546 – INT16, W ✅
            data["limit_inv_max"]     = float(b[29])         # 40548 – INT16, W ✅
            # 40550 / 40552 – unreliable, calculated from module count instead
            num_modules = self._battery_capacity / 5.0
            data["limit_discharge"]   = round(num_modules * 3300)  # 3.3 kW per module
            data["limit_charge"]      = round(num_modules * 2500)  # 2.5 kW per module

        # ── Block C: Battery detail (40574, 6 regs) ───────────────────────────
        c = self._read_block_stable(_REG_BAT_DETAIL, 6)
        if c:
            data["battery_voltage"]     = self._f(c, 0)   # 40574 ✅
            data["battery_current"]     = self._f(c, 2)   # 40576 ✅
            data["battery_temperature"] = self._f(c, 4)   # 40578 – ⚠️ not in verified list

        # ── Block D: AC grid + PV strings (40580, 28 regs → up to 40607) ──────
        d = self._read_block_stable(_REG_AC_PV, 28)
        if d:
            data["voltage_l1"]           = self._f(d, 0)    # 40580 ✅
            data["voltage_l2"]           = self._f(d, 2)    # 40582 ✅
            data["voltage_l3"]           = self._f(d, 4)    # 40584 ✅
            data["current_l1"]           = self._f(d, 6)    # 40586 ✅
            data["current_l2"]           = self._f(d, 8)    # 40588 ✅
            data["current_l3"]           = self._f(d, 10)   # 40590 ✅
            data["inverter_temperature"] = self._f(d, 12)   # 40592 ✅
            data["frequency"]            = self._f(d, 14)   # 40594 ✅
            data["apparent_power"]       = self._f(d, 16)   # 40596 ✅
            v_pv_global                  = self._f(d, 18)   # 40598 ✅
            data["pv_voltage"]           = v_pv_global
            # 40600–40601 (offset 20-21): not in verified register list
            # Apply threshold to filter phantom currents, zero out unconfigured strings
            def _pv_current(raw: float, string_nr: int) -> float:
                if string_nr > self._pv_strings:
                    return 0.0
                return raw if raw >= PV_CURRENT_THRESHOLD else 0.0

            data["pv1_current"] = _pv_current(self._f(d, 22), 1)   # 40602 ✅
            data["pv2_current"] = _pv_current(self._f(d, 24), 2)   # 40604 ✅
            data["pv3_current"] = _pv_current(self._f(d, 26), 3)   # 40606 ⚠️ not in verified list

            # Calculated PV power per string (current × global PV voltage)
            data["pv1_power"] = round(data["pv1_current"] * v_pv_global, 1)
            data["pv2_power"] = round(data["pv2_current"] * v_pv_global, 1)
            data["pv3_power"] = round(data["pv3_current"] * v_pv_global, 1)

            # Solar power: sum of active strings only
            data["solar_power"] = round(
                sum(data[f"pv{i}_power"] for i in range(1, self._pv_strings + 1)), 1
            )

            # Grid power: if register 40521 gave 0, derive from energy balance as fallback
            if data.get("grid_power", 0) == 0.0:
                house  = data.get("house_power", 0)
                solar  = data.get("solar_power", 0)
                bat    = data.get("battery_power", 0)
                if any(v != 0 for v in [house, solar, bat]):
                    data["grid_power"] = round(house - solar + bat, 1)
                    _LOGGER.debug("grid_power derived from balance: %.1f W", data["grid_power"])

        # ── Block E: Energy counters (42161, 100 regs) ────────────────────────
        # Offsets = register_address - 42161
        e = self._read_block_stable(_REG_ENERGY, 100)
        if e:
            # Grid (can be high due to house load)
            data["grid_import_total"] = self._safe_energy(
                "grid_import_total",
                self._f(e, 0),
                max_delta=0.6,
            )
            data["grid_import_today"] = self._safe_energy(
                "grid_import_today",
                self._f(e, 2),
                max_delta=0.6,
            )
            data["grid_export_total"] = self._safe_energy(
                "grid_export_total",
                self._f(e, 16),
                max_delta=0.6,
            )
            data["grid_export_today"] = self._safe_energy(
                "grid_export_today",
                self._f(e, 18),
                max_delta=0.6,
            )

            # Battery (charge/discharge limited)
            data["bat_charged_total"] = self._safe_energy(
                "bat_charged_total",
                self._f(e, 64),
                max_delta=0.2,
            )
            data["bat_charge_today"] = self._safe_energy(
                "bat_charge_today",
                self._f(e, 66),
                max_delta=0.2,
            )
            data["bat_discharged_total"] = self._safe_energy(
                "bat_discharged_total",
                self._f(e, 80),
                max_delta=0.2,
            )
            data["bat_discharge_today"] = self._safe_energy(
                "bat_discharge_today",
                self._f(e, 82),
                max_delta=0.2,
            )

            # Solar (inverter-limited)
            data["solar_total"] = self._safe_energy(
                "solar_total",
                self._f(e, 96),
                max_delta=0.2,
            )
            data["solar_today"] = self._safe_energy(
                "solar_today",
                self._f(e, 98),
                max_delta=0.2,
            )

            # Derived: battery net energy
            data["bat_net_energy"] = round(
                data["bat_charged_total"] - data["bat_discharged_total"], 2
            )

            # Derived: house consumption (no dedicated register – calculated from energy balance)
            data["house_energy_today"] = round(
                data.get("solar_today", 0)
                + data.get("grid_import_today", 0)
                - data.get("grid_export_today", 0)
                - data.get("bat_charge_today", 0)
                + data.get("bat_discharge_today", 0),
                2,
            )
            data["house_energy_total"] = round(
                data.get("solar_total", 0)
                + data.get("grid_import_total", 0)
                - data.get("grid_export_total", 0)
                - data.get("bat_charged_total", 0)
                + data.get("bat_discharged_total", 0),
                0,
            )

        # Plausibility check: if no solar production data at all, something is likely wrong with the readout (e.g. stale connection) – discard all data to avoid misleading values
        if data["solar_total"] == 0:
            raise ValueError("No solar production data read – data seems implausible and will be skipped")
        
        return data

    async def _async_update_data(self) -> dict:
        try:
            return await self.hass.async_add_executor_job(self._fetch_all)
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Modbus read error: {err}") from err
        finally:
            await self.hass.async_add_executor_job(self._disconnect)
