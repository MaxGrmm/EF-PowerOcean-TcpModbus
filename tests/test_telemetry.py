"""Dependency-free tests for the telemetry helpers."""

from __future__ import annotations

import unittest

import pytest
from ef_powerocean_tcpmodbus.telemetry import (
    TelemetryData,
    calculate_derived_values,
    decode_register,
)


@pytest.mark.parametrize(
    ("registers", "register_size", "expected"),
    (
        ([17], 1, 17.0),
        ([0x0000, 0x42F7], 2, 123.5),
    ),
    ids=(
        "single-register",
        "word-swapped-float",
    ),
)
def test_decodes_register_values(
    registers: list[int],
    register_size: int,
    expected: float,
) -> None:
    assert decode_register(registers, register_size) == expected


@pytest.mark.parametrize(
    ("registers", "register_size"),
    (
        ([], 1),
        ([0], 2),
        ([0, 0x7FC0], 2),
        ([0, 0x7F80], 2),
        ([0, 0xFF80], 2),
        ([0x10000, 0], 2),
    ),
    ids=(
        "empty",
        "incomplete-float",
        "nan",
        "positive-infinity",
        "negative-infinity",
        "word-out-of-range",
    ),
)
def test_rejects_invalid_register_values(
    registers: list[int], register_size: int
) -> None:
    assert decode_register(registers, register_size) is None


class CalculateValuesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data = {
            "battery_soc": 60,
            "battery_count": 2,
            "bat_charged_total": 100.5,
            "bat_discharged_total": 80.25,
            "solar_today": 20.0,
            "grid_import_today": 5.0,
            "grid_export_today": 3.0,
            "bat_charged_today": 4.0,
            "bat_discharged_today": 2.0,
            "solar_total": 2000.0,
            "grid_import_total": 500.0,
            "grid_export_total": 300.0,
            "pv1_current": 5.0,
            "pv1_voltage": 300.0,
            "pv2_current": 4.0,
            "pv2_voltage": 249.9,
            "pv3_current": 3.0,
            "pv3_voltage": 250.0,
            "system_modes": 0b101000,
        }

    def calculate(
        self,
        *,
        calculate_solar_power: bool = True,
    ) -> dict:
        return calculate_derived_values(
            TelemetryData.from_mapping(self.data),
            calculate_solar_power=calculate_solar_power,
            startup_voltage=250,
        )

    def test_calculates_energy_and_battery_values(self) -> None:
        result = self.calculate()

        self.assertEqual(result["bat_remaining"], 6.0)
        self.assertEqual(result["bat_net_energy"], 20.25)
        self.assertEqual(result["house_energy_today"], 20.0)
        self.assertEqual(result["house_energy_total"], 2180.0)

    def test_calculates_pv_power_and_honors_voltage_threshold(self) -> None:
        result = self.calculate()

        self.assertEqual(result["pv1_power"], 1500.0)
        self.assertEqual(result["pv2_power"], 0)
        self.assertEqual(result["pv3_power"], 750.0)
        self.assertEqual(result["solar_power"], 2250.0)

    def test_decodes_system_mode_bits(self) -> None:
        result = self.calculate()

        self.assertEqual(result["grid_mode"], "grid")
        self.assertTrue(result["battery_saver_mode_ena"])
        self.assertFalse(result["self_use_mode_ena"])
        self.assertTrue(result["intelligent_mode_ena"])

    def test_omits_optional_values_when_their_inputs_are_disabled_or_absent(
        self,
    ) -> None:
        del self.data["system_modes"]

        result = self.calculate(calculate_solar_power=False)

        self.assertNotIn("solar_power", result)
        self.assertNotIn("grid_mode", result)
        self.assertNotIn("battery_saver_mode_ena", result)
        self.assertNotIn("self_use_mode_ena", result)
        self.assertNotIn("intelligent_mode_ena", result)

    def test_returns_none_when_required_inputs_are_missing(self) -> None:
        del self.data["battery_soc"]
        del self.data["pv1_current"]
        del self.data["grid_import_today"]
        del self.data["solar_total"]

        result = self.calculate()

        self.assertIsNone(result["bat_remaining"])
        self.assertIsNone(result["pv1_power"])
        self.assertIsNone(result["solar_power"])
        self.assertIsNone(result["house_energy_today"])
        self.assertIsNone(result["house_energy_total"])

    def test_treats_zero_as_a_value_and_does_not_mutate_input(self) -> None:
        self.data.update(dict.fromkeys(self.data, 0))
        original = self.data.copy()

        result = self.calculate()

        self.assertEqual(self.data, original)
        self.assertEqual(result["bat_remaining"], 0)
        self.assertEqual(result["bat_net_energy"], 0)
        self.assertEqual(result["house_energy_today"], 0)
        self.assertEqual(result["house_energy_total"], 0)
        self.assertEqual(result["solar_power"], 0)
        self.assertEqual(result["grid_mode"], "grid")
        self.assertFalse(result["battery_saver_mode_ena"])
        self.assertFalse(result["self_use_mode_ena"])
        self.assertFalse(result["intelligent_mode_ena"])


if __name__ == "__main__":
    unittest.main()
