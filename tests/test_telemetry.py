"""Dependency-free tests for the telemetry helpers."""

from __future__ import annotations

import unittest

import pytest
from ef_powerocean_tcpmodbus.models import RegisterType
from ef_powerocean_tcpmodbus.telemetry import (
    TelemetryData,
    calculate_derived_values,
    decode_firmware_version,
    decode_register,
    is_modbus_disabled,
)


@pytest.mark.parametrize(
    ("serial_number", "inverter_rated_power", "limit_inv_max", "expected"),
    (
        ("R123456789", 0, 0, True),
        ("R123456789", 6000, 0, False),
        ("R123456789", 0, 5000, False),
        ("R123456789", None, 0, False),
        ("unknown", 0, 0, False),
        (None, 0, 0, False),
    ),
)
def test_detects_disabled_modbus_from_static_capabilities(
    serial_number: str | None,
    inverter_rated_power: float | None,
    limit_inv_max: float | None,
    expected: bool,
) -> None:
    assert (
        is_modbus_disabled(serial_number, inverter_rated_power, limit_inv_max)
        is expected
    )


@pytest.mark.parametrize(
    ("registers", "data_type", "expected"),
    (
        ([17], RegisterType.UINT16, 17.0),
        ([0x0000, 0x42F7], RegisterType.FLOAT32, 123.5),
        ([0x0000, 0x0001], RegisterType.UINT32, 65536.0),
        # A value that fits the low word alone must decode the same as before.
        ([15000, 0x0000], RegisterType.UINT32, 15000.0),
        ([3000, 0x0000], RegisterType.INT32, 3000.0),
        # Feed-in setpoints are negative, so the sign bit must survive the swap.
        ([0xF448, 0xFFFF], RegisterType.INT32, -3000.0),
    ),
    ids=(
        "single-register",
        "word-swapped-float",
        "uint32-uses-the-high-word",
        "uint32-low-word-only",
        "int32-positive",
        "int32-negative",
    ),
)
def test_decodes_register_values(
    registers: list[int],
    data_type: RegisterType,
    expected: float,
) -> None:
    assert decode_register(registers, data_type) == expected


# The words below are what a three-phase Ocean 2 answered with in the register scan
# of issue #86, where every 32-bit value arrives high word first.
@pytest.mark.parametrize(
    ("registers", "data_type", "expected"),
    (
        ([0x0000, 0x2710], RegisterType.UINT32, 10000.0),
        ([0x435C, 0xC51F], RegisterType.FLOAT32, 220.77),
        ([0xFFFF, 0xF448], RegisterType.INT32, -3000.0),
        # A single word has no order to get wrong.
        ([96], RegisterType.UINT16, 96.0),
    ),
    ids=("uint32", "float32", "int32-negative", "single-register"),
)
def test_decodes_register_values_sent_high_word_first(
    registers: list[int],
    data_type: RegisterType,
    expected: float,
) -> None:
    assert decode_register(registers, data_type, high_word_first=True) == expected


def test_decodes_firmware_version_sent_high_word_first() -> None:
    assert decode_firmware_version([0x0100, 0x034F], high_word_first=True) == "1.0.3.79"
    assert decode_firmware_version([0x034F, 0x0100]) == "1.0.3.79"


@pytest.mark.parametrize(
    ("registers", "data_type"),
    (
        ([], RegisterType.UINT16),
        ([0], RegisterType.FLOAT32),
        ([0], RegisterType.UINT32),
        ([0, 0x7FC0], RegisterType.FLOAT32),
        ([0, 0x7F80], RegisterType.FLOAT32),
        ([0, 0xFF80], RegisterType.FLOAT32),
        ([0x10000, 0], RegisterType.FLOAT32),
        ([0x10000, 0], RegisterType.UINT32),
    ),
    ids=(
        "empty",
        "incomplete-float",
        "incomplete-uint32",
        "nan",
        "positive-infinity",
        "negative-infinity",
        "word-out-of-range",
        "uint32-word-out-of-range",
    ),
)
def test_rejects_invalid_register_values(
    registers: list[int], data_type: RegisterType
) -> None:
    assert decode_register(registers, data_type) is None


class CalculateValuesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data = {
            "battery_soc": 60,
            "battery_count": 2,
            "battery_capacity": 10000.0,
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
        self.assertEqual(result["house_energy_total"], 2179.75)

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
