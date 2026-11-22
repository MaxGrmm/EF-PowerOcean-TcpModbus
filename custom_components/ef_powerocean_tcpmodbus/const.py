"""Constants for EF-PowerOcean-TcpModbus integration."""

from __future__ import annotations

from typing import Final

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactivePower,
    UnitOfTemperature,
)

from .models import (
    BinarySensorDef,
    ControlEntityDef,
    CoordinatorStatus,
    EnergySensorDef,
    NumberWritableDef,
    SensorDef,
)
from .powerocean.const import (
    BATTERY_SOC_KEYS,
    CONF_MAX_BATTERY_CHARGED_POWER,
    CONF_MAX_BATTERY_DISCHARGED_POWER,
    CONF_MAX_GRID_POWER,
    CONF_MAX_SOLAR_POWER,
    FEED_IN_POWER_MAX_EFFECTIVE_KEY,
    FEED_IN_POWER_MAX_KEY,
    FEED_IN_POWER_MAX_SETTING_KEY,
)
from .powerocean.models import (
    UNKNOWN_STATE,
    ControlMode,
    ControlStatus,
    GridFeedMode,
    GridMode,
    InverterModel,
    OperatingMode,
    requires_modbus_control,
)
from .powerocean.registers import (
    REGISTERS_BY_KEY,
)

DOMAIN: Final = "ef_powerocean_tcpmodbus"
EVENT_COMMAND_EXPIRED: Final = f"{DOMAIN}_command_expired"
EVENT_CONTROL_TEST_STARTED: Final = f"{DOMAIN}_control_test_started"
EVENT_CONTROL_TEST_FINISHED: Final = f"{DOMAIN}_control_test_finished"
# A reading check failed for long enough to act on, and passes again; see
# reading_checks.py. Control steps aside in between while it is on.
EVENT_READING_CHECK_FAILED: Final = f"{DOMAIN}_reading_check_failed"
EVENT_READING_CHECK_CLEARED: Final = f"{DOMAIN}_reading_check_cleared"
# Read on a reconnect, so it often marks the end of an outage an update caused.
EVENT_FIRMWARE_CHANGED: Final = f"{DOMAIN}_firmware_changed"
ATTR_FROM: Final = "from"
ATTR_TO: Final = "to"
ATTR_STATUS: Final = "status"
ATTR_CHECK: Final = "check"
ATTR_MODE: Final = "mode"
DEFAULT_PORT: Final = 502
DEFAULT_SLAVE: Final = 1
DEFAULT_SCAN_INTERVAL_S: Final = 5
DEFAULT_BATTERY_COUNT: Final = 0
DEFAULT_MAX_SOLAR_POWER: Final = 12000
DEFAULT_MAX_GRID_POWER: Final = 15000

CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_BATTERY_COUNT: Final = "battery_count"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_CALC_SOLAR_POWER: Final = "calc_solar_power"
CONF_CALC_GRID_ENERGY: Final = "calc_grid_energy"
# How much the integration raises in repairs, beyond what the user must act on.
CONF_ALERT_VERBOSITY: Final = "alert_verbosity"
CONF_MODBUS_CONTROL: Final = "modbus_control"
CONF_INVERTER_MODEL: Final = "inverter_model"
# kWh added to what the device reports for a lifetime total, by its key. Set by the
# user for a counter the inverter reset, so the total continues as in EcoFlow's portal.
CONF_ENERGY_OFFSETS: Final = "energy_offsets"

SLEEP_TIME_AFTER_RECONNECT_S: Final = 1

# Inverters on Wi-Fi can answer slowly (#42). A request and its resend hold the
# client lock for up to about two of these, well inside the heartbeat's window.
MODBUS_REQUEST_TIMEOUT_S: Final = 10
# Room for a request that times out and its retry, and well under the sum of all
# request timeouts, which could pass minutes.
MODBUS_POLL_BUDGET_S: Final = 25
STORAGE_VERSION: Final = 1
STATE_SAVE_DELAY_S: Final = 30
# Where a user reports a held total they believe is wrong, with the bug form's
# fields filled in from the device.
BUG_REPORT_URL: Final = "https://github.com/MaxGrmm/EF-PowerOcean-TcpModbus/issues/new"
MODBUS_DISABLED_READ_THRESHOLD: Final = 3


# Home Assistant only introdcued UnitOfRatio in 2026.7, so for backwards compatibility
# we hardcode this
UNIT_OF_RATIO: Final = "%"

DEFAULT_INVERTER_MODEL: Final = InverterModel.POWEROCEAN_THREE_PHASE

# Polls in a row an optional read may time out or lose the link before it is no
# longer polled.
OPTIONAL_STALLS_TO_DROP: Final = 3


# The control method the device reports in bits 7-10 of the System Status (40530),
# with a value for any other reading.
ACTIVE_CONTROL_MODE_OPTIONS: Final = (
    *(str(mode) for mode in ControlMode),
    UNKNOWN_STATE,
)

SENSOR_MAP: list[SensorDef] = [
    SensorDef(
        key="system_modes",
        unit=None,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="house_power",
        unit=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorDef(
        key="grid_power",
        unit=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorDef(
        key="solar_power",
        unit=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorDef(
        key="battery_power",
        unit=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorDef(
        key="battery_soc",
        unit=UNIT_OF_RATIO,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # A setting, not a battery level: as a battery sensor, Home Assistant would show
    # it as the battery's charge wherever it picks the device's first one.
    SensorDef(
        key="min_soc_limit",
        unit=UNIT_OF_RATIO,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorDef(
        key="battery_charge_power_limit",
        unit=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="battery_discharge_power_limit",
        unit=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="limit_inv_power",
        unit=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="limit_inv_max",
        unit=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="battery_capacity",
        unit=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="battery_voltage",
        unit=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="battery_current",
        unit=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="battery_temperature",
        unit=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="voltage_l1",
        unit=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="voltage_l2",
        unit=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="voltage_l3",
        unit=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="current_l1",
        unit=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="current_l2",
        unit=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="current_l3",
        unit=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="inverter_temperature",
        unit=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="frequency",
        unit=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="pv1_voltage",
        unit=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="pv2_voltage",
        unit=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="pv3_voltage",
        unit=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="pv1_current",
        unit=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="pv2_current",
        unit=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="pv3_current",
        unit=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    *[
        SensorDef(
            key=key,
            unit=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        for key in (FEED_IN_POWER_MAX_KEY, FEED_IN_POWER_MAX_SETTING_KEY)
    ],
    SensorDef(
        key=FEED_IN_POWER_MAX_EFFECTIVE_KEY,
        unit=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        unsupported_models=tuple(
            model
            for model in InverterModel
            if not model.traits.reports_effective_feed_cap
        ),
    ),
    SensorDef(
        key="feed_in_power_max_percent",
        unit=UNIT_OF_RATIO,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        # Accepts and stores any value but acts on none of them (40573 probe).
        unsupported_models=(InverterModel.POWEROCEAN_PLUS,),
    ),
    SensorDef(
        key="grid_connection_code",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="breaker_capacity",
        unit=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="inverter_output_power",
        unit=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="inverter_reactive_power",
        unit=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        device_class=SensorDeviceClass.REACTIVE_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    *[
        SensorDef(
            key=f"grid_voltage_l{phase}",
            unit=UnitOfElectricPotential.VOLT,
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        for phase in (1, 2, 3)
    ],
    *[
        SensorDef(
            key=f"grid_current_l{phase}",
            unit=UnitOfElectricCurrent.AMPERE,
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        for phase in (1, 2, 3)
    ],
    SensorDef(
        key="active_control_mode",
        device_class=SensorDeviceClass.ENUM,
        options=ACTIVE_CONTROL_MODE_OPTIONS,
        entity_category=EntityCategory.DIAGNOSTIC,
        attribute_keys=("system_modes_hex",),
    ),
    SensorDef(
        key="grid_feed_mode",
        device_class=SensorDeviceClass.ENUM,
        options=tuple(GridFeedMode),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="inverter_rated_power",
        unit=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="battery_count",
        unit=None,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    *[
        SensorDef(
            key=key,
            unit=UNIT_OF_RATIO,
            device_class=SensorDeviceClass.BATTERY,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        for key in BATTERY_SOC_KEYS
    ],
    SensorDef(
        key="bat_remaining",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorDef(
        key="pv1_power",
        unit=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorDef(
        key="pv2_power",
        unit=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorDef(
        key="pv3_power",
        unit=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorDef(
        key="bat_net_energy",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="grid_mode",
        unit=None,
        device_class=SensorDeviceClass.ENUM,
        state_class=None,
        options=tuple(GridMode),
    ),
    SensorDef(
        key="operating_mode",
        device_class=SensorDeviceClass.ENUM,
        options=tuple(OperatingMode),
    ),
    SensorDef(
        key="system_alerts",
        attribute_keys=("system_state_2_hex",),
    ),
    *[
        SensorDef(
            key=key,
            unit=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
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
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        attribute_keys=("system_state_2_hex",),
    ),
    SensorDef(
        key="active_faults",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="coordinator_status",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        options=tuple(CoordinatorStatus),
    ),
    SensorDef(
        key="protocol_version",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        key="device_address",
        entity_category=EntityCategory.DIAGNOSTIC,
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
        "inverter_ac_in_total",
        max_power=CONF_MAX_GRID_POWER,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EnergySensorDef(
        "inverter_ac_in_today",
        resets_daily=True,
        max_power=CONF_MAX_GRID_POWER,
        total_source="inverter_ac_in_total",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EnergySensorDef(
        "inverter_ac_out_total",
        max_power=CONF_MAX_GRID_POWER,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EnergySensorDef(
        "inverter_ac_out_today",
        resets_daily=True,
        max_power=CONF_MAX_GRID_POWER,
        total_source="inverter_ac_out_total",
        entity_category=EntityCategory.DIAGNOSTIC,
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

# The counters the device reports, as opposed to the calculated ones. They arrive in
# kWh, or in Wh on a model whose traits say energy_in_watt_hours.
DEVICE_ENERGY_KEYS: Final = frozenset(
    energy_sensor.key
    for energy_sensor in ENERGY_SENSOR_MAP
    if not energy_sensor.is_calculated
)


# The daily sensors have been shown to not reliably reset at midnight. They are
# therefore calculated using the respective total sensor, but we still expose the
# device raw values under a *_raw diagnostic key. This is also used for the initial
# snapshot when initializing the daily counters for the first time after installing.
DAILY_ENERGY_SENSORS_DEVICE_RAW: list[SensorDef] = [
    SensorDef(
        key=f"{energy_sensor.key}_raw",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    for energy_sensor in ENERGY_SENSOR_MAP
    if energy_sensor.total_source is not None
]


# The device's own lifetime counters, next to the validated totals, so a held total
# can be told apart from a device that reports the same. No state class: a raw
# counter can glitch or reset, and must never feed the Energy dashboard or its
# long-term statistics.
TOTAL_ENERGY_SENSORS_DEVICE_RAW: list[SensorDef] = [
    SensorDef(
        key=f"{energy_sensor.key}_raw",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=None,
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    for energy_sensor in ENERGY_SENSOR_MAP
    if not energy_sensor.is_calculated and not energy_sensor.resets_daily
]


BINARY_SENSOR_MAP: list[BinarySensorDef] = [
    BinarySensorDef("self_use_mode_ena"),
    BinarySensorDef("intelligent_mode_ena"),
    BinarySensorDef(
        "system_fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    BinarySensorDef(
        "system_power_on",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Bits 11 and 12 of the System Status (40530).
    BinarySensorDef(
        "device_modbus_control",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BinarySensorDef(
        "bms_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
]

# Whether the heartbeat currently holds control authority
MODBUS_CONTROL_BINARY_SENSOR: Final = BinarySensorDef(
    key="modbus_control",
    device_class=BinarySensorDeviceClass.RUNNING,
)

# On while a check shows readings to be wrong, whether or not Modbus Control is on.
READING_CHECK_BINARY_SENSOR: Final = BinarySensorDef(
    key="reading_check",
    device_class=BinarySensorDeviceClass.PROBLEM,
    entity_category=EntityCategory.DIAGNOSTIC,
)

# No entity category, so Home Assistant shows the mode and its power next to each
# other under Controls rather than mixed in with the settings that always apply.
BATTERY_MODE_SELECT: Final = ControlEntityDef(
    key="battery_mode",
    availability=requires_modbus_control,
)

CHARGE_LIMIT_SOC_NUMBER: Final = ControlEntityDef(
    key="charge_limit_soc",
    entity_category=EntityCategory.CONFIG,
    availability=requires_modbus_control,
)
BATTERY_RESERVE_SOC_NUMBER: Final = ControlEntityDef(
    key="battery_reserve_soc",
    entity_category=EntityCategory.CONFIG,
    availability=requires_modbus_control,
)
# The number that wrote it before the Battery Reserve took over, removed on setup.
RETIRED_MIN_SOC_NUMBER_KEY: Final = "min_soc_limit_control"
# Options: native or emulated Battery Reserve, prefilled with the model's.
CONF_BATTERY_RESERVE: Final = "battery_reserve"
# How many failed reading checks keep a snapshot of the readings for diagnostics.
READING_CHECK_SNAPSHOTS: Final = 5

# Not a device register: whether the selected mode is doing anything, and why not.
CONTROL_STATUS_SENSOR: Final = SensorDef(
    key="control_status",
    device_class=SensorDeviceClass.ENUM,
    options=tuple(str(status) for status in ControlStatus),
)

# Not a device register: the reserve the battery is actually held at, as the control
# manager keeps it. The app's reserve it reads is a control input, so always polled.
EFFECTIVE_RESERVE_SENSOR: Final = SensorDef(
    key="battery_reserve_effective",
    unit=UNIT_OF_RATIO,
    state_class=SensorStateClass.MEASUREMENT,
)

# The control test the run_control_test action starts; its states are those of
# control_test.ControlTestState.
CONTROL_TEST_SENSOR: Final = SensorDef(
    key="control_test",
    device_class=SensorDeviceClass.ENUM,
    entity_category=EntityCategory.DIAGNOSTIC,
    options=("idle", "running", "done", "aborted", "failed"),
)


# Map of all modbus registers available for writing operations.
WRITABLE_NUMBERS_MAP: list[NumberWritableDef] = [
    NumberWritableDef(
        key="device_led_brightness_control",
        read_key="device_led_brightness",
        name="LED Brightness",
        register=REGISTERS_BY_KEY["device_led_brightness"].address,
        min_value=0.0,
        max_value=100.0,
        step=10.0,
        unit=UNIT_OF_RATIO,
    ),
]
