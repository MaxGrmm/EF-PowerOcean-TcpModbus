"""Unit tests for coordinator data validation without Home Assistant."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from ef_powerocean_tcpmodbus import const
from ef_powerocean_tcpmodbus import coordinator as coordinator_module
from ef_powerocean_tcpmodbus.energy_processor import EnergyProcessor


@pytest.fixture
def coordinator():
    instance = coordinator_module.EcoflowCoordinator.__new__(
        coordinator_module.EcoflowCoordinator
    )
    instance._last_checked_data = {}
    instance._last_checked_time = None
    instance._status = None
    instance._store = None
    instance._ena_calc_solar_power = False
    instance.inverter_model = const.DEFAULT_INVERTER_MODEL
    instance.limits = {
        const.CONF_MAX_GRID_POWER: 15_000,
        const.CONF_MAX_SOLAR_POWER: 12_000,
        const.CONF_MAX_BATTERY_CHARGED_POWER: 5_000,
        const.CONF_MAX_BATTERY_DISCHARGED_POWER: 6_600,
    }
    instance._energy_processor = coordinator_module.EnergyProcessor(instance.limits)
    return instance


def validate_totals(
    coordinator,
    data: dict[str, float],
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, float]:
    monkeypatch.setattr(coordinator_module.dt, "now", lambda: now)
    return coordinator._energy_processor.validate_totals(
        data, coordinator._last_checked_data, coordinator._last_checked_time
    )


def run_update(
    coordinator,
    raw_data: dict[str, float | None],
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, float]:
    """Run a full coordinator update cycle with a fixed frame and clock."""
    coordinator.async_get_raw_data = AsyncMock(return_value=dict(raw_data))
    monkeypatch.setattr(coordinator_module.dt, "now", lambda: now)
    return asyncio.run(coordinator._async_update_data())


@pytest.mark.parametrize(
    ("serial_number", "inverter_temperature", "expected"),
    (
        ("R123456789", 0, True),
        ("R123456789", 0.0, True),
        ("R123456789", 21.5, False),
        ("unknown", 0, False),
        ("", 0, False),
        (None, 0, False),
        ("R123456789", None, False),
    ),
)
def test_reports_modbus_disabled_from_current_telemetry(
    coordinator,
    serial_number: str | None,
    inverter_temperature: float | None,
    expected: bool,
) -> None:
    coordinator.serial_number = serial_number
    coordinator._last_inverter_temperature = inverter_temperature

    assert coordinator.is_modbus_disabled is expected


def test_seeds_baseline_from_legacy_persisted_state(coordinator) -> None:
    stored = {
        "last_checked_data": {"grid_import_total": 12.5},
        "last_checked_time": "2026-08-07T12:00:00+00:00",
    }
    coordinator._store = SimpleNamespace(async_load=AsyncMock(return_value=stored))

    asyncio.run(coordinator.async_load_persisted_state())

    assert coordinator._last_checked_data == {"grid_import_total": 12.5}
    assert coordinator._last_checked_time == datetime(
        2026, 8, 7, 12, 0, tzinfo=timezone.utc
    )
    assert coordinator._energy_processor.daily_snapshots == {}
    assert coordinator._energy_processor.last_rollover is None


def test_persisted_state_round_trips(coordinator) -> None:
    coordinator._last_checked_data = {"grid_import_total": 12.5}
    coordinator._last_checked_time = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    coordinator._energy_processor.accepted_at = {
        "grid_import_total": datetime(2026, 8, 7, 11, 0, tzinfo=timezone.utc)
    }
    coordinator._energy_processor.daily_snapshots = {"grid_import_today": 10.0}
    coordinator._energy_processor.last_rollover = datetime(
        2026, 8, 7, 0, 0, tzinfo=timezone.utc
    )

    stored = coordinator._persisted_state()

    coordinator._last_checked_data = {}
    coordinator._last_checked_time = None
    coordinator._energy_processor.accepted_at = {}
    coordinator._energy_processor.daily_snapshots = {}
    coordinator._energy_processor.last_rollover = None
    coordinator._store = SimpleNamespace(async_load=AsyncMock(return_value=stored))

    asyncio.run(coordinator.async_load_persisted_state())

    assert coordinator._last_checked_data == {"grid_import_total": 12.5}
    assert coordinator._energy_processor.accepted_at == {
        "grid_import_total": datetime(2026, 8, 7, 11, 0, tzinfo=timezone.utc)
    }
    assert coordinator._energy_processor.daily_snapshots == {"grid_import_today": 10.0}
    assert coordinator._energy_processor.last_rollover == datetime(
        2026, 8, 7, 0, 0, tzinfo=timezone.utc
    )


def test_accepted_update_publishes_successful_coordinator_status(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    coordinator.async_get_raw_data = AsyncMock(return_value={"grid_import_total": 10.0})
    coordinator._energy_processor.validate_totals = Mock(
        return_value={"grid_import_total": 10.0}
    )
    coordinator._energy_processor.derive_daily = Mock(
        side_effect=lambda data: (data, False)
    )
    coordinator._energy_processor.clamp_calculated = Mock(
        side_effect=lambda data, _prev, **_: data
    )
    coordinator._ena_calc_solar_power = False
    coordinator._store = None
    monkeypatch.setattr(coordinator_module.dt, "now", lambda: now)
    monkeypatch.setattr(
        coordinator_module.TelemetryData,
        "from_mapping",
        Mock(return_value=object()),
    )
    monkeypatch.setattr(
        coordinator_module, "calculate_derived_values", Mock(return_value={})
    )

    asyncio.run(coordinator._async_update_data())

    assert coordinator.status == const.CoordinatorStatus.SUCCESS
    assert "coordinator_status" not in coordinator._last_checked_data


def test_read_failure_raises_to_show_gap(coordinator) -> None:
    coordinator._last_checked_data = {"grid_import_total": 10.0}
    coordinator.async_get_raw_data = AsyncMock(return_value=None)

    with pytest.raises(coordinator_module.UpdateFailed):
        asyncio.run(coordinator._async_update_data())

    assert coordinator.status == const.CoordinatorStatus.READ_FAILED
    # The stale frame is not republished; entities go unavailable instead.
    assert coordinator._last_checked_data == {"grid_import_total": 10.0}


def test_reconnect_failure_updates_coordinator_status(coordinator) -> None:
    coordinator.async_get_raw_data = AsyncMock(
        side_effect=coordinator_module.UpdateFailed("Reconnect failed")
    )

    with pytest.raises(coordinator_module.UpdateFailed):
        asyncio.run(coordinator._async_update_data())

    assert coordinator.status == const.CoordinatorStatus.RECONNECT_FAILED


def test_processing_failure_updates_coordinator_status(coordinator) -> None:
    coordinator.async_get_raw_data = AsyncMock(return_value={})
    coordinator._energy_processor.validate_totals = Mock(
        side_effect=ValueError("Invalid data")
    )

    result = asyncio.run(coordinator._async_update_data())

    assert result is None
    assert coordinator.status == const.CoordinatorStatus.PROCESSING_FAILED


def test_persisted_state_after_reload_clamps_total_reset(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = {
        "last_checked_data": {"grid_import_total": 10.0},
        "last_checked_time": "2026-08-07T11:59:30+00:00",
    }
    coordinator._store = SimpleNamespace(async_load=AsyncMock(return_value=stored))
    asyncio.run(coordinator.async_load_persisted_state())

    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    result = validate_totals(coordinator, {"grid_import_total": 0.0}, now, monkeypatch)

    assert result["grid_import_total"] == 10.0


def test_initial_daily_snapshot_uses_device_daily_value(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_update(
        coordinator,
        {"grid_import_total": 1000.0, "grid_import_today": 5.0},
        datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc),
        monkeypatch,
    )

    assert result["grid_import_today"] == 5.0
    assert result["grid_import_today_raw"] == 5.0
    assert coordinator._energy_processor.daily_snapshots["grid_import_today"] == 995.0


def test_rolls_daily_counters_at_local_midnight(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    totals = {
        "solar_total": 1000.0,
        "grid_import_total": 500.0,
        "grid_export_total": 300.0,
        "bat_charged_total": 200.0,
        "bat_discharged_total": 180.0,
    }
    # Initial observation seeds snapshot to current total; today starts at 0.0
    before = run_update(
        coordinator,
        totals,
        datetime(2026, 8, 27, 16, 0, 0),
        monkeypatch,
    )
    # Energy grows on same date (8 kWh over 7 hours)
    during = run_update(
        coordinator,
        {**totals, "solar_total": 1008.0},
        datetime(2026, 8, 27, 23, 59, 55),
        monkeypatch,
    )
    # Local date boundary crossed at 00:00:05 -> daily reset rolls
    after = run_update(
        coordinator,
        {**totals, "solar_total": 1008.0},
        datetime(2026, 8, 28, 0, 0, 5),
        monkeypatch,
    )
    grown = run_update(
        coordinator,
        {**totals, "solar_total": 1008.01},
        datetime(2026, 8, 28, 0, 0, 10),
        monkeypatch,
    )

    assert before["solar_today"] == 0.0
    assert during["solar_today"] == 8.0
    assert after["solar_today"] == 0.0
    assert grown["solar_today"] == 0.01
    assert coordinator._energy_processor.last_rollover == datetime(2026, 8, 28, 0, 0, 5)


def test_ignores_bogus_zero_device_registers(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay: the daily device registers read a bogus 0 mid-day.

    Derived daily values depend strictly on validated lifetime totals minus
    midnight snapshots, so bogus device registers have no impact.
    """
    totals = {"solar_total": 1000.0, "bat_discharged_total": 180.0}
    coordinator._energy_processor.last_rollover = datetime(2026, 8, 27, 0, 0, 0)
    coordinator._energy_processor.daily_snapshots = {
        "solar_today": 992.0,
        "bat_discharged_today": 177.5,
    }

    result = run_update(
        coordinator,
        {**totals, "solar_today": 0.0, "bat_discharged_today": 0.0},
        datetime(2026, 8, 27, 12, 0, 5),
        monkeypatch,
    )

    assert result["solar_today"] == 8.0
    assert result["bat_discharged_today"] == 2.5
    assert result["solar_today_raw"] == 0.0


def test_publishes_raw_device_daily_as_diagnostic(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The device's own register is published unmodified under a *_raw diagnostic key.
    coordinator._energy_processor.last_rollover = datetime(2026, 8, 27, 0, 0, 0)
    coordinator._energy_processor.daily_snapshots = {"solar_today": 990.0}

    result = run_update(
        coordinator,
        {"solar_total": 1000.0, "solar_today": 9.5},
        datetime(2026, 8, 27, 12, 0, 0),
        monkeypatch,
    )

    assert result["solar_today"] == 10.0
    assert result["solar_today_raw"] == 9.5


def test_clamps_derived_house_energy_rounding_jitter(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 21, 8, 24, 19, tzinfo=timezone.utc)
    previous = {
        "solar_total": 104.0,
        "grid_import_total": 102.0,
        "bat_discharged_total": 101.0,
        "grid_export_total": 100.5,
        "bat_charged_total": 101.35,
        "solar_today": 4.0,
        "grid_import_today": 2.0,
        "bat_discharged_today": 1.0,
        "grid_export_today": 0.5,
        "bat_charged_today": 1.35,
        "house_energy_today": 5.15,
    }
    coordinator._last_checked_time = now - timedelta(seconds=5)
    coordinator._last_checked_data = previous
    coordinator._ena_calc_solar_power = False
    coordinator._energy_processor.daily_snapshots = {
        "solar_today": 100.0,
        "grid_import_today": 100.0,
        "bat_discharged_today": 100.0,
        "grid_export_today": 100.0,
        "bat_charged_today": 100.0,
    }
    coordinator.async_get_raw_data = AsyncMock(
        return_value={
            **previous,
            "bat_charged_total": 101.36,
            "bat_charged_today": 1.36,
        }
    )
    monkeypatch.setattr(coordinator_module.dt, "now", lambda: now)

    result = asyncio.run(coordinator._async_update_data())

    assert result["house_energy_today"] == 5.15


def test_clamps_derived_house_energy_jitter_during_raw_counter_reset(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 21, 9, 10, 54, tzinfo=timezone.utc)
    previous = {
        "solar_total": 105.63,
        "grid_import_total": 100.0,
        "bat_discharged_total": 100.0,
        "grid_export_total": 100.0,
        "bat_charged_total": 100.0,
        "solar_today": 5.63,
        "grid_import_today": 0.0,
        "bat_discharged_today": 0.0,
        "grid_export_today": 0.0,
        "bat_charged_today": 0.0,
        "house_energy_today": 5.63,
    }
    coordinator._last_checked_time = now - timedelta(seconds=5)
    coordinator._last_checked_data = previous
    coordinator._ena_calc_solar_power = False
    coordinator._energy_processor.daily_snapshots = {
        "solar_today": 100.0,
        "grid_import_today": 100.0,
        "bat_discharged_today": 100.0,
        "grid_export_today": 100.0,
        "bat_charged_today": 100.0,
    }
    coordinator.async_get_raw_data = AsyncMock(
        return_value={**previous, "solar_today": 5.62}
    )
    monkeypatch.setattr(coordinator_module.dt, "now", lambda: now)

    result = asyncio.run(coordinator._async_update_data())

    assert result["house_energy_today"] == 5.63


def test_replays_daily_register_flap_without_spike(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay: the device reset its daily registers at local
    midnight, but around 00:00 UTC (02:00 CEST) a ghost of yesterday's values
    flaps onto the daily registers for several polls. The published daily
    sensors derive from the lifetime counters and must not follow the ghost.
    """
    totals = {
        "solar_total": 12408.0,
        "grid_import_total": 820.55,
        "grid_export_total": 9640.02,
        "bat_charged_total": 3120.4,
        "bat_discharged_total": 2980.11,
    }
    ghost_of_yesterday = {
        "solar_today": 62.09,
        "grid_import_today": 0.03,
        "grid_export_today": 42.58,
        "bat_charged_today": 7.35,
        "bat_discharged_today": 6.43,
    }
    new_day = {
        "solar_today": 0.0,
        "grid_import_today": 0.0,
        "grid_export_today": 0.01,
        "bat_charged_today": 0.0,
        "bat_discharged_today": 0.51,
    }

    # The midnight reset occurred two hours earlier at local midnight (2026-08-28 00:00:02)
    coordinator._energy_processor.last_rollover = datetime(2026, 8, 28, 0, 0, 2)
    coordinator._energy_processor.daily_snapshots = {
        "solar_today": 12408.0,
        "grid_import_today": 820.55,
        "grid_export_today": 9640.02,
        "bat_charged_today": 3120.4,
        "bat_discharged_today": 2979.60,
    }
    now = datetime(2026, 8, 28, 1, 59, 57)
    previous = run_update(coordinator, {**totals, **new_day}, now, monkeypatch)
    assert previous["solar_today"] == 0.0
    assert previous["bat_discharged_today"] == 0.51

    flap = [ghost_of_yesterday] * 3 + [new_day] * 3 + [ghost_of_yesterday] * 3
    for raw_dailies in flap:
        now += timedelta(seconds=2)
        # The battery keeps discharging slowly through the night.
        totals["bat_discharged_total"] = round(totals["bat_discharged_total"] + 0.01, 2)
        published = run_update(coordinator, {**totals, **raw_dailies}, now, monkeypatch)

        for key in new_day:
            delta = published[key] - previous[key]
            assert 0 <= round(delta, 2) <= 0.02, f"{key} moved by {delta}"
        assert published["house_energy_today"] >= previous["house_energy_today"] >= 0
        previous = published

    # The ghost never made it through: still the new day's small values.
    assert previous["solar_today"] == 0.0
    assert previous["bat_discharged_today"] == 0.6


def test_replays_hours_long_total_read_gap_with_clean_recovery(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay of the reported statistics gap: solar_total reads failed for
    ~9 h overnight. Published values must hold the last validated value while
    the device's reset cannot be confirmed, then roll and accept the first
    genuine reading the next morning.
    """
    coordinator._energy_processor.last_rollover = datetime(2026, 8, 27, 0, 0, 0)
    coordinator._energy_processor.daily_snapshots = {"solar_today": 12388.0}

    seeded = run_update(
        coordinator,
        {"solar_total": 12408.0, "solar_today": 20.0},
        datetime(2026, 8, 27, 21, 0, 0),
        monkeypatch,
    )
    assert seeded["solar_today"] == 20.0

    for hour in (22, 23):
        held = run_update(
            coordinator,
            {"solar_total": None, "solar_today": None},
            datetime(2026, 8, 27, hour, 0, 0),
            monkeypatch,
        )
        assert held["solar_total"] == 12408.0
        assert held["solar_today"] == 20.0

    past_midnight = run_update(
        coordinator,
        {"solar_total": None, "solar_today": None},
        datetime(2026, 8, 28, 0, 30, 0),
        monkeypatch,
    )
    assert past_midnight["solar_total"] == 12408.0
    assert past_midnight["solar_today"] == 0.0

    recovered = run_update(
        coordinator,
        {"solar_total": 12408.01, "solar_today": 0.01},
        datetime(2026, 8, 28, 6, 20, 0),
        monkeypatch,
    )
    assert recovered["solar_total"] == 12408.01
    assert recovered["solar_today"] == 0.01


def test_floors_derived_house_energy_at_zero_without_baseline(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 26, 12, 0, 4, tzinfo=timezone.utc)
    raw = {
        "solar_total": 100.0,
        "grid_import_total": 100.04,
        "bat_discharged_total": 100.76,
        "grid_export_total": 100.01,
        "bat_charged_total": 107.75,
        "solar_today": 0.0,
        "grid_import_today": 0.04,
        "bat_discharged_today": 0.76,
        "grid_export_today": 0.01,
        "bat_charged_today": 7.75,
    }
    coordinator._last_checked_time = None
    coordinator._last_checked_data = {}
    coordinator._ena_calc_solar_power = False
    coordinator._energy_processor.daily_snapshots = {
        "solar_today": 100.0,
        "grid_import_today": 100.0,
        "bat_discharged_today": 100.0,
        "grid_export_today": 100.0,
        "bat_charged_today": 100.0,
    }
    coordinator.async_get_raw_data = AsyncMock(return_value=dict(raw))
    monkeypatch.setattr(coordinator_module.dt, "now", lambda: now)

    result = asyncio.run(coordinator._async_update_data())

    assert result["house_energy_today"] == 0


@pytest.mark.parametrize("is_error", (False, True), ids=("success", "modbus-error"))
def test_reads_register_block(coordinator, is_error: bool) -> None:
    response = SimpleNamespace(
        isError=Mock(return_value=is_error),
        exception_code=2,
        registers=[11, 22],
    )
    coordinator._client = SimpleNamespace(
        read_holding_registers=AsyncMock(return_value=response)
    )
    coordinator._client_slave_id = const.DEFAULT_SLAVE

    async def read_block():
        coordinator._lock = asyncio.Lock()
        return await coordinator.async_read_block(100, 2)

    if is_error:
        with pytest.raises(coordinator_module.ModbusException):
            asyncio.run(read_block())
    else:
        assert asyncio.run(read_block()) == [11, 22]

    coordinator._client.read_holding_registers.assert_awaited_once_with(
        address=100,
        count=2,
        device_id=const.DEFAULT_SLAVE,
    )


def test_gets_and_decodes_raw_data(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    block = const.RegisterBlock(
        (
            const.RegisterDef("battery_count", 100, const.RegisterType.UINT16),
            const.RegisterDef("grid_power", 101, const.RegisterType.UINT16),
        )
    )
    monkeypatch.setattr(coordinator_module, "REGISTER_BLOCKS", (block,))
    decode_register = Mock(side_effect=(2.0, 42.0))
    monkeypatch.setattr(coordinator_module, "decode_register", decode_register)
    coordinator._client = SimpleNamespace(connected=True)
    coordinator.async_read_block = AsyncMock(return_value=[2, 42])
    coordinator.limits[const.CONF_BATTERY_COUNT] = 2

    result = asyncio.run(coordinator.async_get_raw_data())

    assert result == {"battery_count": 2.0, "grid_power": 42.0}
    coordinator.async_read_block.assert_awaited_once_with(100, 2)


def test_captures_disabled_state_when_battery_count_guard_drops_frame(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    block = const.RegisterBlock(
        (
            const.RegisterDef("battery_count", 100, const.RegisterType.UINT16),
            const.RegisterDef("inverter_temperature", 101, const.RegisterType.UINT16),
        )
    )
    monkeypatch.setattr(coordinator_module, "REGISTER_BLOCKS", (block,))
    decode_register = Mock(side_effect=(0.0, 0.0))
    monkeypatch.setattr(coordinator_module, "decode_register", decode_register)
    monkeypatch.setattr(coordinator_module.asyncio, "sleep", AsyncMock())
    coordinator._client = SimpleNamespace(connected=True)
    coordinator.async_read_block = AsyncMock(return_value=[0, 0])
    coordinator.limits[const.CONF_BATTERY_COUNT] = 2
    coordinator.serial_number = "R123456789"

    result = asyncio.run(coordinator.async_get_raw_data())

    assert result is None
    assert coordinator._last_inverter_temperature == 0.0
    assert coordinator.is_modbus_disabled is True


def test_modbus_disabled_recovers_when_telemetry_returns(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    block = const.RegisterBlock(
        (
            const.RegisterDef("battery_count", 100, const.RegisterType.UINT16),
            const.RegisterDef("inverter_temperature", 101, const.RegisterType.UINT16),
        )
    )
    monkeypatch.setattr(coordinator_module, "REGISTER_BLOCKS", (block,))
    # Provide values for two polls, first with all zeroes and second with values
    decode_register = Mock(side_effect=(0.0, 0.0, 2.0, 21.5))
    monkeypatch.setattr(coordinator_module, "decode_register", decode_register)
    monkeypatch.setattr(coordinator_module.asyncio, "sleep", AsyncMock())
    coordinator._client = SimpleNamespace(connected=True)
    coordinator.async_read_block = AsyncMock(return_value=[0, 0])
    coordinator.limits[const.CONF_BATTERY_COUNT] = 2
    coordinator.serial_number = "R123456789"

    # Run first poll, returning zeros to simulate a Modbus-disabled state.
    assert asyncio.run(coordinator.async_get_raw_data()) is None
    assert coordinator.is_modbus_disabled is True

    # Run second poll, returning valid telemetry to simulate recovery.
    assert asyncio.run(coordinator.async_get_raw_data()) == {
        "battery_count": 2.0,
        "inverter_temperature": 21.5,
    }
    assert coordinator.is_modbus_disabled is False


@pytest.mark.parametrize(
    "registers",
    (const.MODBUS_REGISTERS, const.DEVICE_INFO_BLOCK.registers),
    ids=("polled", "device-info"),
)
def test_registers_do_not_overlap(registers: tuple[const.RegisterDef, ...]) -> None:
    """A multi-word register must not extend into the next register's address."""
    ordered = sorted(registers, key=lambda register: register.address)

    for register, following in zip(ordered, ordered[1:]):
        assert register.end <= following.address, (
            f"{register.key} at {register.address} spans {register.size} words "
            f"and overlaps {following.key} at {following.address}"
        )


def test_blocks_cover_every_register_word_they_map() -> None:
    """Every register must decode from inside the block that was read for it."""
    mapped = [
        register for block in const.REGISTER_BLOCKS for register in block.registers
    ]

    assert sorted(register.key for register in mapped) == sorted(
        register.key for register in const.MODBUS_REGISTERS
    )
    for block in const.REGISTER_BLOCKS:
        for register in block.registers:
            index = block.index_of(register)

            assert index >= 0, (
                f"{register.key} at {register.address} sits before the start of "
                f"its block at {block.start}"
            )
            assert index + register.size <= block.count, (
                f"{register.key} needs words {index}-{index + register.size - 1} "
                f"but the block at {block.start} only reads {block.count}"
            )


def test_writable_numbers_write_to_the_register_they_read() -> None:
    for number in const.WRITABLE_NUMBERS_MAP:
        expected = const.REGISTERS_BY_KEY[number.read_key].address

        assert number.register == expected, (
            f"{number.key} writes to {number.register} but reads "
            f"{number.read_key} from {expected}"
        )


def test_raw_daily_sensors_exist_only_for_device_read_values() -> None:
    """The processor only echoes dailies backed by a total, so entities must match."""
    produced = EnergyProcessor.raw_daily_values(
        {energy_sensor.key: 1.0 for energy_sensor in const.ENERGY_SENSOR_MAP}
    )

    assert {sensor.key for sensor in const.DAILY_ENERGY_SENSORS_DEVICE_RAW} == set(
        produced
    )


def test_enum_sensors_declare_their_options() -> None:
    for sensor in const.SENSOR_MAP:
        if sensor.device_class == "enum":
            assert sensor.options, f"{sensor.key} is an enum without options"


def _device_info_registers(
    serial: str = "R371ZD1AZH3X0450",
    product_number: int = 3,
    product_category: int = 1,
    firmware: int = 0x03001313,
) -> list[int]:
    registers = [0] * const.DEVICE_INFO_BLOCK.count
    registers[const.DEVICE_INFO_BLOCK.index_of(const.PRODUCT_CATEGORY)] = (
        product_category
    )
    registers[const.DEVICE_INFO_BLOCK.index_of(const.PRODUCT_NUMBER)] = product_number
    serial_index = const.DEVICE_INFO_BLOCK.index_of(const.SERIAL_NUMBER)
    for offset in range(const.SERIAL_NUMBER.size):
        high, low = serial[offset * 2], serial[offset * 2 + 1]
        registers[serial_index + offset] = (ord(high) << 8) | ord(low)
    firmware_index = const.DEVICE_INFO_BLOCK.index_of(const.FIRMWARE_VERSION)
    registers[firmware_index] = firmware & 0xFFFF
    registers[firmware_index + 1] = firmware >> 16
    return registers


def test_reads_device_info_in_a_single_request(coordinator) -> None:
    coordinator.firmware_version = None
    coordinator.detected_model = None
    coordinator.inverter_model = const.InverterModel.POWEROCEAN_PLUS
    coordinator.async_read_block = AsyncMock(return_value=_device_info_registers())

    asyncio.run(coordinator.async_read_device_info())

    assert coordinator.serial_number == "R371ZD1AZH3X0450"
    assert coordinator.firmware_version == "3.0.19.19"
    assert coordinator.detected_model == const.InverterModel.POWEROCEAN_PLUS
    coordinator.async_read_block.assert_awaited_once_with(40002, 12)


def test_device_info_read_failure_closes_connection(coordinator) -> None:
    coordinator.firmware_version = None
    coordinator.detected_model = None
    coordinator._client = SimpleNamespace(close=Mock())
    coordinator.async_read_block = AsyncMock(
        side_effect=coordinator_module.ModbusException("boom")
    )

    asyncio.run(coordinator.async_read_device_info())

    assert coordinator.serial_number == "unknown"
    coordinator._client.close.assert_called_once()


def test_read_plan_is_not_split_more_than_necessary() -> None:
    """Neighbouring blocks must be unmergeable, so no poll wastes a round trip."""
    for block, following in zip(const.REGISTER_BLOCKS, const.REGISTER_BLOCKS[1:]):
        gap = following.start - (block.start + block.count)
        merged = following.start + following.count - block.start

        assert gap > const.MAX_REGISTER_GAP or merged > const.MAX_REGISTERS_PER_READ, (
            f"blocks at {block.start} and {following.start} are only {gap} words "
            f"apart and would merge into {merged} words, so they should be one read"
        )


def test_block_rejects_more_registers_than_a_modbus_read_allows() -> None:
    with pytest.raises(ValueError, match="more than the 125"):
        const.RegisterBlock(
            (
                const.RegisterDef("first", 40000, const.RegisterType.UINT16),
                const.RegisterDef("last", 40200, const.RegisterType.UINT16),
            )
        )


@pytest.mark.parametrize(
    ("product_number", "product_category", "expected"),
    (
        (1, 1, const.InverterModel.POWEROCEAN_THREE_PHASE),
        (1, 2, const.InverterModel.POWEROCEAN_SINGLE_PHASE),
        (2, 2, const.InverterModel.POWEROCEAN_SINGLE_PHASE),
        (3, 1, const.InverterModel.POWEROCEAN_PLUS),
        (0, 1, None),
        (None, None, None),
    ),
)
def test_detects_inverter_model_from_product_info(
    product_number: int | None,
    product_category: int | None,
    expected: const.InverterModel | None,
) -> None:
    assert (
        const.InverterModel.from_product_info(product_number, product_category)
        == expected
    )


def test_raw_data_raises_when_reconnect_fails(coordinator) -> None:
    coordinator._client = SimpleNamespace(connected=False)
    coordinator.async_reconnect = AsyncMock(return_value=False)

    with pytest.raises(coordinator_module.UpdateFailed, match="Reconnect failed"):
        asyncio.run(coordinator.async_get_raw_data())
