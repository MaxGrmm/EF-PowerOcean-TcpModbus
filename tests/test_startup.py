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
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import PLATFORMS, const
from custom_components.ef_powerocean_tcpmodbus import modbus as modbus_module
from custom_components.ef_powerocean_tcpmodbus.control import ControlInputs
from custom_components.ef_powerocean_tcpmodbus.coordinator import EcoflowCoordinator
from custom_components.ef_powerocean_tcpmodbus.models import InverterModel, RegisterType

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
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert "frequency" not in polled
    assert {"voltage_l1", "battery_soc", "solar_total", "grid_feed_mode"} <= polled


# ── Every entity gets what it shows from the demand-driven poll ───────────────


def _fill_registers(inverter: FakeInverter, start: int = 1) -> None:
    """Give every polled register a distinct, non-zero value.

    Zero everywhere would hide a missing read behind a value that looks right, and
    leave the branches that only run for a non-zero value untried.
    """
    formats = {
        RegisterType.FLOAT32: "<f",
        RegisterType.UINT32: "<I",
        RegisterType.INT32: "<i",
    }
    for index, register in enumerate(const.MODBUS_REGISTERS):
        register = register.for_model(InverterModel.POWEROCEAN_PLUS)
        value = index + start
        if register.data_type is RegisterType.UINT16:
            words = [value]
        else:
            # Published low word first.
            raw = struct.pack(formats[register.data_type], value)
            words = list(struct.unpack("<HH", raw))
        for offset, word in enumerate(words):
            inverter.registers[register.address + offset] = word


class _RecordingData(dict):
    """The coordinator's data, noting each key read from it."""

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(data)
        self.read: set[str] = set()

    def __getitem__(self, key: str) -> Any:
        self.read.add(key)
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        self.read.add(key)
        return super().get(key, default)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            self.read.add(key)
        return super().__contains__(key)

    def __iter__(self) -> Iterator[str]:
        self.read.add("*")
        return super().__iter__()

    def items(self):  # type: ignore[override]
        self.read.add("*")
        return super().items()

    def values(self):  # type: ignore[override]
        self.read.add("*")
        return super().values()


def _enable_every_entity(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Register the entities that start disabled as enabled, so they load too."""
    registry = er.async_get(hass)
    for number in const.WRITABLE_NUMBERS_MAP:
        registry.async_get_or_create(
            "number",
            const.DOMAIN,
            f"{entry.entry_id}_{number.key}",
            config_entry=entry,
            disabled_by=None,
        )


async def test_every_entity_reads_only_what_it_asks_for(
    hass: HomeAssistant, enable_custom_integrations: None, inverter: FakeInverter
) -> None:
    """An entity that reads a register it did not ask for would show nothing once
    that register is left out of the poll. Every entity is enabled here, so the
    poll still has it; what each one reads is checked against what it asked for.
    """
    _fill_registers(inverter)
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        data={const.CONF_HOST: HOST, const.CONF_PORT: inverter.port},
    )
    entry.add_to_hass(hass)
    _enable_every_entity(hass, entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator: EcoflowCoordinator = entry.runtime_data
    registers = frozenset(const.REGISTERS_BY_KEY)
    always = coordinator._own_keys | ControlInputs.keys()
    data = coordinator.data
    checked = 0
    undeclared: dict[str, set[str]] = {}
    for platform in entity_platform.async_get_platforms(hass, const.DOMAIN):
        for entity in platform.entities.values():
            recording = _RecordingData(data)
            coordinator.data = recording
            entity.async_write_ha_state()
            asked = entity.coordinator_context or frozenset()
            extra = (recording.read & (registers | {"*"})) - asked - always
            if extra:
                undeclared[entity.entity_id] = extra
            checked += 1
    coordinator.data = data
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert checked > 100
    assert undeclared == {}


def _states(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    """Return each entity's state and attributes."""
    states = {}
    for registry_entry in er.async_entries_for_config_entry(
        er.async_get(hass), entry.entry_id
    ):
        if state := hass.states.get(registry_entry.entity_id):
            states[registry_entry.entity_id] = (state.state, dict(state.attributes))
    return states


async def _poll(
    hass: HomeAssistant, coordinator: EcoflowCoordinator, *, everything: bool = False
) -> None:
    """Run a poll, reading every register or only what has been asked for."""
    if everything:
        coordinator._wanted_keys = None
        coordinator._plan_reads()
    # A poll within a second of the last is answered from it. Moving the last
    # back rather than freezing the clock, which stalls the Modbus stack of some
    # Home Assistant versions.
    if coordinator._last_checked_time is not None:
        coordinator._last_checked_time -= timedelta(seconds=6)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    if everything:
        coordinator._async_track_wanted_keys()


def _differences(shown: dict[str, Any], reference: dict[str, Any]) -> list[str]:
    assert len(reference) > 100
    assert shown.keys() == reference.keys()
    return [
        f"{entity_id}: {shown[entity_id]} instead of {reference[entity_id]}"
        for entity_id in reference
        if shown[entity_id] != reference[entity_id]
    ]


async def test_entities_show_what_reading_everything_shows(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    inverter: FakeInverter,
) -> None:
    """Each entity, after a poll of only what was asked for, shows what it would
    after a poll of every register: at setup, with a poll landing while the
    entities are being added, and after a poll in which every value changed.
    """
    _fill_registers(inverter)
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        data={const.CONF_HOST: HOST, const.CONF_PORT: inverter.port},
    )
    entry.add_to_hass(hass)
    _enable_every_entity(hass, entry)
    forward = hass.config_entries.async_forward_entry_setups

    async def poll_then_forward(config_entry, platforms):
        await _poll(hass, config_entry.runtime_data)
        return await forward(config_entry, platforms)

    with patch.object(
        hass.config_entries, "async_forward_entry_setups", poll_then_forward
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator: EcoflowCoordinator = entry.runtime_data

    at_setup = _states(hass, entry)
    await _poll(hass, coordinator, everything=True)
    at_setup_differences = _differences(at_setup, _states(hass, entry))

    # Every value changes, so one not read would keep showing the old.
    _fill_registers(inverter, start=101)
    await _poll(hass, coordinator)
    on_demand = _states(hass, entry)
    await _poll(hass, coordinator, everything=True)
    on_demand_differences = _differences(on_demand, _states(hass, entry))

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert at_setup_differences == []
    assert on_demand_differences == []
