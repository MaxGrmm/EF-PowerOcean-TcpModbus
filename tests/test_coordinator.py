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
    instance._check_monotonic = True
    instance._count_reset_energy_sensor = 5
    instance._count_reset_energy_finished = 5
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
    ("serial_number", "display_brightness", "expected"),
    (
        ("R123456789", 0, True),
        ("R123456789", 19, True),
        ("R123456789", 20, False),
        ("R123456789", 100, False),
        ("unknown", 19, False),
        ("", 19, False),
        (None, 19, False),
        ("R123456789", None, False),
    ),
)
def test_reports_modbus_disabled_from_current_telemetry(
    coordinator,
    serial_number: str | None,
    display_brightness: float | None,
    expected: bool,
) -> None:
    coordinator.serial_number = serial_number
    coordinator.data = {"device_led_brightness": display_brightness}

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
    ("current", "expected", "rejects_snapshot"),
    (
        (9.0, 10.0, False),
        (10.0, 10.0, False),
        (7_510.0, 7_510.0, False),
        (7_510.01, 10.0, True),
        (0.0, 10.0, True),
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
    rejects_snapshot: bool,
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    coordinator._last_checked_time = now - timedelta(minutes=30)
    coordinator._last_checked_data = {
        "grid_import_total": 10.0,
        "previous_snapshot_marker": 1.0,
    }

    result = sanitize(coordinator, {"grid_import_total": current}, now, monkeypatch)

    assert result["grid_import_total"] == expected
    assert ("previous_snapshot_marker" in result) is rejects_snapshot


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


def test_accepts_daily_reset_during_midnight_window(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 8, 0, 0, 30, tzinfo=timezone.utc)
    coordinator._last_checked_time = now - timedelta(minutes=1)
    coordinator._last_checked_data = {"grid_import_today": 10.0}

    result = sanitize(coordinator, {"grid_import_today": 0.0}, now, monkeypatch)

    assert result["grid_import_today"] == 0.0
    assert coordinator._check_monotonic is False
    assert coordinator._count_reset_energy_finished == 1


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


@pytest.mark.parametrize(
    ("now", "sensor_key", "expected_reset"),
    (
        (datetime(2026, 8, 8, 0, 0, tzinfo=timezone.utc), "grid_import_today", True),
        (
            datetime(2026, 8, 8, 0, 0, 59, tzinfo=timezone.utc),
            "grid_import_today",
            True,
        ),
        (
            datetime(2026, 8, 8, 0, 1, tzinfo=timezone.utc),
            "grid_import_today",
            False,
        ),
        (
            datetime(2026, 8, 8, 0, 0, tzinfo=timezone.utc),
            "grid_import_total",
            False,
        ),
    ),
    ids=(
        "start-of-midnight-window",
        "end-of-midnight-window",
        "after-midnight-window",
        "non-resetting-sensor",
    ),
)
def test_daily_reset_window_boundaries(
    coordinator,
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    sensor_key: str,
    expected_reset: bool,
) -> None:
    coordinator._last_checked_time = now - timedelta(minutes=1)
    coordinator._last_checked_data = {sensor_key: 10.0}

    result = sanitize(coordinator, {sensor_key: 0.0}, now, monkeypatch)

    assert result[sensor_key] == (0.0 if expected_reset else 10.0)
    assert (coordinator._check_monotonic is False) is expected_reset


def test_enforces_monotonic_energy_values(coordinator) -> None:
    coordinator._last_checked_data = {
        "grid_import_total": 10.0,
        "grid_export_total": 5.0,
    }
    data = {
        "grid_import_total": 9.0,
        "grid_export_total": 6.0,
    }

    result = coordinator._enforced_monotonic(data)

    assert result is data
    assert result["grid_import_total"] == 10.0
    assert result["grid_export_total"] == 6.0


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
