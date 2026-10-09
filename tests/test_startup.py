"""The integration starts in a real Home Assistant, talking to a real Modbus server.

Every other test mocks something. This one only fakes the inverter: Home Assistant
loads the integration and its dependencies through its own loader, the connection
goes over TCP through whichever Modbus stack that Home Assistant version ships,
and every platform sets up. It is the test that fails when a new Home Assistant or
library version would stop the integration from starting at all.
"""

from __future__ import annotations

import asyncio
import struct
from collections import defaultdict
from collections.abc import AsyncIterator

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import PLATFORMS, const
from custom_components.ef_powerocean_tcpmodbus import modbus as modbus_module

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

    def answer(self, pdu: bytes) -> bytes:
        self.requests += 1
        function = pdu[0]
        address, count = struct.unpack(">HH", pdu[1:5])
        if function == 0x03:  # read holding registers
            values = [self.registers[address + i] for i in range(count)]
            return bytes([function, 2 * count]) + struct.pack(f">{count}H", *values)
        if function == 0x06:  # write single register; count is the value
            self.registers[address] = count
            return pdu[:5]
        if function == 0x10:  # write multiple registers
            values = struct.unpack(f">{count}H", pdu[6 : 6 + 2 * count])
            for i, value in enumerate(values):
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


@pytest.fixture
async def inverter(socket_enabled: None) -> AsyncIterator[FakeInverter]:
    """Serve a fake inverter on localhost."""
    fake = FakeInverter()
    server = await asyncio.start_server(fake.serve, HOST, 0)
    fake.port = server.sockets[0].getsockname()[1]
    yield fake
    server.close()
    await server.wait_closed()


async def test_uses_the_shared_connection_where_home_assistant_has_one(
    hass: HomeAssistant,
) -> None:
    """The fallback to an own connection must not hide a broken shared one."""
    expected = (MAJOR_VERSION, MINOR_VERSION) >= (2026, 9)
    assert await modbus_module.async_prepare(hass) is expected
    assert modbus_module.is_shared() is expected


async def test_starts_and_unloads(
    hass: HomeAssistant, enable_custom_integrations: None, inverter: FakeInverter
) -> None:
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        data={const.CONF_HOST: HOST, const.CONF_PORT: inverter.port},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert {entity.domain for entity in entities} == {str(p) for p in PLATFORMS}
    assert inverter.requests  # Read over the wire, not from a stub.
    states = [hass.states.get(entity.entity_id) for entity in entities]
    assert any(state and state.state != "unavailable" for state in states)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


SERIAL = "HJ31ZAS2TEST0001"


def _serve_serial(inverter: FakeInverter, serial: str) -> None:
    """Put a serial number in the device information block, two characters a word."""
    padded = serial.ljust(const.SERIAL_NUMBER.size * 2, "\x00")
    for word, offset in enumerate(range(0, len(padded), 2)):
        inverter.registers[const.SERIAL_NUMBER.address + word] = (
            ord(padded[offset]) << 8
        ) | ord(padded[offset + 1])


async def test_an_entry_keyed_by_address_takes_the_serial_number(
    hass: HomeAssistant, enable_custom_integrations: None, inverter: FakeInverter
) -> None:
    """Entries made before the serial was read are keyed by host and port."""
    _serve_serial(inverter, SERIAL)
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        unique_id=f"{HOST}:{inverter.port}",
        data={const.CONF_HOST: HOST, const.CONF_PORT: inverter.port},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.unique_id == SERIAL

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_serial_number_another_entry_has_is_left_to_it(
    hass: HomeAssistant, enable_custom_integrations: None, inverter: FakeInverter
) -> None:
    _serve_serial(inverter, SERIAL)
    MockConfigEntry(domain=const.DOMAIN, unique_id=SERIAL).add_to_hass(hass)
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        unique_id=f"{HOST}:{inverter.port}",
        data={const.CONF_HOST: HOST, const.CONF_PORT: inverter.port},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.unique_id == f"{HOST}:{inverter.port}"

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_raises_a_repairs_issue_while_modbus_is_disabled(
    hass: HomeAssistant, enable_custom_integrations: None, inverter: FakeInverter
) -> None:
    """The inverter answers, with a serial number, but every value reads zero."""
    _serve_serial(inverter, SERIAL)
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        data={const.CONF_HOST: HOST, const.CONF_PORT: inverter.port},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = ir.async_get(hass)
    issue_id = f"modbus_disabled_{entry.entry_id}"
    assert registry.async_get_issue(const.DOMAIN, issue_id) is None

    coordinator = entry.runtime_data
    for _ in range(const.MODBUS_DISABLED_READ_THRESHOLD):
        await coordinator.async_refresh()
    await hass.async_block_till_done()

    issue = registry.async_get_issue(const.DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders == {"host": HOST}

    inverter.registers[const.REGISTERS_BY_KEY["inverter_rated_power"].address] = 1
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert registry.async_get_issue(const.DOMAIN, issue_id) is None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_device_registered_as_a_service_becomes_a_device(
    hass: HomeAssistant, enable_custom_integrations: None, inverter: FakeInverter
) -> None:
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        data={const.CONF_HOST: HOST, const.CONF_PORT: inverter.port},
    )
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(const.DOMAIN, entry.entry_id)},
        entry_type=dr.DeviceEntryType.SERVICE,
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(device.id).entry_type is None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_disabled_sensor_is_not_read(
    hass: HomeAssistant, enable_custom_integrations: None, inverter: FakeInverter
) -> None:
    """Only what an enabled entity, the control loop or the coordinator needs."""
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        data={const.CONF_HOST: HOST, const.CONF_PORT: inverter.port},
    )
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor",
        const.DOMAIN,
        f"{entry.entry_id}_frequency",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    polled = entry.runtime_data.polled_registers
    assert "frequency" not in polled
    assert {"voltage_l1", "battery_soc", "solar_total", "grid_feed_mode"} <= polled

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
