"""How each Battery Mode picks its command, and how Export Solar First adapts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.ef_powerocean_tcpmodbus.models import ControlFeature
from custom_components.ef_powerocean_tcpmodbus.plans import (
    PlanState,
    Zone,
    battery_power,
    plan_for,
    zone_for,
)

Feature = ControlFeature
DEADBAND = 200.0
LIMIT = 6000.0
START = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)


def test_only_export_solar_first_adapts() -> None:
    for mode in Feature:
        plan = plan_for(mode)
        assert plan.adapts is (mode is Feature.EXPORT_SOLAR_FIRST)
        if not plan.adapts:
            assert all(plan.command_for(zone) is mode for zone in Zone)


def test_export_solar_first_exports_before_it_stores() -> None:
    plan = plan_for(Feature.EXPORT_SOLAR_FIRST)

    assert plan.command_for(Zone.DEFICIT) is Feature.AUTOMATIC
    assert plan.command_for(Zone.SURPLUS) is Feature.HOLD_BATTERY
    assert plan.command_for(Zone.AT_LIMIT) is Feature.EXPORT_TO_GRID


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
        # The limit is reached at the limit itself and left the deadband below it.
        (6000.0, Zone.SURPLUS, Zone.AT_LIMIT),
        (6000.0, Zone.DEFICIT, Zone.AT_LIMIT),
        (5850.0, Zone.SURPLUS, Zone.SURPLUS),
        (5850.0, Zone.AT_LIMIT, Zone.AT_LIMIT),
        (5750.0, Zone.AT_LIMIT, Zone.SURPLUS),
    ),
)
def test_a_zone_is_left_only_once_clearly_outside_it(
    surplus: float, current: Zone, expected: Zone
) -> None:
    assert zone_for(surplus, LIMIT, current, DEADBAND) is expected


def _choose(state: PlanState, surplus: float | None, at_s: float) -> Feature:
    return state.choose(surplus, LIMIT, START + timedelta(seconds=at_s), DEADBAND)


def test_export_solar_first_follows_the_surplus() -> None:
    state = PlanState(Feature.EXPORT_SOLAR_FIRST)

    assert _choose(state, 150.0, 0) is Feature.AUTOMATIC
    assert _choose(state, 300.0, 5) is Feature.HOLD_BATTERY
    assert _choose(state, 6000.0, 10) is Feature.EXPORT_TO_GRID
    assert _choose(state, -300.0, 70) is Feature.AUTOMATIC


def test_an_unknown_surplus_runs_the_default_and_starts_over() -> None:
    state = PlanState(Feature.EXPORT_SOLAR_FIRST)
    _choose(state, 300.0, 0)

    assert _choose(state, None, 120) is Feature.AUTOMATIC
    # Back as if from a deficit: a small surplus is not enough to switch.
    assert _choose(state, 150.0, 125) is Feature.AUTOMATIC


def test_a_command_left_early_waits_longer_each_time() -> None:
    state = PlanState(Feature.EXPORT_SOLAR_FIRST)
    now = 0.0
    assert _choose(state, 300.0, now) is Feature.HOLD_BATTERY
    waits = []
    for _ in range(3):
        now += 20
        _choose(state, -300.0, now)
        wait = 0
        while (
            _choose(state, 300.0, now + (wait := wait + 5)) is not Feature.HOLD_BATTERY
        ):
            pass
        waits.append(wait)
        now += wait

    assert waits == [60, 120, 240]


def test_a_command_that_lasted_does_not_wait() -> None:
    state = PlanState(Feature.EXPORT_SOLAR_FIRST)
    _choose(state, 300.0, 0)
    _choose(state, -300.0, 120)

    assert _choose(state, 300.0, 125) is Feature.HOLD_BATTERY


def test_waits_outlast_a_stretch_with_nothing_to_decide() -> None:
    """A full battery or the Charge Limit in between does not reset a wait."""
    state = PlanState(Feature.EXPORT_SOLAR_FIRST)
    _choose(state, 300.0, 0)
    _choose(state, -300.0, 20)
    _choose(state, None, 25)

    assert _choose(state, 300.0, 30) is Feature.AUTOMATIC
