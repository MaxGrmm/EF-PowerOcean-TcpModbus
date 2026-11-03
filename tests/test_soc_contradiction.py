"""A system battery level of 0 that the batteries contradict (#200)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import const, diagnostics
from tests.fake_inverter import HOST, FakeInverter
from tests.test_startup import _fill_registers

SYSTEM_SOC = const.REGISTERS_BY_KEY["battery_soc"].address
BATTERY_COUNT = const.REGISTERS_BY_KEY["battery_count"].address
PACK_SOCS = [const.REGISTERS_BY_KEY[key].address for key in const.BATTERY_SOC_KEYS[:2]]


def _socs(inverter: FakeInverter, system: int, *packs: int) -> None:
    inverter.registers[SYSTEM_SOC] = system
    inverter.registers[BATTERY_COUNT] = len(packs)
    for address, pack in zip(PACK_SOCS, packs, strict=False):
        inverter.registers[address] = pack


def _word(raw: dict[int, list[int | None]], address: int) -> int | None:
    start = next(s for s, words in raw.items() if s <= address < s + len(words))
    return raw[start][address - start]


@pytest.fixture
async def entry(
    hass: HomeAssistant, inverter: FakeInverter
) -> AsyncIterator[MockConfigEntry]:
    _fill_registers(inverter)
    _socs(inverter, 100, 100, 99)
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        data={const.CONF_HOST: HOST, const.CONF_PORT: inverter.port},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", const.DOMAIN, f"{entry.entry_id}_{key}"
    )
    assert entity_id is not None, key
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


async def _poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Poll as if a poll interval had passed since the last one.

    A poll less than a second after the last returns its data again. Dating that
    poll back, rather than moving the clock, leaves alone the timeouts of a request
    in flight, which pymodbus 3.11 trips over when the clock jumps.
    """
    coordinator = entry.runtime_data
    if coordinator._last_checked_time is not None:
        coordinator._last_checked_time -= timedelta(
            seconds=const.DEFAULT_SCAN_INTERVAL_S
        )
    await coordinator.async_refresh()
    await hass.async_block_till_done()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_contradicted_level_of_0_is_unavailable_rather_than_shown(
    hass: HomeAssistant,
    inverter: FakeInverter,
    entry: MockConfigEntry,
) -> None:
    assert _state(hass, entry, "battery_soc") == "100"

    _socs(inverter, 0, 100, 99)
    await _poll(hass, entry)

    assert _state(hass, entry, "battery_soc") == STATE_UNAVAILABLE

    _socs(inverter, 99, 100, 99)
    await _poll(hass, entry)

    assert _state(hass, entry, "battery_soc") == "99"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_readings_when_a_check_starts_failing_reach_diagnostics(
    hass: HomeAssistant,
    inverter: FakeInverter,
    entry: MockConfigEntry,
) -> None:
    _socs(inverter, 0, 100, 99)
    await _poll(hass, entry)
    await _poll(hass, entry)  # Still failing: not a new snapshot.

    report = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    (snapshot,) = [
        s for s in report["reading_check_snapshots"] if s["check"] == "battery_soc"
    ]
    assert snapshot["readings"]["battery_soc"] == 0
    assert snapshot["readings"]["soc_battery_1"] == 100
    assert _word(snapshot["raw_registers"], SYSTEM_SOC) == 0
    assert _word(snapshot["raw_registers_before"], SYSTEM_SOC) == 100


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_snapshots_survive_a_restart(
    hass: HomeAssistant,
    inverter: FakeInverter,
    entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    _socs(inverter, 0, 100, 99)
    await _poll(hass, entry)
    # Back to a real level, so the state saved on unload is not the episode's.
    _socs(inverter, 100, 100, 99)
    await _poll(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    key = f"{const.DOMAIN}.{entry.entry_id}.state"
    assert len(hass_storage[key]["data"]["reading_check_snapshots"]) == 1

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    snapshots = entry.runtime_data.reading_check_snapshots
    (snapshot,) = [s for s in snapshots if s["check"] == "battery_soc"]
    # Addresses come back as numbers, as diagnostics expects them.
    assert _word(snapshot["raw_registers"], SYSTEM_SOC) == 0
