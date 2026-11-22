"""Types describing the Home Assistant entities of the integration.

The values that fill these in live in const.py; this module must not import it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.number import NumberDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    EntityCategory,
    UnitOfEnergy,
)

from .powerocean.models import (
    ControlStatus,
    InverterModel,
)
from .powerocean.registers import (
    REGISTER_SIZES,
    RegisterType,
)

if TYPE_CHECKING:
    from .control import ControlManager
    from .coordinator import EcoflowCoordinator


class AlertVerbosity(StrEnum):
    """How much the integration raises in repairs, from least to most.

    Each level raises everything the one before it does.
    """

    # What the user must act on: the inverter stopped updating, Modbus is off, it
    # does not answer, totals are held, another inverter answers.
    ESSENTIAL = "essential"
    # Also readings that cannot be trusted: a failed reading check, a counter
    # going back, sensors unavailable unexplained, control not accepted.
    READINGS = "readings"
    # Also the connection's health: reconnects, late polls, control writes with
    # control off, power that does not add up.
    DETAILED = "detailed"
    # Also every warning and error the integration logs.
    DEBUG = "debug"

    @property
    def rank(self) -> int:
        return list(AlertVerbosity).index(self)


class CoordinatorStatus(StrEnum):
    SUCCESS = "success"
    READ_FAILED = "read_failed"
    RECONNECT_FAILED = "reconnect_failed"
    PROCESSING_FAILED = "processing_failed"
    IDENTITY_MISMATCH = "identity_mismatch"
    # The poll succeeded, but the inverter stopped updating what it serves.
    READINGS_STALLED = "readings_stalled"


@dataclass(frozen=True)
class ControlEntityDef:
    """An entity that carries commanded state rather than a device register."""

    key: str
    entity_category: EntityCategory | None = None
    # When set, the entity is only available while this returns True for the
    # coordinator's current control status. None means always available.
    availability: Callable[[ControlStatus], bool] | None = None


@dataclass(frozen=True)
class SensorDef:
    key: str
    name: str | None = None
    unit: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    entity_category: EntityCategory | None = None
    options: tuple[str, ...] | None = None
    # Models whose register reads a constant 0, so the sensor would only mislead.
    unsupported_models: tuple[InverterModel, ...] = ()
    # Data keys published as state attributes, such as the raw word a value is
    # decoded from.
    attribute_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnergySensorDef:
    key: str
    name: str | None = None
    unit: str = UnitOfEnergy.KILO_WATT_HOUR
    is_calculated: bool = False
    resets_daily: bool = False
    # Config key of the power that bounds how fast the counter can rise.
    max_power: str | None = None
    # The lifetime counter a daily one is checked against.
    total_source: str | None = None
    device_class: SensorDeviceClass = SensorDeviceClass.ENERGY
    state_class: SensorStateClass = SensorStateClass.TOTAL_INCREASING
    entity_category: EntityCategory | None = None


@dataclass(frozen=True)
class BinarySensorDef:
    key: str
    name: str | None = None
    device_class: BinarySensorDeviceClass | None = None
    entity_category: EntityCategory | None = None


@dataclass(frozen=True, kw_only=True)
class SwitchDef:
    key: str
    is_on: Callable[[EcoflowCoordinator], bool]
    turn: Callable[[ControlManager, bool], Awaitable[None]]
    attributes: Callable[[EcoflowCoordinator], dict[str, Any]] | None = None
    available: Callable[[EcoflowCoordinator], bool] | None = None
    entity_category: EntityCategory | None = EntityCategory.CONFIG
    # An icon that overrides the one in icons.json while it returns one, for a
    # state the switch's on and off cannot show.
    icon: Callable[[EcoflowCoordinator], str | None] | None = None


@dataclass(frozen=True)
class NumberWritableDef:
    key: str
    # The register key the value is read back from.
    read_key: str
    name: str
    register: int
    min_value: float
    max_value: float
    step: float
    data_type: RegisterType = RegisterType.UINT16
    unit: str | None = None
    device_class: NumberDeviceClass | None = None
    # Not implemented on every model: hidden from the entity list unless enabled.
    advanced: bool = False
    # Models whose firmware stores the write but never acts on it. Writing anyway
    # would leave the matching sensor reporting a value the device is not using.
    unsupported_models: tuple[InverterModel, ...] = ()

    @property
    def size(self) -> int:
        """Return how many 16-bit words the write occupies."""
        return REGISTER_SIZES[self.data_type]


type EntityDef = (
    SensorDef
    | EnergySensorDef
    | BinarySensorDef
    | NumberWritableDef
    | ControlEntityDef
    | SwitchDef
)


def shown_keys(definition: EntityDef) -> frozenset[str]:
    """Return the data keys an entity made from *definition* shows."""
    keys = {definition.key}
    match definition:
        case SensorDef():
            keys.update(definition.attribute_keys)
        case EnergySensorDef(total_source=str() as total_source):
            keys.add(total_source)
        case NumberWritableDef():
            keys.add(definition.read_key)
    return frozenset(keys)
