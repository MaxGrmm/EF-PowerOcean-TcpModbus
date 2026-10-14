"""A set-up entry, seen from Home Assistant: diagnostics, logbook and controls."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.ef_powerocean_tcpmodbus import const, logbook
from custom_components.ef_powerocean_tcpmodbus.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.ef_powerocean_tcpmodbus.models import RegisterType
from custom_components.ef_powerocean_tcpmodbus.services import (
    ATTR_EXPIRE_IN,
    SERVICE_SET_BATTERY_COMMAND,
)
from tests.fake_inverter import HOST, FakeInverter

SERIAL = "HJ31ZAS2TEST0001"


type SetUp = Callable[..., Awaitable[MockConfigEntry]]


@pytest.fixture
async def set_up(hass: HomeAssistant, inverter: FakeInverter) -> AsyncIterator[SetUp]:
    """Set up entries against the fake inverter, and unload them afterwards.

    An entry left loaded keeps its connection open, and the fake inverter then
    waits for it forever on the way out.
    """
    entries: list[MockConfigEntry] = []

    async def set_up_entry(**data: Any) -> MockConfigEntry:
        inverter.serve_serial(SERIAL, const.SERIAL_NUMBER.address)
        entry = MockConfigEntry(
            domain=const.DOMAIN,
            unique_id=SERIAL,
            data={const.CONF_HOST: HOST, const.CONF_PORT: inverter.port, **data},
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entries.append(entry)
        return entry

    yield set_up_entry
    for entry in entries:
        await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _entity_id(
    hass: HomeAssistant, entry: MockConfigEntry, platform: str, key: str
) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, const.DOMAIN, f"{entry.entry_id}_{key}"
    )
    assert entity_id is not None
    return entity_id


def _state(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_diagnostics_leave_out_the_address_and_most_of_the_serial(
    hass: HomeAssistant, set_up: SetUp
) -> None:
    entry = await set_up()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    text = str(diagnostics)
    assert HOST not in text
    assert SERIAL not in text
    assert diagnostics["serial_number"] == SERIAL[:4]
    assert diagnostics["modbus_connection"] in ("shared", "own")


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_diagnostics_show_whether_an_energy_total_is_held(
    hass: HomeAssistant, set_up: SetUp
) -> None:
    entry = await set_up()

    energy = (await async_get_config_entry_diagnostics(hass, entry))["energy"]

    assert set(energy["counters"]) == set(const.DEVICE_ENERGY_KEYS)
    assert "solar_today_raw" in energy["device_daily"]
    assert energy["last_checked_time"] is not None
    assert "solar_total" in energy["energy_accepted_at"]
    assert energy["energy_resync_pending"] == []


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_battery_controls_are_unavailable_while_modbus_control_is_off(
    hass: HomeAssistant, set_up: SetUp
) -> None:
    entry = await set_up()
    select_id = _entity_id(hass, entry, "select", const.BATTERY_MODE_SELECT.key)
    assert _state(hass, select_id) == STATE_UNAVAILABLE

    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": _entity_id(hass, entry, "switch", "modbus_control")},
        blocking=True,
    )
    # The heartbeat lands in the background; the next poll shows the inverter
    # following us.
    await hass.async_block_till_done()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert _state(hass, select_id) == "automatic"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_command_that_runs_out_returns_to_automatic_and_says_why(
    hass: HomeAssistant, set_up: SetUp, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await set_up(**{const.CONF_MODBUS_CONTROL: True})
    device = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)[0]
    select_id = _entity_id(hass, entry, "select", const.BATTERY_MODE_SELECT.key)
    expired = async_capture_events(hass, const.EVENT_COMMAND_EXPIRED)

    await hass.services.async_call(
        const.DOMAIN,
        SERVICE_SET_BATTERY_COMMAND,
        {"device_id": device.id, "mode": "hold_battery", ATTR_EXPIRE_IN: 60},
        blocking=True,
    )
    # Only the clock the expiry reads moves on. Freezing all time would also stop
    # the timers an own Modbus connection waits on.
    later = dt_util.now() + timedelta(seconds=61)
    monkeypatch.setattr(dt_util, "now", lambda: later)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert _state(hass, select_id) == "automatic"
    assert len(expired) == 1
    assert expired[0].data == {
        "device_id": device.id,
        "entity_id": select_id,
        "mode": "hold_battery",
    }
    entry_text = _describe(hass, expired[0])
    assert entry_text["entity_id"] == select_id
    assert entry_text["message"] == (
        "returned to automatic because the hold_battery action expired"
    )


def _describe(hass: HomeAssistant, event: Event) -> dict[str, Any]:
    """Return the logbook entry the integration writes for *event*."""
    describers: dict[str, Any] = {}

    @callback
    def collect(domain: str, event_type: str, describe: Any) -> None:
        assert domain == const.DOMAIN
        describers[event_type] = describe

    logbook.async_describe_events(hass, collect)
    return describers[str(event.event_type)](event)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_setting_the_register_cannot_hold_is_never_sent(
    set_up: SetUp, inverter: FakeInverter
) -> None:
    entry = await set_up()
    setting = next(
        number
        for number in const.WRITABLE_NUMBERS_MAP
        if number.data_type is RegisterType.UINT16
    )

    with pytest.raises(HomeAssistantError, match="does not fit"):
        await entry.runtime_data.async_write_modbus_register(setting, 0x10000)
    await entry.runtime_data.async_write_modbus_register(setting, 50)

    assert [
        words for address, words in inverter.writes if address == setting.register
    ] == [[50]]
