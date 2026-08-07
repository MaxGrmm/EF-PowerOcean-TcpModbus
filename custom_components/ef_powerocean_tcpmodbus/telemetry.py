"""Decode and derive PowerOcean telemetry values."""

from __future__ import annotations

import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TelemetryData:
    """Raw telemetry values used to calculate derived values."""

    battery_soc: float | None = None
    battery_count: float | None = None
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

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, float | None]
    ) -> "TelemetryData":
        """Create calculation input from the coordinator's raw telemetry."""
        return cls(
            battery_soc=data.get("battery_soc"),
            battery_count=data.get("battery_count"),
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


def decode_register(
    registers: list[int], register_index: int, register_size: int
) -> float | None:
    """Decode a register value, including word-swapped IEEE 754 floats."""
    if not registers:
        return None
    if register_size == 1:
        return round(float(registers[register_index]), 2)
    if len(registers) < register_index + 2:
        return None

    try:
        raw = struct.pack(
            "<HH", registers[register_index], registers[register_index + 1]
        )
        value = struct.unpack("<f", raw)[0]
    except (struct.error, TypeError):
        return None

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


def calculate_derived_values(
    data: TelemetryData,
    *,
    calculate_solar_power: bool,
    daily_reset_complete: bool,
    startup_voltage: int,
    max_battery_charge_power: float,
    max_battery_discharge_power: float,
) -> dict[str, float | bool | None]:
    """Calculate values derived from raw PowerOcean telemetry."""
    calculated: dict[str, float | bool | None] = {}

    battery_soc = data.battery_soc
    battery_count = data.battery_count
    calculated["bat_remaining"] = (
        round(battery_count * 5 * battery_soc / 100, 2)
        if battery_soc is not None and battery_count is not None
        else None
    )
    calculated["limit_discharge"] = (
        round(battery_count * max_battery_discharge_power)
        if battery_count is not None
        else None
    )
    calculated["limit_charge"] = (
        round(battery_count * max_battery_charge_power)
        if battery_count is not None
        else None
    )

    battery_charged_total = data.bat_charged_total
    battery_discharged_total = data.bat_discharged_total
    calculated["bat_net_energy"] = (
        round(battery_charged_total - battery_discharged_total, 2)
        if battery_charged_total is not None
        and battery_discharged_total is not None
        else None
    )

    if daily_reset_complete:
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
        current = data.get(f"pv{pv_number}_current")
        voltage = data.get(f"pv{pv_number}_voltage")
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
        calculated["battery_saver_mode_ena"] = _is_bit_set(int(data.system_modes), 3)
        calculated["self_use_mode_ena"] = _is_bit_set(int(data.system_modes), 4)
        calculated["intelligent_mode_ena"] = _is_bit_set(int(data.system_modes), 5)

    return calculated

