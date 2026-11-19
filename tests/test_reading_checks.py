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


# Polls 5 s apart, from the first to the one five minutes later.
FROZEN_POLLS = 61


def _polls(n: int = FROZEN_POLLS + 5) -> list[datetime]:
    return [T0 + timedelta(seconds=5 * poll) for poll in range(n)]


def test_readings_fail_once_they_have_not_moved_for_five_minutes() -> None:
    check = reading_checks.FrozenReadings()
    frame = _live()

    results = [check.check(frame, now) for now in _polls()]

    assert not any(results[: FROZEN_POLLS - 1])
    assert all(results[FROZEN_POLLS - 1 :])
    assert check.distrusted() == frozenset(frame)


def test_one_reading_that_still_moves_does_not_hide_the_frozen_ones() -> None:
    """In #227 most readings stood still for days; one moving must not clear it."""
    check = reading_checks.FrozenReadings()
    frame = _live()
    at = _polls()
    for now in at:
        check.check(frame, now)

    assert check.check({**frame, "frequency": 49.98}, at[-1])
    assert check.distrusted() == frozenset(frame) - {"frequency"}


def test_one_reading_a_model_rounds_coarsely_never_fails_it_alone() -> None:
    check = reading_checks.FrozenReadings()
    results = [check.check(_live(), now) for now in _polls()]

    # _live() keeps the battery voltage at 410.0 throughout.
    assert not any(results)


def test_a_model_without_these_readings_is_never_frozen() -> None:
    check = reading_checks.FrozenReadings()
    assert not any(
        check.check({"frequency": None, "voltage_l1": 0}, T0 + timedelta(minutes=m))
        for m in range(10)
    )


# The supply counters standing still for 20 minutes, 240 polls 5 s apart.
STALLED_POLLS = 241


def test_counters_fail_once_none_that_supplies_the_house_moved_for_20_minutes() -> None:
    check = reading_checks.CounterStalled()
    frame = {"solar_total": 20719.85, "grid_import_total": 11579.97}

    results = [check.check(frame, now) for now in _polls(STALLED_POLLS + 5)]

    assert not any(results[: STALLED_POLLS - 1])
    assert all(results[STALLED_POLLS - 1 :])
    # One of them moving again clears it at once.
    later = T0 + timedelta(hours=1)
    assert not check.check({**frame, "grid_import_total": 11579.98}, later)


@pytest.mark.parametrize(
    ("frame", "fails"),
    (
        # At night only the grid or the battery supplies the house.
        ({"solar_total": 5.0, "bat_discharged_total": "moves"}, False),
        ({"solar_total": 5.0, "grid_import_total": "moves"}, False),
        # Export and charging do not supply the house, so they prove nothing.
        ({"solar_total": 5.0, "grid_export_total": "moves"}, True),
        ({"solar_total": 5.0, "bat_charged_total": "moves"}, True),
        # The device's own count is judged, not a total held back from it.
        ({"solar_total": 5.0, "solar_total_raw": "moves"}, False),
        # A model that reports no supply counter is not judged by them.
        ({"grid_export_total": 5.0}, False),
        ({"solar_total": None}, False),
    ),
)
def test_counters_are_judged_by_those_that_supply_the_house(
    frame: dict[str, Any], fails: bool
) -> None:
    check = reading_checks.CounterStalled()
    results = [
        check.check(
            {key: poll if value == "moves" else value for key, value in frame.items()},
            now,
        )
        for poll, now in enumerate(_polls(STALLED_POLLS))
    ]
    assert results[-1] is fails


def test_still_counters_alone_never_step_aside() -> None:
    """A model that counts late must not hand control back for it."""
    checks = reading_checks.ReadingChecks()
    for now in _polls(STALLED_POLLS + 5):
        checks.run({**_live(), "solar_total": 5.0}, now)

    assert "counter_stalled" in checks.failing
    assert checks.active is None
    assert not checks.suspect


def _stalled(solar: float) -> dict[str, Any]:
    """An inverter as in #227: only grid and solar power still move."""
    return {
        "frequency": 50.01,
        "voltage_l1": 232.03,
        "voltage_l2": 231.55,
        "battery_voltage": 53.57,
        "house_power": 538.4,
        "battery_power": 490.9,
        "pv1_voltage": 458.15,
        "pv1_current": 1.02,
        "battery_soc": 78.0,
        "solar_total": 20719.85,
        "grid_import_total": 11579.97,
        "solar_power": solar,
        "grid_power": -15.0 - solar / 100,
    }


def test_an_inverter_that_stopped_updating_distrusts_what_stands_still() -> None:
    checks = reading_checks.ReadingChecks()
    at = _polls(STALLED_POLLS + 5)
    for poll, now in enumerate(at):
        checks.run(_stalled(3600.0 + poll % 7), now)

    assert checks.active == Finding(
        Status.INCONSISTENT_READINGS, "telemetry_stalled", at[0]
    )
    assert checks.distrusted >= {
        "frequency",
        "voltage_l1",
        "house_power",
        "battery_power",
        "battery_soc",
        "pv1_voltage",
        # Calculated from what stands still.
        "pv1_power",
        "bat_remaining",
    }
    # Still moving, so still shown; the counters are shown as the device counts.
    assert not checks.distrusted & {"solar_power", "grid_power", "solar_total"}


def test_a_stalled_reading_at_0_is_distrusted_too() -> None:
    """An idle battery reads 0 W; stalled, that 0 is no more live than any other."""
    checks = reading_checks.ReadingChecks()
    for poll, now in enumerate(_polls(STALLED_POLLS + 5)):
        frame = {**_stalled(3600.0 + poll % 7), "battery_current": 0.0}
        checks.run(frame, now)

    assert "battery_current" in checks.distrusted


def test_frozen_voltages_alone_are_not_a_stall() -> None:
    checks = reading_checks.ReadingChecks()
    for poll, now in enumerate(_polls(STALLED_POLLS + 5)):
        # The voltages stand still, but the grid keeps supplying the house.
        frame = {**_stalled(3600.0 + poll % 7), "grid_import_total": now.timestamp()}
        checks.run(frame, now)

    assert checks.active is not None
    assert checks.active.check == "frozen"
    assert "house_power" not in checks.distrusted


def test_a_stall_is_caught_at_night_as_soon_as_by_day() -> None:
    """In #227 the night export was 20 W; the counters prove it all the same."""
    checks = reading_checks.ReadingChecks()
    at = _polls(STALLED_POLLS + 5)
    for poll, now in enumerate(at):
        checks.run({**_stalled(0.0), "grid_power": -15.0 - poll % 7}, now)

    assert checks.active is not None
    assert checks.active.check == "telemetry_stalled"


def test_readings_still_for_half_an_hour_prove_a_stall_without_counters() -> None:
    checks = reading_checks.ReadingChecks()
    # No counters at all; only the grid power from the meter still moves.
    frame = {k: v for k, v in _stalled(0.0).items() if not k.endswith("_total")}
    at = _polls(int(31 * 60 / 5))
    for poll, now in enumerate(at):
        checks.run({**frame, "grid_power": -15.0 - poll % 7}, now)
        if now - at[0] < timedelta(minutes=29):
            assert checks.active is None or checks.active.check == "frozen"

    assert checks.active is not None
    assert checks.active.check == "telemetry_stalled"
    assert "grid_power" not in checks.distrusted


def test_a_live_house_load_keeps_frozen_voltages_from_being_a_stall() -> None:
    checks = reading_checks.ReadingChecks()
    frame = {k: v for k, v in _stalled(0.0).items() if not k.endswith("_total")}
    for poll, now in enumerate(_polls(int(40 * 60 / 5))):
        # Most measured readings move: the house, the battery and the grid.
        checks.run(
            {
                **frame,
                "house_power": 500.0 + poll % 9,
                "battery_power": 400.0 + poll % 5,
                "pv1_current": 1.0 + poll % 3 / 100,
                "pv1_voltage": 450.0 + poll % 4,
                "battery_soc": 78.0 + poll % 2,
                "grid_power": -15.0 - poll % 7,
            },
            now,
        )

    assert checks.active is not None
    assert checks.active.check == "frozen"


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
    # Stalled telemetry judges what is polled anyway, so a disabled sensor such
    # as the inverter temperature is not read for it.
    assert "inverter_temperature" not in reading_checks.KEYS


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
    at = _polls()
    for now in at:
        checks.run(frame, now)
    assert checks.active is not None
    assert checks.active.check == "frozen"

    # Moving again, with one odd SOC read that has not held.
    checks.run({**_live(), **_socs(0, 99, 99)}, at[-1] + timedelta(seconds=5))

    assert checks.active is None
    assert checks.suspect


def test_frozen_readings_count_from_when_they_stopped_moving() -> None:
    checks = reading_checks.ReadingChecks()
    frame = _live()
    at = _polls()
    for now in at:
        checks.run(frame, now)

    assert checks.active == Finding(Status.INCONSISTENT_READINGS, "frozen", at[0])


def test_frozen_readings_and_what_is_calculated_from_them_are_distrusted() -> None:
    checks = reading_checks.ReadingChecks()
    frame = {**_live(), "pv1_voltage": 458.15, "pv1_current": 1.02}
    for now in _polls():
        checks.run(frame, now)

    # The PV readings are not jitter readings, but the calculated power follows
    # whichever of its sources is distrusted.
    assert {"frequency", "voltage_l1"} <= checks.distrusted
    assert "pv1_power" not in checks.distrusted
    assert reading_checks.with_derived(frozenset({"pv1_current"})) >= {"pv1_power"}


def test_a_finding_carries_its_readings_and_what_the_user_can_do() -> None:
    checks = reading_checks.ReadingChecks()
    for now in _polls():
        checks.run({**_live(), **_socs(0, 99, 99)}, now)

    soc = next(f for f in checks.findings if f.check == "battery_soc")
    assert soc.keys == {"battery_soc", "bat_remaining"}
    # Nothing the user can do about a bad read.
    assert soc.remedy is None
    assert reading_checks.TelemetryStalled.remedy == "telemetry_stalled"


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


def test_a_poll_with_every_pack_at_0_does_not_make_the_drop_believed() -> None:
    """#200 on 31 Oct: a pack read 0 alongside the system, then came back."""
    check = reading_checks.SystemSocMismatch()
    assert not check.check(_socs(100, 100, 99), T0)

    frames = (_socs(0, 0, 0), _socs(0, 100, 99), _socs(0, 100, 99))

    assert all(check.check(frame, T0) for frame in frames)
    assert not check.check(_socs(100, 100, 99), T0)


@pytest.mark.parametrize(
    "frame",
    (_socs(0, 0, 0), _socs(0, count=0), {"battery_soc": 0}),
    ids=("packs at 0", "no battery count", "no packs read"),
)
def test_a_drop_to_0_from_a_believed_level_fails_whatever_the_packs_say(
    frame: dict[str, Any],
) -> None:
    check = reading_checks.SystemSocMismatch()
    assert not check.check(_socs(57, 60, 55), T0)

    assert check.check(frame, T0)


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
    assert stored["grid_seen"] is True
    assert stored["bms_seen"] is True

    restarted = reading_checks.ReadingChecks()
    restarted.load_state(stored)
    for poll in range(const.CONTROL_STATUS_DAMPING_POLLS):
        restarted.run(
            {"grid_voltage_l1": 0.0, **_modes(bms=False)},
            T0 + timedelta(seconds=5 * poll),
        )

    assert restarted.active is not None
    assert restarted.active.check == "grid_voltage"


def test_how_long_readings_stood_still_survives_a_restart() -> None:
    """In #227 the readings stood still for a week; a restart must not hide that."""
    import json

    checks = reading_checks.ReadingChecks()
    at = _polls()
    frame = _stalled(0.0)
    for now in at[:30]:
        checks.run(frame, now)
    stored = json.loads(json.dumps(checks.dump_state()))

    restarted = reading_checks.ReadingChecks()
    restarted.load_state(stored)
    for now in at[30:]:
        restarted.run(frame, now)

    assert restarted.active == Finding(Status.INCONSISTENT_READINGS, "frozen", at[0])


def test_a_state_that_cannot_be_read_starts_afresh() -> None:
    checks = reading_checks.ReadingChecks()
    checks.load_state({"frozen_moved": {"frequency": ["50.0", "not a time"]}})
    checks.run(_live(), T0)

    assert checks.active is None


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
    run = sim.run(polls=FROZEN_POLLS + 12, solar=0, house=1500)

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


# ── Events, and no repairs issue ──────────────────────────────────────────────


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


def test_another_cause_for_the_same_state_is_not_announced_again(tracked) -> None:
    tracked.poll(Finding(Status.INCONSISTENT_READINGS, "battery_soc", T0), 12)
    tracked.poll(Finding(Status.INCONSISTENT_READINGS, "frozen", T0), 13)

    assert _fired(tracked) == [
        (const.EVENT_READING_CHECK_FAILED, "inconsistent_readings", "battery_soc")
    ]


def test_another_state_starts_over(tracked) -> None:
    tracked.poll(Finding(Status.INCONSISTENT_READINGS, "battery_soc", T0), 12)
    later = T0 + timedelta(minutes=12)
    tracked.poll(Finding(Status.OFF_GRID, "off_grid", later), 13)

    assert [event for event, _, _ in _fired(tracked)] == [
        const.EVENT_READING_CHECK_FAILED,
        const.EVENT_READING_CHECK_CLEARED,
        const.EVENT_READING_CHECK_FAILED,
    ]


def test_a_failed_check_is_never_raised_in_repairs(tracked) -> None:
    """Nothing the user can fix: an outage, a battery turned off, a fault."""
    finding = Finding(Status.BATTERY_DISCONNECTED, "bms", T0)

    for minute in range(0, 60, 5):
        tracked.poll(finding, minute)

    tracked.issues.created.assert_not_called()


def test_every_status_a_check_stands_for_has_its_text() -> None:
    import json
    from pathlib import Path

    folder = Path(coordinator_module.__file__).parent / "translations"
    for language in ("en", "de"):
        strings = json.loads((folder / f"{language}.json").read_text())
        assert not [key for key in strings["issues"] if key.startswith("reading_")]
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
