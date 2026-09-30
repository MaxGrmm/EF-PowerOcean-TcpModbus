"""Unit tests for the Modbus transport."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import const
from custom_components.ef_powerocean_tcpmodbus import coordinator as coordinator_module
from custom_components.ef_powerocean_tcpmodbus import modbus as modbus_module

if modbus_module.SHARED_CONNECTION:
    from modbus_connection import (
        IllegalDataAddressError,
        ModbusConnectionError,
        ServerDeviceBusyError,
    )
    from modbus_connection.mock import MockModbusConnection

shared_only = pytest.mark.skipif(
    not modbus_module.SHARED_CONNECTION,
    reason="Home Assistant before 2026.9 has no shared Modbus connection",
)


@pytest.fixture
def pymodbus():
    return SimpleNamespace()


@pytest.fixture
def client(pymodbus):
    link = modbus_module.PymodbusLink.__new__(modbus_module.PymodbusLink)
    link._device_id = const.DEFAULT_SLAVE
    link._pymodbus = pymodbus
    return modbus_module.ModbusClient(link)


@pytest.fixture
def unit():
    return MockModbusConnection().for_unit(const.DEFAULT_SLAVE)


@pytest.fixture
def shared_client(unit):
    return modbus_module.ModbusClient(modbus_module.SharedLink(unit))


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
        assert isinstance(raised.value, modbus_module.ModbusException)
        assert raised.value.exception_code == 2
    else:
        assert asyncio.run(client.async_read(100, 2)) == [11, 22]

    pymodbus.read_holding_registers.assert_awaited_once_with(
        address=100,
        count=2,
        device_id=const.DEFAULT_SLAVE,
    )


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

    pymodbus.write_register = AsyncMock(
        side_effect=modbus_module.ModbusException("connection reset")
    )

    with pytest.raises(modbus_module.HomeAssistantError) as raised:
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

    with pytest.raises(modbus_module.ModbusException) as raised:
        await shared_client.async_read(100, 2)
    assert not isinstance(raised.value, modbus_module.ModbusReadRejected)


@shared_only
async def test_a_shared_link_writes_one_word_as_fc6_and_more_as_fc16(
    shared_client, unit
) -> None:
    written = []
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

    with pytest.raises(modbus_module.HomeAssistantError) as raised:
        await shared_client.async_write(40608, [1], what="heartbeat")
    assert not isinstance(raised.value, modbus_module.ModbusRejected)


@shared_only
async def test_any_answer_counts_as_a_shared_link_being_up(shared_client, unit) -> None:
    unit.fail_read(const.DEVICE_INFO_BLOCK.start, IllegalDataAddressError())
    assert await shared_client.async_connect()

    unit.fail_requests(ModbusConnectionError("link down"))
    assert not await shared_client.async_connect()
