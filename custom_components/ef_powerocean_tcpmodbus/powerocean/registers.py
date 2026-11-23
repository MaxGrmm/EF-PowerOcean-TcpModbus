"""The PowerOcean's Modbus registers: how they are typed, mapped and read."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final

from .const import (
    BATTERY_SOC_KEYS,
    FAULT_KEYS,
    FEED_IN_POWER_MAX_EFFECTIVE_KEY,
    FEED_IN_POWER_MAX_SETTING_KEY,
    HEARTBEAT_REGISTER,
    INVERTER_CAPACITY_KEY,
    RECTIFIER_CAPACITY_KEY,
)
from .models import (
    MAX_REGISTER_GAP,
    MAX_REGISTERS_PER_READ,
    InverterModel,
)


class RegisterType(StrEnum):
    """Word layout of a register. Multi-word values are stored low word first."""

    UINT16 = "uint16"
    INT16 = "int16"
    UINT32 = "uint32"
    INT32 = "int32"
    FLOAT32 = "float32"
    SERIAL = "serial"


REGISTER_SIZES: Final = {
    RegisterType.UINT16: 1,
    RegisterType.INT16: 1,
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


class Signal(StrEnum):
    """What kind of signal a register carries, which says how it can go wrong.

    The reading checks judge by kind, so a new register is covered by naming its kind.
    """

    # Jitters from poll to poll on a live grid, whatever the power flows do. One
    # that does not move at all has stopped updating.
    JITTER = "jitter"
    # A measured power, current, voltage or temperature. It may sit still for a
    # while, but not while the readings around it show the inverter stalled.
    MEASURE = "measure"
    # A state of charge.
    LEVEL = "level"
    # An energy counter.
    COUNTER = "counter"
    # A setting, limit or rating that only changes when someone changes it.
    STATIC = "static"
    # Status bits and fault codes: the device's own word, never judged.
    STATUS = "status"


@dataclass(frozen=True)
class RegisterDef:
    key: str
    address: int
    data_type: RegisterType = RegisterType.FLOAT32
    signal: Signal = Signal.STATIC
    address_overrides: Mapping[InverterModel, int] = field(default_factory=dict)
    # Where writes go when the register is read from somewhere else.
    write_address: int | None = None
    # Not confirmed on every model. If reading it fails, it stops being polled,
    # where a required register would fail the poll.
    optional: bool = False

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

    @property
    def optional(self) -> bool:
        """Return whether the block holds nothing but optional registers."""
        return all(register.optional for register in self.registers)

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
    isolate_optional: bool = False,
) -> tuple[RegisterBlock, ...]:
    """Group registers into the fewest Modbus reads.

    A read ends where the next register is more than max_gap away, where it would
    exceed one Modbus response, or where it would cross an address in avoid.

    Optional registers never widen a required read. One joins a required read only
    if that read already covers its address, and otherwise gets a read of its own.
    With isolate_optional, they always get their own.
    """
    registers = tuple(registers)
    required = [register for register in registers if not register.optional]
    optional = [register for register in registers if register.optional]
    if not optional or not required:
        return _group(registers, avoid=avoid, max_gap=max_gap)
    required_blocks = _group(required, avoid=avoid, max_gap=max_gap)
    if isolate_optional:
        return required_blocks + _group(optional, avoid=avoid, max_gap=max_gap)

    joined = [list(block.registers) for block in required_blocks]
    apart: list[RegisterDef] = []
    for register in optional:
        home = next(
            (
                index
                for index, block in enumerate(required_blocks)
                if block.start <= register.address
                and register.end <= block.start + block.count
            ),
            None,
        )
        if home is None:
            apart.append(register)
        else:
            joined[home].append(register)
    return tuple(
        RegisterBlock(tuple(sorted(block, key=lambda register: register.address)))
        for block in joined
    ) + _group(apart, avoid=avoid, max_gap=max_gap)


def _group(
    registers: Iterable[RegisterDef],
    *,
    avoid: Collection[int] = (),
    max_gap: int = MAX_REGISTER_GAP,
) -> tuple[RegisterBlock, ...]:
    """Group registers into reads by distance alone."""
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
    exclude: Collection[str] = (),
) -> tuple[RegisterBlock, ...]:
    """Resolve model-specific addresses and group them into Modbus reads.

    Registers whose key is in exclude are left out, which is how an optional
    register the firmware turned out not to implement stops being read.
    """
    traits = inverter_model.traits
    return plan_blocks(
        (
            register.for_model(inverter_model)
            for register in registers
            if register.key not in exclude
        ),
        avoid=avoid,
        max_gap=traits.max_register_gap,
        isolate_optional=traits.rejects_unimplemented,
    )


PROTOCOL_VERSION: Final = RegisterDef("protocol_version", 40001, RegisterType.UINT16)
PRODUCT_CATEGORY: Final = RegisterDef("product_category", 40002, RegisterType.UINT16)
PRODUCT_NUMBER: Final = RegisterDef("product_number", 40003, RegisterType.UINT16)
SERIAL_NUMBER: Final = RegisterDef("serial_number", 40004, RegisterType.SERIAL)
FIRMWARE_VERSION: Final = RegisterDef("firmware_version", 40012, RegisterType.UINT32)

DEVICE_ADDRESS: Final = RegisterDef("device_address", 40014, RegisterType.UINT16)

# Read on every connect, not on every poll.
DEVICE_INFO_BLOCK: Final = RegisterBlock(
    (PRODUCT_CATEGORY, PRODUCT_NUMBER, SERIAL_NUMBER, FIRMWARE_VERSION)
)
# Read one at a time, so a model that refuses one still reports the other.
DEVICE_INFO_EXTRA: Final = (PROTOCOL_VERSION, DEVICE_ADDRESS)

# Every polled register, by absolute Modbus address. Order is for readability only;
# the reads are worked out by plan_blocks().
MODBUS_REGISTERS: Final[tuple[RegisterDef, ...]] = (
    RegisterDef("grid_connection_code", 40513, RegisterType.UINT16, optional=True),
    RegisterDef("house_power", 40519, signal=Signal.MEASURE),
    RegisterDef("grid_power", 40521, signal=Signal.MEASURE),
    RegisterDef("solar_power", 40523, signal=Signal.MEASURE),
    RegisterDef("battery_power", 40525, signal=Signal.MEASURE),
    RegisterDef("battery_soc", 40527, RegisterType.UINT16, signal=Signal.LEVEL),
    RegisterDef("inverter_rated_power", 40528, RegisterType.UINT32),
    RegisterDef("system_modes", 40530, RegisterType.UINT32, signal=Signal.STATUS),
    RegisterDef(
        "system_state_2",
        40532,
        RegisterType.UINT32,
        optional=True,
        signal=Signal.STATUS,
    ),
    RegisterDef("min_soc_limit", 40536, RegisterType.UINT16),
    RegisterDef("grid_feed_mode", 40537, RegisterType.UINT16),
    # The two export caps: 40538 is the one configured, and the only one that takes
    # a write; 40609 is the one in force after the internal safety rules.
    # feed_in_power_max is derived from them per model.
    RegisterDef(
        FEED_IN_POWER_MAX_SETTING_KEY, 40538, RegisterType.UINT32, optional=True
    ),
    RegisterDef("breaker_capacity", 40540, RegisterType.UINT16, optional=True),
    RegisterDef("device_led_brightness", 40541, RegisterType.UINT16),
    # Setpoints that take effect the moment the matching control method is engaged.
    RegisterDef("system_power_setpoint", 40542, RegisterType.INT32),
    RegisterDef("inverter_power_setpoint", 40544, RegisterType.INT32),
    RegisterDef(INVERTER_CAPACITY_KEY, 40546, RegisterType.UINT32),
    RegisterDef(RECTIFIER_CAPACITY_KEY, 40548, RegisterType.UINT32),
    # Rectifying positive, inverting negative.
    RegisterDef(
        "inverter_output_power",
        40550,
        RegisterType.INT32,
        optional=True,
        signal=Signal.MEASURE,
    ),
    RegisterDef("battery_capacity", 40552, RegisterType.UINT32),
    RegisterDef("battery_discharge_power_limit", 40554, RegisterType.UINT32),
    RegisterDef("battery_charge_power_limit", 40556, RegisterType.UINT32),
    # Read from an external meter (EXTERNAL_METER_KEYS), unlike voltage_l1 and
    # current_l1 below, which the inverter measures itself. On a PowerOcean Plus
    # those come from its built-in meter at the GRID terminals.
    RegisterDef("grid_current_l1", 40559, optional=True, signal=Signal.MEASURE),
    RegisterDef("grid_current_l2", 40561, optional=True, signal=Signal.MEASURE),
    RegisterDef("grid_current_l3", 40563, optional=True, signal=Signal.MEASURE),
    RegisterDef("grid_voltage_l1", 40565, optional=True, signal=Signal.JITTER),
    RegisterDef("grid_voltage_l2", 40567, optional=True, signal=Signal.JITTER),
    RegisterDef("grid_voltage_l3", 40569, optional=True, signal=Signal.JITTER),
    RegisterDef("battery_power_setpoint", 40571, RegisterType.INT32),
    RegisterDef("feed_in_power_max_percent", 40573, RegisterType.UINT16, optional=True),
    RegisterDef("battery_voltage", 40574, signal=Signal.JITTER),
    RegisterDef("battery_current", 40576, signal=Signal.MEASURE),
    RegisterDef("battery_temperature", 40578, signal=Signal.MEASURE),
    RegisterDef("voltage_l1", 40580, signal=Signal.JITTER),
    RegisterDef("voltage_l2", 40582, signal=Signal.JITTER),
    RegisterDef("voltage_l3", 40584, signal=Signal.JITTER),
    RegisterDef("current_l1", 40586, signal=Signal.MEASURE),
    RegisterDef("current_l2", 40588, signal=Signal.MEASURE),
    RegisterDef("current_l3", 40590, signal=Signal.MEASURE),
    RegisterDef("inverter_temperature", 40592, signal=Signal.MEASURE),
    RegisterDef("frequency", 40594, signal=Signal.JITTER),
    RegisterDef("pv1_voltage", 40596, signal=Signal.MEASURE),
    RegisterDef("pv2_voltage", 40598, signal=Signal.MEASURE),
    RegisterDef("pv3_voltage", 40600, signal=Signal.MEASURE),
    RegisterDef("pv1_current", 40602, signal=Signal.MEASURE),
    RegisterDef("pv2_current", 40604, signal=Signal.MEASURE),
    RegisterDef("pv3_current", 40606, signal=Signal.MEASURE),
    # 40609 refuses writes (exception 2 on a PowerOcean Three Phase, issue #89).
    RegisterDef(FEED_IN_POWER_MAX_EFFECTIVE_KEY, 40609, RegisterType.UINT32),
    RegisterDef("fault_count", 42049, RegisterType.UINT16, signal=Signal.STATUS),
    *(
        RegisterDef(
            key, 42049 + fault_number, RegisterType.UINT16, signal=Signal.STATUS
        )
        for fault_number, key in enumerate(FAULT_KEYS, start=1)
    ),
    RegisterDef("battery_count", 42081, RegisterType.UINT16),
    *(
        RegisterDef(
            key, 42081 + battery_number, RegisterType.UINT16, signal=Signal.LEVEL
        )
        for battery_number, key in enumerate(BATTERY_SOC_KEYS, start=1)
    ),
    RegisterDef("grid_import_total", 42161, signal=Signal.COUNTER),
    RegisterDef("grid_import_today", 42163, signal=Signal.COUNTER),
    RegisterDef("grid_export_total", 42177, signal=Signal.COUNTER),
    RegisterDef("grid_export_today", 42179, signal=Signal.COUNTER),
    RegisterDef("inverter_ac_in_total", 42193, optional=True, signal=Signal.COUNTER),
    RegisterDef("inverter_ac_in_today", 42195, optional=True, signal=Signal.COUNTER),
    RegisterDef("inverter_ac_out_total", 42209, optional=True, signal=Signal.COUNTER),
    RegisterDef("inverter_ac_out_today", 42211, optional=True, signal=Signal.COUNTER),
    RegisterDef("bat_charged_total", 42225, signal=Signal.COUNTER),
    RegisterDef("bat_charged_today", 42227, signal=Signal.COUNTER),
    RegisterDef("bat_discharged_total", 42241, signal=Signal.COUNTER),
    RegisterDef("bat_discharged_today", 42243, signal=Signal.COUNTER),
    RegisterDef("solar_total", 42257, signal=Signal.COUNTER),
    RegisterDef("solar_today", 42259, signal=Signal.COUNTER),
    # In var. Only in EcoFlow's register schema, confirmed on a PowerOcean Plus, where
    # it follows the built-in meter like inverter_output_power. Read on its own, far
    # from the other registers, so a model without it only drops this read. Listed
    # last, as the tests fill each register with a value from its place here.
    RegisterDef(
        "inverter_reactive_power",
        40024,
        RegisterType.INT16,
        optional=True,
        signal=Signal.MEASURE,
    ),
)

REGISTERS_BY_KEY: Final = {register.key: register for register in MODBUS_REGISTERS}

# Registers listed for the PowerOcean in EcoFlow's register schema, or in the V1.0
# protocol, that are not polled. The schema:
# https://github.com/ecoflow-oasis-official/ha-ecoflow-official/blob/main/custom_components/ha_ecoflow_official/registers_schema.py
# They are only candidates: most are
# missing from the V1.0 protocol, which leaves 0x0262 (40611) to 0x07FF reserved,
# and the rest read constant, redundant or unclear values on the hardware so far.
# Only the register scan reads them, so each can be checked on a live inverter
# before it is polled or gets an entity. The comment by each says what we think it
# holds, with its address in the specification.
CANDIDATE_REGISTERS: Final[tuple[RegisterDef, ...]] = (
    # 0x0016, total active power at the inverter; reads 0 on a PowerOcean Plus.
    RegisterDef("inverter_total_active_power", 40023, RegisterType.INT16),
    # 0x0205, the time zone times 4, signed; the V1.0 protocol calls it unused.
    RegisterDef("time_zone", 40518, RegisterType.INT16),
    # 0x0262, an unknown battery state enum; read 3 both idle and charging.
    RegisterDef("battery_state", 40611, RegisterType.INT16),
    # 0x0263, read-write, 0 remote, 1 local; read 1 with Modbus control on too.
    RegisterDef("battery_control_mode", 40612, RegisterType.UINT16),
    # 0x0264, reactive power of the whole system in var; reads 0 under load.
    RegisterDef("system_reactive_power", 40613),
    # 0x0266, the rated active power, the same as inverter_rated_power.
    RegisterDef("rated_max_active_power", 40615, RegisterType.UINT16),
    # 0x0267, the apparent power rating in VA.
    RegisterDef("max_apparent_power", 40616, RegisterType.UINT16),
    # 0x0268 and 0x0269, the reactive power the inverter may supply and absorb.
    RegisterDef("max_reactive_output_power", 40617, RegisterType.UINT16),
    RegisterDef("max_reactive_input_power", 40618, RegisterType.UINT16),
    # 0x0270 and 0x0271, the power factor limits, likely in 0.001 (800 = 0.8).
    RegisterDef("pf_limit_supplying_reactive", 40625, RegisterType.UINT16),
    RegisterDef("pf_limit_absorbing_reactive", 40626, RegisterType.UINT16),
    # 0x0272 and 0x0273, named battery limits, but they read the inverter rating.
    RegisterDef("battery_max_charge_capability", 40627, RegisterType.UINT16),
    RegisterDef("battery_max_discharge_capability", 40628, RegisterType.UINT16),
    # 0x0274, read-write, a cap on the inverter's output in 0.1 %; reads 0.
    RegisterDef("inverter_active_output_percent", 40629, RegisterType.UINT16),
    # 0x0275 to 0x0278, write-only switches meant for Australia; they read 0.
    RegisterDef("vpp_control_mode", 40630, RegisterType.UINT16),
    RegisterDef("soft_start_switch", 40631, RegisterType.UINT16),
    RegisterDef("battery_power_switch", 40632, RegisterType.UINT16),
    RegisterDef("force_off_grid", 40633, RegisterType.UINT16),
    # 0x0840, 0x0850 and 0x0860, net energy since midnight at the meter (+ drawn),
    # the inverter (+ rectified) and the battery (+ charged). Each equals the
    # difference of two daily counters that are polled.
    RegisterDef("meter_energy_today", 42113),
    RegisterDef("inverter_energy_today", 42129),
    RegisterDef("battery_energy_today", 42145),
    # 0x1200 and 0x1202, house consumption; refused, past the 44096 address window.
    RegisterDef("house_consumption_total", 44609),
    RegisterDef("house_consumption_today", 44611),
)

# The address ranges the register scan reads beyond the map, first and last address.
REGISTER_SCAN_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (40000, 40700),
    (42000, 42300),
)


def register_blocks_for(
    inverter_model: InverterModel, *, exclude: Collection[str] = ()
) -> tuple[RegisterBlock, ...]:
    """Return register blocks resolved for an inverter model.

    The heartbeat register is kept out of them, at the cost of one more read for the
    effective feed-in cap next to it. The inverter answers a write to a register it
    is serving a read for with "device busy", and losing the heartbeat that way
    costs a minute of control. Keys in exclude are not read at all.
    """
    return plan_blocks_for_model(
        MODBUS_REGISTERS,
        inverter_model,
        avoid=(HEARTBEAT_REGISTER,),
        exclude=exclude,
    )
