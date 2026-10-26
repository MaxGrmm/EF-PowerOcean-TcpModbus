"""Pure unit tests for the EnergyProcessor lifetime-counter validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.ef_powerocean_tcpmodbus import const
from custom_components.ef_powerocean_tcpmodbus.energy_processor import (
    EnergyProcessor,
    HeldTotal,
    offsets_from,
)

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
    current: Mapping[str, float | None],
    previous_data: Mapping[str, float | None],
    previous_time: datetime | None,
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, float]:
    monkeypatch.setattr(dt_util, "now", lambda: now)
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
        datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
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
    now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

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
    now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

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
    now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

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
    now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

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
    now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

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
    now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
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
    start = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)
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
    current_data: Mapping[str, float | None],
    previous_data: Mapping[str, float | None],
    expected: Mapping[str, float | None],
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

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
    now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

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
    data: Mapping[str, float | None],
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, float], bool]:
    monkeypatch.setattr(dt_util, "now", lambda: now)
    return processor.derive_daily(data)


def test_derive_daily_initial_observation_starts_at_zero(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
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
    now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
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
        datetime(2026, 8, 7, 0, 1, tzinfo=UTC),
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
        datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
        monkeypatch,
    )

    assert "grid_import_today" not in result
    assert result["grid_import_today_raw"] == 5.0


def test_derive_daily_accrues_on_same_date(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    t1 = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    t2 = datetime(2026, 8, 7, 18, 0, tzinfo=UTC)

    derive_daily(processor, {"grid_import_total": 1000.0}, t1, monkeypatch)
    result, is_reset = derive_daily(
        processor, {"grid_import_total": 1002.5}, t2, monkeypatch
    )

    assert result["grid_import_today"] == 2.5
    assert not is_reset


def test_derive_daily_rolls_on_local_midnight_date_crossing(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    t1 = datetime(2026, 8, 7, 23, 59, 0, tzinfo=UTC)
    t2 = datetime(2026, 8, 8, 0, 0, 10, tzinfo=UTC)

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
        datetime(2026, 8, 7, 23, 59, tzinfo=UTC),
        monkeypatch,
    )
    result, is_reset = derive_daily(
        processor,
        {"grid_import_total": 1000.01, "grid_import_today_raw": 0.01},
        datetime(2026, 8, 8, 0, 0, 10, tzinfo=UTC),
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
        datetime(2026, 8, 7, 23, 59, tzinfo=UTC),
        monkeypatch,
    )
    missing_result, is_reset = derive_daily(
        processor,
        {"grid_import_today": 0.01, "grid_import_today_raw": 0.01},
        datetime(2026, 8, 8, 0, 0, 10, tzinfo=UTC),
        monkeypatch,
    )

    assert is_reset
    assert "grid_import_today" not in missing_result
    assert "grid_import_today" not in processor.daily_snapshots

    recovered_result, is_reset = derive_daily(
        processor,
        {"grid_import_total": 1000.02, "grid_import_today_raw": 0.02},
        datetime(2026, 8, 8, 0, 1, tzinfo=UTC),
        monkeypatch,
    )

    assert not is_reset
    assert recovered_result["grid_import_today"] == 0.02
    assert processor.daily_snapshots["grid_import_today"] == 1000.0


def test_a_damaged_saved_timestamp_is_treated_as_never_saved(
    processor: EnergyProcessor,
) -> None:
    accepted = datetime(2026, 8, 28, 12, tzinfo=UTC)
    processor.accepted_at = {"grid_import_total": accepted}
    processor.last_rollover = accepted
    saved = processor.dump_state()
    saved["energy_accepted_at"]["solar_total"] = "not a time"
    saved["energy_accepted_at"]["battery_charged_total"] = 12345
    saved["last_daily_rollover"] = "yesterday"

    restored = EnergyProcessor(LIMITS)
    restored.load_state(saved)

    assert restored.accepted_at == {"grid_import_total": accepted}
    assert restored.last_rollover is None


# ── #169: a bad first read and restored counters ─────────────────────────────

POLL = timedelta(seconds=5)
START = datetime(2026, 10, 10, 9, 31, tzinfo=UTC)

# (delay, lifetime counter) or (delay, lifetime counter, device daily counter)
Read = tuple[timedelta, float | None] | tuple[timedelta, float | None, float | None]


def poll_sequence(
    processor: EnergyProcessor,
    reads: Sequence[Read],
    monkeypatch: pytest.MonkeyPatch,
    *,
    baseline: float | None = None,
) -> list[float | None]:
    """Feed reads one after another, chaining the result like the coordinator."""
    previous_data: Mapping[str, float | None] = (
        {} if baseline is None else {"solar_total": baseline}
    )
    previous_time: datetime | None = None if baseline is None else START
    now = START
    published = []
    for delay, read, *daily in reads:
        now += delay
        frame = {"solar_total": read} | ({"solar_today": daily[0]} if daily else {})
        result = validate_totals(
            processor, frame, previous_data, previous_time, now, monkeypatch
        )
        previous_data, previous_time = result, now
        published.append(result.get("solar_total"))
    return published


def test_a_first_read_of_zero_is_shown_but_not_trusted(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = poll_sequence(processor, [(POLL, 0.0)], monkeypatch)

    assert published == [0.0]
    assert "solar_total" not in processor.accepted_at


def test_the_real_value_after_a_bogus_first_zero_is_taken(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FFreund3: a 0 on the first poll, then the device's real 4,296.8 kWh.
    # Before, the 0 became the baseline and the real value was held for days;
    # now it is taken as soon as the next read follows it as a counter.
    published = poll_sequence(
        processor,
        [(POLL, 0.0), (POLL, 0.0), (POLL, 4296.8), (POLL, 4296.81)],
        monkeypatch,
    )

    assert published == [0.0, 0.0, 0.0, 4296.81]
    assert processor.accepted_at["solar_total"] == START + 4 * POLL


def test_a_spike_after_a_bogus_first_zero_is_not_taken(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Taken unchecked, the spike would hold every true read after it as a drop.
    published = poll_sequence(
        processor,
        [(POLL, 0.0), (POLL, 99999.0), (POLL, 4296.8), (POLL, 4296.81)],
        monkeypatch,
    )

    assert published == [0.0, 0.0, 0.0, 4296.81]


def test_once_taken_the_counter_is_validated_as_before(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = poll_sequence(
        processor,
        [
            (POLL, 0.0),
            (POLL, 4296.8),
            (POLL, 4296.8),
            (POLL, 0.0),
            (POLL, 99999.0),
            (POLL, 4296.81),
        ],
        monkeypatch,
    )

    assert published == [0.0, 0.0, 4296.8, 4296.8, 4296.8, 4296.81]


def test_a_genuine_zero_counter_stays_zero_until_it_counts(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Export on a system with feed-in limited to 0 W: 0 for good, then a first
    # 0.01 kWh once it does export.
    published = poll_sequence(
        processor, [(POLL, 0.0)] * 5 + [(POLL, 0.01), (POLL, 0.02)], monkeypatch
    )

    assert published == [0.0] * 5 + [0.0, 0.02]


def test_an_unconfirmed_zero_never_takes_a_negative_read(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = poll_sequence(
        processor, [(POLL, 0.0), (POLL, -5.0), (POLL, 12.0), (POLL, 12.0)], monkeypatch
    )

    assert published == [0.0, 0.0, 0.0, 12.0]


def test_a_zero_with_an_acceptance_time_is_validated_as_before(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A working system whose counter is genuinely 0 already has an acceptance
    # time, so a spike is held exactly as it is today.
    processor.accepted_at = {"solar_total": START}
    published = poll_sequence(
        processor,
        [(POLL, 99999.0), (POLL, 0.0), (POLL, 0.01)],
        monkeypatch,
        baseline=0.0,
    )

    assert published == [0.0, 0.0, 0.01]


def test_taking_the_real_value_rebuilds_today_from_the_device(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The day started from the bogus 0; without a rebuild, today would show the
    # whole lifetime total.
    poll_sequence(processor, [(POLL, 0.0)], monkeypatch)
    processor.derive_daily({"solar_total": 0.0, "solar_today_raw": 0.0})
    assert processor.daily_snapshots == {"solar_today": 0.0}

    poll_sequence(
        processor, [(POLL, 4296.8), (POLL, 4296.8)], monkeypatch, baseline=0.0
    )
    derived, _ = processor.derive_daily(
        {"solar_total": 4296.8, "solar_today_raw": 12.5}
    )

    assert derived["solar_today"] == 12.5


def test_a_restored_counter_without_acceptance_time_is_anchored(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A state file from 2.4.0 has a baseline but no acceptance time. An empty
    # first read after ten minutes down, then what was gained meanwhile: before,
    # the window fell back to the previous poll and this was held for good.
    published = poll_sequence(
        processor,
        [(timedelta(minutes=10), None), (POLL, 1000.8), (POLL, 1000.81)],
        monkeypatch,
        baseline=1000.0,
    )

    assert published == [1000.0, 1000.8, 1000.81]


def test_a_restored_counter_held_on_its_first_poll_is_not_held_forever(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2,516 kWh over the baseline at 12 kW fits once 210 hours have passed.
    reads: list[Read] = [(POLL, 3516.0)]
    reads += [(timedelta(hours=1), 3516.0)] * 240
    published = poll_sequence(processor, reads, monkeypatch, baseline=1000.0)

    assert published[:200] == [1000.0] * 200
    assert published[-1] == 3516.0


RESTORED = {
    "missing-first-read": [(timedelta(minutes=10), None), (POLL, 1000.8)],
    "held-then-fits": [(POLL, 1500.0)] + [(timedelta(hours=1), 1500.0)] * 50,
    "decrease": [(POLL, 500.0), (timedelta(days=30), 900.0)],
    "spike": [(POLL, 1000.01), (POLL, 99999.0), (POLL, 1000.02)],
}


@pytest.mark.parametrize("reads", RESTORED.values(), ids=RESTORED.keys())
def test_a_restored_counter_behaves_as_if_its_acceptance_time_was_saved(
    monkeypatch: pytest.MonkeyPatch, reads: list[Read]
) -> None:
    restored = EnergyProcessor(LIMITS)
    saved = EnergyProcessor(LIMITS)
    saved.accepted_at = {"solar_total": START}

    assert poll_sequence(
        restored, reads, monkeypatch, baseline=1000.0
    ) == poll_sequence(saved, reads, monkeypatch, baseline=1000.0)


def test_an_existing_acceptance_time_is_kept(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepted = START - timedelta(hours=3)
    processor.accepted_at = {"solar_total": accepted}
    poll_sequence(processor, [(POLL, 99999.0)], monkeypatch, baseline=1000.0)

    assert processor.accepted_at["solar_total"] == accepted


def test_raw_totals_echo_every_device_lifetime_counter() -> None:
    raw = {"solar_total": 4296.8, "solar_today": 35.78, "grid_export_total": None}

    values = EnergyProcessor.raw_total_values(raw)

    assert values["solar_total_raw"] == 4296.8
    assert values["grid_export_total_raw"] is None
    assert "solar_today_raw" not in values
    assert "house_energy_total_raw" not in values
    assert set(values) == {f"{key}_raw" for key in const.DEVICE_ENERGY_KEYS} - {
        f"{s.key}_raw" for s in const.ENERGY_SENSOR_MAP if s.resets_daily
    }


# ── #169: a 0 baseline accepted by an earlier version, and long holds ────────

STEP = 0.02  # kWh a poll, within the 12 kW rise budget
TAKEN_AT = 50  # reads after the run's first until the daily counter rose a kWh


def counting(
    start: float,
    reads: int,
    *,
    daily: float = 5.0,
    scale: float = 1.0,
    step: float = STEP,
) -> list[Read]:
    """A device counting step a read, its daily counter rising alongside."""
    return [
        (POLL, round(start + scale * step * n, 3), round(daily + step * n, 3))
        for n in range(reads)
    ]


@pytest.fixture
def old_zero(processor: EnergyProcessor) -> EnergyProcessor:
    """A 0 baseline accepted by an earlier version, with its acceptance time."""
    processor.accepted_at = {"solar_total": START}
    return processor


def test_an_old_zero_gives_way_once_the_daily_counter_confirms(
    old_zero: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FFreund3 on an existing install: held at 0 against 4,296.8 kWh.
    reads = counting(4296.8, 60)
    published = poll_sequence(old_zero, reads, monkeypatch, baseline=0.0)

    assert published[:TAKEN_AT] == [0.0] * TAKEN_AT
    assert published[TAKEN_AT:] == [read[1] for read in reads[TAKEN_AT:]]
    assert "solar_total" not in old_zero.held


REFUSED: dict[str, Sequence[Read]] = {
    "repeating-garbage": [(POLL, 65535.0, 5.0)] * 200,
    "spikes-between-reads": [
        (POLL, value, 5.0)
        for _ in range(50)
        for value in (4296.8, 99999.0, None, 4296.81)
    ],
    "misscaled-by-a-tenth": counting(4296.8, 200, scale=1.1),
    "misscaled-tenfold-at-low-flow": counting(4296.8, 1200, scale=10.0, step=0.001),
    "no-daily-counter": [(POLL, round(4296.8 + STEP * n, 2)) for n in range(200)],
    "daily-counter-stuck": [
        (POLL, round(4296.8 + STEP * n, 2), 5.0) for n in range(200)
    ],
}


@pytest.mark.parametrize("reads", REFUSED.values(), ids=REFUSED.keys())
def test_an_old_zero_refuses_what_is_not_a_counter(
    old_zero: EnergyProcessor,
    monkeypatch: pytest.MonkeyPatch,
    reads: Sequence[Read],
) -> None:
    published = poll_sequence(old_zero, reads, monkeypatch, baseline=0.0)

    assert published == [0.0] * len(reads)


def test_a_genuine_zero_with_an_acceptance_time_is_left_alone(
    old_zero: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = poll_sequence(
        old_zero, [(POLL, 0.0, 0.0)] * 100, monkeypatch, baseline=0.0
    )

    assert published == [0.0] * 100
    assert old_zero.held == {}


def test_a_non_zero_baseline_is_never_overridden_by_the_daily_counter(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unchanged: a held rise from a real baseline waits for the rise budget.
    processor.accepted_at = {"solar_total": START}
    published = poll_sequence(
        processor, counting(4296.8, 120), monkeypatch, baseline=1000.0
    )

    assert published == [1000.0] * 120


def test_a_held_total_is_tracked_until_it_is_accepted(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepted = START - timedelta(days=30)
    processor.accepted_at = {"grid_export_total": accepted}
    previous = {"grid_export_total": 1548.73}

    validate_totals(
        processor,
        {"grid_export_total": 507.5},
        previous,
        START,
        START + POLL,
        monkeypatch,
    )
    assert processor.held["grid_export_total"] == HeldTotal(1548.73, 507.5, accepted)

    validate_totals(
        processor,
        {"grid_export_total": 1548.74},
        previous,
        START,
        START + POLL,
        monkeypatch,
    )
    assert processor.held == {}


def test_taking_device_totals_accepts_them_and_rebuilds_today(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor.accepted_at = {"grid_export_total": START - timedelta(days=30)}
    processor.daily_snapshots = {"grid_export_today": 1540.0}
    validate_totals(
        processor,
        {"grid_export_total": 507.5},
        {"grid_export_total": 1548.73},
        START,
        START + POLL,
        monkeypatch,
    )

    taken = processor.take_device_totals(["grid_export_total", "solar_total"])

    assert taken == {"grid_export_total": 507.5}
    assert processor.accepted_at["grid_export_total"] == START + POLL
    assert processor.held == {}
    assert "grid_export_today" not in processor.daily_snapshots


def test_a_device_value_of_zero_is_never_taken(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor.accepted_at = {"grid_export_total": START - timedelta(days=30)}
    validate_totals(
        processor,
        {"grid_export_total": 0.0},
        {"grid_export_total": 1548.73},
        START,
        START + POLL,
        monkeypatch,
    )

    assert processor.take_device_totals(["grid_export_total"]) == {}


# ── Offsets for a counter the inverter reset ─────────────────────────────────


def corrected_poll(
    processor: EnergyProcessor,
    device: Mapping[str, float | None],
    previous_data: Mapping[str, float | None],
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, float]:
    """Validate a read with the offsets applied and echo the device's totals."""
    result = validate_totals(
        processor,
        processor.apply_offsets(device),
        previous_data,
        now - POLL,
        now,
        monkeypatch,
    )
    return result | processor.raw_total_values(device)


def test_an_offset_is_added_to_real_reads_only(processor: EnergyProcessor) -> None:
    processor.offsets = {"grid_export_total": 1041.23, "solar_total": 10.0}

    corrected = processor.apply_offsets(
        {"grid_export_total": 507.5, "solar_total": 0.0, "grid_import_total": 80.0}
    )

    assert corrected["grid_export_total"] == pytest.approx(1548.73)
    # A bogus 0 stays 0, for the validation to hold back (#169).
    assert corrected["solar_total"] == 0.0
    assert corrected["grid_import_total"] == 80.0


def test_without_offsets_reads_pass_through_unchanged(
    processor: EnergyProcessor,
) -> None:
    raw = {"grid_export_total": 507.5, "solar_total": None}

    assert processor.apply_offsets(raw) == raw


def test_a_held_total_keeps_its_published_value(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repair's choice: the offset is worked out from the held total."""
    processor.accepted_at = {"grid_export_total": START - timedelta(days=2)}
    processor.daily_snapshots = {"grid_export_today": 1540.0}
    now = START + POLL
    last = corrected_poll(
        processor,
        {"grid_export_total": 507.5},
        {"grid_export_total": 1548.73},
        now,
        monkeypatch,
    )
    held = processor.held["grid_export_total"]

    last = processor.change_offsets(
        {"grid_export_total": held.published - held.device}, last
    )
    now += POLL
    result = corrected_poll(
        processor, {"grid_export_total": 507.52}, last, now, monkeypatch
    )

    assert result["grid_export_total"] == pytest.approx(1548.75)
    assert result["grid_export_total_raw"] == 507.52
    assert processor.held == {}
    assert processor.daily_snapshots["grid_export_today"] == pytest.approx(1540.0)


def test_a_total_set_to_a_new_value_keeps_todays_energy(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settings' choice: the user enters the value from EcoFlow's portal."""
    processor.accepted_at = {"solar_total": START}
    processor.daily_snapshots = {"solar_today": 4290.0}
    now = START + POLL
    last = corrected_poll(
        processor, {"solar_total": 4296.8}, {"solar_total": 4296.8}, now, monkeypatch
    )

    last = processor.change_offsets({"solar_total": 4400.0 - 4296.8}, last)
    now += POLL
    result = corrected_poll(processor, {"solar_total": 4296.8}, last, now, monkeypatch)
    daily, _ = derive_daily(processor, result, now, monkeypatch)

    assert result["solar_total"] == pytest.approx(4400.0)
    assert processor.held == {}
    assert daily["solar_today"] == pytest.approx(6.8)


def test_removing_an_offset_returns_the_total_to_the_device_value(
    processor: EnergyProcessor, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor.offsets = {"solar_total": 100.0}
    processor.accepted_at = {"solar_total": START}
    now = START + POLL
    last = corrected_poll(
        processor, {"solar_total": 50.0}, {"solar_total": 150.0}, now, monkeypatch
    )

    last = processor.change_offsets({}, last)
    result = corrected_poll(
        processor, {"solar_total": 50.0}, last, now + POLL, monkeypatch
    )

    assert result["solar_total"] == pytest.approx(50.0)
    assert processor.held == {}


def test_unchanged_offsets_leave_the_totals_alone(processor: EnergyProcessor) -> None:
    processor.offsets = {"solar_total": 100.0}
    last = {"solar_total": 150.0, "solar_total_raw": 49.0}

    assert processor.change_offsets({"solar_total": 100.0}, last) == last
    assert processor.accepted_at == {}


def test_offsets_are_saved_with_the_state_they_are_part_of(
    processor: EnergyProcessor,
) -> None:
    processor.offsets = {"grid_export_total": 1041.23}
    restored = EnergyProcessor(LIMITS)

    restored.load_state(processor.dump_state())

    assert restored.offsets == {"grid_export_total": 1041.23}


@pytest.mark.parametrize(
    "stored",
    (
        None,
        [1.0],
        {"house_energy_total": 5.0},
        {"solar_today": 5.0},
        {"solar_total": "5"},
        {"solar_total": float("nan")},
        {"solar_total": True},
        {"solar_total": 0},
    ),
)
def test_a_damaged_offset_corrects_nothing(stored: object) -> None:
    assert offsets_from(stored) == {}
