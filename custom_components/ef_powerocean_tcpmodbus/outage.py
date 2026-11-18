"""Tell why the inverter stopped answering, as far as the network shows.

A firmware update can switch Modbus TCP off. The inverter then stays silent
rather than answering with zeros, so a timeout is all a read sees. Whether the
port still takes connections tells a silent inverter from one that is gone.
"""

from __future__ import annotations

import asyncio
import contextlib
from enum import StrEnum
from typing import TYPE_CHECKING

from .const import MODBUS_REQUEST_TIMEOUT_S

if TYPE_CHECKING:
    from .modbus import ModbusClient


class Cause(StrEnum):
    """Why the inverter does not answer, as far as the network shows."""

    # Nothing takes a connection on the port.
    UNREACHABLE = "unreachable"
    # The port takes connections, but no Modbus reply comes back.
    MODBUS_SILENT = "modbus_silent"


async def async_classify(host: str, port: int) -> Cause:
    """Connect to the port and close again at once, sending nothing.

    The handshake is all it takes: an inverter that limits its clients mostly
    does so by closing a connection it has already accepted.
    """
    try:
        async with asyncio.timeout(MODBUS_REQUEST_TIMEOUT_S):
            _, writer = await asyncio.open_connection(host, port)
    except (OSError, TimeoutError):
        return Cause.UNREACHABLE
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return Cause.MODBUS_SILENT


async def async_cause(client: ModbusClient, host: str, port: int) -> Cause:
    """Name the cause from the client's link where it can tell, else probe.

    A link that holds a connection its reads get no answer on has already shown
    the port open. One that holds none leaves the inverter free for the probe.
    """
    if client.connected:
        return Cause.MODBUS_SILENT
    return await async_classify(host, port)
