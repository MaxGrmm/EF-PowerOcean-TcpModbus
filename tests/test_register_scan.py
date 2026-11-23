"""The register scan run from Home Assistant, against a fake client."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import const
from custom_components.ef_powerocean_tcpmodbus.coordinator import EcoflowCoordinator
from custom_components.ef_powerocean_tcpmodbus.modbus import ModbusReadRejected
from custom_components.ef_powerocean_tcpmodbus.powerocean import const as device_const
from custom_components.ef_powerocean_tcpmodbus.powerocean import (
    registers as register_map,
)
from custom_components.ef_powerocean_tcpmodbus.powerocean.models import InverterModel
from custom_components.ef_powerocean_tcpmodbus.tools.register_scan import (
    RegisterScan,
    RegisterScanState,
)

SERIAL = "HJ31ZDH4ZF6Q0123"
CANDIDATES = {register.key: register for register in register_map.CANDIDATE_REGISTERS}


class FakeClient:
    """Answers reads from *words*; has no way to write, so a write would fail."""

    def __init__(self) -> None:
        self.words: dict[int, int] = {}
        self.refuse: set[int] = set()
        self.busy_once: set[int] = set()
        self.gone_at: int | None = None
        self.reads: list[tuple[int, int]] = []

    async def async_read(self, address: int, count: int) -> list[int]:
        self.reads.append((address, count))
        span = range(address, address + count)
        if self.gone_at in span:
            raise ConnectionError("gone")
        if address in self.busy_once:
            self.busy_once.discard(address)
            raise ModbusReadRejected("busy", exception_code=6)
        if self.refuse.intersection(span):
            raise ModbusReadRejected("illegal address", exception_code=2)
        return [self.words.get(each, 0) for each in span]

    def serve_serial(self, serial: str, address: int) -> None:
        padded = serial.ljust(16, "\x00")
        for word, offset in enumerate(range(0, len(padded), 2)):
            self.words[address + word] = (ord(padded[offset]) << 8) | ord(
                padded[offset + 1]
            )


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def coordinator(hass: HomeAssistant, client: FakeClient) -> SimpleNamespace:
    entry = MockConfigEntry(domain=const.DOMAIN)
    entry.add_to_hass(hass)
    return SimpleNamespace(
        hass=hass,
        config_entry=entry,
        modbus_client=client,
        inverter_model=InverterModel.POWEROCEAN_PLUS,
        identity=SimpleNamespace(firmware_version="3.0.20.54", protocol_version=1),
        connected=True,
        data={"battery_soc": 50.0},
        control_test=SimpleNamespace(running=False),
    )


def scan(coordinator: SimpleNamespace) -> RegisterScan:
    return RegisterScan(cast(EcoflowCoordinator, coordinator), request_gap_s=0)


def read_addresses(client: FakeClient) -> set[int]:
    return {
        each
        for address, count in client.reads
        for each in range(address, address + count)
    }


async def test_a_scan_reads_the_map_the_candidates_and_the_ranges(
    coordinator: SimpleNamespace, client: FakeClient
) -> None:
    report = await scan(coordinator).async_run()

    assert report["outcome"] == RegisterScanState.DONE
    assert report["firmware_version"] == "3.0.20.54"
    assert set(report["candidates"]) == set(CANDIDATES)
    blocks = register_map.register_blocks_for(InverterModel.POWEROCEAN_PLUS)
    assert {register.key for block in blocks for register in block.registers} == set(
        report["mapped"]
    )
    assert all(block["result"] == "ok" for block in report["blocks"])
    read = read_addresses(client)
    for low, high in register_map.REGISTER_SCAN_RANGES:
        assert set(range(low, high + 1)) - {device_const.HEARTBEAT_REGISTER} <= read
    assert report["refused"] == {}


async def test_the_heartbeat_register_is_never_read(
    coordinator: SimpleNamespace, client: FakeClient
) -> None:
    """A read of it would make the inverter answer the next beat with device busy."""
    await scan(coordinator).async_run()

    assert device_const.HEARTBEAT_REGISTER not in read_addresses(client)


async def test_a_candidate_is_decoded_with_its_own_type(
    coordinator: SimpleNamespace, client: FakeClient
) -> None:
    client.words[CANDIDATES["battery_state"].address] = 0xFF88
    client.words[CANDIDATES["battery_control_mode"].address] = 1
    client.refuse.add(CANDIDATES["house_consumption_total"].address)

    candidates = (await scan(coordinator).async_run())["candidates"]

    assert candidates["battery_state"]["value"] == -120.0
    assert candidates["battery_state"]["raw"] == [0xFF88]
    assert candidates["battery_control_mode"]["value"] == 1.0
    assert candidates["house_consumption_total"]["error"] == "exception code 2"
    assert "value" not in candidates["house_consumption_total"]


async def test_a_refused_chunk_is_read_one_address_at_a_time(
    coordinator: SimpleNamespace, client: FakeClient
) -> None:
    client.refuse.update({40100, 40101})
    client.words[40102] = 7

    report = await scan(coordinator).async_run()

    assert report["refused"] == {40100: {"count": 2, "reason": "exception code 2"}}
    read = {
        start + offset: word
        for start, words in report["words"].items()
        for offset, word in enumerate(words)
    }
    assert read[40102] == 7
    assert 40100 not in read


async def test_a_busy_device_is_asked_again(
    coordinator: SimpleNamespace, client: FakeClient
) -> None:
    address = CANDIDATES["battery_control_mode"].address
    client.busy_once.add(address)
    client.words[address] = 1

    candidates = (await scan(coordinator).async_run())["candidates"]

    assert candidates["battery_control_mode"]["value"] == 1.0


async def test_the_serial_number_never_reaches_the_report(
    coordinator: SimpleNamespace, client: FakeClient
) -> None:
    client.serve_serial(SERIAL, register_map.SERIAL_NUMBER.address)

    report = await scan(coordinator).async_run()

    for start, words in report["words"].items():
        for offset, word in enumerate(words):
            if (
                register_map.SERIAL_NUMBER.address
                <= start + offset
                < register_map.SERIAL_NUMBER.end
            ):
                assert word is None
    assert SERIAL[4:8] not in str(report)


async def test_a_lost_connection_ends_the_scan_and_says_so(
    hass: HomeAssistant, coordinator: SimpleNamespace, client: FakeClient
) -> None:
    client.gone_at = 42050
    runner = scan(coordinator)

    report = await runner.async_run()

    assert report["outcome"] == RegisterScanState.ABORTED
    assert report["abort_reason"].startswith("connection lost at ")
    assert runner.state is RegisterScanState.ABORTED
    notifications = persistent_notification._async_get_or_create_notifications(hass)
    (notification,) = notifications.values()
    assert notification["title"] == "Register scan finished"


async def test_a_finished_scan_says_where_the_report_is(
    hass: HomeAssistant, coordinator: SimpleNamespace
) -> None:
    runner = scan(coordinator)

    report = await runner.async_run()

    assert runner.last_report is report
    notifications = persistent_notification._async_get_or_create_notifications(hass)
    (notification,) = notifications.values()
    assert "Download diagnostics" in notification["message"]
    assert (
        f"of {len(CANDIDATES)} candidate registers answered"
        in (notification["message"])
    )


@pytest.mark.parametrize(
    ("change", "key"),
    (
        ({"connected": False}, "register_scan_no_data"),
        ({"data": None}, "register_scan_no_data"),
        ({"control_test": SimpleNamespace(running=True)}, "register_scan_control_test"),
    ),
)
async def test_a_scan_does_not_start_when_it_cannot_run(
    coordinator: SimpleNamespace, client: FakeClient, change: dict[str, Any], key: str
) -> None:
    for name, value in change.items():
        setattr(coordinator, name, value)

    with pytest.raises(ServiceValidationError) as raised:
        scan(coordinator).async_start()

    assert raised.value.translation_key == key
    assert client.reads == []


async def test_only_one_scan_runs_at_a_time(coordinator: SimpleNamespace) -> None:
    runner = scan(coordinator)
    runner.async_start()
    await asyncio.sleep(0)

    with pytest.raises(ServiceValidationError) as raised:
        runner.async_start()

    assert raised.value.translation_key == "register_scan_running"
    await runner.async_cancel()
    assert runner.state is RegisterScanState.ABORTED
