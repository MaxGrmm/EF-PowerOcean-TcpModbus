"""Modbus TCP transport for the PowerOcean.

One ModbusClient talks to the inverter over a link. From Home Assistant 2026.9 the
link is a unit on the connection Home Assistant shares, so another integration
talking to the same inverter no longer competes with us for it. Older versions get
a pymodbus connection of our own.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Final, Protocol

from awesomeversion import AwesomeVersion
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException
from pymodbus.pdu import ModbusPDU

from .const import (
    DEFAULT_SLAVE,
    DEVICE_INFO_BLOCK,
    DOMAIN,
    MODBUS_REQUEST_TIMEOUT_S,
    SLEEP_TIME_AFTER_RECONNECT_S,
)

_LOGGER = logging.getLogger(__name__)

# The .dev0 makes betas of 2026.9 count.
SHARED_MIN_VERSION: Final = AwesomeVersion("2026.9.0.dev0")


@dataclass(frozen=True, slots=True)
class _SharedModbus:
    """The modules behind Home Assistant's shared connection, imported on demand."""

    ha: ModuleType
    mc: ModuleType


_shared: _SharedModbus | None = None
_unshared_reason = "Home Assistant older than 2026.9"

# Short, so the inverter is read again soon after it is back: each attempt is one
# connect to a device on the local network.
RECONNECT_DELAYS_S: Final = (0, 5, 10, 20)
TRANSPORT_ERRORS: Final = (ModbusException, ConnectionError, asyncio.TimeoutError)
# What pymodbus raises when the connection fails a request: not connected, no
# answer, as after the inverter closed the connection, or a socket error or timeout.
_PYMODBUS_CONNECTION_ERRORS: Final = (ConnectionException, ModbusIOException, OSError)

# When the request running in this task must be done, as a loop time. A retry that
# could not finish before then is not started.
_retry_deadline: ContextVar[float | None] = ContextVar("retry_deadline", default=None)


def is_shared() -> bool:
    """Return whether this Home Assistant shares its Modbus connection with us."""
    return _shared is not None


async def async_prepare(hass: HomeAssistant) -> bool:
    """Set the modbus integration up if it can be shared, and report whether it is.

    Setting it up is what registers its connections panel and installs its
    requirements, so this has to run before anything asks for a unit.
    """
    global _shared, _unshared_reason
    if _shared is not None:
        return True
    if AwesomeVersion(HA_VERSION) < SHARED_MIN_VERSION:
        return False
    if not await async_setup_component(hass, "modbus", {}):
        _unshared_reason = "the modbus integration did not set up"
        return False
    # Imported here: neither exists before Home Assistant 2026.9.
    try:
        import modbus_connection
        from homeassistant.components import modbus as ha_modbus
    except ImportError as err:
        _unshared_reason = str(err)
        return False
    if not hasattr(ha_modbus, "async_get_unit"):
        _unshared_reason = "the modbus integration does not share connections"
        return False
    _shared = _SharedModbus(ha=ha_modbus, mc=modbus_connection)
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

    def __init__(
        self,
        message: str | None = None,
        *,
        exception_code: int | None = None,
        translation_placeholders: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            message,
            translation_domain=DOMAIN if message is None else None,
            translation_key="write_rejected" if message is None else None,
            translation_placeholders=translation_placeholders,
        )
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


class ConnectionLost(ModbusException):
    """A link's report that the connection failed a request the device never saw.

    Not connected, lost before the answer, a socket error or a timeout: asking
    again on a new connection may well work.
    """


class DeviceRefused(Exception):
    """A link's report that the device answered with a Modbus exception response."""

    def __init__(self, exception_code: int | None, detail: object = None) -> None:
        super().__init__(detail or f"Exception-Code {exception_code}")
        self.exception_code = exception_code


class ModbusLink(Protocol):
    """The part that reaches the inverter; the only thing that differs per client.

    Requests raise DeviceRefused when the device refuses, ConnectionLost when the
    connection failed them, and one of TRANSPORT_ERRORS for anything else.
    """

    @property
    def connected(self) -> bool: ...

    async def connect(self) -> bool: ...

    async def reopen(self) -> bool:
        """Drop the connection and open a new one; return whether it is open."""
        ...

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
        timeout: float = MODBUS_REQUEST_TIMEOUT_S,
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

    async def reopen(self) -> bool:
        self.close()
        try:
            return await self.connect()
        except _PYMODBUS_CONNECTION_ERRORS as err:
            _LOGGER.debug("Could not open a new connection: %s", err)
            return False

    def close(self) -> None:
        self._pymodbus.close()

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        with _connection_errors_told_apart(), _cancellation_kept():
            response = await self._pymodbus.read_holding_registers(
                address=address, count=count, device_id=self._device_id
            )
        return list(_answered(response).registers)

    async def write_register(self, address: int, value: int) -> None:
        with _connection_errors_told_apart(), _cancellation_kept():
            response = await self._pymodbus.write_register(
                address=address, value=value, device_id=self._device_id
            )
        _answered(response)

    async def write_registers(self, address: int, values: list[int]) -> None:
        with _connection_errors_told_apart(), _cancellation_kept():
            response = await self._pymodbus.write_registers(
                address=address, values=values, device_id=self._device_id
            )
        _answered(response)


@contextmanager
def _connection_errors_told_apart() -> Iterator[None]:
    """Raise what pymodbus raises for a failed connection as ConnectionLost."""
    try:
        yield
    except ConnectionLost:
        raise
    except _PYMODBUS_CONNECTION_ERRORS as err:
        raise ConnectionLost(str(err) or type(err).__name__) from err


@contextmanager
def _cancellation_kept() -> Iterator[None]:
    """Raise a cancellation that pymodbus turned into an error as one again.

    Older pymodbus releases, such as the 3.11 Home Assistant 2025.12 pins, catch the
    CancelledError of a request in flight and raise a ModbusIOException instead.
    Taken for a failed request, it would keep a cancelled task, such as the
    heartbeat, running, and whatever waits for that task to end would wait forever.
    """
    try:
        yield
    except ModbusException as err:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError from err
        raise


def _answered(response: ModbusPDU) -> ModbusPDU:
    """Return a pymodbus response, raising DeviceRefused for an exception response."""
    if response.isError():
        raise DeviceRefused(getattr(response, "exception_code", None), response)
    return response


@contextmanager
def _translated_errors(mc: ModuleType) -> Iterator[None]:
    """Raise the modbus_connection errors as the ones every link raises."""
    try:
        yield
    except mc.ModbusExceptionError as err:
        raise DeviceRefused(int(err.exception_code), err) from err
    except (mc.ModbusConnectionError, _timeout_error(mc)) as err:
        raise ConnectionLost(str(err)) from err
    except mc.ModbusError as err:
        raise ModbusException(str(err)) from err


def _timeout_error(mc: ModuleType) -> type[Exception]:
    # Older modbus-connection releases lack it.
    error: type[Exception] = getattr(mc, "ModbusTimeoutError", TimeoutError)
    return error


class SharedLink:
    """A unit on the connection the modbus integration shares.

    The connection opens on the first request and again on the next one after it
    drops. It belongs to the modbus integration, which closes it once the last
    config entry holding a unit on it unloads, so this link never closes it. It
    only drops it when it failed a request, for the next one to open a new one.
    """

    def __init__(self, unit: Any, mc: ModuleType) -> None:
        self._unit = unit
        self._mc = mc
        # Older modbus-connection releases lack this; the link then works on the
        # library's 10 s timeout.
        if hasattr(unit, "require_timeout"):
            # The connection uses the longest timeout any unit on it asks for.
            unit.require_timeout(MODBUS_REQUEST_TIMEOUT_S)

    async def _drop(self) -> None:
        """Drop the connection, for the next request to open a new one.

        A connection whose packets a firewall or a dead route swallows stays open
        without answers. It is the same device for every unit on it, so a new
        connection serves them all better. Releases before 4.9 cannot drop it.
        """
        disconnect = getattr(self._unit, "disconnect", None)
        if disconnect is not None:
            with suppress(self._mc.ModbusError):
                await disconnect()

    @property
    def connected(self) -> bool:
        return bool(self._unit.connected)

    async def connect(self) -> bool:
        """Make one request, which opens the link if it is down.

        Any answer counts, even a refusal: it proves the inverter is reachable.
        """
        try:
            await self._unit.read_holding_registers(DEVICE_INFO_BLOCK.start, 1)
        except self._mc.ModbusExceptionError:
            return True
        except _timeout_error(self._mc) as err:
            _LOGGER.debug("Modbus probe failed: %s", err)
            await self._drop()
            return False
        except self._mc.ModbusError as err:
            _LOGGER.debug("Modbus probe failed: %s", err)
            return False
        return True

    async def reopen(self) -> bool:
        """Drop the connection; the request sent next opens a new one."""
        await self._drop()
        return True

    def close(self) -> None:
        """Leave the link up, since other integrations may be using it."""

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        with _translated_errors(self._mc):
            return list(await self._unit.read_holding_registers(address, count))

    async def write_register(self, address: int, value: int) -> None:
        with _translated_errors(self._mc):
            await self._unit.write_register(address, value)

    async def write_registers(self, address: int, values: list[int]) -> None:
        with _translated_errors(self._mc):
            await self._unit.write_registers(address, values)


class ModbusClient:
    """The modbus client talking to the inverter over *link*."""

    def __init__(self, link: ModbusLink) -> None:
        self._link = link
        self._lock = asyncio.Lock()
        # Stops a reconnect in its backoff from reopening the link after unload.
        self._closed = False
        self._write_block: tuple[str, dict[str, str]] | None = None
        # Reads the device replied to, a refusal included. Compared across a poll,
        # it tells a slow or refusing inverter from a silent one.
        self.answers = 0
        # Writes sent, by address, and reconnects started, for the alerts.
        self.writes: Counter[int] = Counter()
        self.reconnects = 0

    @property
    def connected(self) -> bool:
        return self._link.connected

    @property
    def writes_allowed(self) -> bool:
        return self._write_block is None

    def block_writes(
        self, translation_key: str, placeholders: dict[str, str] | None = None
    ) -> None:
        """Refuse writes until allowed, with the error a refused write raises."""
        self._write_block = (translation_key, placeholders or {})

    def allow_writes(self) -> None:
        self._write_block = None

    async def async_connect(self) -> bool:
        return await self._link.connect()

    def close(self) -> None:
        self._link.close()

    async def async_close(self) -> None:
        """Close once any transaction in flight has finished."""
        self._closed = True
        async with self._lock:
            self._link.close()

    async def async_reconnect(self) -> bool:
        """Retry the connection with a widening backoff.

        The backoff waits outside the lock, so writes and unload are not stalled.
        """
        _LOGGER.debug("Modbus TCP is not connected. Start reconnect!")
        self.reconnects += 1
        attempts = len(RECONNECT_DELAYS_S)

        for attempt, delay in enumerate(RECONNECT_DELAYS_S, start=1):
            if delay > 0:
                _LOGGER.debug("Reconnect failed! Wait %ss until next attempt.", delay)
                await asyncio.sleep(delay)

            async with self._lock:
                if self._closed:
                    return False
                if self._link.connected:
                    return True
                _LOGGER.debug("Modbus TCP reconnect (attempt %s/%s)", attempt, attempts)
                if await self._link.connect():
                    _LOGGER.debug("Reconnected after %s/%s attempts", attempt, attempts)
                    connected = True
                else:
                    self._link.close()
                    connected = False
            if connected:
                await asyncio.sleep(SLEEP_TIME_AFTER_RECONNECT_S)
                return True

        # Debug only: the coordinator logs the outage once, and the recovery.
        _LOGGER.debug("All reconnect attempts failed; retrying at the next poll")
        return False

    async def _sent[T](self, request: Callable[[], Awaitable[T]]) -> T:
        """Send *request*, and once more on a new connection if the connection lost it.

        The inverter may close an idle connection just as a request goes out. A
        refusal is an answer and is not retried. Called with the lock held.
        """
        try:
            return await request()
        except ConnectionLost as err:
            if self._closed or not _time_for_a_retry():
                raise
            _LOGGER.debug("%s; retrying on a new connection", err)
            if not await self._link.reopen():
                raise
        return await request()

    async def async_read(self, address: int, count: int) -> list[int]:
        """Read *count* holding registers starting at *address*."""
        async with self._lock:
            try:
                registers = await self._sent(
                    lambda: self._link.read_holding_registers(address, count)
                )
            except DeviceRefused as err:
                self.answers += 1
                raise ModbusReadRejected(
                    f"Modbus error response at 0x{address:04X} with "
                    f"Exception-Code {err.exception_code}",
                    exception_code=err.exception_code,
                ) from err
            self.answers += 1
            return registers

    async def async_write(
        self, address: int, words: Sequence[int], *, what: str
    ) -> None:
        """Write *words* to *address*, as FC6 for one word and FC16 for more.

        *what* names the value in the error a user would see. Raises
        ModbusRejected if the device refused it and HomeAssistantError if it never
        arrived.
        """
        if self._write_block is not None:
            key, placeholders = self._write_block
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=key,
                translation_placeholders={
                    "what": what,
                    "register": str(address),
                    **placeholders,
                },
            )
        values = list(words)
        self.writes[address] += 1
        _LOGGER.debug(
            "Sending Modbus write command [%s]: %s as %s to address %s",
            "FC6" if len(values) == 1 else "FC16",
            what,
            [f"0x{word:04X}" for word in values],
            address,
        )

        try:
            # The control writes set absolute values, so sending one twice is safe.
            async with self._lock:
                if len(values) == 1:
                    await self._sent(
                        lambda: self._link.write_register(address, values[0])
                    )
                else:
                    await self._sent(
                        lambda: self._link.write_registers(address, values)
                    )
        except DeviceRefused as err:
            raise ModbusRejected(
                exception_code=err.exception_code,
                translation_placeholders={
                    "what": what,
                    "register": str(address),
                    "error": str(err),
                },
            ) from err
        except TRANSPORT_ERRORS as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
                translation_placeholders={
                    "what": what,
                    "register": str(address),
                    "error": repr(err),
                },
            ) from err


@contextmanager
def retries_until(deadline: float | None) -> Iterator[None]:
    """Start no retry in this task that could not finish by *deadline*, a loop time."""
    token = _retry_deadline.set(deadline)
    try:
        yield
    finally:
        _retry_deadline.reset(token)


def _time_for_a_retry() -> bool:
    deadline = _retry_deadline.get()
    if deadline is None:
        return True
    return deadline - asyncio.get_running_loop().time() >= MODBUS_REQUEST_TIMEOUT_S


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
    return ModbusClient(SharedLink(unit, _shared.mc))


@asynccontextmanager
async def async_temporary_client(
    hass: HomeAssistant, host: str, port: int
) -> AsyncIterator[ModbusClient]:
    """Hold a client for the context, for a config flow that has no entry yet."""
    await async_prepare(hass)
    if _shared is None:
        client = ModbusClient(PymodbusLink(host, port))
        try:
            yield client
        finally:
            client.close()
        return

    params = _shared.mc.ModbusTcpParams(host=host, port=port)
    async with _shared.ha.async_get_temporary_unit(hass, params, DEFAULT_SLAVE) as unit:
        yield ModbusClient(SharedLink(unit, _shared.mc))
