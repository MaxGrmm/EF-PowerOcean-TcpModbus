"""Unit tests for the control manager without Home Assistant."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.ef_powerocean_tcpmodbus import const, models
from custom_components.ef_powerocean_tcpmodbus import control as control_module
from custom_components.ef_powerocean_tcpmodbus import heartbeat as heartbeat_module
from custom_components.ef_powerocean_tcpmodbus.modbus import ModbusRejected
from custom_components.ef_powerocean_tcpmodbus.switch import (
    GRID_FEED_SWITCH,
    EcoFlowSwitch,
)

HEARTBEAT_START = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def control():
    blocks = const.register_blocks_for(const.DEFAULT_INVERTER_MODEL)
    manager = control_module.ControlManager(
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
        enabled=True,
        scan_interval_s=const.DEFAULT_SCAN_INTERVAL_S,
        on_update=Mock(),
        on_refresh=AsyncMock(),
        write_setting=AsyncMock(),
    )
    # A fresh manager assumes the device may still be following an earlier run; the
    # tests start from a settled state and say so where they mean otherwise.
    manager._control_stale = False
    return manager


def allow_writes(control, monkeypatch: pytest.MonkeyPatch, *, rejected=False):
    """Put the manager in control of a device that answers, and return the writes."""
    monkeypatch.setattr(control_module.dt, "now", lambda: HEARTBEAT_START)
    control._modbus_client.connected = True
    control._modbus_client.async_write = AsyncMock(
        side_effect=ModbusRejected("rejected") if rejected else None
    )
    # The device has answered a heartbeat, so it is following us.
    control._heartbeat._supported = True
    control._heartbeat._last_success = HEARTBEAT_START
    return control._modbus_client.async_write


Feature = models.ControlFeature
Status = models.ControlStatus
Feed = models.GridFeedMode


def commands(write) -> list[tuple[int, list[int]]]:
    """Return the (address, words) of each command, without the heartbeats between."""
    return [
        call.args
        for call in write.await_args_list
        if call.args[0] != const.HEARTBEAT_REGISTER
    ]


def advance(control, monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Move the clock as polling would, keeping control authority alive."""
    now = HEARTBEAT_START + timedelta(seconds=seconds)
    monkeypatch.setattr(control_module.dt, "now", lambda: now)
    control._heartbeat._last_success = now


def beat(control, now: datetime, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(control_module.dt, "now", lambda: now)
    # asyncio.run() builds a fresh loop per call, and a lock binds to the first one.
    control._heartbeat._lock = asyncio.Lock()
    return asyncio.run(control._heartbeat.async_ensure_fresh())


def test_a_recent_beat_is_reused_and_a_stale_one_is_rewritten(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command needs the window held open, not another frame to answer."""
    write = allow_writes(control, monkeypatch)

    fresh = HEARTBEAT_START + timedelta(seconds=const.HEARTBEAT_REUSE_S - 1)
    assert beat(control, fresh, monkeypatch) is True
    assert write.await_count == 0

    stale = HEARTBEAT_START + timedelta(seconds=const.HEARTBEAT_REUSE_S + 1)
    assert beat(control, stale, monkeypatch) is True
    assert write.await_args.args == (const.HEARTBEAT_REGISTER, [const.HEARTBEAT_VALUE])


def test_a_busy_inverter_is_retried_but_an_invalid_request_latches_off(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Device busy faults the moment; an illegal address faults the request."""
    allow_writes(control, monkeypatch)
    control._heartbeat._retry_delays = (0.0, 0.0)
    control._heartbeat._supported = None
    control._heartbeat._last_success = None

    control._modbus_client.async_write = AsyncMock(
        side_effect=(ModbusRejected("busy", exception_code=0x06), None)
    )
    assert beat(control, HEARTBEAT_START, monkeypatch) is True
    assert control.heartbeat_supported is True

    control._heartbeat._last_success = None
    write = AsyncMock(side_effect=ModbusRejected("illegal", exception_code=0x02))
    control._modbus_client.async_write = write

    assert beat(control, HEARTBEAT_START, monkeypatch) is False
    assert control.heartbeat_supported is False
    assert write.await_count == 1

    control._heartbeat._supported = None
    control._modbus_client.async_write = AsyncMock(
        side_effect=control_module.HomeAssistantError("connection reset")
    )

    assert beat(control, HEARTBEAT_START, monkeypatch) is False
    assert control.heartbeat_supported is None


@pytest.mark.parametrize(
    ("scan_interval", "expected"),
    (
        (2, (0.0, *(2.0,) * 7)),
        (5, (0.0, 5.0, 5.0, 5.0)),
        (30, (0.0, 15.0)),
    ),
)
def test_a_retry_waits_a_whole_poll_cycle(scan_interval, expected) -> None:
    """Retrying inside the cycle that caused the busy answer only asks too early."""
    assert heartbeat_module.retry_delays(scan_interval) == expected


def test_a_reconnect_keeps_control_unless_the_window_lapsed(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inverter counts its own deadline and knows nothing of our socket, so a
    blip must not report control lost nor rewrite a command it never dropped."""
    allow_writes(control, monkeypatch)
    control._heartbeat._supported = False

    control.mark_stale()

    # A new socket can mean a different device state, so the refusal goes with it.
    assert control.heartbeat_supported is None
    assert control.in_control is True
    assert control._control_stale is False

    lapsed = HEARTBEAT_START + timedelta(seconds=const.HEARTBEAT_WINDOW_S + 1)
    monkeypatch.setattr(control_module.dt, "now", lambda: lapsed)
    control.mark_stale()

    assert control.in_control is False
    assert control._control_stale is True


def test_a_failed_beat_is_retried_before_the_deadline_not_after_it(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retries come out of the interval: spending them and then waiting a whole one
    on top leaves the deadline to pass, and the inverter reverts to the app."""
    allow_writes(control, monkeypatch)
    heartbeat = control._heartbeat

    assert heartbeat._delay_before_next_write() == const.HEARTBEAT_INTERVAL_S

    for age, expected in ((12, 8), (const.HEARTBEAT_RETRY_TOTAL_S, 5), (40, 5)):
        now = HEARTBEAT_START + timedelta(seconds=age)
        monkeypatch.setattr(control_module.dt, "now", lambda now=now: now)
        assert heartbeat._delay_before_next_write() == expected

    heartbeat._supported = False
    assert heartbeat._delay_before_next_write() == const.HEARTBEAT_UNSUPPORTED_RETRY_S


def test_a_mode_writes_its_setpoint_then_its_method_word(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A setpoint without the method nibble is stored and ignored by the device."""
    write = allow_writes(control, monkeypatch)
    control._battery_saver = True
    control._data = {"battery_soc": 50.0}
    asyncio.run(control.async_set_feature_power(Feature.CHARGE_BATTERY, 2500))

    asyncio.run(control.async_select_feature(Feature.CHARGE_BATTERY))

    assert control.power == 2500.0
    # The device parses multi-register writes high word first, unlike its reads.
    assert commands(write) == [
        (const.REGISTERS_BY_KEY["battery_power_setpoint"].address, [0x0000, 0x09C4]),
        (const.CONTROL_COMMAND_REGISTER, [0x0000, 0x0038]),
    ]


def test_discharge_sends_the_magnitude_as_a_negative_setpoint(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One number in the UI; the sign belongs to the mode."""
    write = allow_writes(control, monkeypatch)
    control._data = {"battery_soc": 50.0}
    asyncio.run(control.async_select_feature(Feature.DISCHARGE_BATTERY))

    asyncio.run(control.async_set_feature_power(Feature.DISCHARGE_BATTERY, 1500))

    assert control.power == 1500.0
    # The method is already selected, so the new setpoint is the last thing sent.
    assert commands(write)[-1] == (
        const.REGISTERS_BY_KEY["battery_power_setpoint"].address,
        [0xFFFF, 0xFA24],
    )


def test_holding_the_battery_commands_one_watt_not_zero(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero reads as 'no limit' on this device, so it would resume self-consumption."""
    write = allow_writes(control, monkeypatch)
    control._data = {"battery_soc": 50.0}

    asyncio.run(control.async_select_feature(Feature.HOLD_BATTERY))

    assert control.power == 1.0
    setpoint = const.REGISTERS_BY_KEY["battery_power_setpoint"].address
    assert commands(write)[0] == (setpoint, [0x0000, 0x0001])


def test_a_full_battery_is_held_against_the_house_but_not_against_the_sun(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full battery cannot charge, so a limit set against a surplus only curtails."""
    write = allow_writes(control, monkeypatch)
    setpoint = const.REGISTERS_BY_KEY["battery_power_setpoint"].address
    control._data = {"battery_soc": 100.0, "solar_power": 100.0, "house_power": 900.0}

    asyncio.run(control.async_select_feature(Feature.HOLD_BATTERY))

    assert commands(write) == [
        (setpoint, [0x0000, 0x0001]),
        (const.CONTROL_COMMAND_REGISTER, [0x0000, 0x0030]),
    ]

    write.reset_mock()
    advance(control, monkeypatch, const.MIN_CONTROL_DWELL_S + 1)
    asyncio.run(
        control.async_apply(
            {"battery_soc": 100.0, "solar_power": 5000.0, "house_power": 1000.0}
        )
    )

    assert control.selected_feature is Feature.HOLD_BATTERY
    assert control.power == 0.0
    assert control.status is Status.HOLD_NOT_NEEDED
    assert commands(write) == [
        (setpoint, [0x0000, 0x0000]),
        (const.CONTROL_COMMAND_REGISTER, [0x0000, 0x0000]),
    ]

    # Anything short of a clear surplus counts as a draw, so the hold is back before
    # the house can reach the battery.
    advance(control, monkeypatch, 2 * const.MIN_CONTROL_DWELL_S + 2)
    asyncio.run(
        control.async_apply(
            {"battery_soc": 100.0, "solar_power": 1000.0, "house_power": 1000.0}
        )
    )

    assert control.power == 1.0


def test_power_is_clamped_to_the_lowest_ceiling_that_applies(control) -> None:
    """The app's battery limit is ignored by Modbus control, so it must not cap us."""
    control._data = {"battery_charge_power_limit": 500.0}
    asyncio.run(control.async_set_feature_power(Feature.CHARGE_BATTERY, 9999))
    assert control.feature_power(Feature.CHARGE_BATTERY) == 5000.0

    # A limit the firmware publishes is real, and so is the inverter's AC rating.
    control._limits[const.CONF_MAX_BATTERY_CHARGED_POWER] = 25_000
    control._data = {"feed_in_power_max": 9000.0, "inverter_rated_power": 11000.0}
    assert control.feature_power_max(Feature.EXPORT_TO_GRID) == 9000.0
    assert control.feature_power_max(Feature.CHARGE_BATTERY) == 11000.0

    # A battery count of zero bounds nothing, so the slider keeps a sane maximum.
    control._limits[const.CONF_MAX_BATTERY_CHARGED_POWER] = 0
    control._data = {}
    assert control.feature_power_max(Feature.CHARGE_BATTERY) == float(
        const.CONTROL_POWER_FALLBACK_MAX
    )


def test_a_mode_cannot_be_selected_without_modbus_control(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = allow_writes(control, monkeypatch)
    control._enabled = False
    control._data = {"battery_soc": 50.0}

    with pytest.raises(control_module.HomeAssistantError):
        asyncio.run(control.async_select_feature(Feature.CHARGE_BATTERY))

    assert control.selected_feature is Feature.AUTOMATIC
    write.assert_not_awaited()


def test_selecting_automatic_returns_control_to_the_device(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default method alone leaves the device holding the setpoint we wrote."""
    write = allow_writes(control, monkeypatch)
    control._data = {"battery_soc": 50.0}
    asyncio.run(control.async_select_feature(Feature.CHARGE_BATTERY))

    asyncio.run(control.async_select_feature(Feature.AUTOMATIC))

    assert control.status is Status.AUTOMATIC
    assert commands(write)[-2:] == [
        (const.REGISTERS_BY_KEY["battery_power_setpoint"].address, [0x0000, 0x0000]),
        (const.CONTROL_COMMAND_REGISTER, [0x0000, 0x0000]),
    ]


def test_the_control_word_refuses_off_grid_and_shutdown_bits(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = allow_writes(control, monkeypatch)
    # Nothing composable through the public API sets them, so bend the bit to reach
    # the guard: battery saver now lands on BIT0, which takes the system off-grid.
    monkeypatch.setattr(control_module, "CONTROL_COMMAND_BATTERY_SAVER_BIT", 0)

    with pytest.raises(control_module.HomeAssistantError):
        asyncio.run(control.async_set_battery_saver(True))

    write.assert_not_awaited()


@pytest.mark.parametrize(
    ("setter", "limit", "soc", "solar", "house", "expected", "feature", "power"),
    (
        (
            "async_set_battery_reserve_soc",
            20,
            20.0,
            100.0,
            900.0,
            Status.RESERVE_REACHED,
            Feature.HOLD_BATTERY,
            1.0,
        ),
        (
            "async_set_battery_reserve_soc",
            20,
            20.0,
            2000.0,
            500.0,
            Status.RESERVE_REACHED,
            Feature.CHARGE_BATTERY,
            1500.0,
        ),
        (
            "async_set_charge_limit_soc",
            80,
            80.0,
            2000.0,
            500.0,
            Status.CHARGE_LIMIT_REACHED,
            Feature.HOLD_BATTERY,
            1.0,
        ),
        (
            "async_set_charge_limit_soc",
            80,
            80.0,
            100.0,
            900.0,
            Status.CHARGE_LIMIT_REACHED,
            Feature.DISCHARGE_BATTERY,
            800.0,
        ),
    ),
)
def test_a_guard_runs_self_consumption_minus_the_direction_it_protects(
    control,
    monkeypatch: pytest.MonkeyPatch,
    setter: str,
    limit: int,
    soc: float,
    solar: float,
    house: float,
    expected,
    feature,
    power: float,
) -> None:
    """A latched guard commands the balance the inverter would have struck anyway,
    clamped to the allowed side, even when that balance points the allowed way: only
    one that lasts is handed back, never the poll the guard latches on."""
    allow_writes(control, monkeypatch)
    asyncio.run(getattr(control, setter)(limit))

    asyncio.run(
        control.async_apply(
            {"battery_soc": soc, "solar_power": solar, "house_power": house}
        )
    )

    assert control.status is expected
    assert control._commanded_feature is feature
    assert control.power == power


def test_a_guard_holds_a_chosen_mode_rather_than_turning_it_around(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tracking the house is only right for the inverter's own mode. Asked to charge,
    the most a guard may do is refuse; draining the battery was never requested."""
    allow_writes(control, monkeypatch)
    control._data = {"battery_soc": 80.0}
    asyncio.run(control.async_set_charge_limit_soc(80))
    asyncio.run(control.async_select_feature(Feature.CHARGE_BATTERY))

    asyncio.run(
        control.async_apply(
            {
                "battery_soc": 80.0,
                "solar_power": 100.0,
                "house_power": 900.0,
                "grid_power": 800.0,
                "battery_power": 0.0,
            }
        )
    )

    assert control.status is Status.CHARGE_LIMIT_REACHED
    assert control._commanded_feature is Feature.HOLD_BATTERY
    assert control.power == 1.0


def test_a_charge_guard_blocks_a_trickle_too_small_to_read_as_a_surplus(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The battery charges from any surplus at all, so a guard that only acted on a
    clear one let the battery charge past its limit while the weather stayed dull."""
    allow_writes(control, monkeypatch)
    asyncio.run(control.async_set_charge_limit_soc(80))

    asyncio.run(
        control.async_apply(
            {
                "battery_soc": 80.0,
                "solar_power": 550.0,
                "house_power": 400.0,
                "grid_power": 0.0,
                "battery_power": 150.0,
            }
        )
    )

    assert control.status is Status.CHARGE_LIMIT_REACHED
    assert control.power == 1.0


def test_a_charge_guard_leaves_the_battery_free_to_serve_the_house(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A charge limit says nothing about discharging, and the hold that blocks
    charging blocks that too, so the draw has to be commanded back explicitly."""
    allow_writes(control, monkeypatch)
    asyncio.run(control.async_set_charge_limit_soc(80))

    asyncio.run(
        control.async_apply(
            {
                "battery_soc": 80.0,
                "solar_power": 250.0,
                "house_power": 400.0,
                "grid_power": 150.0,
                "battery_power": 0.0,
            }
        )
    )

    assert control.status is Status.CHARGE_LIMIT_REACHED
    assert control.selected_feature is Feature.AUTOMATIC
    # The whole draw, so the grid is not left carrying the remainder.
    assert control.power == 150.0


def test_a_held_charge_guard_ignores_the_surplus_its_own_hold_removed(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pinning the battery takes away the sink, the array is curtailed to what the
    house and the export ceiling can absorb, and the surplus the guard was watching
    disappears. Reading that as permission would hand the battery straight back."""
    allow_writes(control, monkeypatch)
    asyncio.run(control.async_set_charge_limit_soc(80))
    charging = {
        "battery_soc": 80.0,
        "solar_power": 2000.0,
        "house_power": 400.0,
        "grid_power": -1600.0,
        "battery_power": 1600.0,
    }
    asyncio.run(control.async_apply(charging))
    assert control.status is Status.CHARGE_LIMIT_REACHED

    # The array is now curtailed, so solar and house sit on top of each other.
    curtailed = {
        "battery_soc": 80.0,
        "solar_power": 400.0,
        "house_power": 400.0,
        "grid_power": 0.0,
        "battery_power": 0.0,
    }
    for poll in range(1, 11):
        advance(control, monkeypatch, poll * (const.MIN_CONTROL_DWELL_S + 1))
        asyncio.run(control.async_apply(curtailed))
        assert control.status is Status.CHARGE_LIMIT_REACHED


def test_a_held_charge_guard_covers_the_house_without_changing_method(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The draw the hold created is met from the battery rather than by handing the
    inverter back, which would let it charge again and start the whole cycle over."""
    write = allow_writes(control, monkeypatch)
    asyncio.run(control.async_set_charge_limit_soc(80))
    asyncio.run(
        control.async_apply(
            {
                "battery_soc": 80.0,
                "solar_power": 2000.0,
                "house_power": 400.0,
                "grid_power": -1600.0,
                "battery_power": 1600.0,
            }
        )
    )
    assert control.status is Status.CHARGE_LIMIT_REACHED
    write.reset_mock()

    importing = {
        "battery_soc": 80.0,
        "solar_power": 100.0,
        "house_power": 400.0,
        "grid_power": 300.0,
        "battery_power": 0.0,
    }
    for poll in range(1, 6):
        advance(control, monkeypatch, poll * (const.MIN_CONTROL_DWELL_S + 1))
        asyncio.run(control.async_apply(importing))
        assert control.status is Status.CHARGE_LIMIT_REACHED

    assert control.power == 300.0
    # One retune and nothing else: the method is already the battery limits, and the
    # inverter acts on the setpoint register without being told again.
    assert commands(write) == [
        (const.REGISTERS_BY_KEY["battery_power_setpoint"].address, [0xFFFF, 0xFED4]),
    ]


def test_a_guard_the_inverter_does_not_follow_reports_the_miss(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #107: the guard asked for the house's whole draw while the battery gave
    only about half of it, and the status only said "charge limit reached". The miss
    must show, and changing the power every poll must not reset it."""
    write = allow_writes(control, monkeypatch)
    asyncio.run(control.async_set_charge_limit_soc(60))
    asyncio.run(
        control.async_apply(
            {
                "battery_soc": 67.0,
                "solar_power": 2000.0,
                "house_power": 400.0,
                "grid_power": -1600.0,
                "battery_power": 0.0,
            }
        )
    )
    write.reset_mock()

    for poll, house in enumerate((3000.0, 3600.0, 4200.0, 4800.0), start=1):
        advance(control, monkeypatch, poll * const.DEFAULT_SCAN_INTERVAL_S)
        asyncio.run(
            control.async_apply(
                {
                    "battery_soc": 67.0,
                    "solar_power": 1400.0,
                    "house_power": house,
                    "grid_power": house - 1400.0 - 1200.0,
                    "battery_power": -1200.0,
                }
            )
        )

    # One setpoint write per poll.
    assert len(commands(write)) == 4
    assert control.status is Status.RAMPING
    assert control.active_guard is Status.CHARGE_LIMIT_REACHED


def test_a_new_power_for_a_chosen_mode_is_judged_afresh(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Polls spent missing the old setpoint say nothing about the new one."""
    allow_writes(control, monkeypatch)
    asyncio.run(control.async_set_feature_power(Feature.CHARGE_BATTERY, 1000.0))
    asyncio.run(control.async_select_feature(Feature.CHARGE_BATTERY))
    frame = {"battery_soc": 50.0, "battery_power": 0.0}

    for poll in range(1, const.CONTROL_STATUS_DAMPING_POLLS):
        advance(control, monkeypatch, poll * const.DEFAULT_SCAN_INTERVAL_S)
        asyncio.run(control.async_apply(frame))
    asyncio.run(control.async_set_feature_power(Feature.CHARGE_BATTERY, 3000.0))

    assert control.status is Status.ACTIVE


def test_a_held_guard_keeps_holding_when_the_frame_loses_the_battery_and_grid(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial read must not read as permission to charge."""
    allow_writes(control, monkeypatch)
    asyncio.run(control.async_set_charge_limit_soc(80))
    asyncio.run(
        control.async_apply(
            {
                "battery_soc": 80.0,
                "solar_power": 2000.0,
                "house_power": 400.0,
                "grid_power": -1600.0,
                "battery_power": 1600.0,
            }
        )
    )
    assert control.status is Status.CHARGE_LIMIT_REACHED

    for poll in range(1, 6):
        advance(control, monkeypatch, poll * (const.MIN_CONTROL_DWELL_S + 1))
        asyncio.run(control.async_apply({"battery_soc": 80.0}))
        assert control.status is Status.CHARGE_LIMIT_REACHED


def test_setting_a_limit_keeps_the_latch_when_the_state_of_charge_is_unknown(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting a limit clears the latch so the new one starts its hysteresis afresh,
    but only where the frame can work it out again."""
    allow_writes(control, monkeypatch)
    asyncio.run(control.async_set_charge_limit_soc(80))
    asyncio.run(
        control.async_apply(
            {
                "battery_soc": 80.0,
                "solar_power": 2000.0,
                "house_power": 400.0,
                "grid_power": -1600.0,
                "battery_power": 1600.0,
            }
        )
    )
    assert control.status is Status.CHARGE_LIMIT_REACHED

    # A read that failed part way through leaves the state of charge behind.
    advance(control, monkeypatch, const.MIN_CONTROL_DWELL_S + 1)
    asyncio.run(control.async_apply({"solar_power": 2000.0, "house_power": 400.0}))

    asyncio.run(control.async_set_charge_limit_soc(70))

    assert control._charge_guard is True
    assert control.status is Status.CHARGE_LIMIT_REACHED


def test_writing_the_same_limit_again_changes_nothing(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An automation may write the limit it already has every few minutes. Starting
    the hysteresis afresh each time would release a guard inside its band."""
    write = allow_writes(control, monkeypatch)
    asyncio.run(control.async_set_charge_limit_soc(60))
    asyncio.run(control.async_apply({"battery_soc": 60.0}))
    asyncio.run(control.async_apply({"battery_soc": 57.0}))
    write.reset_mock()

    asyncio.run(control.async_set_charge_limit_soc(60))

    assert control._charge_guard is True
    write.assert_not_awaited()


@pytest.mark.parametrize(
    ("reserve", "phase"),
    (
        # Still at or above the state of charge, so the guard stays on.
        (52, control_module.HandbackPhase.HANDED_BACK),
        # Now below it, so the guard is off and there is nothing to hand back.
        (40, control_module.HandbackPhase.TRACKING),
    ),
)
def test_a_new_limit_keeps_the_hand_back_only_while_its_guard_stays_on(
    control, monkeypatch: pytest.MonkeyPatch, reserve: int, phase
) -> None:
    """A new value that leaves the guard as it was changes nothing about who should
    run the house, so the inverter is not taken back for a minute."""
    allow_writes(control, monkeypatch)
    asyncio.run(control.async_set_battery_reserve_soc(51))
    surplus = {
        "battery_soc": 50.0,
        "solar_power": 3000.0,
        "house_power": 800.0,
        "grid_power": 0.0,
        "battery_power": 2200.0,
    }
    for poll in range(int(const.GUARD_HANDBACK_S // 5) + 2):
        advance(control, monkeypatch, poll * 5)
        asyncio.run(control.async_apply(surplus))
    assert control._handback.phase is control_module.HandbackPhase.HANDED_BACK

    asyncio.run(control.async_set_battery_reserve_soc(reserve))

    assert control._handback.phase is phase


def test_a_command_sets_its_limit_before_its_mode_reaches_the_inverter(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Choosing charge and then lowering the Charge Limit would charge for one write
    past the new limit. A command sets both before anything is sent."""
    write = allow_writes(control, monkeypatch)
    control._data = {"battery_soc": 85.0}

    asyncio.run(
        control.async_set_command(
            Feature.CHARGE_BATTERY, power=3000.0, charge_limit_soc=80.0, expire_in_s=900
        )
    )

    assert control.selected_feature is Feature.CHARGE_BATTERY
    assert control.feature_power(Feature.CHARGE_BATTERY) == 3000.0
    assert control.charge_limit_soc == 80.0
    assert control.expires_at == HEARTBEAT_START + timedelta(seconds=900)
    # Only the hold goes out: setpoint 1 W, then the method word.
    setpoint = const.REGISTERS_BY_KEY["battery_power_setpoint"].address
    assert len(commands(write)) == 2
    assert commands(write)[0] == (setpoint, [0, 1])


def test_repeating_a_command_only_moves_its_expiry(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planner sends its command again every few minutes, which must cost no write."""
    write = allow_writes(control, monkeypatch)
    control._data = {"battery_soc": 50.0}
    command = control.async_set_command
    asyncio.run(command(Feature.DISCHARGE_BATTERY, power=2000.0, expire_in_s=900))
    write.reset_mock()

    advance(control, monkeypatch, 300)
    asyncio.run(command(Feature.DISCHARGE_BATTERY, power=2000.0, expire_in_s=900))

    assert commands(write) == []
    assert control.expires_at == HEARTBEAT_START + timedelta(seconds=1200)


def test_a_command_not_renewed_returns_to_automatic_and_keeps_its_limit(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the mode is undone. The limit stays where it was set."""
    allow_writes(control, monkeypatch)
    frame = {"battery_soc": 50.0}
    control._data = frame
    asyncio.run(
        control.async_set_command(
            Feature.CHARGE_BATTERY, power=2000.0, charge_limit_soc=80.0, expire_in_s=600
        )
    )

    advance(control, monkeypatch, 599)
    asyncio.run(control.async_apply(frame))
    assert control.selected_feature is Feature.CHARGE_BATTERY

    advance(control, monkeypatch, 600)
    asyncio.run(control.async_apply(frame))
    assert control.selected_feature is Feature.AUTOMATIC
    assert control._commanded_feature is Feature.AUTOMATIC
    assert control.charge_limit_soc == 80.0
    assert control.last_expiry == (
        Feature.CHARGE_BATTERY,
        HEARTBEAT_START + timedelta(seconds=600),
    )


def test_choosing_a_mode_by_hand_cancels_a_commands_expiry(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_writes(control, monkeypatch)
    control._data = {"battery_soc": 50.0}
    asyncio.run(
        control.async_set_command(Feature.CHARGE_BATTERY, power=2000.0, expire_in_s=60)
    )
    asyncio.run(control.async_select_feature(Feature.DISCHARGE_BATTERY))

    advance(control, monkeypatch, 61)
    asyncio.run(control.async_apply({"battery_soc": 50.0}))

    assert control.selected_feature is Feature.DISCHARGE_BATTERY


def test_a_command_needs_modbus_control_unless_it_only_sets_the_limit(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused command must not leave half of itself behind."""
    write = allow_writes(control, monkeypatch)
    control._enabled = False

    with pytest.raises(control_module.HomeAssistantError):
        asyncio.run(
            control.async_set_command(
                Feature.CHARGE_BATTERY, power=2000.0, charge_limit_soc=80.0
            )
        )
    assert control.charge_limit_soc == 100.0

    asyncio.run(control.async_set_command(Feature.AUTOMATIC, charge_limit_soc=80.0))
    assert control.charge_limit_soc == 80.0
    write.assert_not_awaited()


def test_a_guard_releases_only_past_the_hysteresis_band(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whole-percent state of charge would chase a narrow band."""
    allow_writes(control, monkeypatch)
    asyncio.run(control.async_set_battery_reserve_soc(20))
    drawing = {"solar_power": 100.0, "house_power": 900.0}

    asyncio.run(control.async_apply({"battery_soc": 20.0, **drawing}))
    assert control.status is Status.RESERVE_REACHED

    advance(control, monkeypatch, const.MIN_CONTROL_DWELL_S + 1)
    asyncio.run(control.async_apply({"battery_soc": 24.0, **drawing}))
    assert control.status is Status.RESERVE_REACHED

    advance(control, monkeypatch, 2 * const.MIN_CONTROL_DWELL_S + 2)
    asyncio.run(control.async_apply({"battery_soc": 25.0, **drawing}))
    assert control.status is Status.AUTOMATIC


def test_a_guard_engages_at_once_but_releases_only_after_the_dwell(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Protecting the battery late is worse than a command too soon after the last."""
    write = allow_writes(control, monkeypatch)
    asyncio.run(control.async_set_battery_reserve_soc(20))
    drawing = {"solar_power": 100.0, "house_power": 900.0}
    # A command went out a moment ago, so the dwell timer is running.
    control._last_control_write_time = HEARTBEAT_START

    asyncio.run(control.async_apply({"battery_soc": 20.0, **drawing}))
    assert control.status is Status.RESERVE_REACHED
    assert write.await_count > 0
    write.reset_mock()

    asyncio.run(control.async_apply({"battery_soc": 40.0, **drawing}))
    assert control.status is Status.RESERVE_REACHED
    write.assert_not_awaited()

    advance(control, monkeypatch, const.MIN_CONTROL_DWELL_S + 1)
    asyncio.run(control.async_apply({"battery_soc": 40.0, **drawing}))
    assert control.status is Status.AUTOMATIC


def test_an_untouched_install_never_takes_control(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ceiling of 100 is off, so a full battery must not make us seize control."""
    write = allow_writes(control, monkeypatch)

    asyncio.run(
        control.async_apply(
            {"battery_soc": 100.0, "solar_power": 3000.0, "house_power": 500.0}
        )
    )

    assert control.status is Status.AUTOMATIC
    write.assert_not_awaited()


def test_polls_write_nothing_while_modbus_control_is_off(control) -> None:
    control._enabled = False

    for _ in range(3):
        asyncio.run(control.async_poll({"battery_soc": 50.0}))

    control._modbus_client.async_write.assert_not_awaited()


def test_switching_modbus_control_off_hands_back_and_on_takes_control_again(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Letting the heartbeat lapse is the hand-back, so nothing is written for it."""
    write = allow_writes(control, monkeypatch)
    control._data = {"battery_soc": 50.0}
    asyncio.run(control.async_select_feature(Feature.CHARGE_BATTERY))
    write.reset_mock()

    asyncio.run(control.async_set_enabled(False))
    asyncio.run(control.async_poll({"battery_soc": 50.0}))

    write.assert_not_awaited()
    assert control.status is Status.HANDING_BACK
    assert control.hands_back_at == HEARTBEAT_START + timedelta(
        seconds=const.HEARTBEAT_WINDOW_S
    )
    assert control.selected_feature is Feature.AUTOMATIC

    lapsed = HEARTBEAT_START + timedelta(seconds=const.HEARTBEAT_WINDOW_S + 1)
    monkeypatch.setattr(control_module.dt, "now", lambda: lapsed)
    assert control.status is Status.NO_MODBUS_CONTROL
    assert control.hands_back_at is None

    control._heartbeat.start = Mock()
    asyncio.run(control.async_set_enabled(True))
    # The restarted heartbeat has landed.
    advance(control, monkeypatch, const.HEARTBEAT_WINDOW_S + 2)
    asyncio.run(control.async_poll({"battery_soc": 50.0}))

    control._heartbeat.start.assert_called_once()
    # The inverter may still follow the old command, so the default is re-sent.
    assert commands(write) == [(const.CONTROL_COMMAND_REGISTER, [0x0000, 0x0000])]
    assert control.status is Status.AUTOMATIC


def test_a_reserve_of_zero_disables_the_guard(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An external optimiser needs a way to take the floor off entirely."""
    write = allow_writes(control, monkeypatch)
    asyncio.run(control.async_set_battery_reserve_soc(0))

    asyncio.run(
        control.async_apply(
            {"battery_soc": 0.0, "solar_power": 100.0, "house_power": 900.0}
        )
    )

    assert control.status is Status.AUTOMATIC
    write.assert_not_awaited()


def test_a_command_is_re_sent_only_once_authority_has_lapsed(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """40532 reads 0 on a PowerOcean Plus, so polling must never second-guess us,
    but a lapse hands the device back to the app and loses the setpoint too."""
    write = allow_writes(control, monkeypatch)
    control._data = {"battery_soc": 50.0}
    asyncio.run(control.async_select_feature(Feature.CHARGE_BATTERY))
    write.reset_mock()

    asyncio.run(control.async_apply())
    write.assert_not_awaited()

    control._control_stale = True
    asyncio.run(control.async_apply())

    assert [address for address, _ in commands(write)] == [
        const.REGISTERS_BY_KEY["battery_power_setpoint"].address,
        const.CONTROL_COMMAND_REGISTER,
    ]
    assert control._control_stale is False


def test_a_failed_write_does_not_break_the_poll_and_is_retried(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recording a command the device never got would strand it in the old mode."""
    allow_writes(control, monkeypatch)
    control._feature = Feature.CHARGE_BATTERY
    control._modbus_client.async_write = AsyncMock(
        side_effect=control_module.HomeAssistantError("connection reset")
    )

    asyncio.run(control.async_poll({"battery_soc": 50.0}))

    assert control.selected_feature is Feature.CHARGE_BATTERY
    assert control.method is models.ControlMode.DEFAULT

    write = allow_writes(control, monkeypatch)
    asyncio.run(control.async_apply({"battery_soc": 50.0}))

    assert write.await_count > 0
    assert control.method is models.ControlMode.BATTERY_LIMITS


def test_a_brief_excursion_leaves_the_control_status_alone(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A load step pulls the measurement wide until the battery absorbs it."""
    allow_writes(control, monkeypatch)
    control._data = {"battery_soc": 50.0}
    asyncio.run(control.async_select_feature(Feature.EXPORT_TO_GRID))
    settled = {"battery_soc": 50.0, "grid_power": -3000.0}
    excursion = {"battery_soc": 50.0, "grid_power": -2100.0}

    asyncio.run(control.async_apply(settled))
    assert control.status is Status.ACTIVE

    for _ in range(const.CONTROL_STATUS_DAMPING_POLLS - 1):
        asyncio.run(control.async_apply(excursion))
        assert control.status is Status.ACTIVE

    asyncio.run(control.async_apply(excursion))
    assert control.status is Status.RAMPING

    asyncio.run(control.async_apply(settled))
    assert control.status is Status.ACTIVE


def test_battery_saver_applies_without_taking_control_from_the_app(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bit 3 applies like the LED does; only a control method locks the app out."""
    write = allow_writes(control, monkeypatch)
    control._enabled = False

    asyncio.run(control.async_set_battery_saver(True))

    assert control.battery_saver_commanded is True
    assert commands(write) == [(const.CONTROL_COMMAND_REGISTER, [0x0000, 0x0008])]


def test_a_refused_command_rolls_back_the_commanded_state(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The UI must not report a write the device threw out."""
    allow_writes(control, monkeypatch, rejected=True)

    with pytest.raises(control_module.HomeAssistantError):
        asyncio.run(control.async_set_battery_saver(True))

    assert control.battery_saver_commanded is False


def test_the_status_reports_no_control_until_the_device_answers(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modbus control being configured is not the same as the device following us."""
    allow_writes(control, monkeypatch)
    control._heartbeat._supported = None
    control._heartbeat._last_success = None

    assert control.status is Status.NO_MODBUS_CONTROL


def test_the_parameters_survive_a_restart_but_the_selected_mode_does_not(
    control,
) -> None:
    """Re-arming a mode the user cannot see would command the inverter silently."""
    control._feature_power[Feature.CHARGE_BATTERY] = 4000.0
    control._charge_limit_soc = 80.0
    # A restart does not clear battery saver on the device, so it must not lie.
    control._battery_saver = True
    control._grid_feed_restore = {"mode": 1, "power": 9000}
    control._grid_feed_stopped = True
    control._feature = Feature.CHARGE_BATTERY

    stored = control.dump_state()
    control._enabled = False
    control._feature_power[Feature.CHARGE_BATTERY] = 0.0
    control._battery_saver = False
    control._grid_feed_restore = None
    control._grid_feed_stopped = False
    control._feature = Feature.AUTOMATIC
    control.load_state(stored)

    assert control.enabled is True
    assert control.feature_power(Feature.CHARGE_BATTERY) == 4000.0
    assert control.charge_limit_soc == 80.0
    assert control.battery_saver_commanded is True
    assert control.grid_feed_restore == {"mode": 1, "power": 9000}
    assert control._grid_feed_stopped is True
    assert control.selected_feature is Feature.AUTOMATIC


# Unlimited mode ignores the cap, so it allows export even with a zero cap.
@pytest.mark.parametrize(
    ("mode", "power"),
    [(Feed.LIMITED, 9000), (Feed.UNLIMITED, 0), (Feed.UNLIMITED, 9000)],
)
def test_the_grid_feed_switch_stops_the_export_and_restores_it_exactly(
    control, monkeypatch: pytest.MonkeyPatch, mode: models.GridFeedMode, power: int
) -> None:
    allow_writes(control, monkeypatch)
    switch = EcoFlowSwitch.__new__(EcoFlowSwitch)
    switch._definition = GRID_FEED_SWITCH
    switch.coordinator = SimpleNamespace(control=control, data={})
    writes: list[tuple[str, int]] = []
    control._write_setting = AsyncMock(
        side_effect=lambda register, value, **_: writes.append((register.key, value))
    )

    def poll(mode: models.GridFeedMode, power: int) -> None:
        switch.coordinator.data = {
            "grid_feed_mode": mode,
            "feed_in_power_max_setting": power,
        }
        control._data = switch.coordinator.data
        control._track_grid_feed_restore(switch.coordinator.data)

    poll(mode, power)
    assert switch.is_on
    asyncio.run(control.async_set_grid_feed(False))
    poll(Feed.LIMITED, 0)
    assert not switch.is_on
    asyncio.run(control.async_set_grid_feed(True))

    # Each order keeps the export from ever being briefly uncapped.
    assert writes == [
        ("grid_feed_mode", 0),
        ("feed_in_power_max_setting", 0),
        ("feed_in_power_max_setting", power),
        ("grid_feed_mode", mode.register_value),
    ]


def test_the_restore_keeps_the_configured_cap_not_the_effective_one(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A derated effective cap written back would lower the configured one for good."""
    allow_writes(control, monkeypatch)
    control._track_grid_feed_restore(
        {
            "grid_feed_mode": Feed.LIMITED,
            "feed_in_power_max_setting": 10000.0,
            "feed_in_power_max_effective": 4000.0,
            "feed_in_power_max": 4000.0,
        }
    )

    assert control.grid_feed_restore == {"mode": 0, "power": 10000}


def test_the_percentage_mode_is_never_adopted_or_switched(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restoring it as a watt limit or as unlimited would change the installer's cap."""
    allow_writes(control, monkeypatch)
    frame = {
        "grid_feed_mode": Feed.LIMITED_PERCENT,
        "feed_in_power_max_setting": 10000.0,
        "feed_in_power_max_percent": 70.0,
    }
    control._data = frame
    control._track_grid_feed_restore(frame)

    assert control.grid_feed_restore is None
    assert control.grid_feed_allowed(frame) is True

    # Even with a restore remembered from before the mode changed.
    control._grid_feed_restore = {"mode": 1, "power": 9000}
    assert control.grid_feed_switchable is False
    with pytest.raises(control_module.HomeAssistantError):
        asyncio.run(control.async_set_grid_feed(False))
    control._write_setting.assert_not_awaited()


def test_an_export_limit_raised_on_the_device_is_adopted(control) -> None:
    """An installer lifting the limit should not need the entry to be set up again."""
    control._track_grid_feed_restore(
        {"grid_feed_mode": Feed.LIMITED, "feed_in_power_max_setting": 9000.0}
    )
    control._track_grid_feed_restore(
        {"grid_feed_mode": Feed.UNLIMITED, "feed_in_power_max_setting": 15000.0}
    )

    assert control.grid_feed_restore == {"mode": 1, "power": 15000}


@pytest.mark.parametrize(
    ("restore", "enabled"),
    [
        ({"grid_feed_mode": Feed.LIMITED, "feed_in_power_max": 0.0}, True),
        ({"feed_in_power_max": 9000.0}, True),
        ({"grid_feed_mode": Feed.UNLIMITED, "feed_in_power_max": 0.0}, False),
    ],
    ids=["limited-to-zero", "mode-not-read", "modbus-control-off"],
)
def test_the_grid_feed_switch_refuses_without_a_restore_or_modbus_control(
    control, monkeypatch: pytest.MonkeyPatch, restore: dict, enabled: bool
) -> None:
    allow_writes(control, monkeypatch)
    control._enabled = enabled
    control._track_grid_feed_restore(restore)

    with pytest.raises(control_module.HomeAssistantError):
        asyncio.run(control.async_set_grid_feed(False))
    control._write_setting.assert_not_awaited()


def test_discharge_and_export_are_capped_by_the_inverter_capacity(control) -> None:
    """40546 is the most the inverter turns from DC into AC."""
    control._limits[const.CONF_MAX_BATTERY_DISCHARGED_POWER] = 25_000
    control._limits[const.CONF_MAX_BATTERY_CHARGED_POWER] = 25_000
    control._data = {
        const.INVERTER_CAPACITY_KEY: 8000.0,
        const.RECTIFIER_CAPACITY_KEY: 4000.0,
        "feed_in_power_max": 12000.0,
        "inverter_rated_power": 10000.0,
    }

    assert control.feature_power_max(Feature.DISCHARGE_BATTERY) == 8000.0
    assert control.feature_power_max(Feature.EXPORT_TO_GRID) == 8000.0
    # PV on the DC side charges alongside the rectifier, so its capacity is no cap.
    assert control.feature_power_max(Feature.CHARGE_BATTERY) == 10000.0


def test_an_unreported_inverter_capacity_bounds_nothing(control) -> None:
    control._limits[const.CONF_MAX_BATTERY_DISCHARGED_POWER] = 25_000
    control._data = {const.INVERTER_CAPACITY_KEY: 0.0, "inverter_rated_power": 10000.0}

    assert control.feature_power_max(Feature.DISCHARGE_BATTERY) == 10000.0


def test_the_effective_cap_alone_is_never_remembered_for_a_restore(
    control,
) -> None:
    """Without the configured cap there is nothing safe to write back."""
    control._track_grid_feed_restore(
        {"grid_feed_mode": Feed.LIMITED, "feed_in_power_max": 4000.0}
    )

    assert control.grid_feed_restore is None
