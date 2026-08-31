"""Pure unit tests for the EnergyProcessor lifetime-counter validation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from ef_powerocean_tcpmodbus import const
from ef_powerocean_tcpmodbus import energy_processor as energy_processor_module
from ef_powerocean_tcpmodbus.energy_processor import EnergyProcessor

LIMITS = {
    const.CONF_MAX_GRID_POWER: 15_000,
    const.CONF_MAX_SOLAR_POWER: 12_000,
    const.CONF_MAX_BATTERY_CHARGED_POWER: 5_000,
    const.CONF_MAX_BATTERY_DISCHARGED_POWER: 6_600,
}


@pytest.fixture
def processor() -> EnergyProcessor:
    return EnergyProcessor(LIMITS)


def validate_totals(
    processor: EnergyProcessor,
    current: dict[str, float | None],
    previous_data: dict[str, float | None],
    previous_time: datetime | None,
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, float]:
    monkeypatch.setattr(energy_processor_module.dt, "now", lambda: now)
    return processor.validate_totals(current, previous_data, previous_time)


def test_returns_current_data_for_first_observation(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = {"grid_import_total": 10.0}

    result = validate_totals(
        processor,
        data,
        {},
        None,
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
        (timedelta(seconds=1), 10.004),
    ),
    ids=("same-time", "just-under-one-second", "exactly-one-second"),
)
def test_handles_minimum_update_interval_boundary(
    processor: EnergyProcessor,
    monkeypatch: pytest.MonkeyPatch,
    elapsed: timedelta,
    expected: float,
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)

    result = validate_totals(
        processor,
        {"grid_import_total": 10.004},
        {"grid_import_total": 10.0},
        now - elapsed,
        now,
        monkeypatch,
    )

    assert result["grid_import_total"] == expected


@pytest.mark.parametrize(
    ("current", "expected"),
    (
        (9.0, 10.0),
        (10.0, 10.0),
        (17.5, 17.5),
        (18.0, 10.0),
        (0.0, 10.0),
    ),
    ids=(
        "decrease-is-clamped",
        "unchanged",
        "increase-within-budget",
        "increase-above-budget",
        "unexpected-zero",
    ),
)
def test_validates_energy_changes_within_one_hour(
    processor: EnergyProcessor,
    monkeypatch: pytest.MonkeyPatch,
    current: float,
    expected: float,
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)

    result = validate_totals(
        processor,
        {"grid_import_total": current},
        {"grid_import_total": 10.0, "previous_snapshot_marker": 1.0},
        now - timedelta(minutes=30),
        now,
        monkeypatch,
    )

    assert result["grid_import_total"] == expected
    assert "previous_snapshot_marker" not in result


@pytest.mark.parametrize("scan_interval", (2, 5, 30), ids=("2s", "5s", "30s"))
def test_accepts_single_register_step_regardless_of_scan_interval(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch, scan_interval: int
) -> None:
    # A single 0.01 kWh tick must never be flagged as unrealistic, even at a 2 s
    # interval where it implies a high instantaneous power.
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)

    result = validate_totals(
        processor,
        {"grid_import_total": 10.01},
        {"grid_import_total": 10.0},
        now - timedelta(seconds=scan_interval),
        now,
        monkeypatch,
    )

    assert result["grid_import_total"] == 10.01


@pytest.mark.parametrize(
    ("scan_interval", "current", "expected"),
    (
        (2, 10.1, 10.0),
        (30, 10.2, 10.0),
    ),
    ids=("2s-spike-held", "30s-spike-held"),
)
def test_holds_over_budget_increase_scaled_to_interval(
    processor: EnergyProcessor,
    monkeypatch: pytest.MonkeyPatch,
    scan_interval: int,
    current: float,
    expected: float,
) -> None:
    # grid budget at 15 kW: ~0.008 kWh over 2 s, ~0.125 kWh over 30 s.
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)

    result = validate_totals(
        processor,
        {"grid_import_total": current},
        {"grid_import_total": 10.0},
        now - timedelta(seconds=scan_interval),
        now,
        monkeypatch,
    )

    assert result["grid_import_total"] == expected


def test_holds_implausible_energy_read_without_discarding_frame(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A held energy read must not discard the rest of the frame.
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)

    result = validate_totals(
        processor,
        {"bat_discharged_total": 0.0, "battery_soc": 45.0},
        {"bat_discharged_total": 1.77, "battery_soc": 50.0},
        now - timedelta(seconds=30),
        now,
        monkeypatch,
    )

    assert result["bat_discharged_total"] == 1.77
    assert result["battery_soc"] == 45.0


def test_holds_decreasing_total_regardless_of_repetition(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unlike the old debounce, repetition must never grant a decrease amnesty.
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    previous_time = now - timedelta(seconds=30)
    previous_data = {"grid_import_total": 10.0}

    actual = [
        validate_totals(
            processor,
            {"grid_import_total": 0.0},
            previous_data,
            previous_time,
            now,
            monkeypatch,
        )["grid_import_total"]
        for _ in range(5)
    ]

    assert actual == [10.0] * 5


def test_accepts_true_value_after_hours_of_bogus_zero(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay: the counter reads a bogus 0 for hours mid-day.

    The published value must hold the baseline the whole time, and the true
    value must be accepted on recovery because the rise budget accrues from
    the last genuine acceptance.
    """
    start = datetime(2026, 8, 7, 9, 0, tzinfo=timezone.utc)
    previous_data = {"bat_discharged_total": 1000.0}
    processor.accepted_at = {"bat_discharged_total": start}

    now = start
    for _ in range(10):
        now += timedelta(minutes=18)
        result = validate_totals(
            processor,
            {"bat_discharged_total": 0.0},
            previous_data,
            start,
            now,
            monkeypatch,
        )
        assert result["bat_discharged_total"] == 1000.0

    recovered = validate_totals(
        processor,
        {"bat_discharged_total": 1003.5},
        previous_data,
        start,
        now + timedelta(seconds=5),
        monkeypatch,
    )

    assert recovered["bat_discharged_total"] == 1003.5


@pytest.mark.parametrize(
    ("current_data", "previous_data", "expected"),
    (
        ({}, {"grid_import_total": 10.0}, {"grid_import_total": 10.0}),
        ({"grid_import_total": 11.0}, {}, {"grid_import_total": 11.0}),
        (
            {"grid_import_total": None},
            {"grid_import_total": 10.0},
            {"grid_import_total": 10.0},
        ),
        (
            {"grid_import_total": 11.0},
            {"grid_import_total": None},
            {"grid_import_total": 11.0},
        ),
    ),
    ids=("current-missing", "previous-missing", "current-none", "previous-none"),
)
def test_fills_missing_readings_from_last_validated_value(
    processor: EnergyProcessor,
    monkeypatch: pytest.MonkeyPatch,
    current_data: dict[str, float | None],
    previous_data: dict[str, float | None],
    expected: dict[str, float | None],
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)

    result = validate_totals(
        processor,
        current_data,
        previous_data,
        now - timedelta(minutes=30),
        now,
        monkeypatch,
    )

    assert result == expected


@pytest.mark.parametrize(
    ("elapsed", "current", "expected"),
    (
        (timedelta(hours=1), 25.0, 25.0),
        (timedelta(hours=2), 20_000.0, 10.0),
        (timedelta(days=30), 500.0, 500.0),
    ),
    ids=("one-hour-budget", "implausible-jump-held", "month-long-gap-accepted"),
)
def test_scales_rise_budget_with_time_since_last_acceptance(
    processor: EnergyProcessor,
    monkeypatch: pytest.MonkeyPatch,
    elapsed: timedelta,
    current: float,
    expected: float,
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)

    result = validate_totals(
        processor,
        {"grid_import_total": current},
        {"grid_import_total": 10.0},
        now - elapsed,
        now,
        monkeypatch,
    )

    assert result["grid_import_total"] == expected


def derive_daily(
    processor: EnergyProcessor,
    data: dict[str, float | None],
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, float], bool]:
    monkeypatch.setattr(energy_processor_module.dt, "now", lambda: now)
    return processor.derive_daily(data)


def test_derive_daily_initial_observation_starts_at_zero(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    # Device registers like grid_import_today are ignored; derived daily starts at 0.
    result, is_reset = derive_daily(
        processor,
        {"grid_import_total": 1000.0, "grid_import_today": 5.0},
        now,
        monkeypatch,
    )

    assert result["grid_import_today"] == 0.0
    assert not is_reset
    assert processor.daily_snapshots["grid_import_today"] == 1000.0


def test_derive_daily_initial_observation_uses_raw_when_present(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    # A restart without a snapshot must resume from the device's own daily
    # register rather than restarting the count at zero.
    result, is_reset = derive_daily(
        processor,
        {"grid_import_total": 1000.0, "grid_import_today_raw": 5.0},
        now,
        monkeypatch,
    )

    assert result["grid_import_today"] == 5.0
    assert not is_reset
    assert processor.daily_snapshots["grid_import_today"] == 995.0


@pytest.mark.parametrize(
    "raw_daily",
    (-1.0, 1001.0, float("inf"), float("nan"), 1.0),
    ids=("negative", "above-total", "infinite", "nan", "over-power-budget"),
)
def test_derive_daily_rejects_invalid_initial_raw_value(
    processor: EnergyProcessor,
    monkeypatch: pytest.MonkeyPatch,
    raw_daily: float,
) -> None:
    result, _ = derive_daily(
        processor,
        {"grid_import_total": 1000.0, "grid_import_today_raw": raw_daily},
        datetime(2026, 8, 7, 0, 1, tzinfo=timezone.utc),
        monkeypatch,
    )

    assert result["grid_import_today"] == 0.0
    assert processor.daily_snapshots["grid_import_today"] == 1000.0


def test_derive_daily_omits_daily_value_when_total_is_missing(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _ = derive_daily(
        processor,
        {"grid_import_today": 5.0, "grid_import_today_raw": 5.0},
        datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc),
        monkeypatch,
    )

    assert "grid_import_today" not in result
    assert result["grid_import_today_raw"] == 5.0


def test_derive_daily_accrues_on_same_date(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    t1 = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 7, 18, 0, tzinfo=timezone.utc)

    derive_daily(processor, {"grid_import_total": 1000.0}, t1, monkeypatch)
    result, is_reset = derive_daily(
        processor, {"grid_import_total": 1002.5}, t2, monkeypatch
    )

    assert result["grid_import_today"] == 2.5
    assert not is_reset


def test_derive_daily_rolls_on_local_midnight_date_crossing(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    t1 = datetime(2026, 8, 7, 23, 59, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 8, 0, 0, 10, tzinfo=timezone.utc)

    derive_daily(processor, {"grid_import_total": 1000.0}, t1, monkeypatch)
    result, is_reset = derive_daily(
        processor, {"grid_import_total": 1005.0}, t2, monkeypatch
    )

    assert is_reset
    assert result["grid_import_today"] == 0.0
    assert processor.daily_snapshots["grid_import_today"] == 1005.0


def test_derive_daily_preserves_energy_before_first_poll_after_midnight(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    derive_daily(
        processor,
        {"grid_import_total": 1000.0},
        datetime(2026, 8, 7, 23, 59, tzinfo=timezone.utc),
        monkeypatch,
    )
    result, is_reset = derive_daily(
        processor,
        {"grid_import_total": 1000.01, "grid_import_today_raw": 0.01},
        datetime(2026, 8, 8, 0, 0, 10, tzinfo=timezone.utc),
        monkeypatch,
    )

    assert is_reset
    assert result["grid_import_today"] == 0.01
    assert processor.daily_snapshots["grid_import_today"] == 1000.0


def test_derive_daily_defers_rollover_for_missing_total(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    derive_daily(
        processor,
        {"grid_import_total": 1000.0},
        datetime(2026, 8, 7, 23, 59, tzinfo=timezone.utc),
        monkeypatch,
    )
    missing_result, is_reset = derive_daily(
        processor,
        {"grid_import_today": 0.01, "grid_import_today_raw": 0.01},
        datetime(2026, 8, 8, 0, 0, 10, tzinfo=timezone.utc),
        monkeypatch,
    )

    assert is_reset
    assert "grid_import_today" not in missing_result
    assert "grid_import_today" not in processor.daily_snapshots

    recovered_result, is_reset = derive_daily(
        processor,
        {"grid_import_total": 1000.02, "grid_import_today_raw": 0.02},
        datetime(2026, 8, 8, 0, 1, tzinfo=timezone.utc),
        monkeypatch,
    )

    assert not is_reset
    assert recovered_result["grid_import_today"] == 0.02
    assert processor.daily_snapshots["grid_import_today"] == 1000.0
