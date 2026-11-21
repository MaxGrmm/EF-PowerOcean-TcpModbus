"""Unit tests for the Modbus transport."""

from __future__ import annotations

import asyncio
import logging
import sys
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from awesomeversion import AwesomeVersion
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import const
from custom_components.ef_powerocean_tcpmodbus import coordinator as coordinator_module
from custom_components.ef_powerocean_tcpmodbus import modbus as modbus_module

SHARED = (MAJOR_VERSION, MINOR_VERSION) >= (2026, 9)

if SHARED:
    from modbus_connection import (
        ClientClosedError,
        IllegalDataAddressError,
        ModbusConnectionError,
        ModbusTimeoutError,
        ServerDeviceBusyError,
    )
    from modbus_connection.mock import MockModbusConnection

shared_only = pytest.mark.skipif(
    not SHARED,
    reason="Home Assistant before 2026.9 has no shared Modbus connection",
)


@pytest.fixture(autouse=True)
def shared_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for modbus_prepare where Home Assistant can share a connection."""
    if SHARED:
        import modbus_connection
        from homeassistant.components import modbus as ha_modbus

        monkeypatch.setattr(
            modbus_module,
            "_shared",
            modbus_module._SharedModbus(ha=ha_modbus, mc=modbus_connection),
        )


@pytest.fixture
def pymodbus():
    return SimpleNamespace()


@pytest.fixture
def client(pymodbus):
    link = modbus_module.PymodbusLink.__new__(modbus_module.PymodbusLink)
    link._device_id = const.DEFAULT_SLAVE
    link._pymodbus = pymodbus
    return modbus_module.ModbusClient(cast(modbus_module.ModbusLink, link))


@pytest.fixture
def unit():
    return MockModbusConnection().for_unit(const.DEFAULT_SLAVE)


@pytest.fixture
def shared_client(unit):
    import modbus_connection

    return modbus_module.ModbusClient(modbus_module.SharedLink(unit, modbus_connection))


def answer(*, is_error: bool, registers=None):
    return Mock(
        isError=Mock(return_value=is_error),
        exception_code=2,
        registers=registers or [],
    )


@pytest.mark.parametrize("is_error", (False, True), ids=("success", "modbus-error"))
def test_reads_register_block(client, pymodbus, is_error: bool) -> None:
    pymodbus.read_holding_registers = AsyncMock(
        return_value=answer(is_error=is_error, registers=[11, 22])
    )

    if is_error:
        with pytest.raises(modbus_module.ModbusReadRejected) as raised:
            asyncio.run(client.async_read(100, 2))
        # Still a ModbusException, so existing read handling is unchanged.
        assert isinstance(raised.value, ModbusException)
        assert raised.value.exception_code == 2
    else:
        assert asyncio.run(client.async_read(100, 2)) == [11, 22]

    pymodbus.read_holding_registers.assert_awaited_once_with(
        address=100,
        count=2,
        device_id=const.DEFAULT_SLAVE,
    )
    # A refusal is an answer as much as registers are.
    assert client.answers == 1


def test_a_read_that_gets_no_reply_is_no_answer(client, pymodbus) -> None:
    pymodbus.read_holding_registers = AsyncMock(
        side_effect=ModbusException("No response received")
    )

    with pytest.raises(ModbusException):
        asyncio.run(client.async_read(100, 2))

    assert client.answers == 0


def test_a_single_word_is_written_as_fc6(client, pymodbus) -> None:
    """FC16 for one register is legal but some devices only implement FC6."""
    pymodbus.write_register = AsyncMock(return_value=answer(is_error=False))

    asyncio.run(client.async_write(40608, [1], what="heartbeat"))

    pymodbus.write_register.assert_awaited_once_with(
        address=40608, value=1, device_id=const.DEFAULT_SLAVE
    )


def test_several_words_are_written_as_fc16(client, pymodbus) -> None:
    pymodbus.write_registers = AsyncMock(return_value=answer(is_error=False))

    asyncio.run(client.async_write(40534, [0x0000, 0x0038], what="control command"))

    pymodbus.write_registers.assert_awaited_once_with(
        address=40534, values=[0x0000, 0x0038], device_id=const.DEFAULT_SLAVE
    )


def test_a_refused_write_is_told_apart_from_one_that_never_arrived(
    client, pymodbus
) -> None:
    """A rejection is final; a transport failure is worth retrying."""
    pymodbus.write_register = AsyncMock(return_value=answer(is_error=True))

    with pytest.raises(modbus_module.ModbusRejected):
        asyncio.run(client.async_write(40608, [1], what="heartbeat"))

    pymodbus.write_register = AsyncMock(side_effect=ModbusException("connection reset"))

    with pytest.raises(HomeAssistantError) as raised:
        asyncio.run(client.async_write(40608, [1], what="heartbeat"))

    assert not isinstance(raised.value, modbus_module.ModbusRejected)


@shared_only
async def test_the_coordinator_uses_the_shared_connection(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=const.DOMAIN, data={const.CONF_HOST: "10.0.0.2"})

    coordinator = coordinator_module.EcoflowCoordinator(hass, entry)

    assert isinstance(coordinator._modbus_client._link, modbus_module.SharedLink)


@shared_only
async def test_a_shared_link_tells_a_refused_read_from_a_lost_link(
    shared_client, unit
) -> None:
    unit.fail_read(100, IllegalDataAddressError())

    with pytest.raises(modbus_module.ModbusReadRejected) as raised:
        await shared_client.async_read(100, 2)
    assert raised.value.exception_code == 2
    assert raised.value.permanent

    unit.fail_requests(ModbusConnectionError("link down"))

    with pytest.raises(ModbusException) as lost:
        await shared_client.async_read(100, 2)
    assert not isinstance(lost.value, modbus_module.ModbusReadRejected)


@shared_only
async def test_a_shared_link_writes_one_word_as_fc6_and_more_as_fc16(
    shared_client, unit
) -> None:
    written: list[Any] = []
    unit.on_write(written.append)

    await shared_client.async_write(40608, [1], what="heartbeat")
    await shared_client.async_write(40534, [0x0000, 0x0038], what="control command")

    assert [(w.address, w.values, w.function_code) for w in written] == [
        (40608, [1], 0x06),
        (40534, [0x0000, 0x0038], 0x10),
    ]


@shared_only
async def test_a_shared_link_tells_a_refused_write_from_a_lost_link(
    shared_client, unit
) -> None:
    unit.fail_write(40608, ServerDeviceBusyError())

    with pytest.raises(modbus_module.ModbusRejected) as raised:
        await shared_client.async_write(40608, [1], what="heartbeat")
    assert raised.value.transient

    unit.fail_requests(ModbusConnectionError("link down"))

    with pytest.raises(HomeAssistantError) as lost:
        await shared_client.async_write(40608, [1], what="heartbeat")
    assert not isinstance(lost.value, modbus_module.ModbusRejected)


@shared_only
async def test_any_answer_counts_as_a_shared_link_being_up(shared_client, unit) -> None:
    unit.fail_read(const.DEVICE_INFO_BLOCK.start, IllegalDataAddressError())
    assert await shared_client.async_connect()

    unit.fail_requests(ModbusConnectionError("link down"))
    assert not await shared_client.async_connect()


@shared_only
async def test_a_shared_link_asks_for_our_timeout(unit) -> None:
    import modbus_connection

    modbus_module.SharedLink(unit, modbus_connection)

    assert unit.required_timeout == const.MODBUS_REQUEST_TIMEOUT_S


@shared_only
async def test_a_shared_link_drops_a_connection_that_stops_answering(
    shared_client, unit
) -> None:
    """A firewall that swallows packets leaves the old connection open but useless."""
    unit.disconnect = AsyncMock()
    unit.fail_requests(ModbusTimeoutError("Response timeout"))

    assert not await shared_client.async_connect()

    unit.disconnect.assert_awaited_once()


@shared_only
async def test_a_failed_probe_keeps_a_connection_that_did_not_time_out(
    shared_client, unit
) -> None:
    unit.disconnect = AsyncMock()
    unit.fail_requests(ModbusConnectionError("link down"))

    assert not await shared_client.async_connect()

    unit.disconnect.assert_not_awaited()


# ── A request the connection lost is sent once more, on a new connection ─────

OWN_CONNECTION_ERRORS = (
    ConnectionException("Not connected"),
    ModbusIOException("No response received after 0 retries"),
    ConnectionResetError("reset by peer"),
    BrokenPipeError("broken pipe"),
    TimeoutError(),
)


def _reconnecting(pymodbus, *, works: bool = True) -> None:
    pymodbus.connected = True
    pymodbus.close = Mock()
    pymodbus.connect = AsyncMock(
        side_effect=lambda: setattr(pymodbus, "connected", works)
    )


@pytest.mark.parametrize("error", OWN_CONNECTION_ERRORS, ids=repr)
def test_an_own_link_resends_a_read_the_connection_lost(
    client, pymodbus, error: Exception
) -> None:
    _reconnecting(pymodbus)
    pymodbus.read_holding_registers = AsyncMock(
        side_effect=[error, answer(is_error=False, registers=[11, 22])]
    )

    assert asyncio.run(client.async_read(100, 2)) == [11, 22]

    pymodbus.close.assert_called_once()
    pymodbus.connect.assert_awaited_once()
    assert pymodbus.read_holding_registers.await_count == 2
    # The lost attempt was no answer.
    assert client.answers == 1


def test_an_own_link_resends_a_write_the_connection_lost(client, pymodbus) -> None:
    _reconnecting(pymodbus)
    pymodbus.write_register = AsyncMock(
        side_effect=[ConnectionResetError("reset"), answer(is_error=False)]
    )

    asyncio.run(client.async_write(40608, [1], what="heartbeat"))

    assert pymodbus.write_register.await_count == 2


def test_an_own_link_sends_a_request_once_more_at_most(client, pymodbus) -> None:
    _reconnecting(pymodbus)
    pymodbus.read_holding_registers = AsyncMock(side_effect=ModbusIOException("none"))

    with pytest.raises(ModbusException):
        asyncio.run(client.async_read(100, 2))

    assert pymodbus.read_holding_registers.await_count == 2
    assert client.answers == 0


def test_an_own_link_does_not_resend_when_no_new_connection_opens(
    client, pymodbus
) -> None:
    _reconnecting(pymodbus, works=False)
    pymodbus.read_holding_registers = AsyncMock(side_effect=ConnectionResetError())

    with pytest.raises(ModbusException):
        asyncio.run(client.async_read(100, 2))

    assert pymodbus.read_holding_registers.await_count == 1


def test_an_own_link_does_not_resend_when_opening_one_fails(client, pymodbus) -> None:
    _reconnecting(pymodbus)
    pymodbus.connect = AsyncMock(side_effect=OSError("unreachable"))
    pymodbus.read_holding_registers = AsyncMock(side_effect=ConnectionResetError())

    with pytest.raises(ModbusException):
        asyncio.run(client.async_read(100, 2))

    assert pymodbus.read_holding_registers.await_count == 1


def test_a_refusal_is_not_sent_again(client, pymodbus) -> None:
    _reconnecting(pymodbus)
    pymodbus.read_holding_registers = AsyncMock(return_value=answer(is_error=True))

    with pytest.raises(modbus_module.ModbusReadRejected):
        asyncio.run(client.async_read(100, 2))

    pymodbus.read_holding_registers.assert_awaited_once()
    pymodbus.close.assert_not_called()


def test_a_closed_client_does_not_resend(client, pymodbus) -> None:
    _reconnecting(pymodbus)
    pymodbus.read_holding_registers = AsyncMock(side_effect=ConnectionResetError())
    asyncio.run(client.async_close())

    with pytest.raises(ModbusException):
        asyncio.run(client.async_read(100, 2))

    pymodbus.read_holding_registers.assert_awaited_once()


def _lost_once(unit, error: Exception) -> None:
    """Fail every request until the connection is dropped, as a dead connection."""
    unit.fail_requests(error)
    unit.disconnect = AsyncMock(side_effect=lambda: unit.fail_requests(None))


SHARED_CONNECTION_ERRORS = (
    (
        "lost before the answer",
        lambda: ModbusConnectionError(
            "read_holding_registers(40519, 89): Connection lost before response "
            "was received."
        ),
    ),
    ("closed", lambda: ClientClosedError("connection is closed")),
    ("timeout", lambda: ModbusTimeoutError("Response timeout")),
)


@shared_only
@pytest.mark.parametrize(
    "error",
    [make for _, make in SHARED_CONNECTION_ERRORS],
    ids=[name for name, _ in SHARED_CONNECTION_ERRORS],
)
async def test_a_shared_link_resends_a_read_the_connection_lost(
    shared_client, unit, error
) -> None:
    unit.holding[100] = 7
    _lost_once(unit, error())

    assert await shared_client.async_read(100, 1) == [7]

    unit.disconnect.assert_awaited_once()
    assert len(unit.read_events) == 2
    assert shared_client.answers == 1


@shared_only
async def test_a_shared_link_resends_a_write_the_connection_lost(
    shared_client, unit
) -> None:
    written: list[Any] = []
    unit.on_write(written.append)
    _lost_once(unit, ModbusConnectionError("Connection lost before response"))

    await shared_client.async_write(40608, [1], what="heartbeat")

    unit.disconnect.assert_awaited_once()
    assert [(w.address, w.values) for w in written] == [(40608, [1])]


@shared_only
async def test_a_shared_link_sends_a_request_once_more_at_most(
    shared_client, unit
) -> None:
    unit.disconnect = AsyncMock()
    unit.fail_requests(ModbusConnectionError("link down"))

    with pytest.raises(ModbusException):
        await shared_client.async_read(100, 2)

    assert len(unit.read_events) == 2
    unit.disconnect.assert_awaited_once()


@shared_only
async def test_a_shared_link_does_not_resend_a_refusal(shared_client, unit) -> None:
    unit.disconnect = AsyncMock()
    unit.fail_read(100, IllegalDataAddressError())

    with pytest.raises(modbus_module.ModbusReadRejected):
        await shared_client.async_read(100, 2)

    assert len(unit.read_events) == 1
    unit.disconnect.assert_not_awaited()


@shared_only
async def test_a_shared_link_without_disconnect_resends_all_the_same() -> None:
    """Releases before 4.9 cannot drop the connection; a lost one reopens anyway."""
    import modbus_connection

    shared = SimpleNamespace(
        connected=True,
        read_holding_registers=AsyncMock(
            side_effect=[ModbusConnectionError("lost"), [7]]
        ),
    )
    client = modbus_module.ModbusClient(
        modbus_module.SharedLink(shared, modbus_connection)
    )

    assert await client.async_read(100, 1) == [7]


class _FlakyLink:
    def __init__(self, up_on_attempt: int) -> None:
        self._up_on_attempt = up_on_attempt
        self.attempts = 0
        self.connected = False
        self.lock_held_while_sleeping: list[bool] = []

    async def connect(self) -> bool:
        self.attempts += 1
        self.connected = self.attempts >= self._up_on_attempt
        return self.connected

    def close(self) -> None:
        self.connected = False


def test_the_reconnect_backoff_does_not_hold_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link = _FlakyLink(up_on_attempt=3)
    client = modbus_module.ModbusClient(cast(modbus_module.ModbusLink, link))

    async def sleep(_delay: float) -> None:
        link.lock_held_while_sleeping.append(client._lock.locked())

    monkeypatch.setattr(asyncio, "sleep", sleep)

    assert asyncio.run(client.async_reconnect())
    assert link.attempts == 3
    assert link.lock_held_while_sleeping
    assert not any(link.lock_held_while_sleeping)


def test_a_closed_client_stops_reconnecting(monkeypatch: pytest.MonkeyPatch) -> None:
    link = _FlakyLink(up_on_attempt=99)
    client = modbus_module.ModbusClient(cast(modbus_module.ModbusLink, link))

    async def sleep(_delay: float) -> None:
        await client.async_close()

    monkeypatch.setattr(asyncio, "sleep", sleep)

    assert not asyncio.run(client.async_reconnect())
    assert link.attempts == 1


def test_blocked_writes_never_reach_the_link(client, pymodbus) -> None:
    pymodbus.write_register = AsyncMock()
    client.block_writes("identity_unverified")

    with pytest.raises(HomeAssistantError) as raised:
        asyncio.run(client.async_write(40536, [10], what="reserve"))
    assert raised.value.translation_key == "identity_unverified"
    assert raised.value.translation_placeholders == {
        "what": "reserve",
        "register": "40536",
    }
    pymodbus.write_register.assert_not_called()

    client.allow_writes()
    pymodbus.write_register.return_value = answer(is_error=False)
    asyncio.run(client.async_write(40536, [10], what="reserve"))
    pymodbus.write_register.assert_awaited_once()


def test_an_own_link_waits_a_few_seconds_not_twenty() -> None:
    async def make() -> modbus_module.PymodbusLink:
        return modbus_module.PymodbusLink("192.0.2.10", 502)

    link = asyncio.run(make())

    assert link._pymodbus.comm_params.timeout_connect == const.MODBUS_REQUEST_TIMEOUT_S
    assert const.MODBUS_POLL_BUDGET_S < const.HEARTBEAT_WINDOW_S / 2


def test_reconnecting_an_open_link_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    link = _FlakyLink(up_on_attempt=1)
    link.connected = True
    client = modbus_module.ModbusClient(cast(modbus_module.ModbusLink, link))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    assert asyncio.run(client.async_reconnect())
    assert link.attempts == 0


def test_reconnecting_gives_up_after_every_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link = _FlakyLink(up_on_attempt=99)
    client = modbus_module.ModbusClient(cast(modbus_module.ModbusLink, link))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    assert not asyncio.run(client.async_reconnect())
    assert link.attempts == len(modbus_module.RECONNECT_DELAYS_S)


def test_closing_a_client_closes_its_link() -> None:
    link = _FlakyLink(up_on_attempt=1)
    link.connected = True
    modbus_module.ModbusClient(cast(modbus_module.ModbusLink, link)).close()

    assert not link.connected


# ── An own connection, where Home Assistant cannot share one ─────────────────


@pytest.mark.usefixtures("socket_enabled")
async def test_an_own_link_connects_reads_and_closes(inverter: Any) -> None:
    inverter.registers[40000] = 7
    link = modbus_module.PymodbusLink("127.0.0.1", inverter.port)

    assert await link.connect()
    assert link.connected
    assert await link.read_holding_registers(40000, 1) == [7]
    link.close()
    assert not link.connected


@pytest.mark.usefixtures("socket_enabled")
async def test_a_temporary_client_is_an_own_link_without_sharing(
    hass: HomeAssistant, inverter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(modbus_module, "_shared", None)
    monkeypatch.setattr(modbus_module, "SHARED_MIN_VERSION", AwesomeVersion("9999.1"))

    async with modbus_module.async_temporary_client(
        hass, "127.0.0.1", inverter.port
    ) as client:
        assert isinstance(client._link, modbus_module.PymodbusLink)
        assert await client.async_connect()
        link = client._link

    assert not link.connected


async def _prepare_unshared(hass: HomeAssistant) -> bool:
    modbus_module._shared = None
    return await modbus_module.async_prepare(hass)


async def test_an_older_home_assistant_is_not_shared(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(modbus_module, "SHARED_MIN_VERSION", AwesomeVersion("9999.1"))

    assert not await _prepare_unshared(hass)


async def test_a_modbus_integration_that_fails_is_not_shared(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(modbus_module, "SHARED_MIN_VERSION", AwesomeVersion("0.0"))
    monkeypatch.setattr(
        modbus_module, "async_setup_component", AsyncMock(return_value=False)
    )

    assert not await _prepare_unshared(hass)
    assert modbus_module._unshared_reason == "the modbus integration did not set up"


async def test_missing_modbus_modules_are_not_shared(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(modbus_module, "SHARED_MIN_VERSION", AwesomeVersion("0.0"))
    monkeypatch.setattr(
        modbus_module, "async_setup_component", AsyncMock(return_value=True)
    )
    monkeypatch.setitem(sys.modules, "modbus_connection", None)

    assert not await _prepare_unshared(hass)
    assert "modbus_connection" in modbus_module._unshared_reason


async def test_a_modbus_integration_without_sharing_is_not_shared(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeassistant.components import modbus as ha_modbus

    monkeypatch.setattr(modbus_module, "SHARED_MIN_VERSION", AwesomeVersion("0.0"))
    monkeypatch.setattr(
        modbus_module, "async_setup_component", AsyncMock(return_value=True)
    )
    monkeypatch.setitem(sys.modules, "modbus_connection", SimpleNamespace())
    monkeypatch.delattr(ha_modbus, "async_get_unit", raising=False)

    assert not await _prepare_unshared(hass)
    assert modbus_module._unshared_reason == (
        "the modbus integration does not share connections"
    )


def test_a_failed_reconnect_does_not_log_above_debug(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The coordinator logs an outage once; the transport stays quiet each poll."""
    link = _FlakyLink(up_on_attempt=99)
    client = modbus_module.ModbusClient(cast(modbus_module.ModbusLink, link))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    for _ in range(3):
        assert not asyncio.run(client.async_reconnect())

    assert [r for r in caplog.records if r.levelno > logging.DEBUG] == []


class _SwallowingPymodbus:
    """Turns the cancellation of a request into an error, as pymodbus 3.11 does."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def write_register(self, **_kwargs: Any) -> Any:
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError as err:
            raise ModbusException("Request cancelled outside pymodbus.") from err


async def test_a_cancelled_request_stays_cancelled_whatever_pymodbus_raises() -> None:
    """A heartbeat cancelled mid-write has to end, or unloading waits for it."""
    pymodbus = _SwallowingPymodbus()
    link = modbus_module.PymodbusLink.__new__(modbus_module.PymodbusLink)
    link._device_id = const.DEFAULT_SLAVE
    link._pymodbus = pymodbus  # type: ignore[assignment]

    task = asyncio.create_task(link.write_register(40608, 1))
    await pymodbus.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_failed_request_that_was_not_cancelled_still_fails() -> None:
    link = modbus_module.PymodbusLink.__new__(modbus_module.PymodbusLink)
    link._device_id = const.DEFAULT_SLAVE
    link._pymodbus = SimpleNamespace(  # type: ignore[assignment]
        write_register=AsyncMock(side_effect=ModbusException("no answer"))
    )

    with pytest.raises(ModbusException, match="no answer"):
        await link.write_register(40608, 1)
