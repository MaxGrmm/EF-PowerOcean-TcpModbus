"""The control test run from Home Assistant, against a fake inverter.

The fake answers each poll from what was last written to it, and follows only the
methods it is told to, so a run shows what the test concludes from a firmware that
does or does not act on a command.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry
from homeassistant.util import dt
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import const
from custom_components.ef_powerocean_tcpmodbus import control as control_module
from custom_components.ef_powerocean_tcpmodbus.control_test import (
    ControlTest,
    ControlTestState,
    Timing,
)
from custom_components.ef_powerocean_tcpmodbus.modbus import ModbusRejected
from custom_components.ef_powerocean_tcpmodbus.models import (
    ControlFeature,
    ControlMode,
    ControlStatus,
    GridFeedMode,
)

SETPOINTS = {
    key: const.REGISTERS_BY_KEY[key]
    for key in (
        "battery_power_setpoint",
        "system_power_setpoint",
        "inverter_power_setpoint",
    )
}
METHOD_SHIFT = const.CONTROL_COMMAND_METHOD_SHIFT
HOUSE_W = 500.0
FAST = Timing(
    settle_s=0.5, return_s=0.05, status_bit_s=0.3, handback_s=0.3, frame_timeout_s=1
)


def int32(words: list[int]) -> int:
    value = (words[0] << 16) | words[1]
    return value - (1 << 32) if value & 0x8000_0000 else value


@dataclass
class FakeInverter:
    """Follows the methods in *follows* while its heartbeat is current."""

    follows: set[ControlMode] = field(default_factory=lambda: set(ControlMode))
    soc: float = 50.0
    beating: bool = False
    # Polls after the heartbeat stops until it reports the app back in charge.
    handback_polls: int = 0
    method: ControlMode = ControlMode.DEFAULT
    setpoints: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(SETPOINTS, 0)
    )
    writes: list[tuple[int, list[int]]] = field(default_factory=list)
    refuse: dict[int, int] = field(default_factory=dict)

    @property
    def in_control(self) -> bool:
        return self.beating or self.handback_polls > 0

    async def async_write(self, address: int, words: list[int], *, what: str) -> None:
        if code := self.refuse.get(address):
            raise ModbusRejected("refused", exception_code=code)
        self.writes.append((address, list(words)))
        if address == const.CONTROL_COMMAND_REGISTER:
            word = int32(words)
            value = (word >> METHOD_SHIFT) & const.CONTROL_COMMAND_METHOD_MASK
            self.method = next(m for m in ControlMode if m.command_value == value)
            return
        for key, register in SETPOINTS.items():
            if register.address == address:
                self.setpoints[key] = int32(words)

    def frame(self) -> dict[str, Any]:
        if not self.beating and self.handback_polls:
            self.handback_polls -= 1
        battery, grid, inverter = 0.0, HOUSE_W, 0.0
        acting = self.in_control and self.method in self.follows
        if acting and self.method is ControlMode.BATTERY_LIMITS:
            battery = float(self.setpoints["battery_power_setpoint"])
            grid = HOUSE_W + battery
        elif acting and self.method is ControlMode.SYSTEM_FEED:
            grid = float(self.setpoints["system_power_setpoint"])
            battery = grid - HOUSE_W
        elif acting and self.method is ControlMode.INVERTER_FEED:
            inverter = float(self.setpoints["inverter_power_setpoint"])
            battery, grid = inverter, HOUSE_W + inverter
        return {
            "battery_power": battery,
            "grid_power": grid,
            "solar_power": 0.0,
            "house_power": HOUSE_W,
            "inverter_output_power": inverter,
            "battery_soc": self.soc,
            "min_soc_limit": 10,
            "grid_feed_mode": GridFeedMode.UNLIMITED,
            "system_modes_hex": "0x00000004",
            "battery_saver_mode_ena": False,
            "active_control_mode": str(
                self.method if self.in_control else ControlMode.DEFAULT
            ),
            "device_modbus_control": self.in_control,
            **self.setpoints,
        }


class FakeHeartbeat:
    def __init__(self, inverter: FakeInverter, *, supported: bool = True) -> None:
        self._inverter = inverter
        self._supported = supported
        self.supported: bool | None = None
        self.last_success = None
        self.running = False

    @property
    def in_control(self) -> bool:
        return self.running

    async def async_ensure_fresh(self) -> bool:
        self.supported = self._supported
        if not self._supported:
            return False
        self._inverter.beating = True
        self.last_success = dt.now()
        return True

    def start(self) -> None:
        self.running = True

    async def async_stop(self) -> None:
        if self.running or self._inverter.beating:
            self._inverter.beating = False
            self._inverter.handback_polls = 2
        self.running = False


class FakeCoordinator:
    """Polls the fake inverter on a short timer, as the coordinator would."""

    def __init__(self, hass: HomeAssistant, inverter: FakeInverter) -> None:
        self.hass = hass
        self.inverter = inverter
        self.config_entry = MockConfigEntry(domain=const.DOMAIN)
        self.config_entry.add_to_hass(hass)
        self.modbus_client = inverter
        self.registers_by_key = dict(SETPOINTS)
        self.inverter_model = const.DEFAULT_INVERTER_MODEL
        self.scan_interval = 5
        self.identity = SimpleNamespace(firmware_version="V1.2.3", protocol_version=1)
        self.is_modbus_disabled = False
        self.connected = True
        self.control = SimpleNamespace(
            enabled=False,
            holds_control=False,
            test_running=False,
            async_begin_test=AsyncMock(),
            end_test=Mock(),
        )
        self.data: dict[str, Any] | None = inverter.frame()
        self._listeners: list[Any] = []
        self.required: frozenset[str] | None = None
        self._task: asyncio.Task | None = None

    def async_add_listener(self, listener: Any) -> Any:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def async_require(self, keys: frozenset[str]) -> Any:
        self.required = keys
        return self.release_required

    def release_required(self) -> None:
        self.required = None

    def async_update_listeners(self) -> None:
        for listener in list(self._listeners):
            listener()

    def start_polling(self) -> None:
        async def poll() -> None:
            while True:
                await asyncio.sleep(0.01)
                self.data = self.inverter.frame()
                self.async_update_listeners()

        self._task = asyncio.get_running_loop().create_task(poll())

    async def stop_polling(self) -> None:
        if self._task:
            self._task.cancel()


@pytest.fixture
def inverter() -> FakeInverter:
    return FakeInverter()


@pytest.fixture
async def coordinator(hass: HomeAssistant, inverter: FakeInverter, tmp_path: Path):
    hass.config.config_dir = str(tmp_path)
    fake = FakeCoordinator(hass, inverter)
    fake.start_polling()
    yield fake
    await fake.stop_polling()


def runner(coordinator: FakeCoordinator, *, supported: bool = True) -> ControlTest:
    return ControlTest(
        coordinator,
        timing=FAST,
        heartbeat_factory=lambda: FakeHeartbeat(
            coordinator.inverter, supported=supported
        ),
    )


def verdicts(report: dict[str, Any]) -> dict[str, str]:
    return {item["name"]: item["verdict"] for item in report["features"]}


async def test_a_firmware_that_follows_every_method_passes_every_test(
    hass: HomeAssistant, coordinator: FakeCoordinator
) -> None:
    test = runner(coordinator)

    report = await test.async_run(1500)

    assert report["outcome"] == "done"
    assert set(verdicts(report).values()) == {"followed"}
    assert all(item["method_reported"] for item in report["features"])
    assert report["heartbeat"] == "accepted"
    assert report["manual_mode_bit_s"] is not None
    assert report["handback_s"] is not None
    assert report["firmware_version"] == "V1.2.3"
    assert test.state is ControlTestState.DONE
    # Asked for only while it ran.
    assert coordinator.required is None


async def test_the_registers_it_reads_stay_polled_while_it_runs(
    hass: HomeAssistant, coordinator: FakeCoordinator, inverter: FakeInverter
) -> None:
    test = runner(coordinator)
    test.async_start(1500)
    while not inverter.beating:
        await asyncio.sleep(0.01)

    assert coordinator.required is not None
    assert {"inverter_output_power", "battery_power_setpoint", "system_modes"} <= (
        coordinator.required
    )
    await test.async_cancel()
    assert coordinator.required is None


async def test_a_method_the_firmware_ignores_is_reported_not_followed(
    hass: HomeAssistant, coordinator: FakeCoordinator, inverter: FakeInverter
) -> None:
    inverter.follows = {ControlMode.BATTERY_LIMITS, ControlMode.SYSTEM_FEED}

    report = await runner(coordinator).async_run(1500)

    result = verdicts(report)
    assert result["battery charge"] == "followed"
    assert result["inverter draw"] == "not_followed"
    assert result["inverter feed"] == "not_followed"


async def test_the_inverter_is_left_on_the_default_method(
    hass: HomeAssistant, coordinator: FakeCoordinator, inverter: FakeInverter
) -> None:
    await runner(coordinator).async_run(1500)

    assert inverter.method is ControlMode.DEFAULT
    assert set(inverter.setpoints.values()) == {0}
    assert not inverter.beating


async def test_the_report_is_saved_and_kept_for_the_diagnostics(
    hass: HomeAssistant, coordinator: FakeCoordinator, tmp_path: Path
) -> None:
    test = runner(coordinator)

    report = await test.async_run(1500)

    saved = list((tmp_path / const.DOMAIN).glob("control_test_*_V1.2.3_*.json"))
    assert len(saved) == 1
    assert json.loads(saved[0].read_text()) == json.loads(json.dumps(report))
    assert test.last_report == report
    assert test.attributes["report_file"] == str(saved[0])


async def test_a_full_battery_skips_the_charge_tests(
    hass: HomeAssistant, coordinator: FakeCoordinator, inverter: FakeInverter
) -> None:
    inverter.soc = 98

    report = await runner(coordinator).async_run(1500)

    result = verdicts(report)
    assert result["battery charge"] == "skipped"
    assert result["battery discharge"] == "followed"


async def test_a_refused_heartbeat_tests_nothing(
    hass: HomeAssistant, coordinator: FakeCoordinator
) -> None:
    report = await runner(coordinator, supported=False).async_run(1500)

    assert report["outcome"] == "done"
    assert set(verdicts(report).values()) == {"not_tested"}
    assert "refused" in report["heartbeat"]


async def test_a_refused_setpoint_is_reported_and_the_run_goes_on(
    hass: HomeAssistant, coordinator: FakeCoordinator, inverter: FakeInverter
) -> None:
    inverter.refuse[SETPOINTS["inverter_power_setpoint"].address] = 2
    inverter.follows = set(ControlMode)

    report = await runner(coordinator).async_run(1500)

    result = verdicts(report)
    assert result["inverter draw"] == "write_refused"
    assert result["battery charge"] == "followed"


async def test_cancelling_hands_control_back(
    hass: HomeAssistant, coordinator: FakeCoordinator, inverter: FakeInverter
) -> None:
    test = runner(coordinator)
    test.async_start(1500)
    while not inverter.beating:
        await asyncio.sleep(0.01)

    await test.async_cancel()

    assert test.state is ControlTestState.ABORTED
    assert test.last_report["abort_reason"] == "cancelled"
    assert inverter.method is ControlMode.DEFAULT
    assert not inverter.beating
    coordinator.control.end_test.assert_called_once()


async def test_a_lost_connection_aborts_and_hands_back(
    hass: HomeAssistant, coordinator: FakeCoordinator, inverter: FakeInverter
) -> None:
    test = runner(coordinator)
    test.async_start(1500)
    while not inverter.beating:
        await asyncio.sleep(0.01)
    await coordinator.stop_polling()

    await asyncio.wait_for(test._task, 5)

    assert test.state is ControlTestState.ABORTED
    assert "connection lost" in test.last_report["abort_reason"]
    assert not inverter.beating


async def test_it_takes_over_when_the_integration_holds_control(
    hass: HomeAssistant, coordinator: FakeCoordinator, inverter: FakeInverter
) -> None:
    """No switching Modbus Control off and waiting: the confirmation is enough."""
    inverter.beating = True
    inverter.method = ControlMode.BATTERY_LIMITS
    coordinator.data = inverter.frame()
    coordinator.control.enabled = True
    coordinator.control.holds_control = True

    report = await runner(coordinator).async_run(1500)

    assert report["outcome"] == "done"
    assert report["parameters"]["took_over_modbus_control"] is True
    assert set(verdicts(report).values()) == {"followed"}
    coordinator.control.async_begin_test.assert_awaited_once()
    coordinator.control.end_test.assert_called_once()


async def test_it_does_not_start_while_another_controller_is_active(
    hass: HomeAssistant, coordinator: FakeCoordinator, inverter: FakeInverter
) -> None:
    inverter.beating = True
    coordinator.data = inverter.frame()
    inverter.beating = False

    with pytest.raises(ServiceValidationError) as raised:
        runner(coordinator).async_start(1500)

    assert raised.value.translation_key == "control_test_other_controller"


async def test_only_one_run_at_a_time(
    hass: HomeAssistant, coordinator: FakeCoordinator
) -> None:
    test = runner(coordinator)
    test.async_start(1500)

    with pytest.raises(ServiceValidationError) as raised:
        test.async_start(1500)

    assert raised.value.translation_key == "control_test_running"
    await test.async_cancel()


async def test_a_notification_says_the_report_is_ready(
    hass: HomeAssistant, coordinator: FakeCoordinator
) -> None:
    await runner(coordinator).async_run(1500)

    notifications = persistent_notification._async_get_or_create_notifications(hass)
    (notification,) = notifications.values()
    assert notification["title"] == "Control test finished"
    assert "6 followed" in notification["message"]
    assert "control_test_" in notification["message"]
    # Without a device registered, the device page is named but not linked.
    assert "device page" in notification["message"]


async def test_the_notification_links_to_the_device_page(
    hass: HomeAssistant, coordinator: FakeCoordinator
) -> None:
    device = device_registry.async_get(hass).async_get_or_create(
        config_entry_id=coordinator.config_entry.entry_id,
        identifiers={(const.DOMAIN, "HJ31")},
    )

    await runner(coordinator).async_run(1500)

    notifications = persistent_notification._async_get_or_create_notifications(hass)
    (notification,) = notifications.values()
    assert f"(/config/devices/device/{device.id})" in notification["message"]


async def test_events_bracket_the_run(
    hass: HomeAssistant, coordinator: FakeCoordinator
) -> None:
    events: list[str] = []
    for name in (const.EVENT_CONTROL_TEST_STARTED, const.EVENT_CONTROL_TEST_FINISHED):
        hass.bus.async_listen(name, lambda event: events.append(event.event_type))

    await runner(coordinator).async_run(1500)
    await hass.async_block_till_done()

    assert events == [
        const.EVENT_CONTROL_TEST_STARTED,
        const.EVENT_CONTROL_TEST_FINISHED,
    ]


# ── The control manager stands aside while a test runs ────────────────────────


def control_manager() -> control_module.ControlManager:
    blocks = const.register_blocks_for(const.DEFAULT_INVERTER_MODEL)
    return control_module.ControlManager(
        SimpleNamespace(connected=True, async_write=AsyncMock()),
        registers_by_key={
            register.key: register for block in blocks for register in block.registers
        },
        limits={
            const.CONF_MAX_GRID_POWER: 15_000,
            const.CONF_MAX_SOLAR_POWER: 12_000,
            const.CONF_MAX_BATTERY_CHARGED_POWER: 5_000,
            const.CONF_MAX_BATTERY_DISCHARGED_POWER: 6_600,
        },
        inverter_model=const.DEFAULT_INVERTER_MODEL,
        enabled=False,
        scan_interval_s=const.DEFAULT_SCAN_INTERVAL_S,
        on_update=Mock(),
        on_refresh=AsyncMock(),
        write_setting=AsyncMock(),
        on_command_expired=Mock(),
    )


async def test_modbus_control_cannot_be_switched_on_during_a_test() -> None:
    manager = control_manager()
    await manager.async_begin_test()

    with pytest.raises(HomeAssistantError, match="control test is running"):
        await manager.async_set_enabled(True)

    assert not manager.enabled


async def test_no_command_or_battery_saver_reaches_the_inverter_during_a_test() -> None:
    manager = control_manager()
    await manager.async_begin_test()

    with pytest.raises(HomeAssistantError, match="control test is running"):
        await manager.async_set_command(ControlFeature.CHARGE_BATTERY, power=1000)
    with pytest.raises(HomeAssistantError, match="control test is running"):
        await manager.async_set_battery_saver(True)
    await manager.async_poll({"battery_soc": 50})

    manager._modbus_client.async_write.assert_not_awaited()


async def test_control_status_shows_the_test() -> None:
    manager = control_manager()
    await manager.async_begin_test()

    assert manager.status is ControlStatus.CONTROL_TEST


async def test_a_mode_selected_before_the_test_resumes_after_it() -> None:
    manager = control_manager()
    await manager.async_set_enabled(True)
    await manager.async_set_command(ControlFeature.CHARGE_BATTERY, power=1000)
    manager._heartbeat.async_stop = AsyncMock(wraps=manager._heartbeat.async_stop)
    await manager.async_begin_test()
    manager._heartbeat.async_stop.assert_awaited_once()
    manager._heartbeat.start = Mock()

    manager.end_test()

    assert manager.selected_feature is ControlFeature.CHARGE_BATTERY
    assert manager._control_stale
    manager._heartbeat.start.assert_called_once()
    await manager.async_stop()


async def test_a_test_that_found_modbus_control_off_leaves_it_off() -> None:
    manager = control_manager()
    await manager.async_begin_test()
    manager._heartbeat.start = Mock()

    manager.end_test()

    assert not manager.enabled
    manager._heartbeat.start.assert_not_called()


async def test_commands_are_accepted_again_after_the_test() -> None:
    manager = control_manager()
    await manager.async_begin_test()
    manager.end_test()

    await manager.async_set_enabled(True)

    assert manager.enabled
    await manager.async_stop()


# ── What the user sees ────────────────────────────────────────────────────────


def test_the_sensor_offers_every_state_the_test_can_be_in() -> None:
    assert const.CONTROL_TEST_SENSOR.options == tuple(ControlTestState)

    strings = json.loads(
        (Path(const.__file__).parent / "strings.json").read_text(encoding="utf-8")
    )
    states = strings["entity"]["sensor"]["control_test"]["state"]
    assert set(states) == set(ControlTestState)


def describe(hass: HomeAssistant, event_type: str, data: dict[str, Any]) -> str:
    from custom_components.ef_powerocean_tcpmodbus import logbook

    described: dict[str, Any] = {}

    def register(_domain: str, name: str, describer: Any) -> None:
        described[name] = describer

    logbook.async_describe_events(hass, register)
    return described[event_type](SimpleNamespace(data=data))["message"]


def test_the_logbook_counts_the_verdicts(hass: HomeAssistant) -> None:
    message = describe(
        hass,
        const.EVENT_CONTROL_TEST_FINISHED,
        {
            "outcome": "done",
            "verdicts": {
                "battery charge": "followed",
                "battery discharge": "followed",
                "inverter feed": "not_followed",
            },
        },
    )

    assert message == "finished: 2 followed, 1 not followed"


def test_the_logbook_says_why_a_run_stopped(hass: HomeAssistant) -> None:
    message = describe(
        hass,
        const.EVENT_CONTROL_TEST_FINISHED,
        {"outcome": "aborted", "abort_reason": "cancelled"},
    )

    assert message == "aborted: cancelled"
