"""Decode and derive PowerOcean telemetry values."""

from __future__ import annotations

import math
import struct
from typing import Any


def _is_bit_set(value: int, bit_position: int) -> bool:
    return bool(value & (1 << bit_position))


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
    data: dict[str, Any],
    *,
    calculate_solar_power: bool,
    daily_reset_complete: bool,
    startup_voltage: int,
    max_battery_charge_power: float,
    max_battery_discharge_power: float,
) -> dict[str, Any]:
    """Calculate values derived from raw PowerOcean telemetry."""
    calculated: dict[str, Any] = {}

    battery_soc = data.get("battery_soc")
    battery_count = data.get("battery_count")
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

    battery_charged_total = data.get("bat_charged_total")
    battery_discharged_total = data.get("bat_discharged_total")
    calculated["bat_net_energy"] = (
        round(battery_charged_total - battery_discharged_total, 2)
        if battery_charged_total is not None
        and battery_discharged_total is not None
        else None
    )

    if daily_reset_complete:
        daily_values = (
            data.get("solar_today"),
            data.get("grid_import_today"),
            data.get("bat_discharged_today"),
            data.get("grid_export_today"),
            data.get("bat_charged_today"),
        )
        calculated["house_energy_today"] = (
            round(
                daily_values[0]
                + daily_values[1]
                + daily_values[2]
                - daily_values[3]
                - daily_values[4],
                2,
            )
            if all(value is not None for value in daily_values)
            else None
        )

    total_values = (
        data.get("solar_total"),
        data.get("grid_import_total"),
        battery_discharged_total,
        data.get("grid_export_total"),
        battery_charged_total,
    )
    calculated["house_energy_total"] = (
        round(
            total_values[0]
            + total_values[1]
            + total_values[2]
            - total_values[3]
            - total_values[4],
            0,
        )
        if all(value is not None for value in total_values)
        else None
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

    system_mode = data.get("system_modes")
    if system_mode is not None:
        calculated["battery_saver_mode_ena"] = _is_bit_set(int(system_mode), 3)
        calculated["self_use_mode_ena"] = _is_bit_set(int(system_mode), 4)
        calculated["intelligent_mode_ena"] = _is_bit_set(int(system_mode), 5)

    return calculated

