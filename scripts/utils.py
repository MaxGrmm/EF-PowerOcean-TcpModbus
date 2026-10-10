"""Shared helpers for the inverter scan scripts."""

from __future__ import annotations

import importlib
import sys
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
    """Load the register map without importing all of Home Assistant."""
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
        for module in ("const", "models", "telemetry", "control_test_core")
    )


const, models, telemetry, core = load_integration()

from pymodbus.client import ModbusTcpClient  # noqa: E402

EXCEPTION_MEANINGS: Final[dict[int, str]] = {
    1: "illegal function",
    2: "illegal data address",
    3: "illegal data value",
    4: "device failure",
    6: "device busy",
}


def render_address(address: int) -> str:
    return f"{address} (0x{address:04X})"


class RegisterReader:
    """Interface to read a register from the inverter."""

    def __init__(self, client: ModbusTcpClient, slave: int) -> None:
        self._client = client
        self._slave = slave
        # The caller sets this after detecting the model's published word order.
        self.high_word_first = False
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

    def read_value(self, register: models.RegisterDef) -> tuple[float | None, str]:
        """Read one register and decode it; an empty reason means the read worked."""
        words, reason = self.read(register.address, register.size)
        if words is None:
            return None, reason
        return self.decode(words, register), ""

    def read_mapped(self, block: models.RegisterBlock) -> dict[str, float | None]:
        """Read a whole block and decode every register it carries."""
        words, _ = self.read(block.start, block.count)
        if words is None:
            return {}
        return {
            register.key: self.decode(block.registers_for(words, register), register)
            for register in block.registers
        }

    def decode(self, words: list[int], register: models.RegisterDef) -> float | None:
        return telemetry.decode_register(
            words, register.data_type, self.high_word_first
        )


def report_device(
    reader: RegisterReader, show_serial: bool, identity: dict | None = None
) -> models.InverterModel | None:
    """Print the device identity and return its detected model, if known.

    *identity*, if given, is filled with the firmware and protocol version.
    """
    print("== Device ==")
    block = const.DEVICE_INFO_BLOCK
    words, reason = reader.read(block.start, block.count)
    if words is None:
        print(f"  Device info read failed at {render_address(block.start)}: {reason}")
        print("  Pass --model to continue with a specific map.\n")
        return None

    def words_for(register: models.RegisterDef) -> list[int]:
        return block.registers_for(words, register)

    serial = telemetry.decode_serial_number(words_for(const.SERIAL_NUMBER)) or "unknown"
    number = words_for(const.PRODUCT_NUMBER)[0]
    category = words_for(const.PRODUCT_CATEGORY)[0]
    detected = models.InverterModel.from_product_info(number, category)
    firmware = telemetry.decode_firmware_version(
        words_for(const.FIRMWARE_VERSION),
        detected.traits.high_word_first if detected else False,
    )
    protocol, protocol_reason = reader.read_value(const.PROTOCOL_VERSION)
    address, address_reason = reader.read_value(const.DEVICE_ADDRESS)

    print(f"  Serial number:    {serial if show_serial else serial[:4] + '****'}")
    print(f"  Firmware:          {firmware}")
    print(
        f"  Protocol version:  {int(protocol) if protocol is not None else 'unknown'}"
        + (f" [unreadable: {protocol_reason}]" if protocol is None else "")
    )
    print(f"  Product number:    {number}")
    print(f"  Product category:  {category}")
    print(
        f"  Device address:    {int(address) if address is not None else 'unknown'}"
        + (f" [unreadable: {address_reason}]" if address is None else "")
    )
    if identity is not None:
        identity["firmware_version"] = firmware
        identity["protocol_version"] = int(protocol) if protocol is not None else None
    name = detected.traits.display_name if detected else "UNKNOWN"
    print(f"  Detected model:    {name}")
    if detected is None:
        print(
            "  No model matches these product registers; the default map will be used."
        )
        print(
            "  Include the product number and category in the issue so support can add it."
        )
    print()
    return detected
