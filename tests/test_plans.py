"""The Battery Modes, and how each picks what to run for the solar surplus."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.ef_powerocean_tcpmodbus.models import ControlFeature
from custom_components.ef_powerocean_tcpmodbus.plans import (
    CHARGE,
    MODES,
    Mode,
    ModeState,
    Zone,
    automatic,
    battery_power,
    export_to_grid,
    hold_battery,
    import_from_grid,
    zone_for,
)

Feature = ControlFeature
DEADBAND = 200.0
LIMIT = 6000.0
START = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)


def test_every_mode_is_written_down() -> None:
    assert set(MODES) == set(Feature)


def test_a_mode_that_always_runs_one_command() -> None:
    for feature, mode in MODES.items():
        if feature is Feature.EXPORT_SOLAR_FIRST:
            continue
        assert not mode.adapts
        assert {mode.step_for(zone).command for zone in Zone} == {feature}


def test_export_solar_first_exports_before_it_stores() -> None:
    mode = MODES[Feature.EXPORT_SOLAR_FIRST]

    assert mode.step_for(Zone.DEFICIT_ABOVE_LIMIT) == automatic()
    assert mode.step_for(Zone.DEFICIT) == automatic()
    assert mode.step_for(Zone.SURPLUS).never is CHARGE
    assert mode.step_for(Zone.SURPLUS_ABOVE_LIMIT) == export_to_grid()


def test_a_zone_left_out_runs_its_neighbour_closer_to_zero() -> None:
    mode = Mode(deficit=hold_battery(), surplus=automatic())

    assert mode.step_for(Zone.DEFICIT_ABOVE_LIMIT) == hold_battery()
    assert mode.step_for(Zone.SURPLUS_ABOVE_LIMIT) == automatic()
    assert not mode.uses_limit


@pytest.mark.parametrize(
    ("command", "surplus", "battery"),
    (
        (Feature.AUTOMATIC, 1500.0, 1500.0),
        (Feature.HOLD_BATTERY, 1500.0, 0.0),
        (Feature.CHARGE_BATTERY, -1500.0, 2000.0),
        (Feature.DISCHARGE_BATTERY, 1500.0, -2000.0),
        # Export to Grid charges with a surplus above its power, and discharges
        # below it; Import from Grid the mirror of that.
        (Feature.EXPORT_TO_GRID, 5000.0, 3000.0),
        (Feature.EXPORT_TO_GRID, 500.0, -1500.0),
        (Feature.IMPORT_FROM_GRID, -1000.0, 1000.0),
        (Feature.IMPORT_FROM_GRID, -3000.0, -1000.0),
        # A command that moves with the surplus cannot tell without it.
        (Feature.EXPORT_TO_GRID, None, None),
        (Feature.HOLD_BATTERY, None, 0.0),
    ),
)
def test_what_each_command_does_to_the_battery(
    command: Feature, surplus: float | None, battery: float | None
) -> None:
    assert battery_power(command, surplus, 2000.0) == battery


@pytest.mark.parametrize(
    ("surplus", "current", "expected"),
    (
        # Zero is crossed only once the deadband past it, either way.
        (150.0, Zone.DEFICIT, Zone.DEFICIT),
        (250.0, Zone.DEFICIT, Zone.SURPLUS),
        (-150.0, Zone.SURPLUS, Zone.SURPLUS),
        (-250.0, Zone.SURPLUS, Zone.DEFICIT),
        # A limit is reached at the limit itself and left the deadband inside it.
        (6000.0, Zone.SURPLUS, Zone.SURPLUS_ABOVE_LIMIT),
        (6000.0, Zone.DEFICIT, Zone.SURPLUS_ABOVE_LIMIT),
        (5850.0, Zone.SURPLUS, Zone.SURPLUS),
        (5850.0, Zone.SURPLUS_ABOVE_LIMIT, Zone.SURPLUS_ABOVE_LIMIT),
        (5750.0, Zone.SURPLUS_ABOVE_LIMIT, Zone.SURPLUS),
        (-6000.0, Zone.DEFICIT, Zone.DEFICIT_ABOVE_LIMIT),
        (-5850.0, Zone.DEFICIT_ABOVE_LIMIT, Zone.DEFICIT_ABOVE_LIMIT),
        (-5750.0, Zone.DEFICIT_ABOVE_LIMIT, Zone.DEFICIT),
        # From a deficit beyond the limit, a surplus still needs the deadband.
        (150.0, Zone.DEFICIT_ABOVE_LIMIT, Zone.DEFICIT),
    ),
)
def test_a_zone_is_left_only_once_clearly_outside_it(
    surplus: float, current: Zone, expected: Zone
) -> None:
    assert zone_for(surplus, LIMIT, current, DEADBAND) is expected


def _choose(state: ModeState, surplus: float | None, at_s: float, **kwargs: bool):
    return state.choose(
        surplus, LIMIT, START + timedelta(seconds=at_s), DEADBAND, **kwargs
    ).command


def test_export_solar_first_follows_the_surplus() -> None:
    state = ModeState(Feature.EXPORT_SOLAR_FIRST)

    assert _choose(state, 150.0, 0) is Feature.AUTOMATIC
    assert state.zone is Zone.DEFICIT
    _choose(state, 300.0, 5)
    assert state.zone is Zone.SURPLUS
    assert _choose(state, 6000.0, 10) is Feature.EXPORT_TO_GRID
    assert _choose(state, -300.0, 70) is Feature.AUTOMATIC


def test_nothing_to_decide_runs_the_deficit_step_and_starts_over() -> None:
    state = ModeState(Feature.EXPORT_SOLAR_FIRST)
    _choose(state, 6000.0, 0)

    assert _choose(state, None, 120) is Feature.AUTOMATIC
    assert _choose(state, 6000.0, 125, battery_full=True) is Feature.AUTOMATIC
    # Back as if from a deficit: a small surplus is not enough to switch.
    _choose(state, 150.0, 130)
    assert state.zone is Zone.DEFICIT


def test_a_mode_using_a_limit_has_nothing_to_decide_without_one() -> None:
    state = ModeState(Feature.EXPORT_SOLAR_FIRST)

    step = state.choose(6000.0, None, START, DEADBAND)

    assert step == automatic()


def test_a_step_left_early_waits_longer_each_time() -> None:
    state = ModeState(Feature.EXPORT_SOLAR_FIRST)
    now = 0.0
    assert _choose(state, 6000.0, now) is Feature.EXPORT_TO_GRID
    waits = []
    for _ in range(3):
        now += 20
        _choose(state, -300.0, now)
        wait = 0
        while _choose(state, 6000.0, now + (wait := wait + 5)) is not (
            Feature.EXPORT_TO_GRID
        ):
            pass
        waits.append(wait)
        now += wait

    assert waits == [60, 120, 240]


def test_a_step_that_lasted_does_not_wait() -> None:
    state = ModeState(Feature.EXPORT_SOLAR_FIRST)
    _choose(state, 6000.0, 0)
    _choose(state, -300.0, 120)

    assert _choose(state, 6000.0, 125) is Feature.EXPORT_TO_GRID


def test_waits_outlast_a_stretch_with_nothing_to_decide() -> None:
    state = ModeState(Feature.EXPORT_SOLAR_FIRST)
    _choose(state, 6000.0, 0)
    _choose(state, -300.0, 20)
    _choose(state, None, 25)

    assert _choose(state, 6000.0, 30) is Feature.AUTOMATIC


def test_a_deficit_beyond_the_limit_can_have_a_step_of_its_own() -> None:
    """Room for a mode such as limiting the grid import."""
    mode = Mode(deficit=hold_battery(), deficit_above_limit=import_from_grid())

    assert mode.uses_limit
    assert mode.step_for(Zone.DEFICIT_ABOVE_LIMIT) == import_from_grid()
    assert mode.step_for(Zone.SURPLUS) == hold_battery()
