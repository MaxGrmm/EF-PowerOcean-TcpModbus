"""Checks of each poll's readings, and control stepping aside on them (#32, #200)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import count
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from custom_components.ef_powerocean_tcpmodbus import const, models, reading_checks
from custom_components.ef_powerocean_tcpmodbus import coordinator as coordinator_module
from custom_components.ef_powerocean_tcpmodbus.reading_checks import Finding
from tests.simulation import Simulation
from tests.test_coordinator import coordinator, issues  # noqa: F401 - fixtures

Status = models.ControlStatus
T0 = datetime(2026, 10, 28, 16, 14, tzinfo=UTC)
_jitter = count()


def _live() -> dict[str, float]:
    """Voltages and frequency as a live grid reports them, never twice the same."""
    step = next(_jitter) % 7
    return {
        "frequency": 50.0 + step / 100,
        "voltage_l1": 230.0 + step / 10,
        "grid_voltage_l1": 231.0 - step / 10,
        "battery_voltage": 410.0,
    }


# ── The checks on their own ───────────────────────────────────────────────────


def _socs(system: float | None, *packs: float, count: int = 2) -> dict[str, Any]:
    packs_by_key = dict(zip(const.BATTERY_SOC_KEYS, packs, strict=False))
    return {"battery_soc": system, "battery_count": count, **packs_by_key}


@pytest.mark.parametrize(
    ("data", "fails"),
    (
        (_socs(0, 99, 99), True),
        # Battery 1 glitched to 0 as well in the report.
        (_socs(0, 0, 99), True),
        # Really empty.
        (_socs(0, 0, 1), False),
        # Nothing to compare with.
        ({"battery_soc": 0}, False),
        # A slot beyond the battery count is not a pack in use.
        (_socs(0, 0, 80, count=1), False),
        (_socs(57, 99, 99), False),
        (_socs(None, 99, 99), False),
    ),
)
def test_a_zero_system_soc_fails_only_where_a_pack_contradicts_it(
    data: dict[str, Any], fails: bool
) -> None:
    assert reading_checks.SystemSocMismatch().check(data, T0) is fails


def test_readings_fail_once_they_have_not_moved_for_two_minutes() -> None:
    check = reading_checks.FrozenReadings()
    frame = _live()

    at = [T0 + timedelta(seconds=5 * poll) for poll in range(30)]
    results = [check.check(frame, now) for now in at]

    assert not any(results[:24])
    assert all(results[24:])
    # One reading moving again clears it at once.
    assert not check.check({**frame, "frequency": 49.98}, at[-1])


def test_a_model_without_these_readings_is_never_frozen() -> None:
    check = reading_checks.FrozenReadings()
    assert not any(
        check.check({"frequency": None, "voltage_l1": 0}, T0 + timedelta(minutes=m))
        for m in range(10)
    )


def test_a_lost_grid_voltage_fails_only_after_a_grid_was_seen() -> None:
    check = reading_checks.GridVoltageLost()
    gone = {"grid_voltage_l1": 0.0, "grid_voltage_l2": 0.0, "grid_voltage_l3": 0.0}

    # A model that reads these as 0 throughout never fires.
    assert not check.check(gone, T0)
    # Single phase: L2 and L3 read 0 while L1 carries the grid.
    assert not check.check({**gone, "grid_voltage_l1": 231.0}, T0)
    assert check.check(gone, T0)
    assert not check.check({}, T0)


def _modes(*, off_grid: bool = False, bms: bool = True) -> dict[str, float]:
    return {"system_modes": float(off_grid | bms << 12)}


def test_off_grid_is_read_from_the_status_bits() -> None:
    check = reading_checks.OffGridReported()
    assert check.check(_modes(off_grid=True), T0)
    assert not check.check(_modes(), T0)
    assert not check.check({}, T0)


def test_a_battery_is_only_missing_once_one_was_seen() -> None:
    check = reading_checks.BatteryDisconnected()
    assert not check.check(_modes(bms=False), T0)
    assert not check.check(_modes(bms=True), T0)
    assert check.check(_modes(bms=False), T0)


def test_a_failure_counts_after_three_polls_and_keeps_its_start() -> None:
    checks = reading_checks.ReadingChecks()
    at = [T0 + timedelta(seconds=5 * poll) for poll in range(5)]

    checks.run(_modes(off_grid=True), at[0])
    checks.run(_modes(off_grid=True), at[1])
    assert checks.active is None
    checks.run(_modes(off_grid=True), at[2])
    checks.run(_modes(off_grid=True), at[3])
    # Since the first poll that failed, not the one that confirmed it.
    assert checks.active == Finding(Status.OFF_GRID, "off_grid", at[0])

    checks.run(_modes(), at[4])
    assert checks.active is None


def test_only_contradicting_readings_keep_a_frame_from_the_guards() -> None:
    checks = reading_checks.ReadingChecks()
    checks.run(_modes(off_grid=True), T0)
    assert not checks.suspect
    checks.run({**_socs(0, 99, 99), **_modes()}, T0)
    assert checks.suspect


def test_every_key_a_check_reads_is_kept_polled() -> None:
    assert {"soc_battery_1", "battery_count", "frequency", "grid_voltage_l1"} <= (
        reading_checks.KEYS
    )
    assert const.REGISTERS_BY_KEY.keys() >= reading_checks.KEYS


def test_a_check_that_keeps_failing_holds_while_one_above_it_flickers() -> None:
    """An outage the off-grid bit only reports now and then is still an outage."""
    checks = reading_checks.ReadingChecks()
    checks.run({"grid_voltage_l1": 231.0, **_modes()}, T0)
    for poll in range(1, const.CONTROL_STATUS_DAMPING_POLLS + 3):
        checks.run(
            {"grid_voltage_l1": 0.0, **_modes(off_grid=poll % 2 == 1)},
            T0 + timedelta(seconds=5 * poll),
        )

    assert checks.active is not None
    assert checks.active.status is Status.OFF_GRID


def test_a_failure_ends_as_soon_as_its_check_passes() -> None:
    checks = reading_checks.ReadingChecks()
    frame = _live()
    for poll in range(30):
        checks.run(frame, T0 + timedelta(seconds=5 * poll))
    assert checks.active is not None
    assert checks.active.check == "frozen"

    # Moving again, with one odd SOC read that has not held.
    checks.run({**_live(), **_socs(0, 99, 99)}, T0 + timedelta(seconds=150))

    assert checks.active is None
    assert checks.suspect


def test_frozen_readings_count_from_when_they_stopped_moving() -> None:
    checks = reading_checks.ReadingChecks()
    frame = _live()
    at = [T0 + timedelta(seconds=5 * poll) for poll in range(30)]
    for now in at:
        checks.run(frame, now)

    assert checks.active == Finding(Status.INCONSISTENT_READINGS, "frozen", at[0])


def test_another_cause_for_the_same_state_keeps_its_start() -> None:
    checks = reading_checks.ReadingChecks()
    at = [T0 + timedelta(seconds=5 * poll) for poll in range(8)]
    checks.run({"grid_voltage_l1": 231.0, **_modes()}, at[0])
    for now in at[1:4]:
        checks.run({"grid_voltage_l1": 0.0, **_modes()}, now)
    assert checks.active == Finding(Status.OFF_GRID, "grid_voltage", at[1])

    for now in at[4:]:
        checks.run({"grid_voltage_l1": 0.0, **_modes(off_grid=True)}, now)

    assert checks.active == Finding(Status.OFF_GRID, "off_grid", at[1])


def test_a_battery_running_empty_is_not_contradicted() -> None:
    """Packs may stay a few percent above a system level that reaches 0."""
    check = reading_checks.SystemSocMismatch()
    levels = (30, 12, 6, 3, 1, 0, 0, 0)
    assert not any(check.check(_socs(soc, 9, 7), T0) for soc in levels)


def test_a_zero_soc_stays_contradicted_for_as_long_as_it_lasts() -> None:
    check = reading_checks.SystemSocMismatch()
    assert not check.check(_socs(60, 61, 59), T0)
    assert all(check.check(_socs(0, 61, 59), T0) for _ in range(360))
    assert not check.check(_socs(60, 61, 59), T0)


def test_no_battery_is_never_contradicted() -> None:
    assert not reading_checks.SystemSocMismatch().check(_socs(0, 80, count=0), T0)


def test_a_check_that_breaks_passes_and_leaves_the_others_running(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*_: Any) -> bool:
        raise ValueError("odd reading")

    monkeypatch.setattr(reading_checks.SystemSocMismatch, "check", broken)
    checks = reading_checks.ReadingChecks()
    for poll in range(const.CONTROL_STATUS_DAMPING_POLLS):
        checks.run(
            {**_socs(0, 99, 99), **_modes(off_grid=True)},
            T0 + timedelta(seconds=5 * poll),
        )

    assert checks.active is not None
    assert checks.active.check == "off_grid"
    assert not checks.suspect
    # Logged once, not on every poll.
    assert caplog.text.count("battery_soc reading check could not run") == 1


def test_what_the_model_reports_survives_a_restart() -> None:
    checks = reading_checks.ReadingChecks()
    checks.run({"grid_voltage_l1": 231.0, **_modes()}, T0)
    stored = checks.dump_state()
    assert stored == {"grid_seen": True, "bms_seen": True}

    restarted = reading_checks.ReadingChecks()
    restarted.load_state(stored)
    for poll in range(const.CONTROL_STATUS_DAMPING_POLLS):
        restarted.run(
            {"grid_voltage_l1": 0.0, **_modes(bms=False)},
            T0 + timedelta(seconds=5 * poll),
        )

    assert restarted.active is not None
    assert restarted.active.check == "grid_voltage"


# ── What control does ─────────────────────────────────────────────────────────


def _simulate(
    monkeypatch: pytest.MonkeyPatch,
    alter: Callable[[dict[str, Any]], None] = lambda _: None,
    **kwargs: Any,
) -> tuple[Simulation, Callable[[Callable[[dict[str, Any]], None]], None]]:
    """A full battery and a reserve, with the frame altered as *alter* says."""
    sim = Simulation(
        monkeypatch,
        soc=99.0,
        reserve=20.0,
        model=models.InverterModel.POWEROCEAN_SINGLE_PHASE,
        **kwargs,
    )
    real = sim.inverter.frame
    state = {"alter": alter}

    def frame() -> dict[str, Any]:
        data: dict[str, Any] = {**real(), **_live(), "battery_count": 2}
        data["soc_battery_1"] = data["soc_battery_2"] = data["battery_soc"]
        state["alter"](data)
        return data

    sim.inverter.frame = frame

    def set_alter(alter: Callable[[dict[str, Any]], None]) -> None:
        state["alter"] = alter

    return sim, set_alter


def _zero_soc(data: dict[str, Any]) -> None:
    data["battery_soc"] = 0.0


def test_a_contradicted_zero_soc_steps_aside_instead_of_holding_the_battery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim, alter = _simulate(monkeypatch)
    sim.run(polls=24, solar=0, house=1500)

    alter(_zero_soc)
    episode = sim.run(polls=420, solar=0, house=1500)  # 35 minutes

    # The battery keeps carrying the house; the reserve never takes hold.
    assert max(episode.grid) < 50
    assert Status.RESERVE_REACHED not in episode.status
    assert episode.status[-1] is Status.INCONSISTENT_READINGS
    aside = sim.control.reading_checks.active
    assert aside is not None
    assert (aside.status, aside.check) == (Status.INCONSISTENT_READINGS, "battery_soc")

    alter(lambda _: None)
    after = sim.run(polls=12, solar=0, house=1500)
    assert after.status[-1] is Status.AUTOMATIC
    assert sim.control.reading_checks.active is None


def test_a_single_bad_read_neither_steps_aside_nor_latches_the_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim, alter = _simulate(monkeypatch)
    sim.run(polls=24, solar=0, house=1500)

    alter(_zero_soc)
    blip = sim.run(polls=1, solar=0, house=1500)
    alter(lambda _: None)
    after = sim.run(polls=12, solar=0, house=1500)

    assert set(blip.status + after.status) == {Status.AUTOMATIC}
    assert max(blip.grid + after.grid) < 50


def test_frozen_readings_step_aside(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = _live()
    sim, alter = _simulate(monkeypatch)
    sim.run(polls=24, solar=0, house=1500)

    alter(lambda data: data.update(frozen))
    run = sim.run(polls=36, solar=0, house=1500)

    assert run.status[-1] is Status.INCONSISTENT_READINGS
    aside = sim.control.reading_checks.active
    assert aside is not None
    assert aside.check == "frozen"


def test_a_lost_grid_voltage_steps_aside_as_off_grid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim, alter = _simulate(monkeypatch)
    sim.run(polls=24, solar=0, house=1500)

    alter(lambda data: data.update(grid_voltage_l1=0.0))
    run = sim.run(polls=6, solar=0, house=1500)

    assert run.status[-1] is Status.OFF_GRID
    aside = sim.control.reading_checks.active
    assert aside is not None
    assert aside.check == "grid_voltage"


def test_the_checks_run_while_modbus_control_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim, alter = _simulate(monkeypatch)
    alter(_zero_soc)
    sim.control._enabled = False

    run = sim.run(polls=12, solar=0, house=1500)

    active = sim.control.reading_checks.active
    assert active is not None
    assert active.check == "battery_soc"
    # Control is off, so there is nothing to step aside from.
    assert Status.INCONSISTENT_READINGS not in run.status


# ── Events and the repairs issue ──────────────────────────────────────────────


@pytest.fixture
def tracked(coordinator, issues, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    monkeypatch.setattr(
        dr, "async_entries_for_config_entry", lambda *_: [SimpleNamespace(id="device")]
    )
    monkeypatch.setattr(dr, "async_get", lambda _: None)
    clock = {"now": T0}
    monkeypatch.setattr(dt_util, "now", lambda *_: clock["now"])
    checks = SimpleNamespace(active=None)
    coordinator.control = SimpleNamespace(reading_checks=checks)

    def poll(finding: Finding | None, minutes: float) -> None:
        checks.active = finding
        clock["now"] = T0 + timedelta(minutes=minutes)
        coordinator._async_track_reading_checks()

    return SimpleNamespace(
        poll=poll, issues=issues, fire=coordinator.hass.bus.async_fire
    )


def _fired(tracked) -> list[tuple[str, str, str | None]]:
    return [
        (call.args[0], call.args[1]["status"], call.args[1]["check"])
        for call in tracked.fire.call_args_list
    ]


def test_a_failed_check_is_announced_when_it_starts_and_when_it_ends(tracked) -> None:
    finding = Finding(Status.OFF_GRID, "off_grid", T0)

    tracked.poll(finding, 0)
    tracked.poll(finding, 1)
    tracked.poll(None, 2)

    assert _fired(tracked) == [
        (const.EVENT_READING_CHECK_FAILED, "off_grid", "off_grid"),
        (const.EVENT_READING_CHECK_CLEARED, "off_grid", "off_grid"),
    ]


@pytest.mark.parametrize("check", [check.name for check in reading_checks.CHECKS])
def test_every_failed_check_raises_an_issue_after_ten_minutes_that_clears_itself(
    tracked, check: str
) -> None:
    finding = Finding(Status.INCONSISTENT_READINGS, check, T0)

    for minute in range(10):
        tracked.poll(finding, minute)
    tracked.issues.created.assert_not_called()

    tracked.poll(finding, 10)
    tracked.poll(finding, 11)
    tracked.issues.created.assert_called_once()
    kwargs = tracked.issues.created.call_args.kwargs
    assert kwargs["translation_key"] == f"reading_check_{check}"
    assert kwargs["is_fixable"] is False

    tracked.poll(None, 35)
    tracked.issues.deleted.assert_called_once()
    assert tracked.issues.deleted.call_args.args[2] == "reading_check_entry"


def test_another_cause_for_the_same_state_is_not_announced_again(tracked) -> None:
    tracked.poll(Finding(Status.INCONSISTENT_READINGS, "battery_soc", T0), 12)
    tracked.poll(Finding(Status.INCONSISTENT_READINGS, "frozen", T0), 13)

    # The issue stays up, now naming the new cause.
    assert tracked.issues.created.call_count == 2
    assert tracked.issues.created.call_args.kwargs["translation_key"] == (
        "reading_check_frozen"
    )
    tracked.issues.deleted.assert_not_called()
    assert _fired(tracked) == [
        (const.EVENT_READING_CHECK_FAILED, "inconsistent_readings", "battery_soc")
    ]


def test_another_state_starts_over(tracked) -> None:
    tracked.poll(Finding(Status.INCONSISTENT_READINGS, "battery_soc", T0), 12)
    later = T0 + timedelta(minutes=12)
    tracked.poll(Finding(Status.OFF_GRID, "off_grid", later), 13)

    # The first issue goes; the new state gets its own after its 10 minutes.
    tracked.issues.created.assert_called_once()
    tracked.issues.deleted.assert_called_once()
    assert [event for event, _, _ in _fired(tracked)] == [
        const.EVENT_READING_CHECK_FAILED,
        const.EVENT_READING_CHECK_CLEARED,
        const.EVENT_READING_CHECK_FAILED,
    ]


def test_every_check_that_raises_an_issue_has_its_text() -> None:
    import json
    from pathlib import Path

    folder = Path(coordinator_module.__file__).parent / "translations"
    for language in ("en", "de"):
        strings = json.loads((folder / f"{language}.json").read_text())
        for check in reading_checks.CHECKS:
            assert f"reading_check_{check.name}" in strings["issues"]
        states = strings["entity"]["sensor"]["control_status"]["state"]
        assert "inconsistent_readings" in states


def test_an_error_announcing_a_step_aside_never_fails_the_poll(
    coordinator,  # noqa: F811
) -> None:
    coordinator.control = SimpleNamespace(
        reading_checks=SimpleNamespace(active=Finding(Status.OFF_GRID, "off_grid", T0))
    )
    coordinator.hass.bus.async_fire.side_effect = RuntimeError("bus gone")

    coordinator._async_track_reading_checks_safely()
