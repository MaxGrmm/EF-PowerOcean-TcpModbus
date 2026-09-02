"""Decode and derive PowerOcean telemetry values."""

from __future__ import annotations

import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass

from .models import REGISTER_SIZES, GridMode, OperatingMode, RegisterType


def decode_serial_number(registers: list[int] | None) -> str | None:
    """Decode a serial number from Modbus registers."""
    if not registers:
        return None

    serial_number = (
        "".join(
            chr((register >> 8) & 0xFF) + chr(register & 0xFF) for register in registers
        )
        .strip()
        .replace("\x00", "")
    )
    return serial_number or None


def decode_firmware_version(registers: list[int] | None) -> str | None:
    """Decode the UINT32 firmware version, low word first, as a dotted string."""
    if not registers or len(registers) < 2:
        return None

    firmware = (registers[1] << 16) | registers[0]
    if not firmware:
        return None

    return ".".join(str((firmware >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def is_modbus_disabled(
    serial_number: str | None, inverter_temperature: float | None
) -> bool:
    """Return whether Modbus responds but telemetry appears disabled."""
    return bool(
        serial_number and serial_number != "unknown" and inverter_temperature == 0
    )


@dataclass(frozen=True, slots=True)
class TelemetryData:
    """Raw telemetry values used to calculate derived values."""

    battery_soc: float | None = None
    bat_charged_total: float | None = None
    bat_discharged_total: float | None = None
    solar_today: float | None = None
    grid_import_today: float | None = None
    bat_discharged_today: float | None = None
    grid_export_today: float | None = None
    bat_charged_today: float | None = None
    solar_total: float | None = None
    grid_import_total: float | None = None
    grid_export_total: float | None = None
    pv1_current: float | None = None
    pv1_voltage: float | None = None
    pv2_current: float | None = None
    pv2_voltage: float | None = None
    pv3_current: float | None = None
    pv3_voltage: float | None = None
    system_modes: float | None = None
    battery_capacity: float | None = None
    fault_codes: tuple[float | None, ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, float | None]) -> TelemetryData:
        """Create calculation input from the coordinator's raw telemetry."""
        faults = [
            (int(key.removeprefix("fault_")), value)
            for key, value in data.items()
            if key.startswith("fault_") and key.removeprefix("fault_").isdigit()
        ]
        return cls(
            battery_soc=data.get("battery_soc"),
            bat_charged_total=data.get("bat_charged_total"),
            bat_discharged_total=data.get("bat_discharged_total"),
            solar_today=data.get("solar_today"),
            grid_import_today=data.get("grid_import_today"),
            bat_discharged_today=data.get("bat_discharged_today"),
            grid_export_today=data.get("grid_export_today"),
            bat_charged_today=data.get("bat_charged_today"),
            solar_total=data.get("solar_total"),
            grid_import_total=data.get("grid_import_total"),
            grid_export_total=data.get("grid_export_total"),
            pv1_current=data.get("pv1_current"),
            pv1_voltage=data.get("pv1_voltage"),
            pv2_current=data.get("pv2_current"),
            pv2_voltage=data.get("pv2_voltage"),
            pv3_current=data.get("pv3_current"),
            pv3_voltage=data.get("pv3_voltage"),
            system_modes=data.get("system_modes"),
            battery_capacity=data.get("battery_capacity"),
            fault_codes=tuple(value for _, value in sorted(faults)),
        )


def _is_bit_set(value: int, bit_position: int) -> bool:
    return bool(value & (1 << bit_position))


def _calculate_house_energy(
    *,
    solar: float | None,
    grid_import: float | None,
    battery_discharged: float | None,
    grid_export: float | None,
    battery_charged: float | None,
    precision: int,
) -> float | None:
    values = (
        solar,
        grid_import,
        battery_discharged,
        grid_export,
        battery_charged,
    )
    if any(value is None for value in values):
        return None

    return round(
        solar + grid_import + battery_discharged - grid_export - battery_charged,
        precision,
    )


def decode_register(registers: list[int], data_type: RegisterType) -> float | None:
    """Decode a register's words, which are stored low word first."""
    if len(registers) < REGISTER_SIZES[data_type]:
        return None
    if data_type is RegisterType.UINT16:
        return round(float(registers[0]), 2)

    try:
        raw = struct.pack("<HH", registers[0], registers[1])
    except (struct.error, TypeError):
        return None

    if data_type is RegisterType.UINT32:
        return float(struct.unpack("<I", raw)[0])

    value = struct.unpack("<f", raw)[0]
    if not math.isfinite(value) or abs(value) > 1e9:
        return None
    return round(value, 2)


def _calculate_pv_power(
    current: float | None, voltage: float | None, startup_voltage: int
) -> float | None:
    if current is None or voltage is None:
        return None

    if voltage < startup_voltage:
        return 0.0

    return round(current * voltage, 1)


_OPERATING_MODES = {
    0: OperatingMode.STANDBY,
    1: OperatingMode.SELF_CONSUMPTION,
    2: OperatingMode.AI,
}


def _format_active_faults(fault_codes: tuple[float | None, ...]) -> str:
    active = [f"0x{int(code):04X}" for code in fault_codes if code]
    return ", ".join(active) if active else "none"


def calculate_derived_values(
    data: TelemetryData,
    *,
    calculate_solar_power: bool,
    startup_voltage: int,
) -> dict[str, float | bool | str | None]:
    """Calculate values derived from raw PowerOcean telemetry."""
    calculated: dict[str, float | bool | str | None] = {}

    battery_soc = data.battery_soc
    # The device reports its pack capacity in Wh.
    battery_capacity = data.battery_capacity
    calculated["bat_remaining"] = (
        round(battery_capacity / 1000 * battery_soc / 100, 2)
        if battery_soc is not None and battery_capacity is not None
        else None
    )

    battery_charged_total = data.bat_charged_total
    battery_discharged_total = data.bat_discharged_total
    calculated["bat_net_energy"] = (
        round(battery_charged_total - battery_discharged_total, 2)
        if battery_charged_total is not None and battery_discharged_total is not None
        else None
    )

    calculated["house_energy_today"] = _calculate_house_energy(
        solar=data.solar_today,
        grid_import=data.grid_import_today,
        battery_discharged=data.bat_discharged_today,
        grid_export=data.grid_export_today,
        battery_charged=data.bat_charged_today,
        precision=2,
    )

    calculated["house_energy_total"] = _calculate_house_energy(
        solar=data.solar_total,
        grid_import=data.grid_import_total,
        battery_discharged=data.bat_discharged_total,
        grid_export=data.grid_export_total,
        battery_charged=data.bat_charged_total,
        precision=0,
    )

    for pv_number in range(1, 4):
        current = getattr(data, f"pv{pv_number}_current")
        voltage = getattr(data, f"pv{pv_number}_voltage")
        calculated[f"pv{pv_number}_power"] = _calculate_pv_power(
            current,
            voltage,
            startup_voltage,
        )

    if calculate_solar_power:
        pv_power_values = tuple(
            calculated[f"pv{pv_number}_power"] for pv_number in range(1, 4)
        )
        calculated["solar_power"] = (
            sum(pv_power_values)
            if all(value is not None for value in pv_power_values)
            else None
        )

    if data.system_modes is not None:
        system_modes = int(data.system_modes)
        calculated["grid_mode"] = (
            GridMode.ISLANDED if _is_bit_set(system_modes, 0) else GridMode.GRID
        )
        calculated["system_fault"] = _is_bit_set(system_modes, 1)
        calculated["system_power_on"] = _is_bit_set(system_modes, 2)
        calculated["battery_saver_mode_ena"] = _is_bit_set(system_modes, 3)
        calculated["self_use_mode_ena"] = _is_bit_set(system_modes, 4)
        calculated["intelligent_mode_ena"] = _is_bit_set(system_modes, 5)
        calculated["operating_mode"] = _OPERATING_MODES.get(
            (system_modes >> 4) & 0b111, OperatingMode.UNKNOWN
        )

    calculated["active_faults"] = _format_active_faults(data.fault_codes)

    return calculated
