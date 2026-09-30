"""Unit tests for the Modbus transport over Home Assistant's shared connection."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import const, shared_modbus
from custom_components.ef_powerocean_tcpmodbus import coordinator as coordinator_module
from custom_components.ef_powerocean_tcpmodbus.modbus import (
    ModbusException,
    ModbusReadRejected,
    ModbusRejected,
)

if not shared_modbus.SHARED_CONNECTION:
    pytest.skip(
        "Home Assistant before 2026.9 has no shared Modbus connection",
        allow_module_level=True,
    )

from modbus_connection import (  # noqa: E402
    IllegalDataAddressError,
    ModbusConnectionError,
    ServerDeviceBusyError,
)
from modbus_connection.mock import MockModbusConnection  # noqa: E402


@pytest.fixture
def unit():
    return MockModbusConnection().for_unit(const.DEFAULT_SLAVE)


@pytest.fixture
def client(unit):
    return shared_modbus.SharedModbusClient(unit)


async def test_the_coordinator_uses_the_shared_connection(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=const.DOMAIN, data={const.CONF_HOST: "10.0.0.2"})

    coordinator = coordinator_module.EcoflowCoordinator(hass, entry)

    assert isinstance(coordinator._modbus_client, shared_modbus.SharedModbusClient)


async def test_reads_register_block(client, unit) -> None:
    unit.holding[100] = [11, 22]

    assert await client.async_read(100, 2) == [11, 22]


async def test_a_refused_read_is_told_apart_from_a_lost_link(client, unit) -> None:
    unit.fail_read(100, IllegalDataAddressError())

    with pytest.raises(ModbusReadRejected) as raised:
        await client.async_read(100, 2)
    assert raised.value.exception_code == 2
    assert raised.value.permanent

    unit.fail_requests(ModbusConnectionError("link down"))

    with pytest.raises(ModbusException) as raised:
        await client.async_read(100, 2)
    assert not isinstance(raised.value, ModbusReadRejected)


async def test_one_word_is_written_as_fc6_and_more_as_fc16(client, unit) -> None:
    written = []
    unit.on_write(written.append)

    await client.async_write(40608, [1], what="heartbeat")
    await client.async_write(40534, [0x0000, 0x0038], what="control command")

    assert [(w.address, w.values, w.function_code) for w in written] == [
        (40608, [1], 0x06),
        (40534, [0x0000, 0x0038], 0x10),
    ]


async def test_a_refused_write_is_told_apart_from_one_that_never_arrived(
    client, unit
) -> None:
    unit.fail_write(40608, ServerDeviceBusyError())

    with pytest.raises(ModbusRejected) as raised:
        await client.async_write(40608, [1], what="heartbeat")
    assert raised.value.transient

    unit.fail_requests(ModbusConnectionError("link down"))

    with pytest.raises(HomeAssistantError) as raised:
        await client.async_write(40608, [1], what="heartbeat")
    assert not isinstance(raised.value, ModbusRejected)


async def test_any_answer_counts_as_connected(client, unit) -> None:
    unit.fail_read(const.DEVICE_INFO_BLOCK.start, IllegalDataAddressError())
    assert await client.async_connect()

    unit.fail_requests(ModbusConnectionError("link down"))
    assert not await client.async_reconnect()
