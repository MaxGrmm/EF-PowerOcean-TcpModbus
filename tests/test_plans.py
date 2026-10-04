"""The plans each Battery Mode runs, and the rules every plan is carried out by."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.ef_powerocean_tcpmodbus.models import ControlFeature
from custom_components.ef_powerocean_tcpmodbus.plans import (
    PLANS,
    Plan,
    PlanState,
    Situation,
    always,
    battery_power,
    situation_for,
)

Feature = ControlFeature
DEADBAND = 200.0
START = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)


def test_every_mode_has_a_plan() -> None:
    assert set(PLANS) == set(Feature)


def test_a_fixed_mode_runs_one_command_whatever_the_balance() -> None:
    for mode in Feature:
        if mode is Feature.EXPORT_SOLAR_FIRST:
            continue
        assert PLANS[mode] == always(mode)
        assert not PLANS[mode].departs


def test_export_solar_first_departs_only_with_a_surplus() -> None:
    plan = PLANS[Feature.EXPORT_SOLAR_FIRST]

    assert [plan.run(situation) for situation in Situation] == [
        Feature.AUTOMATIC,
        Feature.AUTOMATIC,
        Feature.HOLD_BATTERY,
        Feature.EXPORT_TO_GRID,
    ]
    # Which is all a charge limit forbids, so under one it has nothing to decide.
    assert plan.steers == 1


def test_a_plan_departing_only_with_a_deficit_steers_discharging() -> None:
    peak_shaving = Plan(
        otherwise=Feature.AUTOMATIC,
        deficit=Feature.HOLD_BATTERY,
        deficit_over_limit=Feature.IMPORT_FROM_GRID,
    )

    assert peak_shaving.steers == -1
    assert peak_shaving.start is Situation.SURPLUS


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
        # A command that follows the balance cannot tell without it.
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
        # Read as it stands at first.
        (150.0, None, Situation.SURPLUS),
        (-150.0, None, Situation.DEFICIT),
        # Zero is left only once past it by the deadband, either way.
        (150.0, Situation.DEFICIT, Situation.DEFICIT),
        (250.0, Situation.DEFICIT, Situation.SURPLUS),
        (-150.0, Situation.SURPLUS, Situation.SURPLUS),
        (-250.0, Situation.SURPLUS, Situation.DEFICIT),
        # The limit is entered at the limit itself, since a cap holds the balance
        # there, and left once under it by the deadband.
        (6000.0, Situation.SURPLUS, Situation.SURPLUS_OVER_LIMIT),
        (5850.0, Situation.SURPLUS, Situation.SURPLUS),
        (5850.0, Situation.SURPLUS_OVER_LIMIT, Situation.SURPLUS_OVER_LIMIT),
        (5750.0, Situation.SURPLUS_OVER_LIMIT, Situation.SURPLUS),
        (-6100.0, Situation.DEFICIT, Situation.DEFICIT_OVER_LIMIT),
        (-5850.0, Situation.DEFICIT_OVER_LIMIT, Situation.DEFICIT_OVER_LIMIT),
        (-5750.0, Situation.DEFICIT_OVER_LIMIT, Situation.DEFICIT),
    ),
)
def test_a_situation_is_left_only_once_clearly_past_its_edge(
    surplus: float, current: Situation | None, expected: Situation
) -> None:
    assert situation_for(surplus, 6000.0, current, DEADBAND) is expected


def test_a_departure_left_early_waits_longer_each_time() -> None:
    state = PlanState.begin(Feature.EXPORT_SOLAR_FIRST)
    now = START
    waits = []
    for _ in range(3):
        state.switch(Feature.EXPORT_TO_GRID, now)
        now += timedelta(seconds=20)
        state.switch(Feature.AUTOMATIC, now)
        wait = 0
        while not state.may_depart(
            Feature.EXPORT_TO_GRID, now + timedelta(seconds=wait)
        ):
            wait += 5
        waits.append(wait)
        now += timedelta(seconds=wait)

    assert waits == [60, 120, 240]
    # Running otherwise never waits.
    assert state.may_depart(Feature.AUTOMATIC, now)


def test_a_departure_that_lasted_resets_its_wait() -> None:
    state = PlanState.begin(Feature.EXPORT_SOLAR_FIRST)
    state.switch(Feature.EXPORT_TO_GRID, START)
    state.switch(Feature.AUTOMATIC, START + timedelta(seconds=20))
    state.switch(Feature.EXPORT_TO_GRID, START + timedelta(seconds=80))
    state.switch(Feature.AUTOMATIC, START + timedelta(seconds=200))

    assert state.may_depart(Feature.EXPORT_TO_GRID, START + timedelta(seconds=200))
