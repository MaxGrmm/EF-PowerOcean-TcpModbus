"""Constants for EF-PowerOcean-TcpModbus integration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

DOMAIN: Final = "ef_powerocean_tcpmodbus"
DEFAULT_PORT: Final = 502
DEFAULT_SLAVE: Final = 1
DEFAULT_SCAN_INTERVAL: Final = 5  # seconds
DEFAULT_BATTERY_COUNT: Final = 0
DEFAULT_MAX_SOLAR_POWER: Final = 12000
DEFAULT_MAX_GRID_POWER: Final = 15000
DEFAULT_MAX_POWER: Final = 30000

CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_BATTERY_COUNT: Final = "battery_count"
CONF_MAX_SOLAR_POWER: Final = "solar_power_max"
CONF_MAX_GRID_POWER: Final = "grid_power_max"
CONF_MAX_BATTERY_CHARGED_POWER: Final = "battery_charged_power_max"
CONF_MAX_BATTERY_DISCHARGED_POWER: Final = "battery_discharged_power_max"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_CALC_SOLAR_POWER: Final = "calc_solar_power"
CONF_INVERTER_MODEL: Final = "inverter_model"

MAX_BATTERY_CHARGED_POWER: Final = 2500
MAX_BATTERY_DISCHARGED_POWER: Final = 3300


class InverterModel(StrEnum):
    POWEROCEAN_SINGLE_PHASE = "powerocean_single_phase"
    POWEROCEAN_THREE_PHASE = "powerocean_three_phase"
    POWEROCEAN_PLUS = "powerocean_plus"
    OCEAN_2 = "ocean_2"

    @property
    def startup_voltage(self) -> int:
        return {
            # The startup voltage is used to filter out phantom string power when the PV input is not actually producing power.
            # The values are based on the datasheets of each model. However, the single phase does not have a dedicated startup voltage specification
            # so this value is deducted from the MPPT operating range.
            self.POWEROCEAN_SINGLE_PHASE: 90,
            self.POWEROCEAN_THREE_PHASE: 160,
            self.POWEROCEAN_PLUS: 160,
            self.OCEAN_2: 120,
        }[self]

    @property
    def display_name(self) -> str:
        return {
            self.POWEROCEAN_SINGLE_PHASE: "PowerOcean Single Phase",
            self.POWEROCEAN_THREE_PHASE: "PowerOcean Three Phase",
            self.POWEROCEAN_PLUS: "PowerOcean Plus",
            self.OCEAN_2: "Ocean 2",
        }[self]


DEFAULT_INVERTER_MODEL: Final = InverterModel.POWEROCEAN_THREE_PHASE


@dataclass(frozen=True)
class RegisterDef:
    key: str
    block_index: int
    size: int = 2


@dataclass(frozen=True)
class BlockDef:
    start_register: int
    content: list[RegisterDef]
    num_read_regs: int = 100


@dataclass(frozen=True)
class SensorDef:
    key: str
    name: str | None = None
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    entity_category: str | None = None


@dataclass(frozen=True)
class EnergySensorDef:
    key: str
    name: str | None = None
    unit: str = "kWh"
    reset_at_midnight: bool = False  # nur bei Sensor die aus einem Register gelesen werden, keine berechneten Sensoren
    is_calculated: bool = False
    max_power: int | None = None
    device_class: str = "energy"
    state_class: str = "total_increasing"
    entity_category: str | None = None


@dataclass(frozen=True)
class BinarySensorDef:
    key: str
    name: str | None = None
    device_class: str | None = None
    entity_category: str | None = None


MOD_REGISTER_MAP = {
    "serial_number": 40004,
    "blocks": [
        BlockDef(
            start_register=40519,
            content=[
                RegisterDef(key="house_power", block_index=0),
                RegisterDef(key="grid_power", block_index=2),
                RegisterDef(key="solar_power", block_index=4),
                RegisterDef(key="battery_power", block_index=6),
                RegisterDef(key="battery_soc", block_index=8, size=1),
                RegisterDef(key="system_modes", block_index=11, size=1),
                RegisterDef(key="min_soc_limit", block_index=17, size=1),
                RegisterDef(key="bat_temp_warn_max", block_index=21, size=1),
                RegisterDef(key="device_led_brightness", block_index=22, size=1),
                RegisterDef(key="limit_inv_power", block_index=27, size=1),
                RegisterDef(key="limit_inv_max", block_index=29, size=1),
                RegisterDef(key="battery_capacity", block_index=33, size=1),
                RegisterDef(key="battery_voltage", block_index=55),
                RegisterDef(key="battery_current", block_index=57),
                RegisterDef(key="battery_temperature", block_index=59),
                RegisterDef(key="voltage_l1", block_index=61),
                RegisterDef(key="voltage_l2", block_index=63),
                RegisterDef(key="voltage_l3", block_index=65),
                RegisterDef(key="current_l1", block_index=67),
                RegisterDef(key="current_l2", block_index=69),
                RegisterDef(key="current_l3", block_index=71),
                RegisterDef(key="inverter_temperature", block_index=73),
                RegisterDef(key="frequency", block_index=75),
                RegisterDef(key="pv1_voltage", block_index=77),
                RegisterDef(key="pv2_voltage", block_index=79),
                RegisterDef(key="pv3_voltage", block_index=81),
                RegisterDef(key="pv1_current", block_index=83),
                RegisterDef(key="pv2_current", block_index=85),
                RegisterDef(key="pv3_current", block_index=87),
                RegisterDef(key="feed_in_power_max", block_index=90, size=1),
            ],
        ),
        BlockDef(
            start_register=42081,
            num_read_regs=4,
            content=[
                RegisterDef(key="battery_count", block_index=0, size=1),
                RegisterDef(key="soc_battery_1", block_index=1, size=1),
                RegisterDef(key="soc_battery_2", block_index=2, size=1),
                RegisterDef(key="soc_battery_3", block_index=3, size=1),
            ],
        ),
        BlockDef(
            start_register=42161,
            content=[
                RegisterDef(key="grid_import_total", block_index=0),
                RegisterDef(key="grid_import_today", block_index=2),
                RegisterDef(key="grid_export_total", block_index=16),
                RegisterDef(key="grid_export_today", block_index=18),
                RegisterDef(key="bat_charged_total", block_index=64),
                RegisterDef(key="bat_charged_today", block_index=66),
                RegisterDef(key="bat_discharged_total", block_index=80),
                RegisterDef(key="bat_discharged_today", block_index=82),
                RegisterDef(key="solar_total", block_index=96),
                RegisterDef(key="solar_today", block_index=98),
            ],
        ),
    ],
}


SENSOR_MAP: list[SensorDef] = [
    SensorDef(
        key="system_modes",
        unit=None,
        device_class=None,
        state_class="measurement",
    ),
    SensorDef(
        key="house_power",
        unit="W",
        device_class="power",
        state_class="measurement",
    ),
    SensorDef(
        key="grid_power",
        unit="W",
        device_class="power",
        state_class="measurement",
    ),
    SensorDef(
        key="solar_power",
        unit="W",
        device_class="power",
        state_class="measurement",
    ),
    SensorDef(
        key="battery_power",
        unit="W",
        device_class="power",
        state_class="measurement",
    ),
    SensorDef(
        key="battery_soc",
        unit="%",
        device_class="battery",
        state_class="measurement",
    ),
    SensorDef(
        key="min_soc_limit",
        unit="%",
        device_class="battery",
        state_class="measurement",
    ),
    SensorDef(
        key="bat_temp_warn_max",
        unit="°C",
        device_class="temperature",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="device_led_brightness",
        unit="%",
        device_class=None,
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="limit_inv_power",
        unit="W",
        device_class="power",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="limit_inv_max",
        unit="W",
        device_class="power",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="battery_capacity",
        unit="Wh",
        device_class="storage",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="battery_voltage",
        unit="V",
        device_class="voltage",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="battery_current",
        unit="A",
        device_class="current",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="battery_temperature",
        unit="°C",
        device_class="temperature",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="voltage_l1",
        unit="V",
        device_class="voltage",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="voltage_l2",
        unit="V",
        device_class="voltage",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="voltage_l3",
        unit="V",
        device_class="voltage",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="current_l1",
        unit="A",
        device_class="current",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="current_l2",
        unit="A",
        device_class="current",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="current_l3",
        unit="A",
        device_class="current",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="inverter_temperature",
        unit="°C",
        device_class="temperature",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="frequency",
        unit="Hz",
        device_class="frequency",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="pv1_voltage",
        unit="V",
        device_class="voltage",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="pv2_voltage",
        unit="V",
        device_class="voltage",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="pv3_voltage",
        unit="V",
        device_class="voltage",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="pv1_current",
        unit="A",
        device_class="current",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="pv2_current",
        unit="A",
        device_class="current",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="pv3_current",
        unit="A",
        device_class="current",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="feed_in_power_max",
        unit="W",
        device_class="power",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="battery_count",
        unit=None,
        device_class=None,
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="soc_battery_1",
        unit="%",
        device_class="battery",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="soc_battery_2",
        unit="%",
        device_class="battery",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="soc_battery_3",
        unit="%",
        device_class="battery",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    SensorDef(
        key="bat_remaining",
        unit="kWh",
        device_class="energy_storage",
        state_class="measurement",
    ),
    SensorDef(
        key="pv1_power",
        unit="W",
        device_class="power",
        state_class="measurement",
    ),
    SensorDef(
        key="pv2_power",
        unit="W",
        device_class="power",
        state_class="measurement",
    ),
    SensorDef(
        key="pv3_power",
        unit="W",
        device_class="power",
        state_class="measurement",
    ),
    SensorDef(
        key="bat_net_energy",
        unit="kWh",
        device_class="energy",
        state_class="total",
    ),
]


ENERGY_SENSOR_MAP: list[EnergySensorDef] = [
    EnergySensorDef("grid_import_total", max_power=CONF_MAX_GRID_POWER),
    EnergySensorDef(
        "grid_import_today", reset_at_midnight=True, max_power=CONF_MAX_GRID_POWER
    ),
    EnergySensorDef("grid_export_total", max_power=CONF_MAX_SOLAR_POWER),
    EnergySensorDef(
        "grid_export_today", reset_at_midnight=True, max_power=CONF_MAX_SOLAR_POWER
    ),
    EnergySensorDef("bat_charged_total", max_power=CONF_MAX_BATTERY_CHARGED_POWER),
    EnergySensorDef(
        "bat_charged_today",
        reset_at_midnight=True,
        max_power=CONF_MAX_BATTERY_CHARGED_POWER,
    ),
    EnergySensorDef(
        "bat_discharged_total", max_power=CONF_MAX_BATTERY_DISCHARGED_POWER
    ),
    EnergySensorDef(
        "bat_discharged_today",
        reset_at_midnight=True,
        max_power=CONF_MAX_BATTERY_DISCHARGED_POWER,
    ),
    EnergySensorDef("solar_total", max_power=CONF_MAX_SOLAR_POWER),
    EnergySensorDef(
        "solar_today", reset_at_midnight=True, max_power=CONF_MAX_SOLAR_POWER
    ),
    EnergySensorDef(
        "house_energy_today",
        is_calculated=True,
        max_power=CONF_MAX_GRID_POWER,
    ),
    EnergySensorDef(
        "house_energy_total",
        is_calculated=True,
        max_power=CONF_MAX_GRID_POWER,
    ),
]


BINARY_SENSOR_MAP: list[BinarySensorDef] = [
    BinarySensorDef("self_use_mode_ena", "battery"),
    BinarySensorDef("intelligent_mode_ena", "battery"),
    BinarySensorDef("battery_saver_mode_ena", "battery"),
]
