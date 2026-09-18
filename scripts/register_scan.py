#!/usr/bin/env python3
"""Compare an inverter's Modbus registers against the implemented map in const.py.

To run it:

    uv pip install -r requirements-development.txt
    uv run python scripts/register_scan.py <inverter_ip>
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
import types
from pathlib import Path
from typing import Final

INTEGRATION: Final = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "ef_powerocean_tcpmodbus"
)
MANIFEST: Final = INTEGRATION / "manifest.json"
PACKAGE: Final = "ef_powerocean_registers"


def load_integration() -> tuple[types.ModuleType, ...]:
    """Custom import of the register map, to not import the whole of Home Assistant."""
    try:
        import homeassistant.const  # noqa: F401
    except ImportError:
        stub = types.ModuleType("homeassistant.const")
        stub.__getattr__ = lambda _name: type("Any", (), {"__getattr__": str})()
        sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
        sys.modules["homeassistant.const"] = stub

    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(INTEGRATION)]
    sys.modules[PACKAGE] = package
    return tuple(
        importlib.import_module(f"{PACKAGE}.{module}")
        for module in ("const", "models", "telemetry")
    )


const, models, telemetry = load_integration()

from pymodbus.client import ModbusTcpClient  # noqa: E402

DEFAULT_SCAN: Final = ["40000-40700", "42000-42300"]

# Used to attempt to determine what an unknown value could be.
PLAUSIBLE_RANGE: Final[dict[str, tuple[float, float]]] = {
    "battery": (0, 100),
    "current": (-300, 300),
    "energy": (0, 10_000_000),
    "energy_storage": (0, 10_000_000),
    "frequency": (40, 70),
    "power": (-100_000, 100_000),
    "temperature": (-40, 150),
    "voltage": (0, 1000),
}

DEVICE_CLASS_BY_KEY: Final[dict[str, str]] = {
    definition.key: definition.device_class
    for definition in (*const.SENSOR_MAP, *const.ENERGY_SENSOR_MAP)
    if definition.device_class
}

SIGNATURES: Final[tuple[tuple[str, float, float], ...]] = (
    ("mains frequency", 49.0, 51.0),
    ("battery voltage", 40.0, 60.0),
    ("phase voltage", 200.0, 260.0),
    ("temperature", 10.0, 90.0),
    ("string voltage", 100.0, 900.0),
    ("power or energy", 1000.0, 100_000.0),
)

EXCEPTION_MEANINGS: Final[dict[int, str]] = {
    1: "illegal function",
    2: "illegal data address",
    3: "illegal data value",
    4: "device failure",
    6: "device busy",
}

NOT_POLLED: Final[dict[int, str]] = {
    const.CONTROL_COMMAND_REGISTER: "the integration's control command, write-only",
    const.HEARTBEAT_REGISTER: "the integration's heartbeat register",
}

# A register holds one small number; bigger ones are built from two neighbours.
# Pairing up two unrelated registers gives an absurd result, so a reading past
# these ceilings is discarded.
INTEGER_CEILING: Final = 10_000_000
FLOAT_CEILING: Final = 1_000_000.0

# Long enough that anything live has moved, which is what tells a measurement
# apart from a setting and a real mirror apart from a passing coincidence. Some
# registers drift only slowly, so a short window misreports them as static, and
# a third sample makes a coincidence harder still.
SAMPLE_COUNT: Final = 3
SAMPLE_GAP_S: Final = 30


def render_address(address: int) -> str:
    return f"{address} (0x{address:04X})"


def render_span(first: int, last: int) -> str:
    if first == last:
        return render_address(first)
    return f"{render_address(first)} .. {render_address(last)}"


def contiguous_spans(addresses: list[int]) -> list[tuple[int, int]]:
    """Collapse addresses into inclusive first-last spans."""
    spans: list[list[int]] = []
    for address in sorted(addresses):
        if spans and address == spans[-1][1] + 1:
            spans[-1][1] = address
        else:
            spans.append([address, address])
    return [(first, last) for first, last in spans]


def plausibility_note(key: str, value: float | None) -> str:
    """Say whether a decoded value is believable for the register it came from."""
    if value is None:
        return "undecodable"
    if value == 0:
        return "zero"
    bounds = PLAUSIBLE_RANGE.get(DEVICE_CLASS_BY_KEY.get(key, ""))
    if bounds and not bounds[0] <= value <= bounds[1]:
        return "out of range"
    return "ok"


class RegisterReader:
    """Interface to read a register from the inverter."""

    def __init__(self, client: ModbusTcpClient, slave: int) -> None:
        self._client = client
        self._slave = slave
        self.reads = 0

    def read(self, start: int, count: int) -> tuple[list[int] | None, str]:
        """Return the words, or None and the reason the device gave."""
        self.reads += 1
        try:
            response = self._client.read_holding_registers(
                address=start, count=count, device_id=self._slave
            )
        except Exception as error:  # a dropped connection rather than a refusal
            return None, str(error)

        if response.isError():
            code = getattr(response, "exception_code", None)
            if not code:
                return None, str(response)
            return None, f"exception code {code}, {EXCEPTION_MEANINGS.get(code, '?')}"
        return response.registers, ""


def report_device(
    reader: RegisterReader, show_serial: bool
) -> models.InverterModel | None:
    """Print what the device says about itself, and the model that implies."""
    print("== Device ==")
    block = const.DEVICE_INFO_BLOCK
    words, reason = reader.read(block.start, block.count)
    if words is None:
        print(f"  device info at {render_address(block.start)}: FAILED ({reason})")
        print("  Pass --model to carry on against a specific address map.\n")
        return None

    def words_for(register: models.RegisterDef) -> list[int]:
        return block.registers_for(words, register)

    serial = telemetry.decode_serial_number(words_for(const.SERIAL_NUMBER)) or "unknown"
    number = words_for(const.PRODUCT_NUMBER)[0]
    category = words_for(const.PRODUCT_CATEGORY)[0]
    detected = models.InverterModel.from_product_info(number, category)
    firmware = telemetry.decode_firmware_version(words_for(const.FIRMWARE_VERSION))

    print(f"  serial number     {serial if show_serial else serial[:4] + '****'}")
    print(f"  firmware          {firmware}")
    print(f"  product number    {number}")
    print(f"  product category  {category}")
    print(f"  detected model    {detected.display_name if detected else 'UNKNOWN'}")
    if detected is None:
        print("    -> no model matches these product registers, so the default map")
        print("       is used. Quote the two numbers above in the issue: they are")
        print("       what teaches the integration to recognise this model.")
    print()
    return detected


def report_block_reads(
    reader: RegisterReader, blocks: tuple[models.RegisterBlock, ...]
) -> None:
    """Read exactly what the integration reads on every poll."""
    print("== Block reads the integration performs ==")
    for block in blocks:
        words, reason = reader.read(block.start, block.count)
        result = "OK" if words is not None else f"FAILED ({reason})"
        last = block.start + block.count - 1
        print(f"  {render_span(block.start, last)}, {block.count} words: {result}")
    print()


def report_mapped_registers(
    reader: RegisterReader, registers: list[models.RegisterDef]
) -> tuple[list, list, dict[str, float | None]]:
    """Read every declared register on its own and attempt to parse the results."""
    print("== Every mapped register in const.py ==")
    print(f"  {'key':<30} {'address':>16} {'type':<8} {'value':>14}  note")
    missing: list[models.RegisterDef] = []
    suspect: list[tuple[models.RegisterDef, float | None, str]] = []
    values: dict[str, float | None] = {}

    for register in registers:
        row = (
            f"  {register.key:<30} {render_address(register.address):>16} "
            f"{register.data_type:<8}"
        )
        words, reason = reader.read(register.address, register.size)
        if words is None:
            print(f"{row} {'-':>14}  UNREADABLE ({reason})")
            missing.append(register)
            continue

        value = telemetry.decode_register(words, register.data_type)
        values[register.key] = value
        note = plausibility_note(register.key, value)
        if note in ("out of range", "undecodable"):
            suspect.append((register, value, note))
        decoded = "None" if value is None else f"{value:.2f}"
        print(f"{row} {decoded:>14}  {'' if note == 'ok' else note}".rstrip())
    print()
    return missing, suspect, values


def report_derived_values(
    values: dict[str, float | None], model: models.InverterModel
) -> None:
    """Print the entity values derived on top of those registers."""
    print("== Derived values ==")
    derived = telemetry.calculate_derived_values(
        telemetry.TelemetryData.from_mapping(values),
        calculate_solar_power=False,
        startup_voltage=model.startup_voltage,
    )
    for key, value in derived.items():
        text = f"{value:.2f}" if isinstance(value, float) else str(value)
        print(f"  {key:<30} {text:>16}")
    print()


def report_readable_addresses(
    reader: RegisterReader, ranges: list[tuple[int, int]]
) -> tuple[dict[int, int], dict[int, str]]:
    """Find what the device answers to, in chunks, dropping to single reads only
    where a chunk is refused. Returns the words read and the refusals."""

    print("== Readable addresses ==")
    print("  (can take a minute)")
    found: dict[int, int] = {}
    refused: dict[int, str] = {}
    for low, high in ranges:
        print(f"  searching {low}-{high} ...")
        for base in range(low, high + 1, 16):
            count = min(16, high - base + 1)
            chunk, _ = reader.read(base, count)
            if chunk is not None:
                found.update(zip(range(base, base + count), chunk))
                continue
            for address in range(base, base + count):
                word, reason = reader.read(address, 1)
                if word is None:
                    refused[address] = reason
                else:
                    found[address] = word[0]

    for first, last in contiguous_spans(list(found)):
        print(f"    readable {render_span(first, last)}")
    for first, last in contiguous_spans(list(refused)):
        print(f"    refused  {render_span(first, last)}")
    if not found and not refused:
        print("    nothing answered in the searched ranges")
    print()
    return found, refused


def word_as_text(word: int) -> str:
    """Return the word's two bytes when they look like part of a name.

    A letter is required, so a pair of digits is not mistaken for text.
    """
    characters = (chr(word >> 8) + chr(word & 0xFF)).rstrip("\x00 ")
    if not characters or not any(c.isalpha() for c in characters):
        return ""
    return characters if all(c.isascii() and c.isalnum() for c in characters) else ""


def guess_quantity(real: float | None) -> str:
    """Name what a reading might be from its value alone, offering at most two."""
    if real is None:
        return ""
    names = [name for name, low, high in SIGNATURES if low <= real <= high]
    return f"maybe {', '.join(names[:2])}" if names else ""


def believable_float(low: int, high: int) -> float | None:
    """The word pair as a float, when that could be a real reading."""
    real = telemetry.decode_register([low, high], models.RegisterType.FLOAT32)
    if real is None or not 0.001 <= abs(real) <= FLOAT_CEILING:
        return None
    return real


def render_all_readings(low: int, high: int) -> tuple[str, float | None]:
    """Format a word pair as every reading the integration knows, blanking any
    that cannot plausibly be real, and return the float when there is one."""
    pair = [low, high]
    unsigned = telemetry.decode_register(pair, models.RegisterType.UINT32)
    signed = telemetry.decode_register(pair, models.RegisterType.INT32)
    real = believable_float(low, high)

    wide = f"{unsigned:.0f}" if unsigned <= INTEGER_CEILING else ""
    negative = (
        f"{signed:.0f}" if signed != unsigned and abs(signed) <= INTEGER_CEILING else ""
    )
    columns = (
        f"{low:>10} {wide:>12} {negative:>12} "
        f"{f'{real:.2f}' if real is not None else '':>14}"
    )
    return columns, real


def collapse_reason(
    address: int, words: dict[int, int], refused: dict[int, str]
) -> str:
    """Return why this address needs no row of its own, or "" if it does."""
    if address in refused:
        owned = NOT_POLLED.get(address)
        return f"refused, {refused[address]}" + (f" - {owned}" if owned else "")
    return "all zero" if words[address] == 0 else ""


def read_mapped_values(
    reader: RegisterReader, blocks: tuple[models.RegisterBlock, ...]
) -> dict[str, float | None]:
    """Decode every mapped register from whole-block reads, which costs three
    requests rather than eighty."""
    values: dict[str, float | None] = {}
    for block in blocks:
        words, _ = reader.read(block.start, block.count)
        if words is None:
            continue
        for register in block.registers:
            values[register.key] = telemetry.decode_register(
                block.registers_for(words, register), register.data_type
            )
    return values


def read_addresses(reader: RegisterReader, addresses: list[int]) -> dict[int, int]:
    """Read scattered addresses in as few requests as their spans allow."""
    sampled: dict[int, int] = {}
    for first, last in contiguous_spans(addresses):
        words, _ = reader.read(first, last - first + 1)
        if words is not None:
            sampled.update(zip(range(first, last + 1), words))
    return sampled


def sampled_readings(
    words: dict[int, int], address: int
) -> tuple[float, tuple[float, ...]] | None:
    """The value to show for this address, and every value it could be carrying.

    Carrying all of them means a uint32 or an int32 is tracked in its own right
    rather than through its low half, which would ignore the other half and turn
    a wrap into an apparent jump.
    """
    if address not in words:
        return None

    low, high = words[address], words.get(address + 1, 0)
    pair = [low, high]
    unsigned = telemetry.decode_register(pair, models.RegisterType.UINT32)
    signed = telemetry.decode_register(pair, models.RegisterType.INT32)
    real = believable_float(low, high)

    readings = [float(low)]
    if unsigned <= INTEGER_CEILING:
        readings.append(unsigned)
    if signed != unsigned and abs(signed) <= INTEGER_CEILING:
        readings.append(signed)
    if real is not None:
        readings.append(real)
    return (real if real is not None else float(low)), tuple(readings)


def matching_registers(
    readings: tuple[float, ...], values: dict[str, float | None]
) -> set[str]:
    """Every mapped register reporting one of these values."""
    return {
        key
        for value in readings
        for key, mapped in values.items()
        if mapped and abs(value - mapped) <= max(0.01, abs(mapped) * 0.005)
    }


def report_behaviour(
    reader: RegisterReader,
    blocks: tuple[models.RegisterBlock, ...],
    words: dict[int, int],
    candidates: list[int],
    samples: int,
    gap: int,
) -> tuple[dict[int, tuple[bool, str]], dict[int, int]]:
    """Sample the unmapped addresses and the mapped registers together, repeatedly.

    Reading both at the same moment removes the skew between passes, and reading
    them again once values have moved separates a register that really carries a
    quantity we already map from one that briefly held the same number.

    Returns what each address was seen doing, and the last sample, so the table
    can show the reading the note is about rather than the one from the scan.
    """
    window = gap * (samples - 1)
    print("== Scans addresses outside the const.py map ==")
    print(
        f"  {samples} samples alongside the mapped registers, {gap}s apart "
        f"({window}s total)"
    )
    wanted = sorted(
        {near for address in candidates for near in (address, address + 1)} & set(words)
    )

    taken: list[tuple[dict[str, float | None], dict[int, int]]] = []
    for number in range(1, samples + 1):
        if taken:
            time.sleep(gap)
        print(f"  sample {number} of {samples} ...")
        taken.append(
            (read_mapped_values(reader, blocks), read_addresses(reader, wanted))
        )

    behaviour: dict[int, tuple[bool, str]] = {}
    for address in candidates:
        readings = [sampled_readings(sampled, address) for _, sampled in taken]
        if any(reading is None for reading in readings):
            continue

        # The raw word alone decides movement: the following word may belong to
        # a different register, and would report this one as moving with it.
        moved = len({sampled[address] for _, sampled in taken}) > 1
        shared = set.intersection(
            *(
                matching_registers(candidates_seen, values)
                for (_, candidates_seen), (values, _) in zip(readings, taken)
            )
        )
        mirrored = min(shared) if shared else None

        chain = " -> ".join(f"{shown:.2f}" for shown, _ in readings)
        if mirrored and moved:
            note = f"{chain}, mirrors {mirrored}"
        elif mirrored:
            note = f"equals {mirrored}, unchanged over {window}s"
        elif moved:
            note = f"{chain}, matches nothing we map"
        else:
            note = f"unchanged over {window}s"
        behaviour[address] = (moved, note)

    moving = sum(1 for moved, _ in behaviour.values() if moved)
    print(f"  {len(behaviour)} sampled, {moving} of them moving\n")
    return behaviour, taken[-1][1]


def report_unmapped_addresses(
    words: dict[int, int],
    refused: dict[int, str],
    known: set[int],
    behaviour: dict[int, tuple[bool, str]],
) -> None:
    """Show what sits at the addresses the integration does not map."""
    addresses = sorted(
        address for address in (*words, *refused) if address not in known
    )
    if not addresses:
        return

    groups: list[tuple[list[int], str]] = []
    for address in addresses:
        label = collapse_reason(address, words, refused)
        same = groups and groups[-1][1] == label and address == groups[-1][0][-1] + 1
        if label and same:
            groups[-1][0].append(address)
        else:
            groups.append(([address], label))

    print("== Addresses outside the map the integration reads ==")
    print("  Parsed using every decoding we are aware of")
    print(
        f"  {'address':>16} {'uint16':>10} {'uint32':>12} "
        f"{'int32':>12} {'float32':>14}  notes"
    )

    float_at = -1
    for group, label in groups:
        if label:
            print(f"  {render_span(group[0], group[-1])}: {label}")
            float_at = -1
            continue

        address = group[0]
        columns, real = render_all_readings(words[address], words.get(address + 1, 0))
        moved, observed = behaviour.get(address, (False, ""))
        if address in NOT_POLLED:
            note = NOT_POLLED[address]
        # A value seen moving is a measurement, whatever its bytes spell.
        elif not moved and (text := word_as_text(words[address])):
            note = f'text "{text}"'
        elif address == float_at + 1:
            note = "high word of the reading above"
        else:
            note = observed or guess_quantity(real)
        float_at = address if real is not None else -1
        print(f"  {render_address(address):>16} {columns}  {note}".rstrip())
    print()


def mapped_addresses(blocks: tuple[models.RegisterBlock, ...]) -> set[int]:
    """Every address the integration reads, holes inside a block included."""
    return {
        address
        for block in (*blocks, const.DEVICE_INFO_BLOCK)
        for address in range(block.start, block.start + block.count)
    }


def report_summary(
    missing: list[models.RegisterDef],
    suspect: list[tuple[models.RegisterDef, float | None, str]],
    values: dict[str, float | None],
    candidates: list[int],
) -> None:
    """Produce the report summary."""
    print("== Differences ==")
    problems = 0

    if missing:
        problems += 1
        print(f"  {len(missing)} declared register(s) this device refuses:")
        for first, last in contiguous_spans([register.address for register in missing]):
            keys = [r.key for r in missing if first <= r.address <= last]
            label = keys[0] if len(keys) == 1 else f"{keys[0]} .. {keys[-1]}"
            print(f"    {label}: {render_span(first, last)}")

    if suspect:
        problems += 1
        print(f"  {len(suspect)} register(s) that read but carry an implausible value,")
        print("  which usually means the address moved on this model:")
        for register, value, note in suspect:
            address = render_address(register.address)
            print(f"    {register.key} at {address}: {value} ({note})")

    # Every device carries settings we do not read, so unmapped ground is a lead
    # rather than a finding, and only worth pointing at once something is wrong.
    if candidates and (missing or suspect):
        print(f"  {len(candidates)} address(es) outside the map hold a non-zero value.")
        print("    A map that has moved still reports the same quantity, just at")
        print("    another address, so look for the registers above in that table.")

    if values and all(value == 0 for value in values.values()):
        problems += 1
        print("  Every register reads as zero, which is the signature of Modbus TCP")
        print("  being switched off in the EcoFlow app rather than a map difference.")

    if not problems:
        print("  None. This device matches the map the integration expects.")
    print()


def parse_address_range(text: str) -> tuple[int, int]:
    low, _, high = text.partition("-")
    return int(low), int(high)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("host", help="the inverter's IP address")
    parser.add_argument("--port", type=int, default=const.DEFAULT_PORT)
    parser.add_argument("--slave", type=int, default=const.DEFAULT_SLAVE)
    parser.add_argument(
        "--model",
        choices=[model.value for model in models.InverterModel],
        help="address map to compare against. Default: whatever the device "
        "reports, else the integration's default.",
    )
    parser.add_argument(
        "--scan",
        action="append",
        metavar="LOW-HIGH",
        help=f"address range to search, repeatable. "
        f"Default {' and '.join(DEFAULT_SCAN)}.",
    )
    parser.add_argument("--no-scan", action="store_true", help="skip that search")
    parser.add_argument(
        "--samples",
        type=int,
        default=SAMPLE_COUNT,
        help="how many times to read the addresses outside the map. "
        f"Default {SAMPLE_COUNT}. More samples make a coincidental match less "
        "likely to survive.",
    )
    parser.add_argument(
        "--gap",
        type=int,
        default=SAMPLE_GAP_S,
        metavar="SECONDS",
        help=f"how long to wait between those samples. Default {SAMPLE_GAP_S}. "
        "A slowly drifting register needs a longer wait to be seen moving.",
    )
    parser.add_argument(
        "--show-serial", action="store_true", help="the serial is masked by default"
    )
    arguments = parser.parse_args()
    if arguments.samples < 2:
        parser.error("--samples needs at least 2 to tell a change from a setting")
    return arguments


def main() -> int:
    arguments = parse_arguments()
    version = json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]
    print("EcoFlow PowerOcean register scan")
    print(
        f"integration {version}, target {arguments.host}:{arguments.port}, "
        f"unit id {arguments.slave}\n"
    )

    client = ModbusTcpClient(arguments.host, port=arguments.port, timeout=10)
    if not client.connect():
        print(f"Could not connect to {arguments.host}:{arguments.port}.")
        print("Check the IP, and that Modbus TCP is enabled in the EcoFlow app.")
        return 1

    try:
        reader = RegisterReader(client, arguments.slave)
        detected = report_device(reader, arguments.show_serial)
        model = (
            models.InverterModel(arguments.model)
            if arguments.model
            else detected or const.DEFAULT_INVERTER_MODEL
        )
        print(f"Comparing against the {model.display_name} address map.\n")

        blocks = const.register_blocks_for(model)
        registers = sorted(
            (register for block in blocks for register in block.registers),
            key=lambda register: register.address,
        )
        ranges = [parse_address_range(text) for text in arguments.scan or DEFAULT_SCAN]

        report_block_reads(reader, blocks)
        missing, suspect, values = report_mapped_registers(reader, registers)
        report_derived_values(values, model)
        words, refused = (
            ({}, {}) if arguments.no_scan else report_readable_addresses(reader, ranges)
        )
        known = mapped_addresses(blocks)
        candidates = sorted(
            address
            for address, word in words.items()
            if word and address not in known and address not in NOT_POLLED
        )
        behaviour, latest = (
            report_behaviour(
                reader, blocks, words, candidates, arguments.samples, arguments.gap
            )
            if candidates
            else ({}, {})
        )
        words.update(latest)
        report_unmapped_addresses(words, refused, known, behaviour)
        report_summary(missing, suspect, values, candidates)

        print(f"{reader.reads} reads issued. Nothing was written.")
        print("Please attach this whole report to the GitHub issue.")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
