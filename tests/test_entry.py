"""A set-up entry, seen from Home Assistant: diagnostics, logbook and controls."""

from __future__ import annotations

import json
import struct
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.const import STATE_UNAVAILABLE, EntityCategory
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component
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
async def test_raw_registers_in_the_diagnostics_never_hold_the_serial_number(
    hass: HomeAssistant, set_up: SetUp
) -> None:
    """Not even from a read that covers it."""
    entry = await set_up()
    serial = const.SERIAL_NUMBER
    words = list(range(1, serial.end - 40001 + 3))
    entry.runtime_data._raw_blocks = {40001: words}

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    raw = diagnostics["raw_registers"][40001]
    assert len(raw) == len(words)
    for offset, word in enumerate(raw):
        if serial.address <= 40001 + offset < serial.end:
            assert word is None
        else:
            assert word == words[offset]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_diagnostics_replay_on_the_fake_inverter(
    hass: HomeAssistant, set_up: SetUp, inverter: FakeInverter
) -> None:
    """A user's diagnostics download makes the same poll in a test."""
    entry = await set_up()
    coordinator = entry.runtime_data
    _serve_float(inverter, const.REGISTERS_BY_KEY["grid_power"].address, -1234.5)
    await coordinator.async_refresh()
    recorded = coordinator.raw_registers
    assert recorded

    # As downloaded: JSON, with the addresses as strings.
    diagnostics = json.loads(
        json.dumps(await async_get_config_entry_diagnostics(hass, entry), default=str)
    )
    inverter.registers.clear()
    inverter.serve_serial(SERIAL, const.SERIAL_NUMBER.address)
    inverter.load_diagnostics(diagnostics)
    await coordinator.async_refresh()

    assert coordinator.raw_registers == recorded
    replayed = await coordinator.async_get_raw_data()
    assert replayed is not None
    assert replayed["grid_power"] == -1234.5


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_active_faults_are_named_where_the_code_is_known(
    hass: HomeAssistant, set_up: SetUp, inverter: FakeInverter
) -> None:
    entry = await set_up()
    inverter.registers[const.REGISTERS_BY_KEY["fault_count"].address] = 2
    inverter.registers[const.REGISTERS_BY_KEY["fault_1"].address] = 521
    inverter.registers[const.REGISTERS_BY_KEY["fault_2"].address] = 9999
    coordinator = entry.runtime_data
    # A poll within a second of the last one returns the last frame.
    coordinator._last_checked_time = dt_util.now() - timedelta(seconds=5)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id(hass, entry, "sensor", "active_faults"))

    assert state is not None
    assert state.state == "521, 9999"
    assert state.attributes["faults"] == [
        {"code": 521, "name": "Energy storage communication error"},
        {"code": 9999, "name": None},
    ]


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


def _serve_float(inverter: FakeInverter, address: int, value: float) -> None:
    """Write a FLOAT register the way the device sends it, low word first."""
    low, high = struct.unpack("<HH", struct.pack("<f", value))
    inverter.registers[address] = low
    inverter.registers[address + 1] = high


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_device_totals_are_diagnostic_sensors_without_statistics(
    hass: HomeAssistant, set_up: SetUp, inverter: FakeInverter
) -> None:
    _serve_float(inverter, const.REGISTERS_BY_KEY["solar_total"].address, 4296.8)
    entry = await set_up()

    entity_id = _entity_id(hass, entry, "sensor", "solar_total_raw")
    state = hass.states.get(entity_id)
    entity = er.async_get(hass).async_get(entity_id)

    assert state is not None
    assert float(state.state) == pytest.approx(4296.8)
    assert "state_class" not in state.attributes
    assert entity is not None
    assert entity.entity_category is EntityCategory.DIAGNOSTIC


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_diagnostics_show_every_energy_counter_at_every_stage(
    hass: HomeAssistant, set_up: SetUp, inverter: FakeInverter
) -> None:
    _serve_float(inverter, const.REGISTERS_BY_KEY["solar_total"].address, 4296.8)
    entry = await set_up()

    energy = (await async_get_config_entry_diagnostics(hass, entry))["energy"]

    assert energy["published"]["solar_total"] == pytest.approx(4296.8)
    assert energy["device_totals"]["solar_total_raw"] == pytest.approx(4296.8)
    assert set(energy["device_totals"]) == {
        f"{s.key}_raw"
        for s in const.ENERGY_SENSOR_MAP
        if not s.is_calculated and not s.resets_daily
    }
    assert "solar_today_raw" in energy["device_daily"]
    assert energy["limits_w"][const.CONF_MAX_SOLAR_POWER] > 0
    assert energy["last_checked_time"] is not None
    assert "solar_total" in energy["energy_accepted_at"]
    # The fake inverter reads 0 everywhere else: shown, but not yet accepted.
    assert energy["published"]["grid_export_total"] == 0.0
    assert "grid_export_total" not in energy["energy_accepted_at"]


async def _held_export_issue(
    hass: HomeAssistant, set_up: SetUp, inverter: FakeInverter
) -> tuple[MockConfigEntry, str]:
    """Set up an entry whose export total was held at 1,548.73 kWh for a month."""
    assert await async_setup_component(hass, "repairs", {})
    _serve_float(inverter, const.REGISTERS_BY_KEY["grid_export_total"].address, 507.5)
    entry = await set_up()
    coordinator = entry.runtime_data
    coordinator._last_checked_data["grid_export_total"] = 1548.73
    coordinator._energy_processor.accepted_at["grid_export_total"] = (
        dt_util.now() - timedelta(days=30)
    )
    coordinator._last_checked_time = dt_util.now() - timedelta(minutes=1)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    issue_id = f"energy_totals_held_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(const.DOMAIN, issue_id)
    assert issue is not None
    assert issue.is_fixable
    return entry, issue_id


async def _poll_a_minute_later(entry: MockConfigEntry) -> None:
    coordinator = entry.runtime_data
    coordinator._last_checked_time = dt_util.now() - timedelta(minutes=1)
    await coordinator.async_refresh()


async def _choose(hass: HomeAssistant, issue_id: str, choice: str) -> None:
    manager = repairs_flow_manager(hass)
    assert manager is not None
    menu = await manager.async_init(const.DOMAIN, data={"issue_id": issue_id})
    assert menu["type"] == "menu"
    done = await manager.async_configure(menu["flow_id"], {"next_step_id": choice})
    await hass.async_block_till_done()
    assert done["type"] == "create_entry"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_held_totals_repair_runs_end_to_end_in_home_assistant(
    hass: HomeAssistant, set_up: SetUp, inverter: FakeInverter
) -> None:
    """The fix flow through Home Assistant's own Repairs, as the frontend runs it.

    Home Assistant loads repairs.py itself and passes the issue id as the first
    step's input, which must never count as the user choosing.
    """
    entry, issue_id = await _held_export_issue(hass, set_up, inverter)
    manager = repairs_flow_manager(hass)
    assert manager is not None
    menu = await manager.async_init(const.DOMAIN, data={"issue_id": issue_id})

    assert menu["type"] == "menu"
    assert entry.runtime_data.data["grid_export_total"] == 1548.73
    translations = await async_get_translations(hass, "en", "issues", [const.DOMAIN])
    prefix = f"component.{const.DOMAIN}.issues.energy_totals_held"
    description = translations[f"{prefix}.fix_flow.step.init.description"]
    rendered = description.format(**(menu.get("description_placeholders") or {}))
    assert "507.50 kWh on the inverter" in rendered
    assert "github.com" in rendered
    issue = ir.async_get(hass).async_get_issue(const.DOMAIN, issue_id)
    assert issue is not None
    assert translations[f"{prefix}.title"].format(
        **(issue.translation_placeholders or {})
    )
    manager.async_abort(menu["flow_id"])

    await _choose(hass, issue_id, "take_device")

    assert entry.runtime_data.data["grid_export_total"] == pytest.approx(507.5)
    assert ir.async_get(hass).async_get_issue(const.DOMAIN, issue_id) is None


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_held_total_keeps_its_value_in_home_assistant_after_the_repair(
    hass: HomeAssistant, set_up: SetUp, inverter: FakeInverter
) -> None:
    entry, issue_id = await _held_export_issue(hass, set_up, inverter)

    await _choose(hass, issue_id, "keep_shown")
    _serve_float(inverter, const.REGISTERS_BY_KEY["grid_export_total"].address, 507.51)
    await _poll_a_minute_later(entry)
    await hass.async_block_till_done()

    data = entry.runtime_data.data
    assert entry.data[const.CONF_ENERGY_OFFSETS] == {
        "grid_export_total": pytest.approx(1041.23)
    }
    assert data["grid_export_total"] == pytest.approx(1548.74, abs=0.001)
    assert data["grid_export_total_raw"] == pytest.approx(507.51)
    assert ir.async_get(hass).async_get_issue(const.DOMAIN, issue_id) is None


def _section_fields(form: Any) -> set[str]:
    fields = {str(key): value for key, value in form["data_schema"].schema.items()}
    return {str(key) for key in fields["energy_totals"].schema.schema}


async def _parameters_form(
    hass: HomeAssistant, entry: MockConfigEntry, inverter: FakeInverter
) -> Any:
    form = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        form["flow_id"], {const.CONF_HOST: HOST, const.CONF_PORT: inverter.port}
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_total_set_in_the_settings_continues_from_the_value_entered(
    hass: HomeAssistant, set_up: SetUp, inverter: FakeInverter
) -> None:
    _serve_float(inverter, const.REGISTERS_BY_KEY["solar_total"].address, 4296.8)
    entry = await set_up()
    today = entry.runtime_data.data["solar_today"]

    form = await _parameters_form(hass, entry, inverter)
    fields = {str(key): value for key, value in form["data_schema"].schema.items()}
    assert fields["energy_totals"].options["collapsed"] is True
    # Nothing to remove yet.
    assert "remove_corrections" not in _section_fields(form)
    assert "eu/en/" in (form.get("description_placeholders") or {})["portal_url"]
    done = await hass.config_entries.options.async_configure(
        form["flow_id"], {"energy_totals": {"solar_total": "4400,5"}}
    )
    await hass.async_block_till_done()

    data = entry.runtime_data.data
    assert done["type"] == "create_entry"
    assert data["solar_total"] == pytest.approx(4400.5)
    assert data["solar_total_raw"] == pytest.approx(4296.8)
    assert data["solar_today"] == pytest.approx(today)
    assert _state(hass, _entity_id(hass, entry, "sensor", "solar_total")) == "4400.5"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_only_the_totals_in_ecoflows_portal_can_be_corrected(
    hass: HomeAssistant, set_up: SetUp, inverter: FakeInverter
) -> None:
    for key in ("solar_total", "inverter_ac_in_total"):
        _serve_float(inverter, const.REGISTERS_BY_KEY[key].address, 1234.5)
    entry = await set_up()

    fields = _section_fields(await _parameters_form(hass, entry, inverter))

    assert "solar_total" in fields
    assert "inverter_ac_in_total" not in fields


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_total_not_in_the_portal_is_never_corrected(
    set_up: SetUp, inverter: FakeInverter
) -> None:
    _serve_float(
        inverter, const.REGISTERS_BY_KEY["inverter_ac_in_total"].address, 1234.5
    )

    entry = await set_up(**{const.CONF_ENERGY_OFFSETS: {"inverter_ac_in_total": 50.0}})

    assert entry.runtime_data.data["inverter_ac_in_total"] == pytest.approx(1234.5)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_total_not_in_the_portal_takes_the_inverters_value_after_a_day(
    hass: HomeAssistant, set_up: SetUp, inverter: FakeInverter
) -> None:
    """The user has nothing to check it against, so repairs does not ask."""
    assert await async_setup_component(hass, "repairs", {})
    _serve_float(
        inverter, const.REGISTERS_BY_KEY["inverter_ac_in_total"].address, 1234.5
    )
    entry = await set_up()
    coordinator = entry.runtime_data
    coordinator._last_checked_data["inverter_ac_in_total"] = 2000.0
    coordinator._energy_processor.accepted_at["inverter_ac_in_total"] = (
        dt_util.now() - timedelta(days=30)
    )

    # Held since it was last accepted, a month ago, so taken on the first poll
    # and published from the next.
    await _poll_a_minute_later(entry)
    await _poll_a_minute_later(entry)
    await hass.async_block_till_done()

    assert coordinator.data["inverter_ac_in_total"] == pytest.approx(1234.5)
    issue_id = f"energy_totals_held_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(const.DOMAIN, issue_id) is None


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_total_that_is_not_a_number_is_asked_for_again(
    hass: HomeAssistant, set_up: SetUp, inverter: FakeInverter
) -> None:
    _serve_float(inverter, const.REGISTERS_BY_KEY["solar_total"].address, 4296.8)
    entry = await set_up()

    form = await _parameters_form(hass, entry, inverter)
    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {const.CONF_SCAN_INTERVAL: 10, "energy_totals": {"solar_total": "4.400,5"}},
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_energy_total"}
    schema = result["data_schema"]
    assert schema is not None
    fields = {str(key): value for key, value in schema.schema.items()}
    assert fields["energy_totals"].options["collapsed"] is False
    assert const.CONF_ENERGY_OFFSETS not in entry.data
    assert const.CONF_SCAN_INTERVAL not in entry.data


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_lower_total_is_saved_only_once_confirmed(
    hass: HomeAssistant, set_up: SetUp, inverter: FakeInverter
) -> None:
    _serve_float(inverter, const.REGISTERS_BY_KEY["solar_total"].address, 4296.8)
    entry = await set_up()
    form = await _parameters_form(hass, entry, inverter)

    async def submit(total: str) -> Any:
        return await hass.config_entries.options.async_configure(
            form["flow_id"], {"energy_totals": {"solar_total": total}}
        )

    assert (await submit("4200"))["errors"] == {"base": "energy_total_lowered"}
    # A different lower value is a new warning, not a confirmation.
    assert (await submit("4100"))["errors"] == {"base": "energy_total_lowered"}
    assert const.CONF_ENERGY_OFFSETS not in entry.data

    assert (await submit("4100"))["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.runtime_data.data["solar_total"] == pytest.approx(4100.0)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_removing_the_corrections_returns_every_total_to_the_inverter(
    hass: HomeAssistant, set_up: SetUp, inverter: FakeInverter
) -> None:
    _serve_float(inverter, const.REGISTERS_BY_KEY["solar_total"].address, 4296.8)
    entry = await set_up(**{const.CONF_ENERGY_OFFSETS: {"solar_total": 103.2}})
    assert entry.runtime_data.data["solar_total"] == pytest.approx(4400.0)
    today = entry.runtime_data.data["solar_today"]

    form = await _parameters_form(hass, entry, inverter)
    assert "remove_corrections" in _section_fields(form)
    remove = {"energy_totals": {"solar_total": "4500", "remove_corrections": True}}
    warned = await hass.config_entries.options.async_configure(form["flow_id"], remove)
    # The inverter's value is lower than the total shown.
    assert warned["errors"] == {"base": "energy_total_lowered"}
    await hass.config_entries.options.async_configure(form["flow_id"], remove)
    await hass.async_block_till_done()

    data = entry.runtime_data.data
    assert entry.data[const.CONF_ENERGY_OFFSETS] == {}
    assert data["solar_total"] == pytest.approx(4296.8)
    assert data["solar_today"] == pytest.approx(today)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_total_left_as_shown_is_not_corrected(
    hass: HomeAssistant, set_up: SetUp, inverter: FakeInverter
) -> None:
    _serve_float(inverter, const.REGISTERS_BY_KEY["solar_total"].address, 4296.8)
    entry = await set_up()

    form = await _parameters_form(hass, entry, inverter)
    # The total counts on while the form is open.
    _serve_float(inverter, const.REGISTERS_BY_KEY["solar_total"].address, 4296.81)
    await _poll_a_minute_later(entry)
    await hass.config_entries.options.async_configure(
        form["flow_id"], {"energy_totals": {"solar_total": "4296.8"}}
    )
    await hass.async_block_till_done()

    assert const.CONF_ENERGY_OFFSETS not in entry.data
    assert entry.runtime_data.data["solar_total"] == pytest.approx(4296.81, abs=0.001)
