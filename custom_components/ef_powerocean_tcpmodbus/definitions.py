"""The integration's entities: what each one shows, and how.

Each is defined as a Home Assistant entity description.

They live apart from the platforms because the coordinator and the energy
processor read them too: which counters there are, and how fast each can rise.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntityDescription,
)
from homeassistant.components.number import NumberEntityDescription
from homeassistant.components.select import SelectEntityDescription
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchEntityDescription
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
from homeassistant.helpers.entity import EntityDescription

from .const import UNIT_OF_RATIO
from .models import CoordinatorStatus
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
from .powerocean.registers import REGISTER_SIZES, REGISTERS_BY_KEY, RegisterType

if TYPE_CHECKING:
    from .control import ControlManager
    from .coordinator import EcoflowCoordinator


@dataclass(frozen=True, kw_only=True)
class EcoflowSensorEntityDescription(SensorEntityDescription):
    """A sensor showing one data key of the coordinator."""

    # Models whose register reads a constant 0, so the sensor would only mislead.
    unsupported_models: tuple[InverterModel, ...] = ()
    # Data keys published as state attributes, such as the raw word a value is
    # decoded from.
    attribute_keys: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class EcoflowEnergySensorEntityDescription(SensorEntityDescription):
    """An energy counter, which the energy processor checks before it is shown."""

    native_unit_of_measurement: str | None = UnitOfEnergy.KILO_WATT_HOUR
    device_class: SensorDeviceClass | None = SensorDeviceClass.ENERGY
    state_class: SensorStateClass | None = SensorStateClass.TOTAL_INCREASING
    is_calculated: bool = False
    resets_daily: bool = False
    # Config key of the power that bounds how fast the counter can rise.
    max_power: str | None = None
    # The lifetime counter a daily one is checked against.
    total_source: str | None = None


@dataclass(frozen=True, kw_only=True)
class EcoflowNumberEntityDescription(NumberEntityDescription):
    """A writable register."""

    # The register key the value is read back from.
    read_key: str
    register: int
    data_type: RegisterType = RegisterType.UINT16
    # Not implemented on every model: hidden from the entity list unless enabled.
    advanced: bool = False
    # Models whose firmware stores the write but never acts on it. Writing anyway
    # would leave the matching sensor reporting a value the device is not using.
    unsupported_models: tuple[InverterModel, ...] = ()

    @property
    def size(self) -> int:
        """Return how many 16-bit words the write occupies."""
        return REGISTER_SIZES[self.data_type]


# When set, the entity is only available while this returns True for the
# coordinator's current control status. None means always available.
type Availability = Callable[[ControlStatus], bool] | None


@dataclass(frozen=True, kw_only=True)
class ControlSelectEntityDescription(SelectEntityDescription):
    """A select that carries commanded state rather than a device register."""

    availability: Availability = None


@dataclass(frozen=True, kw_only=True)
class ControlNumberEntityDescription(NumberEntityDescription):
    """A number that carries commanded state rather than a device register."""

    availability: Availability = None


@dataclass(frozen=True, kw_only=True)
class EcoflowSwitchEntityDescription(SwitchEntityDescription):
    """A switch over a setting the control manager keeps."""

    is_on_fn: Callable[[EcoflowCoordinator], bool]
    turn_fn: Callable[[ControlManager, bool], Awaitable[None]]
    attributes_fn: Callable[[EcoflowCoordinator], dict[str, Any]] | None = None
    available_fn: Callable[[EcoflowCoordinator], bool] | None = None
    entity_category: EntityCategory | None = EntityCategory.CONFIG
    # An icon that overrides the one in icons.json while it returns one, for a
    # state the switch's on and off cannot show.
    icon_fn: Callable[[EcoflowCoordinator], str | None] | None = None


def shown_keys(description: EntityDescription) -> frozenset[str]:
    """Return the data keys an entity made from *description* shows."""
    keys = {description.key}
    match description:
        case EcoflowSensorEntityDescription():
            keys.update(description.attribute_keys)
        case EcoflowEnergySensorEntityDescription(total_source=str() as total_source):
            keys.add(total_source)
        case EcoflowNumberEntityDescription():
            keys.add(description.read_key)
    return frozenset(keys)


# The control method the device reports in bits 7-10 of the System Status (40530),
# with a value for any other reading.
ACTIVE_CONTROL_MODE_OPTIONS: Final = (
    *(str(mode) for mode in ControlMode),
    UNKNOWN_STATE,
)

SENSOR_MAP: list[EcoflowSensorEntityDescription] = [
    EcoflowSensorEntityDescription(
        key="system_modes",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="house_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EcoflowSensorEntityDescription(
        key="grid_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EcoflowSensorEntityDescription(
        key="solar_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EcoflowSensorEntityDescription(
        key="battery_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EcoflowSensorEntityDescription(
        key="battery_soc",
        native_unit_of_measurement=UNIT_OF_RATIO,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # A setting, not a battery level: as a battery sensor, Home Assistant would show
    # it as the battery's charge wherever it picks the device's first one.
    EcoflowSensorEntityDescription(
        key="min_soc_limit",
        native_unit_of_measurement=UNIT_OF_RATIO,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EcoflowSensorEntityDescription(
        key="battery_charge_power_limit",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="battery_discharge_power_limit",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="limit_inv_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="limit_inv_max",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="battery_capacity",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="battery_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="battery_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="battery_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="voltage_l1",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="voltage_l2",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="voltage_l3",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="current_l1",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="current_l2",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="current_l3",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="inverter_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="frequency",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="pv1_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="pv2_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="pv3_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="pv1_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="pv2_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="pv3_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    *[
        EcoflowSensorEntityDescription(
            key=key,
            native_unit_of_measurement=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        for key in (FEED_IN_POWER_MAX_KEY, FEED_IN_POWER_MAX_SETTING_KEY)
    ],
    EcoflowSensorEntityDescription(
        key=FEED_IN_POWER_MAX_EFFECTIVE_KEY,
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        unsupported_models=tuple(
            model
            for model in InverterModel
            if not model.traits.reports_effective_feed_cap
        ),
    ),
    EcoflowSensorEntityDescription(
        key="feed_in_power_max_percent",
        native_unit_of_measurement=UNIT_OF_RATIO,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        # Accepts and stores any value but acts on none of them (40573 probe).
        unsupported_models=(InverterModel.POWEROCEAN_PLUS,),
    ),
    EcoflowSensorEntityDescription(
        key="grid_connection_code",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="breaker_capacity",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="inverter_output_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="inverter_reactive_power",
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        device_class=SensorDeviceClass.REACTIVE_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    *[
        EcoflowSensorEntityDescription(
            key=f"grid_voltage_l{phase}",
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        for phase in (1, 2, 3)
    ],
    *[
        EcoflowSensorEntityDescription(
            key=f"grid_current_l{phase}",
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        for phase in (1, 2, 3)
    ],
    EcoflowSensorEntityDescription(
        key="active_control_mode",
        device_class=SensorDeviceClass.ENUM,
        options=list(ACTIVE_CONTROL_MODE_OPTIONS),
        entity_category=EntityCategory.DIAGNOSTIC,
        attribute_keys=("system_modes_hex",),
    ),
    EcoflowSensorEntityDescription(
        key="grid_feed_mode",
        device_class=SensorDeviceClass.ENUM,
        options=[str(option) for option in GridFeedMode],
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="inverter_rated_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="battery_count",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    *[
        EcoflowSensorEntityDescription(
            key=key,
            native_unit_of_measurement=UNIT_OF_RATIO,
            device_class=SensorDeviceClass.BATTERY,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        for key in BATTERY_SOC_KEYS
    ],
    EcoflowSensorEntityDescription(
        key="bat_remaining",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EcoflowSensorEntityDescription(
        key="pv1_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EcoflowSensorEntityDescription(
        key="pv2_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EcoflowSensorEntityDescription(
        key="pv3_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EcoflowSensorEntityDescription(
        key="bat_net_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="grid_mode",
        native_unit_of_measurement=None,
        device_class=SensorDeviceClass.ENUM,
        state_class=None,
        options=[str(option) for option in GridMode],
    ),
    EcoflowSensorEntityDescription(
        key="operating_mode",
        device_class=SensorDeviceClass.ENUM,
        options=[str(option) for option in OperatingMode],
    ),
    EcoflowSensorEntityDescription(
        key="system_alerts",
        attribute_keys=("system_state_2_hex",),
    ),
    *[
        EcoflowSensorEntityDescription(
            key=key,
            native_unit_of_measurement=UnitOfPower.WATT,
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
    EcoflowSensorEntityDescription(
        key="fault_count",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        attribute_keys=("system_state_2_hex",),
    ),
    EcoflowSensorEntityDescription(
        key="active_faults",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="coordinator_status",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        options=[str(option) for option in CoordinatorStatus],
    ),
    EcoflowSensorEntityDescription(
        key="protocol_version",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowSensorEntityDescription(
        key="device_address",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
]


ENERGY_SENSOR_MAP: list[EcoflowEnergySensorEntityDescription] = [
    EcoflowEnergySensorEntityDescription(
        key="grid_import_total", max_power=CONF_MAX_GRID_POWER
    ),
    EcoflowEnergySensorEntityDescription(
        key="grid_import_today",
        resets_daily=True,
        max_power=CONF_MAX_GRID_POWER,
        total_source="grid_import_total",
    ),
    EcoflowEnergySensorEntityDescription(
        key="grid_export_total", max_power=CONF_MAX_SOLAR_POWER
    ),
    EcoflowEnergySensorEntityDescription(
        key="grid_export_today",
        resets_daily=True,
        max_power=CONF_MAX_SOLAR_POWER,
        total_source="grid_export_total",
    ),
    EcoflowEnergySensorEntityDescription(
        key="bat_charged_total", max_power=CONF_MAX_BATTERY_CHARGED_POWER
    ),
    EcoflowEnergySensorEntityDescription(
        key="bat_charged_today",
        resets_daily=True,
        max_power=CONF_MAX_BATTERY_CHARGED_POWER,
        total_source="bat_charged_total",
    ),
    EcoflowEnergySensorEntityDescription(
        key="bat_discharged_total", max_power=CONF_MAX_BATTERY_DISCHARGED_POWER
    ),
    EcoflowEnergySensorEntityDescription(
        key="bat_discharged_today",
        resets_daily=True,
        max_power=CONF_MAX_BATTERY_DISCHARGED_POWER,
        total_source="bat_discharged_total",
    ),
    EcoflowEnergySensorEntityDescription(
        key="solar_total", max_power=CONF_MAX_SOLAR_POWER
    ),
    EcoflowEnergySensorEntityDescription(
        key="solar_today",
        resets_daily=True,
        max_power=CONF_MAX_SOLAR_POWER,
        total_source="solar_total",
    ),
    EcoflowEnergySensorEntityDescription(
        key="inverter_ac_in_total",
        max_power=CONF_MAX_GRID_POWER,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowEnergySensorEntityDescription(
        key="inverter_ac_in_today",
        resets_daily=True,
        max_power=CONF_MAX_GRID_POWER,
        total_source="inverter_ac_in_total",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowEnergySensorEntityDescription(
        key="inverter_ac_out_total",
        max_power=CONF_MAX_GRID_POWER,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowEnergySensorEntityDescription(
        key="inverter_ac_out_today",
        resets_daily=True,
        max_power=CONF_MAX_GRID_POWER,
        total_source="inverter_ac_out_total",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EcoflowEnergySensorEntityDescription(
        key="house_energy_today",
        is_calculated=True,
        resets_daily=True,
        max_power=CONF_MAX_GRID_POWER,
    ),
    EcoflowEnergySensorEntityDescription(
        key="house_energy_total",
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
DAILY_ENERGY_SENSORS_DEVICE_RAW: list[EcoflowSensorEntityDescription] = [
    EcoflowSensorEntityDescription(
        key=f"{energy_sensor.key}_raw",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
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
TOTAL_ENERGY_SENSORS_DEVICE_RAW: list[EcoflowSensorEntityDescription] = [
    EcoflowSensorEntityDescription(
        key=f"{energy_sensor.key}_raw",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=None,
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    for energy_sensor in ENERGY_SENSOR_MAP
    if not energy_sensor.is_calculated and not energy_sensor.resets_daily
]


BINARY_SENSOR_MAP: list[BinarySensorEntityDescription] = [
    BinarySensorEntityDescription(key="self_use_mode_ena"),
    BinarySensorEntityDescription(key="intelligent_mode_ena"),
    BinarySensorEntityDescription(
        key="battery_heating",
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
    BinarySensorEntityDescription(
        key="system_fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    BinarySensorEntityDescription(
        key="system_power_on",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Bits 11 and 12 of the System Status (40530).
    BinarySensorEntityDescription(
        key="device_modbus_control",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BinarySensorEntityDescription(
        key="bms_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
]

# Whether the heartbeat currently holds control authority
MODBUS_CONTROL_BINARY_SENSOR: Final = BinarySensorEntityDescription(
    key="modbus_control",
    device_class=BinarySensorDeviceClass.RUNNING,
)

# On while a check shows readings to be wrong, whether or not Modbus Control is on.
READING_CHECK_BINARY_SENSOR: Final = BinarySensorEntityDescription(
    key="reading_check",
    device_class=BinarySensorDeviceClass.PROBLEM,
    entity_category=EntityCategory.DIAGNOSTIC,
)

# No entity category, so Home Assistant shows the mode and its power next to each
# other under Controls rather than mixed in with the settings that always apply.
BATTERY_MODE_SELECT: Final = ControlSelectEntityDescription(
    key="battery_mode",
    availability=requires_modbus_control,
)

CHARGE_LIMIT_SOC_NUMBER: Final = ControlNumberEntityDescription(
    key="charge_limit_soc",
    entity_category=EntityCategory.CONFIG,
    availability=requires_modbus_control,
)
BATTERY_RESERVE_SOC_NUMBER: Final = ControlNumberEntityDescription(
    key="battery_reserve_soc",
    entity_category=EntityCategory.CONFIG,
    availability=requires_modbus_control,
)

# Not a device register: whether the selected mode is doing anything, and why not.
CONTROL_STATUS_SENSOR: Final = EcoflowSensorEntityDescription(
    key="control_status",
    device_class=SensorDeviceClass.ENUM,
    options=[str(status) for status in ControlStatus],
)

# Not a device register: the reserve the battery is actually held at, as the control
# manager keeps it. The app's reserve it reads is a control input, so always polled.
EFFECTIVE_RESERVE_SENSOR: Final = EcoflowSensorEntityDescription(
    key="battery_reserve_effective",
    native_unit_of_measurement=UNIT_OF_RATIO,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=0,
)

# The control test the run_control_test action starts; its states are those of
# tools/control_test.ControlTestState.
CONTROL_TEST_SENSOR: Final = EcoflowSensorEntityDescription(
    key="control_test",
    device_class=SensorDeviceClass.ENUM,
    entity_category=EntityCategory.DIAGNOSTIC,
    options=["idle", "running", "done", "aborted", "failed"],
)


# Map of all modbus registers available for writing operations.
WRITABLE_NUMBERS_MAP: list[EcoflowNumberEntityDescription] = [
    EcoflowNumberEntityDescription(
        key="device_led_brightness_control",
        read_key="device_led_brightness",
        register=REGISTERS_BY_KEY["device_led_brightness"].address,
        native_min_value=0.0,
        native_max_value=100.0,
        native_step=10.0,
        native_unit_of_measurement=UNIT_OF_RATIO,
    ),
]
