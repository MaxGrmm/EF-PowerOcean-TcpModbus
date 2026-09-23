"""Modbus TCP transport for EcoFlow PowerOcean Plus."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Final

from homeassistant.exceptions import HomeAssistantError
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .const import DEFAULT_SLAVE, SLEEP_TIME_AFTER_RECONNECT_S

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAYS_S: Final = (0, 5, 30, 120)
TRANSPORT_ERRORS: Final = (ModbusException, ConnectionError, asyncio.TimeoutError)


class ModbusRejected(HomeAssistantError):
    """The device answered a write with a Modbus exception response.

    Worth telling apart from a transport failure: the device was reached and said
    no. Whether repeating the write is worth it depends on the exception code.
    """

    # Illegal function, data address and data value fault the request itself, so it
    # will be refused again. Everything else — device busy above all — faults the
    # moment, and a device that was busy is the same device a second later.
    PERMANENT_CODES: Final = frozenset({0x01, 0x02, 0x03})

    def __init__(self, message: str, *, exception_code: int | None = None) -> None:
        super().__init__(message)
        self.exception_code = exception_code

    @property
    def transient(self) -> bool:
        return self.exception_code not in self.PERMANENT_CODES


class ModbusClient:
    """The modbus client talking to the inverter."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        slave_id: int = DEFAULT_SLAVE,
        timeout: float = 20,
    ) -> None:
        self.host = host
        self.port = port
        self.slave_id = slave_id
        self._pymodbus = AsyncModbusTcpClient(
            host=host, port=port, timeout=timeout, reconnect_delay=0, retries=0
        )
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._pymodbus.connected

    async def async_connect(self) -> bool:
        await self._pymodbus.connect()
        return self._pymodbus.connected

    def close(self) -> None:
        self._pymodbus.close()

    async def async_close(self) -> None:
        """Close once any transaction in flight has finished."""
        async with self._lock:
            self._pymodbus.close()

    async def async_reconnect(self) -> bool:
        """Retry the connection with a widening backoff."""
        _LOGGER.debug(
            f"Modbus TCP {self.host}:{self.port} is not connected. Start reconnect!"
        )
        attempts = len(RECONNECT_DELAYS_S)

        for attempt, delay in enumerate(RECONNECT_DELAYS_S, start=1):
            async with self._lock:
                if delay > 0:
                    _LOGGER.debug(
                        f"Reconnect failed! Wait {delay}s until next attempt."
                    )
                    await asyncio.sleep(delay)

                _LOGGER.debug(f"Modbus TCP reconnect (Attempt {attempt}/{attempts})...")
                if await self._pymodbus.connect() and self._pymodbus.connected:
                    _LOGGER.debug(
                        f"Reconnect successful! Attempts: {attempt}/{attempts}"
                    )
                    await asyncio.sleep(SLEEP_TIME_AFTER_RECONNECT_S)
                    return True
                self._pymodbus.close()

        _LOGGER.error(
            "EF-Modbus-TCP: All reconnect attempts failed! – will retry next poll"
        )
        return False

    async def async_read(self, address: int, count: int) -> list[int]:
        """Read *count* holding registers starting at *address*."""
        async with self._lock:
            response = await self._pymodbus.read_holding_registers(
                address=address, count=count, device_id=self.slave_id
            )
            if response.isError():
                # A Modbus error response means the connection may be stale.
                raise ModbusException(
                    f"Modbus error response at 0x{address:04X} with "
                    f"Exception-Code {response.exception_code}"
                )
            return response.registers

    async def async_write(
        self, address: int, words: Sequence[int], *, what: str
    ) -> None:
        """Write *words* to *address*, as FC6 for one word and FC16 for more.

        *what* names the value in the error a user would see. Raises
        ModbusRejected if the device refused it and HomeAssistantError if it never
        arrived.
        """
        values = list(words)
        _LOGGER.debug(
            "Sending Modbus write command [%s]: %s as %s to address %s (Device ID: %s)",
            "FC6" if len(values) == 1 else "FC16",
            what,
            [f"0x{word:04X}" for word in values],
            address,
            self.slave_id,
        )

        try:
            async with self._lock:
                if len(values) == 1:
                    response = await self._pymodbus.write_register(
                        address=address, value=values[0], device_id=self.slave_id
                    )
                else:
                    response = await self._pymodbus.write_registers(
                        address=address, values=values, device_id=self.slave_id
                    )
        except TRANSPORT_ERRORS as err:
            raise HomeAssistantError(
                f"Could not send {what} to register {address}: {err!r}"
            ) from err

        if response.isError():
            raise ModbusRejected(
                f"Modbus rejected {what} to register {address}: {response}",
                exception_code=getattr(response, "exception_code", None),
            )
