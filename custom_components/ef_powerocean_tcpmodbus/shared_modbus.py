"""Modbus transport over the connection Home Assistant's modbus integration shares.

Home Assistant 2026.9 hands out units over one connection per device, so another
integration talking to the same inverter no longer competes with us for it. On
older versions the factories at the bottom return modbus.ModbusClient instead. Both
offer the same interface and raise the same errors, so the callers need not know
which one they hold.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pymodbus.exceptions import ModbusException

from .const import DEFAULT_SLAVE, DEVICE_INFO_BLOCK
from .modbus import ModbusClient, ModbusReadRejected, ModbusRejected

try:
    from homeassistant.components.modbus import (
        async_get_temporary_unit,
        async_get_unit,
    )
    from modbus_connection import (
        ModbusError,
        ModbusExceptionError,
        ModbusTcpParams,
        ModbusUnit,
    )
except ImportError as err:  # Home Assistant before 2026.9 cannot share a connection.
    SHARED_CONNECTION = False
    _UNSHARED_REASON = str(err)
else:
    SHARED_CONNECTION = True

_LOGGER = logging.getLogger(__name__)


class SharedModbusClient:
    """The modbus client talking to the inverter over a shared connection.

    The connection opens on the first request and again on the next one after it
    drops. It belongs to the modbus integration, which closes it once the last
    config entry holding a unit on it unloads, so this client never closes it.
    """

    def __init__(self, unit: ModbusUnit) -> None:
        self._unit = unit

    @property
    def connected(self) -> bool:
        return self._unit.connected

    async def async_connect(self) -> bool:
        return await self._async_probe()

    def close(self) -> None:
        """Leave the link up, since other integrations may be using it."""

    async def async_close(self) -> None:
        """Leave the link up; the modbus integration closes it on unload."""

    async def async_reconnect(self) -> bool:
        if await self._async_probe():
            return True
        _LOGGER.debug("EF-Modbus-TCP: reconnect failed, will retry next poll")
        return False

    async def _async_probe(self) -> bool:
        """Make one request, which opens the link if it is down.

        Any answer counts, even a refusal: it proves the inverter is reachable.
        """
        try:
            await self._unit.read_holding_registers(DEVICE_INFO_BLOCK.start, 1)
        except ModbusExceptionError:
            return True
        except ModbusError as err:
            _LOGGER.debug("Modbus probe failed: %s", err)
            return False
        return True

    async def async_read(self, address: int, count: int) -> list[int]:
        """Read *count* holding registers starting at *address*."""
        try:
            return await self._unit.read_holding_registers(address, count)
        except ModbusExceptionError as err:
            exception_code = int(err.exception_code)
            raise ModbusReadRejected(
                f"Modbus error response at 0x{address:04X} with "
                f"Exception-Code {exception_code}",
                exception_code=exception_code,
            ) from err
        except ModbusError as err:
            raise ModbusException(f"Could not read register {address}: {err}") from err

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
            "Sending Modbus write command [%s]: %s as %s to address %s",
            "FC6" if len(values) == 1 else "FC16",
            what,
            [f"0x{word:04X}" for word in values],
            address,
        )

        try:
            if len(values) == 1:
                await self._unit.write_register(address, values[0])
            else:
                await self._unit.write_registers(address, values)
        except ModbusExceptionError as err:
            raise ModbusRejected(
                f"Modbus rejected {what} to register {address}: {err}",
                exception_code=int(err.exception_code),
            ) from err
        except ModbusError as err:
            raise HomeAssistantError(
                f"Could not send {what} to register {address}: {err!r}"
            ) from err


def create_client(
    hass: HomeAssistant, entry: ConfigEntry, host: str, port: int
) -> ModbusClient | SharedModbusClient:
    """Return the client for *entry*; a shared hold on the link ends when it unloads."""
    if not SHARED_CONNECTION:
        _LOGGER.info(
            "Using an own Modbus connection to %s:%s (%s)",
            host,
            port,
            _UNSHARED_REASON,
        )
        return ModbusClient(host, port)
    _LOGGER.info(
        "Using the Modbus connection Home Assistant shares to %s:%s", host, port
    )
    params = ModbusTcpParams(host=host, port=port)
    return SharedModbusClient(async_get_unit(hass, entry, params, DEFAULT_SLAVE))


@asynccontextmanager
async def async_temporary_client(
    hass: HomeAssistant, host: str, port: int
) -> AsyncIterator[ModbusClient | SharedModbusClient]:
    """Hold a client for the context, for a config flow that has no entry yet."""
    if not SHARED_CONNECTION:
        client = ModbusClient(host, port, timeout=5)
        try:
            yield client
        finally:
            client.close()
        return

    params = ModbusTcpParams(host=host, port=port)
    async with async_get_temporary_unit(hass, params, DEFAULT_SLAVE) as unit:
        yield SharedModbusClient(unit)
