"""A fake PowerOcean on localhost, answering real Modbus TCP requests."""

from __future__ import annotations

import asyncio
import struct
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

HOST = "127.0.0.1"
MBAP = struct.Struct(">HHHB")  # transaction, protocol, length, unit


class FakeInverter:
    """A minimal Modbus TCP server: holding registers, all zero until written.

    Written by hand rather than taken from pymodbus, whose server API changes
    between the versions Home Assistant ships, so it stays a fixed point.
    """

    port: int

    def __init__(self) -> None:
        self.registers: defaultdict[int, int] = defaultdict(int)
        self.requests = 0
        self.writes: list[tuple[int, list[int]]] = []

    def serve_serial(self, serial: str, address: int) -> None:
        """Put a serial number at *address*, two characters a word."""
        padded = serial.ljust(16, "\x00")
        for word, offset in enumerate(range(0, len(padded), 2)):
            self.registers[address + word] = (ord(padded[offset]) << 8) | ord(
                padded[offset + 1]
            )

    def load_diagnostics(self, diagnostics: Mapping[str, Any]) -> None:
        """Serve the words a diagnostics download recorded, as the device sent them."""
        for start, words in diagnostics["raw_registers"].items():
            for offset, word in enumerate(words):
                self.registers[int(start) + offset] = word

    def answer(self, pdu: bytes) -> bytes:
        self.requests += 1
        function = pdu[0]
        address, count = struct.unpack(">HH", pdu[1:5])
        if function == 0x03:  # read holding registers
            values = [self.registers[address + i] for i in range(count)]
            return bytes([function, 2 * count]) + struct.pack(f">{count}H", *values)
        if function == 0x06:  # write single register; count is the value
            self.writes.append((address, [count]))
            self.registers[address] = count
            return pdu[:5]
        if function == 0x10:  # write multiple registers
            written = struct.unpack(f">{count}H", pdu[6 : 6 + 2 * count])
            self.writes.append((address, list(written)))
            for i, value in enumerate(written):
                self.registers[address + i] = value
            return pdu[:5]
        return bytes([function | 0x80, 0x01])  # illegal function

    async def serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                transaction, _, length, unit = MBAP.unpack(
                    await reader.readexactly(MBAP.size)
                )
                pdu = self.answer(await reader.readexactly(length - 1))
                writer.write(MBAP.pack(transaction, 0, len(pdu) + 1, unit) + pdu)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
