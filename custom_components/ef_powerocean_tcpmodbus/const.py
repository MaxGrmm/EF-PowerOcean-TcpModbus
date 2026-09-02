"""Constants for EF-PowerOcean-TcpModbus integration."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfRatio,
    UnitOfTemperature,
)

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
MAX_BATTERY_COUNT: Final = 12
MAX_FAULT_EVENTS: Final = 20

MAX_REGISTERS_PER_READ: Final = 125
# Reading a few unused registers is cheaper than a second round trip, so registers
# closer together than this share one request.
MAX_REGISTER_GAP: Final = 48

SLEEP_TIME_AFTER_RECONNECT_S: Final = 1
SLEEP_TIME_AFTER_BATTERY_CHECK_FAILED_S: Final = 15
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

    @classmethod
    def from_product_info(
        cls, product_number: int | None, product_category: int | None
    ) -> InverterModel | None:
        """Map the device's product registers to a model, or None if unknown.

        We are not sure of the product number for the remaining models. Feel free
        to contribute this if you own such a model.
        """
        if product_number == 1:
            return (
                cls.POWEROCEAN_SINGLE_PHASE
                if product_category == 2
                else cls.POWEROCEAN_THREE_PHASE
            )
        if product_number == 2:
            return cls.POWEROCEAN_SINGLE_PHASE
        if product_number == 3:
            return cls.POWEROCEAN_PLUS
        return None


DEFAULT_INVERTER_MODEL: Final = InverterModel.POWEROCEAN_THREE_PHASE


class CoordinatorStatus(StrEnum):
    SUCCESS = "success"
    READ_FAILED = "read_failed"
    RECONNECT_FAILED = "reconnect_failed"
    PROCESSING_FAILED = "processing_failed"


class OperatingMode(StrEnum):
    STANDBY = "standby"
    SELF_CONSUMPTION = "self_consumption"
    AI = "ai"
    UNKNOWN = "unknown"


class GridMode(StrEnum):
    GRID = "grid"
    ISLANDED = "islanded"


class RegisterType(StrEnum):
    """Word layout of a register. Multi-word values are stored low word first."""

    UINT16 = "uint16"
    UINT32 = "uint32"
    FLOAT32 = "float32"
    SERIAL = "serial"


REGISTER_SIZES: Final = {
    RegisterType.UINT16: 1,
    RegisterType.UINT32: 2,
    RegisterType.FLOAT32: 2,
    # 16 ASCII bytes.
    RegisterType.SERIAL: 8,
}


@dataclass(frozen=True)
class RegisterDef:
    key: str
    address: int
    data_type: RegisterType = RegisterType.FLOAT32

    @property
    def size(self) -> int:
        """Return how many 16-bit words this register occupies."""
        return REGISTER_SIZES[self.data_type]

    @property
    def end(self) -> int:
        """Return the address just past this register."""
        return self.address + self.size


@dataclass(frozen=True)
class RegisterBlock:
    """Registers that are fetched with a single Modbus request."""

    registers: tuple[RegisterDef, ...]

    def __post_init__(self) -> None:
        if self.count > MAX_REGISTERS_PER_READ:
            raise ValueError(
                f"Block at {self.start} spans {self.count} registers, "
                f"more than the {MAX_REGISTERS_PER_READ} a Modbus read allows."
            )

    @property
    def start(self) -> int:
        return min(register.address for register in self.registers)

    @property
    def count(self) -> int:
        return max(register.end for register in self.registers) - self.start

    def index_of(self, register: RegisterDef) -> int:
        """Return the register's offset within this block's response."""
        return register.address - self.start

    def registers_for(self, raw: Sequence[int], register: RegisterDef) -> list[int]:
        """Return the raw words of *register* within this block's response."""
        index = self.index_of(register)
        return list(raw[index : index + register.size])


def plan_blocks(registers: Iterable[RegisterDef]) -> tuple[RegisterBlock, ...]:
    """Group registers into the fewest Modbus reads.

    A new read starts when the next register is too far away to be worth reading
    through, or when the block would outgrow a single Modbus response.
    """
    blocks: list[RegisterBlock] = []
    current: list[RegisterDef] = []

    for register in sorted(registers, key=lambda register: register.address):
        if current:
            gap = register.address - max(mapped.end for mapped in current)
            span = register.end - current[0].address
            if gap > MAX_REGISTER_GAP or span > MAX_REGISTERS_PER_READ:
                blocks.append(RegisterBlock(tuple(current)))
                current = []
        current.append(register)

    if current:
        blocks.append(RegisterBlock(tuple(current)))
    return tuple(blocks)


@dataclass(frozen=True)
class SensorDef:
    key: str
    name: str | None = None
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    entity_category: EntityCategory | None = None
    icon: str | None = None
    options: tuple[str, ...] | None = None


@dataclass(frozen=True)
class EnergySensorDef:
    key: str
    name: str | None = None
    unit: str = UnitOfEnergy.KILO_WATT_HOUR
    is_calculated: bool = False
    resets_daily: bool = False
    max_power: int | None = None
    # The _total value of the energy counter, used to validate daily resets and prevent invalid spikes.
    total_source: str | None = None
    device_class: str = "energy"
    state_class: str = "total_increasing"
    entity_category: EntityCategory | None = None
    icon: str | None = None


@dataclass(frozen=True)
class BinarySensorDef:
    key: str
    name: str | None = None
    device_class: str | None = None
    entity_category: EntityCategory | None = None


PRODUCT_CATEGORY: Final = RegisterDef("product_category", 40002, RegisterType.UINT16)
PRODUCT_NUMBER: Final = RegisterDef("product_number", 40003, RegisterType.UINT16)
SERIAL_NUMBER: Final = RegisterDef("serial_number", 40004, RegisterType.SERIAL)
FIRMWARE_VERSION: Final = RegisterDef("firmware_version", 40012, RegisterType.UINT32)

# Read once when the connection is established, not on every poll.
DEVICE_INFO_BLOCK: Final = RegisterBlock(
    (PRODUCT_CATEGORY, PRODUCT_NUMBER, SERIAL_NUMBER, FIRMWARE_VERSION)
)

BATTERY_SOC_KEYS: Final = tuple(
    f"soc_battery_{battery_number}"
    for battery_number in range(1, MAX_BATTERY_COUNT + 1)
)

# Every polled register, by absolute Modbus address. Order is for readability only;
# the reads are worked out by plan_blocks().
MODBUS_REGISTERS: Final[tuple[RegisterDef, ...]] = (
    RegisterDef("house_power", 40519),
    RegisterDef("grid_power", 40521),
    RegisterDef("solar_power", 40523),
    RegisterDef("battery_power", 40525),
    RegisterDef("battery_soc", 40527, RegisterType.UINT16),
    RegisterDef("inverter_rated_power", 40528, RegisterType.UINT32),
    RegisterDef("system_modes", 40530, RegisterType.UINT32),
    RegisterDef("min_soc_limit", 40536, RegisterType.UINT16),
    RegisterDef("feed_in_power_max", 40538, RegisterType.UINT32),
    RegisterDef("device_led_brightness", 40541, RegisterType.UINT16),
    RegisterDef("limit_inv_power", 40546, RegisterType.UINT32),
    RegisterDef("limit_inv_max", 40548, RegisterType.UINT32),
    RegisterDef("battery_capacity", 40552, RegisterType.UINT32),
    RegisterDef("battery_discharge_power_limit", 40554, RegisterType.UINT32),
    RegisterDef("battery_charge_power_limit", 40556, RegisterType.UINT32),
    RegisterDef("battery_voltage", 40574),
    RegisterDef("battery_current", 40576),
    RegisterDef("battery_temperature", 40578),
    RegisterDef("voltage_l1", 40580),
    RegisterDef("voltage_l2", 40582),
    RegisterDef("voltage_l3", 40584),
    RegisterDef("current_l1", 40586),
    RegisterDef("current_l2", 40588),
    RegisterDef("current_l3", 40590),
    RegisterDef("inverter_temperature", 40592),
    RegisterDef("frequency", 40594),
    RegisterDef("pv1_voltage", 40596),
    RegisterDef("pv2_voltage", 40598),
    RegisterDef("pv3_voltage", 40600),
    RegisterDef("pv1_current", 40602),
    RegisterDef("pv2_current", 40604),
    RegisterDef("pv3_current", 40606),
    RegisterDef("fault_count", 42049, RegisterType.UINT16),
    *(
        RegisterDef(f"fault_{fault_number}", 42049 + fault_number, RegisterType.UINT16)
        for fault_number in range(1, MAX_FAULT_EVENTS + 1)
    ),
    RegisterDef("battery_count", 42081, RegisterType.UINT16),
    *(
        RegisterDef(key, 42081 + battery_number, RegisterType.UINT16)
        for battery_number, key in enumerate(BATTERY_SOC_KEYS, start=1)
    ),
    RegisterDef("grid_import_total", 42161),
    RegisterDef("grid_import_today", 42163),
    RegisterDef("grid_export_total", 42177),
    RegisterDef("grid_export_today", 42179),
    RegisterDef("bat_charged_total", 42225),
    RegisterDef("bat_charged_today", 42227),
    RegisterDef("bat_discharged_total", 42241),
    RegisterDef("bat_discharged_today", 42243),
    RegisterDef("solar_total", 42257),
    RegisterDef("solar_today", 42259),
)

REGISTER_BLOCKS: Final = plan_blocks(MODBUS_REGISTERS)
REGISTERS_BY_KEY: Final = {register.key: register for register in MODBUS_REGISTERS}


SENSOR_MAP: list[SensorDef] = [
    SensorDef(
        key="system_modes",
        unit=None,
        device_class=None,
        state_class="measurement",
    ),
    SensorDef(
        key="house_power",
        unit=UnitOfPower.WATT,
        device_class="power",
        state_class="measurement",
    ),
    SensorDef(
        key="grid_power",
        unit=UnitOfPower.WATT,
        device_class="power",
        state_class="measurement",
    ),
    SensorDef(
        key="solar_power",
        unit=UnitOfPower.WATT,
        device_class="power",
        state_class="measurement",
    ),
    SensorDef(
        key="battery_power",
        unit=UnitOfPower.WATT,
        device_class="power",
        state_class="measurement",
    ),
    SensorDef(
        key="battery_soc",
        unit=UnitOfRatio.PERCENTAGE,
        device_class="battery",
        state_class="measurement",
    ),
    SensorDef(
        key="min_soc_limit",
        unit=UnitOfRatio.PERCENTAGE,
        device_class="battery",
        state_class="measurement",
    ),
    SensorDef(
        key="battery_charge_power_limit",
        unit=UnitOfPower.WATT,
        device_class="power",
        state_class="measurement",
    ),
    SensorDef(
        key="battery_discharge_power_limit",
        unit=UnitOfPower.WATT,
        device_class="power",
        state_class="measurement",
    ),
    SensorDef(
        key="device_led_brightness",
        unit=UnitOfRatio.PERCENTAGE,
        device_class=None,
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="limit_inv_power",
        unit=UnitOfPower.WATT,
        device_class="power",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="limit_inv_max",
        unit=UnitOfPower.WATT,
        device_class="power",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="battery_capacity",
        unit=UnitOfEnergy.WATT_HOUR,
        device_class="energy_storage",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="battery_voltage",
        unit=UnitOfElectricPotential.VOLT,
        device_class="voltage",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="battery_current",
        unit=UnitOfElectricCurrent.AMPERE,
        device_class="current",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="battery_temperature",
        unit=UnitOfTemperature.CELSIUS,
        device_class="temperature",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="voltage_l1",
        unit=UnitOfElectricPotential.VOLT,
        device_class="voltage",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="voltage_l2",
        unit=UnitOfElectricPotential.VOLT,
        device_class="voltage",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="voltage_l3",
        unit=UnitOfElectricPotential.VOLT,
        device_class="voltage",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="current_l1",
        unit=UnitOfElectricCurrent.AMPERE,
        device_class="current",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="current_l2",
        unit=UnitOfElectricCurrent.AMPERE,
        device_class="current",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="current_l3",
        unit=UnitOfElectricCurrent.AMPERE,
        device_class="current",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="inverter_temperature",
        unit=UnitOfTemperature.CELSIUS,
        device_class="temperature",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="frequency",
        unit=UnitOfFrequency.HERTZ,
        device_class="frequency",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="pv1_voltage",
        unit=UnitOfElectricPotential.VOLT,
        device_class="voltage",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="pv2_voltage",
        unit=UnitOfElectricPotential.VOLT,
        device_class="voltage",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="pv3_voltage",
        unit=UnitOfElectricPotential.VOLT,
        device_class="voltage",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="pv1_current",
        unit=UnitOfElectricCurrent.AMPERE,
        device_class="current",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="pv2_current",
        unit=UnitOfElectricCurrent.AMPERE,
        device_class="current",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="pv3_current",
        unit=UnitOfElectricCurrent.AMPERE,
        device_class="current",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="feed_in_power_max",
        unit=UnitOfPower.WATT,
        device_class="power",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="inverter_rated_power",
        unit=UnitOfPower.WATT,
        device_class="power",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="battery_count",
        unit=None,
        device_class=None,
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    *[
        SensorDef(
            key=key,
            unit=UnitOfRatio.PERCENTAGE,
            device_class="battery",
            state_class="measurement",
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        for key in BATTERY_SOC_KEYS
    ],
    SensorDef(
        key="bat_remaining",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        device_class="energy_storage",
        state_class="measurement",
    ),
    SensorDef(
        key="pv1_power",
        unit=UnitOfPower.WATT,
        device_class="power",
        state_class="measurement",
    ),
    SensorDef(
        key="pv2_power",
        unit=UnitOfPower.WATT,
        device_class="power",
        state_class="measurement",
    ),
    SensorDef(
        key="pv3_power",
        unit=UnitOfPower.WATT,
        device_class="power",
        state_class="measurement",
    ),
    SensorDef(
        key="bat_net_energy",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        device_class="energy",
        state_class="total",
    ),
    SensorDef(
        key="grid_mode",
        unit=None,
        device_class="enum",
        state_class=None,
        options=tuple(GridMode),
        icon="mdi:transmission-tower",
    ),
    SensorDef(
        key="operating_mode",
        device_class="enum",
        options=tuple(OperatingMode),
        icon="mdi:home-lightning-bolt",
    ),
    SensorDef(
        key="fault_count",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:alert-circle-outline",
    ),
    SensorDef(
        key="active_faults",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:alert-circle-outline",
    ),
    SensorDef(
        key="coordinator_status",
        device_class="enum",
        entity_category=EntityCategory.DIAGNOSTIC,
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


# The daily sensors have been shown to not reliably reset at midnight. They are
# therefore calculated using the respective total sensor, but we still expose the
# device raw values under a *_raw diagnostic key. This is also used for the initial
# snapshot when initializing the daily counters for the first time after installing.
DAILY_ENERGY_SENSORS_DEVICE_RAW: list[SensorDef] = [
    SensorDef(
        key=f"{energy_sensor.key}_raw",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        device_class="energy",
        state_class="total_increasing",
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    for energy_sensor in ENERGY_SENSOR_MAP
    if energy_sensor.total_source is not None
]


BINARY_SENSOR_MAP: list[BinarySensorDef] = [
    BinarySensorDef("self_use_mode_ena"),
    BinarySensorDef("intelligent_mode_ena"),
    BinarySensorDef("battery_saver_mode_ena"),
    BinarySensorDef(
        "system_fault",
        device_class="problem",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BinarySensorDef(
        "system_power_on",
        device_class="running",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
]


@dataclass(frozen=True)
class NumberWritableDef:
    key: str  # Unique key for the number entity (e.g., "min_soc_limit_control")
    read_key: str  # The original key from MODBUS_REGISTERS used for reading (e.g., "min_soc_limit")
    name: str  # Display name for Home Assistant UI
    min_value: float  # Slider minimum value
    max_value: float  # Slider maximum value
    step: float  # Step size (1.0 for integers, 0.1 for floats)
    unit: str | None = None  # Unit of measurement
    device_class: str | None = None  # Device class type
    icon: str | None = None  # Custom icon for the slider

    @property
    def register(self) -> int:
        """Write back to the same address the value is read from."""
        return REGISTERS_BY_KEY[self.read_key].address


# Map of all modbus registers available for writing operations.
WRITABLE_NUMBERS_MAP: list[NumberWritableDef] = [
    NumberWritableDef(
        key="min_soc_limit_control",
        read_key="min_soc_limit",
        name="Minimum SOC Limit Control",
        min_value=0.0,
        max_value=100.0,
        step=1.0,
        unit=UnitOfRatio.PERCENTAGE,
        device_class="battery",
    ),
    NumberWritableDef(
        key="device_led_brightness_control",
        read_key="device_led_brightness",
        name="LED Brightness Control",
        min_value=0.0,
        max_value=100.0,
        step=10.0,
        unit=UnitOfRatio.PERCENTAGE,
        icon="mdi:led-on",
    ),
]
