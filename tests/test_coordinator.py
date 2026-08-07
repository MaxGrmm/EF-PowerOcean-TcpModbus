"""Unit tests for coordinator data validation without Home Assistant."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
        "unrelated": 1.0,
    }

    result = sanitize(coordinator, {"grid_import_total": current}, now, monkeypatch)

    assert result["grid_import_total"] == expected
    assert (result == coordinator._last_checked_data) is rejects_snapshot


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
