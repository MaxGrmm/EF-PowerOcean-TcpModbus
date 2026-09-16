"""Unit tests for the Modbus transport without Home Assistant."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from ef_powerocean_tcpmodbus import const
from ef_powerocean_tcpmodbus import modbus as modbus_module


@pytest.fixture
def client():
    instance = modbus_module.ModbusClient.__new__(modbus_module.ModbusClient)
    instance.host = "localhost"
    instance.port = 502
    instance.slave_id = const.DEFAULT_SLAVE
    instance._pymodbus = SimpleNamespace()
    # asyncio.run() builds a fresh loop per call, and a lock binds to the first one.
    instance._lock = asyncio.Lock()
    return instance


def answer(*, is_error: bool, registers=None):
    return Mock(
        isError=Mock(return_value=is_error),
        exception_code=2,
        registers=registers or [],
    )


@pytest.mark.parametrize("is_error", (False, True), ids=("success", "modbus-error"))
def test_reads_register_block(client, is_error: bool) -> None:
    client._pymodbus.read_holding_registers = AsyncMock(
        return_value=answer(is_error=is_error, registers=[11, 22])
    )

    if is_error:
        with pytest.raises(modbus_module.ModbusException):
            asyncio.run(client.async_read(100, 2))
    else:
        assert asyncio.run(client.async_read(100, 2)) == [11, 22]

    client._pymodbus.read_holding_registers.assert_awaited_once_with(
        address=100,
        count=2,
        device_id=const.DEFAULT_SLAVE,
    )


def test_a_single_word_is_written_as_fc6(client) -> None:
    """FC16 for one register is legal but some devices only implement FC6."""
    client._pymodbus.write_register = AsyncMock(return_value=answer(is_error=False))

    asyncio.run(client.async_write(40608, [1], what="heartbeat"))

    client._pymodbus.write_register.assert_awaited_once_with(
        address=40608, value=1, device_id=const.DEFAULT_SLAVE
    )


def test_several_words_are_written_as_fc16(client) -> None:
    client._pymodbus.write_registers = AsyncMock(return_value=answer(is_error=False))

    asyncio.run(client.async_write(40534, [0x0000, 0x0038], what="control command"))

    client._pymodbus.write_registers.assert_awaited_once_with(
        address=40534, values=[0x0000, 0x0038], device_id=const.DEFAULT_SLAVE
    )


def test_a_refused_write_is_told_apart_from_one_that_never_arrived(client) -> None:
    """A rejection is final; a transport failure is worth retrying."""
    client._pymodbus.write_register = AsyncMock(return_value=answer(is_error=True))

    with pytest.raises(modbus_module.ModbusRejected):
        asyncio.run(client.async_write(40608, [1], what="heartbeat"))

    client._pymodbus.write_register = AsyncMock(
        side_effect=modbus_module.ModbusException("connection reset")
    )

    with pytest.raises(modbus_module.HomeAssistantError) as raised:
        asyncio.run(client.async_write(40608, [1], what="heartbeat"))

    assert not isinstance(raised.value, modbus_module.ModbusRejected)
