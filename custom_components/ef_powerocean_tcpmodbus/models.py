"""Types describing PowerOcean devices, Modbus registers and Home Assistant entities.

The values that fill these in live in const.py; this module must not import it.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import IntEnum, StrEnum
from typing import Final, NamedTuple

from homeassistant.const import EntityCategory, UnitOfEnergy

MAX_REGISTERS_PER_READ: Final = 125
# Reading a few unused registers is cheaper than a second round trip, so registers
# closer together than this share one request.
MAX_REGISTER_GAP: Final = 48

# A commanded setpoint is never met exactly. The inverter reaches a new setpoint
# within a poll or two, but house load steps instantly and the battery takes a moment
# to give up the difference. We therefore have some tolerance to prevent over adjusting.
POWER_TOLERANCE_W: Final = 500.0
POWER_TOLERANCE_FRACTION: Final = 0.15
# SOC readings are whole percent, so leave room rather than testing for exactly 100.
BATTERY_FULL_SOC: Final = 99.0
BATTERY_EMPTY_MARGIN_SOC: Final = 1.0


class ProductCategory(IntEnum):
    """The phase count the device reports."""

    THREE_PHASE = 1
    SINGLE_PHASE = 2


class ProductId(NamedTuple):
    """What a model answers in the product registers.

    A category of None matches whatever the device reports, which is how the
    models that need no tiebreak are listed.
    """

    number: int
    category: ProductCategory | None = None


@dataclass(frozen=True, slots=True)
class ModelTraits:
    """What sets one model apart from the rest of the family.

    The defaults describe every PowerOcean; the three-phase Ocean 2 is so far the
    only model that speaks Modbus differently.
    """

    display_name: str
    # Filters out phantom string power when the PV input is not producing yet.
    startup_voltage: int
    product_ids: tuple[ProductId, ...] = ()
    # Whether the device publishes 32-bit values high word first. Reading them the
    # wrong way round leaves the low word empty, which turns a 10 kW rating into
    # 655360000 and voltages into denormals that round to zero.
    high_word_first: bool = False
    # How far apart two registers may be and still share one read.
    max_register_gap: int = MAX_REGISTER_GAP

    def identifies(
        self, product_number: int | None, product_category: int | None
    ) -> bool:
        return any(
            product.number == product_number
            and product.category in (None, product_category)
            for product in self.product_ids
        )


class InverterModel(StrEnum):
    POWEROCEAN_SINGLE_PHASE = "powerocean_single_phase"
    POWEROCEAN_THREE_PHASE = "powerocean_three_phase"
    POWEROCEAN_PLUS = "powerocean_plus"
    POWEROCEAN_DC_FIT = "powerocean_dc_fit"
    # The three-phase Ocean 2 shipped as plain "ocean_2", which config entries
    # already store, so its value stays as it is.
    OCEAN_2_THREE_PHASE = "ocean_2"
    OCEAN_2_SINGLE_PHASE = "ocean_2_single_phase"

    @property
    def traits(self) -> ModelTraits:
        return MODEL_TRAITS[self]

    @classmethod
    def from_product_info(
        cls, product_number: int | None, product_category: int | None
    ) -> InverterModel | None:
        """Map the device's product registers to a model, or None if unknown.

        The first match wins, so a model that needs the category to be told apart
        is listed before the one that takes the product number on its own.
        """
        return next(
            (
                model
                for model, traits in MODEL_TRAITS.items()
                if traits.identifies(product_number, product_category)
            ),
            None,
        )


# Startup voltages come from the datasheet linked above each model. The single
# phase has no such specification, so its value is deduced from the MPPT range.
# The DC Fit has no product id because nobody has reported one yet, so it is only
# picked by hand.
MODEL_TRAITS: Final[Mapping[InverterModel, ModelTraits]] = {
    # https://enterprise-service-eu-cdn.ecoflow.com/enterprise/content/2024-03-27-1485da5d-eae4-4a38-830a-4e340517d968.pdf
    InverterModel.POWEROCEAN_SINGLE_PHASE: ModelTraits(
        "PowerOcean Single Phase",
        startup_voltage=90,
        product_ids=(
            ProductId(1, ProductCategory.SINGLE_PHASE),
            ProductId(2),
        ),
    ),
    # https://enterprise-service-eu-cdn.ecoflow.com/enterprise/documentation/1772090325968/EcoFlow%20PowerOcean%20(Three-phase)_Datasheet_EN.pdf
    InverterModel.POWEROCEAN_THREE_PHASE: ModelTraits(
        "PowerOcean Three Phase",
        startup_voltage=160,
        product_ids=(ProductId(1, ProductCategory.THREE_PHASE),),
    ),
    # https://enterprise-service-eu-cdn.ecoflow.com/enterprise/documentation/1754035729875/PowerOcean%20Plus%20(three-phase)_Brochure_20241223_EN.pdf
    InverterModel.POWEROCEAN_PLUS: ModelTraits(
        "PowerOcean Plus",
        startup_voltage=160,
        product_ids=(ProductId(3),),
    ),
    # https://enterprise-service-eu-cdn.ecoflow.com/enterprise/documentation/1735192805714/EcoFlow%20PowerOcean%20DC%20Fit_Datasheet_EN_20241225.pdf
    InverterModel.POWEROCEAN_DC_FIT: ModelTraits(
        "PowerOcean DC Fit",
        startup_voltage=90,
    ),
    # https://enterprise-service-eu-cdn.ecoflow.com/enterprise/documentation/1779447439219/OCEAN%202%20Three-Phase_Datasheet_EN_260522.pdf
    InverterModel.OCEAN_2_THREE_PHASE: ModelTraits(
        "Ocean 2 Three Phase",
        startup_voltage=120,
        product_ids=(ProductId(4, ProductCategory.THREE_PHASE),),
        high_word_first=True,
        # It rejects the whole request when it reaches over an address it does not
        # implement, so only neighbouring registers can share a read.
        max_register_gap=0,
    ),
    # Nobody has scanned one yet, so this entry follows the three-phase Ocean 2:
    # same product number, same Modbus dialect, phase told apart by the category.
    # A single-phase Ocean 2 on older firmware has been seen reporting number 2
    # instead, which is the PowerOcean single phase above and reads like it.
    InverterModel.OCEAN_2_SINGLE_PHASE: ModelTraits(
        "Ocean 2 Single Phase",
        # No startup voltage is published; the PowerOcean single phase figure
        # stands in until someone with the device reports a better one.
        startup_voltage=90,
        product_ids=(ProductId(4, ProductCategory.SINGLE_PHASE),),
        high_word_first=True,
        max_register_gap=0,
    ),
}


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


class ControlMode(StrEnum):
    """Control method the device follows.

    Commanded through bits 4-7 of the System Control Command (0x0215).
    """

    DEFAULT = "default"
    SYSTEM_FEED = "system_feed"
    INVERTER_FEED = "inverter_feed"
    BATTERY_LIMITS = "battery_limits"

    @property
    def command_value(self) -> int:
        """Return the protocol enumeration value."""
        return {
            ControlMode.DEFAULT: 0,
            ControlMode.SYSTEM_FEED: 1,
            ControlMode.INVERTER_FEED: 2,
            ControlMode.BATTERY_LIMITS: 3,
        }[self]


class ControlFeature(StrEnum):
    """The control that the inverter should follow.

    The protocol follows a single control method, so these are the options of one
    select rather than independent toggles.
    """

    AUTOMATIC = "automatic"
    HOLD_BATTERY = "hold_battery"
    CHARGE_BATTERY = "charge_battery"
    DISCHARGE_BATTERY = "discharge_battery"
    EXPORT_TO_GRID = "export_to_grid"


class ControlStatus(StrEnum):
    """What the inverter is doing about the selected mode."""

    NO_MODBUS_CONTROL = "no_modbus_control"
    AUTOMATIC = "automatic"
    CHARGE_LIMIT_REACHED = "charge_limit_reached"
    RESERVE_REACHED = "reserve_reached"
    HOLD_NOT_NEEDED = "hold_not_needed"
    ACTIVE = "active"
    RAMPING = "ramping"
    UNREACHABLE_BATTERY_FULL = "unreachable_battery_full"
    UNREACHABLE_BATTERY_EMPTY = "unreachable_battery_empty"


def requires_modbus_control(status: ControlStatus) -> bool:
    """Return whether the device is following Modbus commands at all.

    Used as an entity availability rule: a control the inverter would store and
    ignore is shown as unavailable rather than pretending to work.
    """
    return status is not ControlStatus.NO_MODBUS_CONTROL


@dataclass(frozen=True)
class ControlFeatureDef:
    """A mode and the single instruction it sends."""

    method: ControlMode
    # Read key of the setpoint register the method acts on
    setpoint_key: str | None = None
    # Whether the power should be considered positive or negative. +1 for charging, -1 for discharging
    sign: int = 1
    # Sensor that this control measures against, e.g. battery_power for the battery_power_setpoint
    measure_key: str | None = None
    # Sensor that stores the maximum allowed value for this mode, if available
    limit_key: str | None = None
    # Configuration that stores the maximum allowed value for this mode, if available
    config_limit_key: str | None = None
    # None for a mode with no power to configure, which only holds the battery.
    default_power: float | None = None

    @property
    def commands_power(self) -> bool:
        return self.setpoint_key is not None

    @property
    def has_power(self) -> bool:
        return self.default_power is not None

    @property
    def direction(self) -> int:
        """Return +1 while charging the battery, -1 while draining it, 0 for neither."""
        return self.sign if self.has_power else 0


@dataclass(frozen=True)
class ControlEntityDef:
    """An entity that carries commanded state rather than a device register."""

    key: str
    icon: str | None = None
    entity_category: EntityCategory | None = None
    # When set, the entity is only available while this returns True for the
    # coordinator's current control status. None means always available.
    availability: Callable[[ControlStatus], bool] | None = None


def deviation_state(
    *,
    signed_target: float,
    measured: float | None,
    soc: float | None,
    min_soc: float,
) -> ControlStatus:
    """Compare the deviation between what control we command and what the inverter reports."""
    if measured is None:
        return ControlStatus.ACTIVE

    error = signed_target - measured
    tolerance = max(POWER_TOLERANCE_W, abs(signed_target) * POWER_TOLERANCE_FRACTION)
    if abs(error) <= tolerance:
        return ControlStatus.ACTIVE

    if soc is not None:
        if error > 0 and soc >= BATTERY_FULL_SOC:
            return ControlStatus.UNREACHABLE_BATTERY_FULL
        if error < 0 and soc <= min_soc + BATTERY_EMPTY_MARGIN_SOC:
            return ControlStatus.UNREACHABLE_BATTERY_EMPTY

    return ControlStatus.RAMPING


class RegisterType(StrEnum):
    """Word layout of a register. Multi-word values are stored low word first."""

    UINT16 = "uint16"
    UINT32 = "uint32"
    INT32 = "int32"
    FLOAT32 = "float32"
    SERIAL = "serial"


REGISTER_SIZES: Final = {
    RegisterType.UINT16: 1,
    RegisterType.UINT32: 2,
    RegisterType.INT32: 2,
    RegisterType.FLOAT32: 2,
    # 16 ASCII bytes.
    RegisterType.SERIAL: 8,
}


def encode_register(value: int, data_type: RegisterType) -> list[int]:
    """Return the raw words for writing *value*, HIGH word first.

    Reads and writes are not behaving the same in the inverter. It publishes
    32-bit values low word first (see decode_register) but parses multi-register
    writes high word first. For example, a setpoint of 500 sent low word first
    is taken as 500 << 16 and the command is ignored, while the same value sent
    high word first is applied and then re-published low word first. At least
    on the PowerOcean Plus, this behavior has been observed consistently.

    Raises ValueError when the value does not fit the type or cannot be written.
    """
    if data_type is RegisterType.UINT16:
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f"{value} does not fit a UINT16 register")
        return [value]

    if data_type is RegisterType.UINT32:
        if not 0 <= value <= 0xFFFFFFFF:
            raise ValueError(f"{value} does not fit a UINT32 register")
        word = value
    elif data_type is RegisterType.INT32:
        if not -0x80000000 <= value <= 0x7FFFFFFF:
            raise ValueError(f"{value} does not fit an INT32 register")
        word = value & 0xFFFFFFFF
    else:
        raise ValueError(f"Registers of type {data_type} cannot be written")

    return [(word >> 16) & 0xFFFF, word & 0xFFFF]


@dataclass(frozen=True)
class RegisterDef:
    key: str
    address: int
    data_type: RegisterType = RegisterType.FLOAT32
    address_overrides: Mapping[InverterModel, int] = field(default_factory=dict)

    def for_model(self, inverter_model: InverterModel) -> RegisterDef:
        """Return a concrete register definition for an inverter model."""
        return replace(
            self,
            address=self.address_overrides.get(inverter_model, self.address),
            address_overrides={},
        )

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


def plan_blocks(
    registers: Iterable[RegisterDef],
    *,
    avoid: Collection[int] = (),
    max_gap: int = MAX_REGISTER_GAP,
) -> tuple[RegisterBlock, ...]:
    """Group registers into the fewest Modbus reads.

    A new read starts when the next register is too far away to be worth reading
    through, when the block would outgrow a single Modbus response, or when reading
    through would take in an address in avoid.
    """
    blocks: list[RegisterBlock] = []
    current: list[RegisterDef] = []

    for register in sorted(registers, key=lambda register: register.address):
        if current:
            reach = max(mapped.end for mapped in current)
            gap = register.address - reach
            span = register.end - current[0].address
            crosses = any(reach <= address < register.address for address in avoid)
            if gap > max_gap or span > MAX_REGISTERS_PER_READ or crosses:
                blocks.append(RegisterBlock(tuple(current)))
                current = []
        current.append(register)

    if current:
        blocks.append(RegisterBlock(tuple(current)))
    return tuple(blocks)


def plan_blocks_for_model(
    registers: Iterable[RegisterDef],
    inverter_model: InverterModel,
    *,
    avoid: Collection[int] = (),
) -> tuple[RegisterBlock, ...]:
    """Resolve model-specific addresses and group them into Modbus reads."""
    return plan_blocks(
        (register.for_model(inverter_model) for register in registers),
        avoid=avoid,
        max_gap=inverter_model.traits.max_register_gap,
    )


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


@dataclass(frozen=True)
class SwitchDef:
    key: str
    name: str | None = None
    device_class: str | None = None
    entity_category: EntityCategory | None = None
    icon: str | None = None


@dataclass(frozen=True)
class NumberWritableDef:
    key: str  # Unique key for the number entity (e.g., "min_soc_limit_control")
    read_key: str  # The original key from MODBUS_REGISTERS used for reading (e.g., "min_soc_limit")
    name: str  # Display name for Home Assistant UI
    register: int  # Physical Modbus register address for writing
    min_value: float  # Slider minimum value
    max_value: float  # Slider maximum value
    step: float  # Step size (1.0 for integers, 0.1 for floats)
    data_type: RegisterType = RegisterType.UINT16  # Word layout used for the write
    unit: str | None = None  # Unit of measurement
    device_class: str | None = None  # Device class type
    icon: str | None = None  # Custom icon for the slider
    # Not implemented on every model: hidden from the entity list unless enabled.
    advanced: bool = False
    # Models whose firmware stores the write but never acts on it. Writing anyway
    # would leave the matching sensor reporting a value the device is not using.
    unsupported_models: tuple[InverterModel, ...] = ()

    @property
    def size(self) -> int:
        """Return how many 16-bit words the write occupies."""
        return REGISTER_SIZES[self.data_type]
