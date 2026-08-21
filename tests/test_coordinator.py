"""Unit tests for coordinator data validation without Home Assistant."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from ef_powerocean_tcpmodbus import const
from ef_powerocean_tcpmodbus import coordinator as coordinator_module


@pytest.fixture
def coordinator():
    instance = coordinator_module.EcoflowCoordinator.__new__(
        coordinator_module.EcoflowCoordinator
    )
    instance._last_checked_data = {}
    instance._last_checked_time = None
    instance._unrealistic_energy_read_counts = {}
    instance._store = None
    instance.inverter_model = const.DEFAULT_INVERTER_MODEL
    instance.limits = {
        const.CONF_MAX_GRID_POWER: 15_000,
        const.CONF_MAX_SOLAR_POWER: 12_000,
        const.CONF_MAX_BATTERY_CHARGED_POWER: 5_000,
        const.CONF_MAX_BATTERY_DISCHARGED_POWER: 6_600,
    }
    return instance


def sanitize(
    coordinator,
    data: dict[str, float],
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, float]:
    monkeypatch.setattr(coordinator_module.dt, "now", lambda: now)
    return coordinator._sanitize_energy_values(data)


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


def test_returns_current_data_for_first_observation(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = {"grid_import_total": 10.0}

    result = sanitize(
        coordinator,
        data,
        datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc),
        monkeypatch,
    )

    assert result == data
    assert result is not data


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    (
        (timedelta(0), 10.0),
        (timedelta(milliseconds=999), 10.0),
        (timedelta(seconds=1), 11.0),
    ),
    ids=("same-time", "just-under-one-second", "exactly-one-second"),
)
def test_handles_minimum_update_interval_boundary(
    coordinator,
    monkeypatch: pytest.MonkeyPatch,
    elapsed: timedelta,
    expected: float,
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    coordinator._last_checked_time = now - elapsed
    coordinator._last_checked_data = {"grid_import_total": 10.0}

    result = sanitize(coordinator, {"grid_import_total": 11.0}, now, monkeypatch)

    assert result["grid_import_total"] == expected


@pytest.mark.parametrize(
    ("current", "expected"),
    (
        (9.0, 10.0),
        (10.0, 10.0),
        (7_510.0, 7_510.0),
        (7_510.01, 10.0),
        (0.0, 10.0),
    ),
    ids=(
        "decrease-is-clamped",
        "unchanged",
        "increase-at-power-limit",
        "increase-above-power-limit",
        "unexpected-zero",
    ),
)
def test_validates_energy_changes_within_one_hour(
    coordinator,
    monkeypatch: pytest.MonkeyPatch,
    current: float,
    expected: float,
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    coordinator._last_checked_time = now - timedelta(minutes=30)
    coordinator._last_checked_data = {
        "grid_import_total": 10.0,
        "previous_snapshot_marker": 1.0,
    }

    result = sanitize(coordinator, {"grid_import_total": current}, now, monkeypatch)

    assert result["grid_import_total"] == expected
    assert "previous_snapshot_marker" not in result


def test_debounces_unrealistic_energy_read_without_discarding_frame(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    coordinator._last_checked_time = now - timedelta(seconds=30)
    coordinator._last_checked_data = {
        "bat_discharged_today": 1.77,
        "battery_soc": 50.0,
    }

    for read_number in (1, 2, 3):
        result = sanitize(
            coordinator,
            {"bat_discharged_today": 0.0, "battery_soc": 45.0},
            now,
            monkeypatch,
        )

        expected_bat_discharged_today = 0.0 if read_number == 3 else 1.77
        assert result["bat_discharged_today"] == expected_bat_discharged_today
        assert result["battery_soc"] == 45.0


def test_debounces_nonzero_energy_decrease(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    coordinator._last_checked_time = now - timedelta(seconds=30)
    coordinator._last_checked_data = {"grid_import_today": 10.0}

    results = [
        sanitize(coordinator, {"grid_import_today": 1.0}, now, monkeypatch)
        for _ in range(3)
    ]

    assert [result["grid_import_today"] for result in results] == [10.0, 10.0, 1.0]


def test_total_energy_decrease_is_never_accepted(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    coordinator._last_checked_time = now - timedelta(seconds=30)
    coordinator._last_checked_data = {"grid_import_total": 10.0}

    results = [
        sanitize(coordinator, {"grid_import_total": 0.0}, now, monkeypatch)
        for _ in range(5)
    ]

    assert [result["grid_import_total"] for result in results] == [10.0] * 5
    assert coordinator._unrealistic_energy_read_counts == {}


def test_seeds_baseline_from_persisted_state(coordinator) -> None:
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


def test_accepted_update_publishes_last_read_time_without_persisting_it(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    coordinator.async_get_raw_data = AsyncMock(return_value={"grid_import_total": 10.0})
    coordinator._sanitize_energy_values = Mock(return_value={"grid_import_total": 10.0})
    coordinator._clamp_calculated_energy_values = Mock(side_effect=lambda data: data)
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

    result = asyncio.run(coordinator._async_update_data())

    assert result["last_read_time"] == now
    assert "last_read_time" not in coordinator._last_checked_data


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
    result = sanitize(coordinator, {"grid_import_total": 0.0}, now, monkeypatch)

    assert result["grid_import_total"] == 10.0


def test_valid_energy_read_clears_unrealistic_read_count(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    coordinator._last_checked_time = now - timedelta(seconds=30)
    coordinator._last_checked_data = {"grid_import_total": 10.0}

    sanitize(coordinator, {"grid_import_total": 1_000.0}, now, monkeypatch)
    sanitize(coordinator, {"grid_import_total": 11.0}, now, monkeypatch)
    result = sanitize(coordinator, {"grid_import_total": 1_000.0}, now, monkeypatch)

    assert result["grid_import_total"] == 10.0
    assert coordinator._unrealistic_energy_read_counts == {"grid_import_total": 1}


def test_accepted_nonzero_daily_reset_updates_derived_house_energy(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 8, 0, 1, tzinfo=timezone.utc)
    daily_energy = {
        "solar_today": 20.0,
        "grid_import_today": 5.0,
        "bat_discharged_today": 2.0,
        "grid_export_today": 3.0,
        "bat_charged_today": 4.0,
    }
    coordinator._last_checked_time = now - timedelta(seconds=30)
    coordinator._last_checked_data = {
        **daily_energy,
        "house_energy_today": 20.0,
    }
    coordinator._unrealistic_energy_read_counts = dict.fromkeys(daily_energy, 2)
    coordinator._ena_calc_solar_power = False
    coordinator.async_get_raw_data = AsyncMock(
        return_value=dict.fromkeys(daily_energy, 0.1)
    )
    monkeypatch.setattr(coordinator_module.dt, "now", lambda: now)

    result = asyncio.run(coordinator._async_update_data())

    assert result["house_energy_today"] == 0.1


def test_clamps_derived_house_energy_rounding_jitter(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 21, 8, 24, 19, tzinfo=timezone.utc)
    previous = {
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
    coordinator.async_get_raw_data = AsyncMock(
        return_value={**previous, "bat_charged_today": 1.36}
    )
    monkeypatch.setattr(coordinator_module.dt, "now", lambda: now)

    result = asyncio.run(coordinator._async_update_data())

    assert result["house_energy_today"] == 5.15


def test_clamps_derived_house_energy_jitter_during_raw_counter_reset(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 21, 9, 10, 54, tzinfo=timezone.utc)
    previous = {
        "solar_today": 5.63,
        "grid_import_today": 0.0,
        "bat_discharged_today": 0.0,
        "grid_export_today": 0.0,
        "bat_charged_today": 0.0,
        "house_energy_today": 5.63,
    }
    coordinator._last_checked_time = now - timedelta(seconds=5)
    coordinator._last_checked_data = previous
    # A raw daily counter completes an accepted downward wobble this poll.
    coordinator._unrealistic_energy_read_counts = {"solar_today": 2}
    coordinator._ena_calc_solar_power = False
    coordinator.async_get_raw_data = AsyncMock(
        return_value={**previous, "solar_today": 5.62}
    )
    monkeypatch.setattr(coordinator_module.dt, "now", lambda: now)

    result = asyncio.run(coordinator._async_update_data())

    assert result["house_energy_today"] == 5.63


@pytest.mark.parametrize(
    ("current_data", "previous_data"),
    (
        ({}, {"grid_import_total": 10.0}),
        ({"grid_import_total": 11.0}, {}),
        ({"grid_import_total": None}, {"grid_import_total": 10.0}),
        ({"grid_import_total": 11.0}, {"grid_import_total": None}),
    ),
    ids=("current-missing", "previous-missing", "current-none", "previous-none"),
)
def test_leaves_values_unchanged_when_a_reading_is_missing(
    coordinator,
    monkeypatch: pytest.MonkeyPatch,
    current_data: dict[str, float | None],
    previous_data: dict[str, float | None],
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    coordinator._last_checked_time = now - timedelta(minutes=30)
    coordinator._last_checked_data = previous_data

    result = sanitize(coordinator, current_data, now, monkeypatch)

    assert result == current_data


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    (
        (timedelta(hours=1), 15_010.0),
        (timedelta(hours=1, microseconds=1), 20_000.0),
        (timedelta(hours=2), 20_000.0),
    ),
    ids=("exactly-one-hour", "just-over-one-hour", "two-hours"),
)
def test_handles_maximum_validation_window_boundary(
    coordinator,
    monkeypatch: pytest.MonkeyPatch,
    elapsed: timedelta,
    expected: float,
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    coordinator._last_checked_time = now - elapsed
    coordinator._last_checked_data = {"grid_import_total": 10.0}

    current = 15_010.0 if elapsed == timedelta(hours=1) else 20_000.0
    result = sanitize(coordinator, {"grid_import_total": current}, now, monkeypatch)

    assert result["grid_import_total"] == expected


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
    block = SimpleNamespace(
        start_register=100,
        num_read_regs=2,
        content=(
            const.RegisterDef(key="battery_count", block_index=0, size=1),
            const.RegisterDef(key="grid_power", block_index=1, size=1),
        ),
    )
    monkeypatch.setitem(coordinator_module.MOD_REGISTER_MAP, "blocks", (block,))
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
    block = SimpleNamespace(
        start_register=100,
        num_read_regs=2,
        content=(
            const.RegisterDef(key="battery_count", block_index=0, size=1),
            const.RegisterDef(key="inverter_temperature", block_index=1, size=1),
        ),
    )
    monkeypatch.setitem(coordinator_module.MOD_REGISTER_MAP, "blocks", (block,))
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
    block = SimpleNamespace(
        start_register=100,
        num_read_regs=2,
        content=(
            const.RegisterDef(key="battery_count", block_index=0, size=1),
            const.RegisterDef(key="inverter_temperature", block_index=1, size=1),
        ),
    )
    monkeypatch.setitem(coordinator_module.MOD_REGISTER_MAP, "blocks", (block,))
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
    ("inverter_model", "expected_index"),
    (
        (const.InverterModel.POWEROCEAN_THREE_PHASE, 90),
        (const.InverterModel.POWEROCEAN_PLUS, 19),
    ),
    ids=("three-phase-default", "powerocean-plus-override"),
)
def test_resolves_model_specific_feed_in_register_index(
    inverter_model: const.InverterModel, expected_index: int
) -> None:
    registers = {
        register.key: register
        for register in const.MOD_REGISTER_MAP["blocks"][0].content
    }

    assert "feed_in_power_max_ai" not in registers
    assert (
        registers["feed_in_power_max"].block_index_for(inverter_model) == expected_index
    )


def test_raw_data_raises_when_reconnect_fails(coordinator) -> None:
    coordinator._client = SimpleNamespace(connected=False)
    coordinator.async_reconnect = AsyncMock(return_value=False)

    with pytest.raises(coordinator_module.UpdateFailed, match="Reconnect failed"):
        asyncio.run(coordinator.async_get_raw_data())
