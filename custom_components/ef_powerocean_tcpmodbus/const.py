"""Constants for EF-PowerOcean-TcpModbus integration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

DOMAIN: Final = "ef_powerocean_tcpmodbus"
DEFAULT_PORT: Final = 502
DEFAULT_SLAVE: Final = 1
DEFAULT_SCAN_INTERVAL_S: Final = 5
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
MAX_BATTERY_COUNT: Final = 9

SLEEP_TIME_AFTER_RECONNECT_S: Final = 1
SLEEP_TIME_AFTER_BATTERY_CHECK_FAILED_S: Final = 15
DAILY_RESET_MIN_PERIOD_HOURS: Final = 20
# We use >24h since there can be timezone differences and this will handle DST
DAILY_RESET_FORCE_HOURS: Final = 26
ENERGY_RESOLUTION_KWH: Final = 0.01
STORAGE_VERSION: Final = 1
STATE_SAVE_DELAY_S: Final = 30


class InverterModel(StrEnum):
    POWEROCEAN_SINGLE_PHASE = "powerocean_single_phase"
    POWEROCEAN_THREE_PHASE = "powerocean_three_phase"
    POWEROCEAN_PLUS = "powerocean_plus"
    POWEROCEAN_DC_FIT = "powerocean_dc_fit"
    OCEAN_2 = "ocean_2"

    @property
    def startup_voltage(self) -> int:
        return {
            # The startup voltage is used to filter out phantom string power when the PV input is not actually producing power.
            # The values are based on the datasheets of each model. However, the single phase does not have a dedicated startup voltage specification
            # so this value is deducted from the MPPT operating range.
            # https://enterprise-service-eu-cdn.ecoflow.com/enterprise/content/2024-03-27-1485da5d-eae4-4a38-830a-4e340517d968.pdf
            self.POWEROCEAN_SINGLE_PHASE: 90,
            # https://enterprise-service-eu-cdn.ecoflow.com/enterprise/documentation/1772090325968/EcoFlow%20PowerOcean%20(Three-phase)_Datasheet_EN.pdf
            self.POWEROCEAN_THREE_PHASE: 160,
            # https://enterprise-service-eu-cdn.ecoflow.com/enterprise/documentation/1754035729875/PowerOcean%20Plus%20(three-phase)_Brochure_20241223_EN.pdf
            self.POWEROCEAN_PLUS: 160,
            # https://enterprise-service-eu-cdn.ecoflow.com/enterprise/documentation/1735192805714/EcoFlow%20PowerOcean%20DC%20Fit_Datasheet_EN_20241225.pdf
            self.POWEROCEAN_DC_FIT: 90,
            # https://enterprise-service-eu-cdn.ecoflow.com/enterprise/documentation/1779447439219/OCEAN%202%20Three-Phase_Datasheet_EN_260522.pdf
            self.OCEAN_2: 120,
        }[self]

    @property
    def display_name(self) -> str:
        return {
            self.POWEROCEAN_SINGLE_PHASE: "PowerOcean Single Phase",
            self.POWEROCEAN_THREE_PHASE: "PowerOcean Three Phase",
            self.POWEROCEAN_PLUS: "PowerOcean Plus",
            self.POWEROCEAN_DC_FIT: "PowerOcean DC Fit",
            self.OCEAN_2: "Ocean 2",
        }[self]


DEFAULT_INVERTER_MODEL: Final = InverterModel.POWEROCEAN_THREE_PHASE


class CoordinatorStatus(StrEnum):
    SUCCESS = "success"
    READ_FAILED = "read_failed"
    RECONNECT_FAILED = "reconnect_failed"
    PROCESSING_FAILED = "processing_failed"


@dataclass(frozen=True)
class ModelBlockIndex:
    default: int
    overrides: Mapping[InverterModel, int]

    def for_model(self, inverter_model: InverterModel) -> int:
        return self.overrides.get(inverter_model, self.default)


@dataclass(frozen=True)
class RegisterDef:
    key: str
    block_index: int | ModelBlockIndex
    size: int = 2

    def block_index_for(self, inverter_model: InverterModel) -> int:
        if isinstance(self.block_index, int):
            return self.block_index
        return self.block_index.for_model(inverter_model)


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
    icon: str | None = None
    options: tuple[str, ...] | None = None


@dataclass(frozen=True)
class EnergySensorDef:
    key: str
    name: str | None = None
    unit: str = "kWh"
    is_calculated: bool = False
    resets_daily: bool = False
    max_power: int | None = None
    # The _total value of the energy counter, used to validate daily resets and prevent invalid spikes.
    total_source: str | None = None
    device_class: str = "energy"
    state_class: str = "total_increasing"
    entity_category: str | None = None
    icon: str | None = None


@dataclass(frozen=True)
class BinarySensorDef:
    key: str
    name: str | None = None
    device_class: str | None = None
    entity_category: str | None = None


SERIAL_NUMBER_REGISTER: Final = 40004
MAIN_BLOCK_START_REGISTER: Final = 40519
MAIN_BLOCK_REGISTER_COUNT: Final = 100
BATTERY_TEMPERATURE_BLOCK_INDEX: Final = 59
BATTERY_TEMPERATURE_REGISTER_SIZE: Final = 2

MOD_REGISTER_MAP = {
    "serial_number": SERIAL_NUMBER_REGISTER,
    "blocks": [
        BlockDef(
            start_register=MAIN_BLOCK_START_REGISTER,
            num_read_regs=MAIN_BLOCK_REGISTER_COUNT,
            content=[
                RegisterDef(key="house_power", block_index=0),
                RegisterDef(key="grid_power", block_index=2),
                RegisterDef(key="solar_power", block_index=4),
                RegisterDef(key="battery_power", block_index=6),
                RegisterDef(key="battery_soc", block_index=8, size=1),
                RegisterDef(key="inverter_rated_power", block_index=9, size=1),
                RegisterDef(key="system_modes", block_index=11, size=1),
                RegisterDef(key="min_soc_limit", block_index=17, size=1),
                RegisterDef(key="bat_temp_warn_max", block_index=21, size=1),
                RegisterDef(key="device_led_brightness", block_index=22, size=1),
                RegisterDef(key="limit_inv_power", block_index=27, size=1),
                RegisterDef(key="limit_inv_max", block_index=29, size=1),
                RegisterDef(key="battery_capacity", block_index=33, size=1),
                RegisterDef(key="battery_charge_power_limit", block_index=37, size=1),
                RegisterDef(key="battery_voltage", block_index=55),
                RegisterDef(key="battery_current", block_index=57),
                RegisterDef(
                    key="battery_temperature",
                    block_index=BATTERY_TEMPERATURE_BLOCK_INDEX,
                    size=BATTERY_TEMPERATURE_REGISTER_SIZE,
                ),
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
                RegisterDef(
                    key="feed_in_power_max",
                    block_index=ModelBlockIndex(
                        default=90,
                        overrides={InverterModel.POWEROCEAN_PLUS: 19},
                    ),
                    size=1,
                ),
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
        key="battery_charge_power_limit",
        unit="W",
        device_class="power",
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
        key="inverter_rated_power",
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
    SensorDef(
        key="grid_mode",
        unit=None,
        device_class="enum",
        state_class=None,
        icon="mdi:transmission-tower",
    ),
    SensorDef(
        key="coordinator_status",
        device_class="enum",
        entity_category="diagnostic",
        options=tuple(CoordinatorStatus),
    ),
]


ENERGY_SENSOR_MAP: list[EnergySensorDef] = [
    EnergySensorDef("grid_import_total", max_power=CONF_MAX_GRID_POWER),
    EnergySensorDef(
        "grid_import_today",
        resets_daily=True,
        max_power=CONF_MAX_GRID_POWER,
        total_source="grid_import_total",
    ),
    EnergySensorDef("grid_export_total", max_power=CONF_MAX_SOLAR_POWER),
    EnergySensorDef(
        "grid_export_today",
        resets_daily=True,
        max_power=CONF_MAX_SOLAR_POWER,
        total_source="grid_export_total",
    ),
    EnergySensorDef("bat_charged_total", max_power=CONF_MAX_BATTERY_CHARGED_POWER),
    EnergySensorDef(
        "bat_charged_today",
        resets_daily=True,
        max_power=CONF_MAX_BATTERY_CHARGED_POWER,
        total_source="bat_charged_total",
    ),
    EnergySensorDef(
        "bat_discharged_total", max_power=CONF_MAX_BATTERY_DISCHARGED_POWER
    ),
    EnergySensorDef(
        "bat_discharged_today",
        resets_daily=True,
        max_power=CONF_MAX_BATTERY_DISCHARGED_POWER,
        total_source="bat_discharged_total",
    ),
    EnergySensorDef("solar_total", max_power=CONF_MAX_SOLAR_POWER),
    EnergySensorDef(
        "solar_today",
        resets_daily=True,
        max_power=CONF_MAX_SOLAR_POWER,
        total_source="solar_total",
    ),
    EnergySensorDef(
        "house_energy_today",
        is_calculated=True,
        resets_daily=True,
        max_power=CONF_MAX_GRID_POWER,
    ),
    EnergySensorDef(
        "house_energy_total",
        is_calculated=True,
        max_power=CONF_MAX_GRID_POWER,
    ),
]


# During testing of the new logic deriving daily from total, publish the real daily registers as debug sensors.
DEVICE_DAILY_DEBUG_SENSORS: list[SensorDef] = [
    SensorDef(
        key=f"{energy_sensor.key}_raw",
        unit="kWh",
        device_class="energy",
        state_class="total_increasing",
        entity_category="diagnostic",
    )
    for energy_sensor in ENERGY_SENSOR_MAP
    if energy_sensor.resets_daily
]


BINARY_SENSOR_MAP: list[BinarySensorDef] = [
    BinarySensorDef("self_use_mode_ena", "battery"),
    BinarySensorDef("intelligent_mode_ena", "battery"),
    BinarySensorDef("battery_saver_mode_ena", "battery"),
]


@dataclass(frozen=True)
class NumberWritableDef:
    key: str  # Unique key for the number entity (e.g., "min_soc_limit_control")
    read_key: str  # The original key from MOD_REGISTER_MAP used for reading (e.g., "min_soc_limit")
    name: str  # Display name for Home Assistant UI
    register: int  # Physical Modbus register address for writing
    min_value: float  # Slider minimum value
    max_value: float  # Slider maximum value
    step: float  # Step size (1.0 for integers, 0.1 for floats)
    unit: str | None = None  # Unit of measurement ("%", "W", or None)
    device_class: str | None = None  # Device class type
    icon: str | None = None  # Custom icon for the slider


# Map of all modbus registers available for writing operations.
WRITABLE_NUMBERS_MAP: list[NumberWritableDef] = [
    NumberWritableDef(
        key="min_soc_limit_control",
        read_key="min_soc_limit",  # Points to the existing read sensor data
        name="Minimum SOC Limit Control",
        register=40536,  # 40519 + 17
        min_value=0.0,
        max_value=100.0,
        step=1.0,
        unit="%",
        device_class="battery",
    ),
    NumberWritableDef(
        key="device_led_brightness_control",
        read_key="device_led_brightness",  # Points to the existing read sensor data
        name="LED Brightness Control",
        register=40541,  # 40519 + 22
        min_value=0.0,
        max_value=100.0,
        step=10.0,
        unit="%",
        icon="mdi:led-on",
    ),
]
