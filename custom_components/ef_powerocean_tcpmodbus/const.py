"""Constants for EF-PowerOcean-TcpModbus integration."""

from __future__ import annotations

from typing import Final

from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
)

from .models import (
    BinarySensorDef,
    ControlEntityDef,
    ControlFeature,
    ControlFeatureDef,
    ControlMode,
    ControlStatus,
    CoordinatorStatus,
    EnergySensorDef,
    GridMode,
    InverterModel,
    NumberWritableDef,
    OperatingMode,
    RegisterBlock,
    RegisterDef,
    RegisterType,
    SensorDef,
    SwitchDef,
    plan_blocks_for_model,
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
CONF_MODBUS_CONTROL: Final = "modbus_control"
CONF_INVERTER_MODEL: Final = "inverter_model"

MAX_BATTERY_CHARGED_POWER: Final = 2500
MAX_BATTERY_DISCHARGED_POWER: Final = 3300
MAX_BATTERY_COUNT: Final = 12
MAX_FAULT_EVENTS: Final = 20

SLEEP_TIME_AFTER_RECONNECT_S: Final = 1

# The device stores writes but acts on none of them unless this register is written
# at least once a minute. Sent well inside that window so a missed poll is harmless.
HEARTBEAT_REGISTER: Final = 40608
HEARTBEAT_INTERVAL_S: Final = 20
HEARTBEAT_VALUE: Final = 1
# The device's own window. A gap longer than this means it has dropped Modbus
# control and re-inherited the app settings.
HEARTBEAT_LAPSE_S: Final = 60

# 0x0215, write-only. Bit 0 forces the system off-grid and bit 1 shuts it down, so a
# command touching either is refused before it reaches the wire. Bit 3 is the
# battery saver switch and bits 4-7 select the control method; the setpoint registers
# only take effect while their control method is selected here.
CONTROL_COMMAND_REGISTER: Final = 40534
CONTROL_COMMAND_UNSAFE_BITS: Final = 0b11
CONTROL_COMMAND_BATTERY_SAVER_BIT: Final = 3
CONTROL_COMMAND_METHOD_SHIFT: Final = 4
CONTROL_COMMAND_METHOD_MASK: Final = 0xF

ENERGY_RESOLUTION_KWH: Final = 0.01
STORAGE_VERSION: Final = 1
STATE_SAVE_DELAY_S: Final = 30
MODBUS_DISABLED_READ_THRESHOLD: Final = 3


# Home Assistant only introdcued UnitOfRatio in 2026.7, so for backwards compatibility
# we hardcode this
UNIT_OF_RATIO: Final = "%"

DEFAULT_INVERTER_MODEL: Final = InverterModel.POWEROCEAN_THREE_PHASE


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
    RegisterDef(
        "feed_in_power_max",
        40609,
        RegisterType.UINT32,
        address_overrides={InverterModel.POWEROCEAN_PLUS: 40538},
    ),
    RegisterDef("device_led_brightness", 40541, RegisterType.UINT16),
    # Setpoints that take effect the moment the matching control method is engaged.
    RegisterDef("system_power_setpoint", 40542, RegisterType.INT32),
    RegisterDef("inverter_power_setpoint", 40544, RegisterType.INT32),
    RegisterDef("limit_inv_power", 40546, RegisterType.UINT32),
    RegisterDef("limit_inv_max", 40548, RegisterType.UINT32),
    RegisterDef("battery_capacity", 40552, RegisterType.UINT32),
    RegisterDef("battery_discharge_power_limit", 40554, RegisterType.UINT32),
    RegisterDef("battery_charge_power_limit", 40556, RegisterType.UINT32),
    RegisterDef("battery_power_setpoint", 40571, RegisterType.INT32),
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

REGISTERS_BY_KEY: Final = {register.key: register for register in MODBUS_REGISTERS}


def register_blocks_for(inverter_model: InverterModel) -> tuple[RegisterBlock, ...]:
    """Return register blocks resolved for an inverter model."""
    return plan_blocks_for_model(MODBUS_REGISTERS, inverter_model)


SENSOR_MAP: list[SensorDef] = [
    SensorDef(
        key="system_modes",
        unit=None,
        device_class=None,
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
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
        unit=UNIT_OF_RATIO,
        device_class="battery",
        state_class="measurement",
    ),
    SensorDef(
        key="min_soc_limit",
        unit=UNIT_OF_RATIO,
        device_class="battery",
        state_class="measurement",
    ),
    SensorDef(
        key="battery_charge_power_limit",
        unit=UnitOfPower.WATT,
        device_class="power",
        state_class="measurement",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="battery_discharge_power_limit",
        unit=UnitOfPower.WATT,
        device_class="power",
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
            unit=UNIT_OF_RATIO,
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
        entity_category=EntityCategory.DIAGNOSTIC,
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
    *[
        SensorDef(
            key=key,
            unit=UnitOfPower.WATT,
            device_class="power",
            state_class="measurement",
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        for key in (
            "system_power_setpoint",
            "inverter_power_setpoint",
            "battery_power_setpoint",
        )
    ],
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
    BinarySensorDef(
        "system_fault",
        device_class="problem",
    ),
    BinarySensorDef(
        "system_power_on",
        device_class="running",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
]

# Whether the heartbeat currently holds control authority
MODBUS_CONTROL_BINARY_SENSOR: Final = BinarySensorDef(
    key="modbus_control",
    device_class="running",
)

BATTERY_SAVER_SWITCH: Final = SwitchDef(
    key="battery_saver_mode_control",
    entity_category=EntityCategory.CONFIG,
    icon="mdi:leaf",
)

CONTROL_FEATURES: Final[dict[ControlFeature, ControlFeatureDef]] = {
    ControlFeature.AUTOMATIC: ControlFeatureDef(method=ControlMode.DEFAULT),
    ControlFeature.HOLD_BATTERY: ControlFeatureDef(
        method=ControlMode.BATTERY_LIMITS,
        setpoint_key="battery_power_setpoint",
        measure_key="battery_power",
    ),
    ControlFeature.CHARGE_BATTERY: ControlFeatureDef(
        method=ControlMode.BATTERY_LIMITS,
        setpoint_key="battery_power_setpoint",
        sign=1,
        measure_key="battery_power",
        config_limit_key=CONF_MAX_BATTERY_CHARGED_POWER,
        default_power=2000.0,
    ),
    ControlFeature.DISCHARGE_BATTERY: ControlFeatureDef(
        method=ControlMode.BATTERY_LIMITS,
        setpoint_key="battery_power_setpoint",
        sign=-1,
        measure_key="battery_power",
        config_limit_key=CONF_MAX_BATTERY_DISCHARGED_POWER,
        default_power=2000.0,
    ),
    ControlFeature.EXPORT_TO_GRID: ControlFeatureDef(
        method=ControlMode.SYSTEM_FEED,
        setpoint_key="system_power_setpoint",
        sign=-1,
        measure_key="grid_power",
        limit_key="feed_in_power_max",
        default_power=3000.0,
    ),
}

# No entity category, so Home Assistant shows the mode and its power next to each
# other under Controls rather than mixed in with the settings that always apply.
BATTERY_MODE_SELECT: Final = ControlEntityDef(
    key="battery_mode",
    icon="mdi:home-battery",
)

# House loads step instantly and the battery needs a poll or two to absorb the step,
# so a single reading outside tolerance is a transient rather than a failed command.
CONTROL_STATUS_DAMPING_POLLS: Final = 3
# 0 means "no limit" to this device rather than "hold at zero", so holding the
# battery still needs a setpoint just above it.
HOLD_SETPOINT_W: Final = 1.0

# Not a device register: whether the selected mode is doing anything, and why not.
CONTROL_STATUS_SENSOR: Final = SensorDef(
    key="control_status",
    device_class="enum",
    options=tuple(str(status) for status in ControlStatus),
    icon="mdi:robot",
)

# Ceiling for a mode whose limit registers are all missing or read zero.
CONTROL_POWER_FALLBACK_MAX: Final = DEFAULT_MAX_POWER


# Map of all modbus registers available for writing operations.
WRITABLE_NUMBERS_MAP: list[NumberWritableDef] = [
    NumberWritableDef(
        key="min_soc_limit_control",
        read_key="min_soc_limit",
        name="Minimum SOC Limit",
        register=REGISTERS_BY_KEY["min_soc_limit"].address,
        min_value=0.0,
        max_value=100.0,
        step=1.0,
        unit=UNIT_OF_RATIO,
        device_class="battery",
        # The Plus stores this and never acts on it; writing would leave the sensor
        # reporting our value while the device keeps enforcing the app's.
        unsupported_models=(InverterModel.POWEROCEAN_PLUS,),
    ),
    NumberWritableDef(
        key="device_led_brightness_control",
        read_key="device_led_brightness",
        name="LED Brightness",
        register=REGISTERS_BY_KEY["device_led_brightness"].address,
        min_value=0.0,
        max_value=100.0,
        step=10.0,
        unit=UNIT_OF_RATIO,
        icon="mdi:led-on",
    ),
]
