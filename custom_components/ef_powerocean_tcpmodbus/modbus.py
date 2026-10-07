"""Modbus TCP transport for EcoFlow PowerOcean Plus.

One ModbusClient talks to the inverter over a link. From Home Assistant 2026.9 the
link is a unit on the connection Home Assistant shares, so another integration
talking to the same inverter no longer competes with us for it. Older versions get
a pymodbus connection of our own.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from typing import Any, Final, Protocol

from awesomeversion import AwesomeVersion
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .const import DEFAULT_SLAVE, DEVICE_INFO_BLOCK, SLEEP_TIME_AFTER_RECONNECT_S

_LOGGER = logging.getLogger(__name__)

# The .dev0 makes betas of 2026.9 count.
SHARED_MIN_VERSION: Final = AwesomeVersion("2026.9.0.dev0")

_shared: SimpleNamespace | None = None
_unshared_reason = "Home Assistant older than 2026.9"

RECONNECT_DELAYS_S: Final = (0, 5, 30, 120)
TRANSPORT_ERRORS: Final = (ModbusException, ConnectionError, asyncio.TimeoutError)


def is_shared() -> bool:
    """Return whether this Home Assistant shares its Modbus connection with us."""
    return _shared is not None


async def async_prepare(hass: HomeAssistant) -> bool:
    """Set the modbus integration up if it can be shared, and report whether it is.

    Setting it up is what registers its connections panel and installs its
    requirements, so this has to run before anything asks for a unit.
    """
    global _shared, _unshared_reason  # noqa: PLW0603
    if _shared is not None:
        return True
    if AwesomeVersion(HA_VERSION) < SHARED_MIN_VERSION:
        return False
    if not await async_setup_component(hass, "modbus", {}):
        _unshared_reason = "the modbus integration did not set up"
        return False
    try:
        import modbus_connection  # noqa: PLC0415
        from homeassistant.components import modbus as ha_modbus  # noqa: PLC0415

        ha_modbus.async_get_unit  # noqa: B018
    except (ImportError, AttributeError) as err:
        _unshared_reason = str(err)
        return False
    _shared = SimpleNamespace(ha=ha_modbus, mc=modbus_connection)
    return True


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


class ModbusReadRejected(ModbusException):
    """The device answered a read with a Modbus exception response.

    A ModbusException like any other read failure, so existing handling treats it
    the same, but it can be told apart from a lost connection: the device is there
    and refused this particular request, typically for an address it lacks.
    """

    def __init__(self, message: str, *, exception_code: int | None = None) -> None:
        super().__init__(message)
        self.exception_code = exception_code

    @property
    def permanent(self) -> bool:
        """Return whether the request itself is invalid, so asking again is pointless.

        Illegal address is how a device answers for a register it does not have.
        """
        return self.exception_code in ModbusRejected.PERMANENT_CODES


class DeviceRefused(Exception):
    """A link's report that the device answered with a Modbus exception response."""

    def __init__(self, exception_code: int | None, detail: object = None) -> None:
        super().__init__(detail or f"Exception-Code {exception_code}")
        self.exception_code = exception_code


class ModbusLink(Protocol):
    """The part that reaches the inverter; the only thing that differs per client.

    Requests raise DeviceRefused when the device refuses, and one of
    TRANSPORT_ERRORS when it cannot be reached.
    """

    @property
    def connected(self) -> bool: ...

    async def connect(self) -> bool: ...

    def close(self) -> None: ...

    async def read_holding_registers(self, address: int, count: int) -> list[int]: ...

    async def write_register(self, address: int, value: int) -> None: ...

    async def write_registers(self, address: int, values: list[int]) -> None: ...


class PymodbusLink:
    """A connection of our own, opened with pymodbus."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        device_id: int = DEFAULT_SLAVE,
        timeout: float = 20,
    ) -> None:
        self._device_id = device_id
        self._pymodbus = AsyncModbusTcpClient(
            host=host, port=port, timeout=timeout, reconnect_delay=0, retries=0
        )

    @property
    def connected(self) -> bool:
        return self._pymodbus.connected

    async def connect(self) -> bool:
        await self._pymodbus.connect()
        return self._pymodbus.connected

    def close(self) -> None:
        self._pymodbus.close()

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        response = await self._pymodbus.read_holding_registers(
            address=address, count=count, device_id=self._device_id
        )
        return _answered(response).registers

    async def write_register(self, address: int, value: int) -> None:
        _answered(
            await self._pymodbus.write_register(
                address=address, value=value, device_id=self._device_id
            )
        )

    async def write_registers(self, address: int, values: list[int]) -> None:
        _answered(
            await self._pymodbus.write_registers(
                address=address, values=values, device_id=self._device_id
            )
        )


def _answered(response: Any) -> Any:
    """Return a pymodbus response, raising DeviceRefused for an exception response."""
    if response.isError():
        raise DeviceRefused(getattr(response, "exception_code", None), response)
    return response


@contextmanager
def _translated_errors() -> Iterator[None]:
    """Raise the modbus_connection errors as the ones every link raises."""
    try:
        yield
    except _shared.mc.ModbusExceptionError as err:
        raise DeviceRefused(int(err.exception_code), err) from err
    except _shared.mc.ModbusError as err:
        raise ModbusException(str(err)) from err


class SharedLink:
    """A unit on the connection the modbus integration shares.

    The connection opens on the first request and again on the next one after it
    drops. It belongs to the modbus integration, which closes it once the last
    config entry holding a unit on it unloads, so this link never closes it.
    """

    def __init__(self, unit: Any) -> None:
        self._unit = unit

    @property
    def connected(self) -> bool:
        return self._unit.connected

    async def connect(self) -> bool:
        """Make one request, which opens the link if it is down.

        Any answer counts, even a refusal: it proves the inverter is reachable.
        """
        try:
            await self._unit.read_holding_registers(DEVICE_INFO_BLOCK.start, 1)
        except _shared.mc.ModbusExceptionError:
            return True
        except _shared.mc.ModbusError as err:
            _LOGGER.debug("Modbus probe failed: %s", err)
            return False
        return True

    def close(self) -> None:
        """Leave the link up, since other integrations may be using it."""

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        with _translated_errors():
            return await self._unit.read_holding_registers(address, count)

    async def write_register(self, address: int, value: int) -> None:
        with _translated_errors():
            await self._unit.write_register(address, value)

    async def write_registers(self, address: int, values: list[int]) -> None:
        with _translated_errors():
            await self._unit.write_registers(address, values)


class ModbusClient:
    """The modbus client talking to the inverter over *link*."""

    def __init__(self, link: ModbusLink) -> None:
        self._link = link
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._link.connected

    async def async_connect(self) -> bool:
        return await self._link.connect()

    def close(self) -> None:
        self._link.close()

    async def async_close(self) -> None:
        """Close once any transaction in flight has finished."""
        async with self._lock:
            self._link.close()

    async def async_reconnect(self) -> bool:
        """Retry the connection with a widening backoff."""
        _LOGGER.debug("Modbus TCP is not connected. Start reconnect!")
        attempts = len(RECONNECT_DELAYS_S)

        for attempt, delay in enumerate(RECONNECT_DELAYS_S, start=1):
            async with self._lock:
                if delay > 0:
                    _LOGGER.debug(
                        f"Reconnect failed! Wait {delay}s until next attempt."
                    )
                    await asyncio.sleep(delay)

                _LOGGER.debug(f"Modbus TCP reconnect (Attempt {attempt}/{attempts})...")
                if await self._link.connect():
                    _LOGGER.debug(
                        f"Reconnect successful! Attempts: {attempt}/{attempts}"
                    )
                    await asyncio.sleep(SLEEP_TIME_AFTER_RECONNECT_S)
                    return True
                self._link.close()

        _LOGGER.error(
            "EF-Modbus-TCP: All reconnect attempts failed! – will retry next poll"
        )
        return False

    async def async_read(self, address: int, count: int) -> list[int]:
        """Read *count* holding registers starting at *address*."""
        async with self._lock:
            try:
                return await self._link.read_holding_registers(address, count)
            except DeviceRefused as err:
                raise ModbusReadRejected(
                    f"Modbus error response at 0x{address:04X} with "
                    f"Exception-Code {err.exception_code}",
                    exception_code=err.exception_code,
                ) from err

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
            async with self._lock:
                if len(values) == 1:
                    await self._link.write_register(address, values[0])
                else:
                    await self._link.write_registers(address, values)
        except DeviceRefused as err:
            raise ModbusRejected(
                f"Modbus rejected {what} to register {address}: {err}",
                exception_code=err.exception_code,
            ) from err
        except TRANSPORT_ERRORS as err:
            raise HomeAssistantError(
                f"Could not send {what} to register {address}: {err!r}"
            ) from err


def create_client(
    hass: HomeAssistant, entry: ConfigEntry, host: str, port: int
) -> ModbusClient:
    """Return the client for *entry*; a shared hold on the link ends when it unloads."""
    if _shared is None:
        _LOGGER.info(
            "Using an own Modbus connection to %s:%s (%s)",
            host,
            port,
            _unshared_reason,
        )
        return ModbusClient(PymodbusLink(host, port))
    _LOGGER.info(
        "Using the Modbus connection Home Assistant shares to %s:%s", host, port
    )
    params = _shared.mc.ModbusTcpParams(host=host, port=port)
    unit = _shared.ha.async_get_unit(hass, entry, params, DEFAULT_SLAVE)
    return ModbusClient(SharedLink(unit))


@asynccontextmanager
async def async_temporary_client(
    hass: HomeAssistant, host: str, port: int
) -> AsyncIterator[ModbusClient]:
    """Hold a client for the context, for a config flow that has no entry yet."""
    await async_prepare(hass)
    if _shared is None:
        client = ModbusClient(PymodbusLink(host, port, timeout=5))
        try:
            yield client
        finally:
            client.close()
        return

    params = _shared.mc.ModbusTcpParams(host=host, port=port)
    async with _shared.ha.async_get_temporary_unit(
        hass, params, DEFAULT_SLAVE
    ) as unit:
        yield ModbusClient(SharedLink(unit))
