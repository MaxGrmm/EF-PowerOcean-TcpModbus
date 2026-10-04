"""What every Battery Mode does, written down so that a change in it shows.

Three kinds of test, each small enough to read at a glance:

- what each mode settles on for a deficit, a small surplus and a large one;
- that no guard ever lets the battery move the way it forbids, in any mode;
- that a balance close to a threshold does not switch the method back and forth.
"""

from __future__ import annotations

from typing import Final

import pytest
from simulation import BATTERY_LIMITS, SYSTEM_FEED, Run, Simulation, select

from custom_components.ef_powerocean_tcpmodbus import const, models

Feature = models.ControlFeature
Status = models.ControlStatus

CAP: Final = 6000.0
DEADBAND: Final = const.GUARD_POWER_DEADBAND_W
SOLAR_FIRST_LIMIT: Final = CAP - const.SOLAR_EXPORT_CAP_MARGIN_W

# Control only differs between models whose guards track setpoints and those whose
# guards hold instead, so one of each stands in for all of them.
TRACKING: Final = models.InverterModel.POWEROCEAN_PLUS
HOLDING: Final = models.InverterModel.POWEROCEAN_SINGLE_PHASE

AUTOMATIC = "automatic"
HOLD = "battery +1 W"

# What the inverter is told once each mode has settled, for a deficit, a small
# surplus and a large one: Automatic, a battery setpoint, or a meter setpoint
# (positive draws from the grid, negative exports).
SETTLED: Final = {
    Feature.AUTOMATIC: (AUTOMATIC, AUTOMATIC, AUTOMATIC),
    Feature.HOLD_BATTERY: (HOLD, HOLD, HOLD),
    Feature.CHARGE_BATTERY: ("battery +2000 W",) * 3,
    Feature.DISCHARGE_BATTERY: ("battery -2000 W",) * 3,
    Feature.EXPORT_TO_GRID: ("meter -3000 W",) * 3,
    Feature.IMPORT_FROM_GRID: ("meter +3000 W",) * 3,
    Feature.EXPORT_SOLAR_FIRST: (AUTOMATIC, HOLD, f"meter -{SOLAR_FIRST_LIMIT:.0f} W"),
}
BALANCES: Final = ((0, 1500), (2500, 500), (9500, 500))  # (solar, house)


def _told(sim: Simulation) -> str:
    inverter = sim.inverter
    if inverter.method == BATTERY_LIMITS:
        return f"battery {inverter.setpoint:+d} W"
    if inverter.method == SYSTEM_FEED:
        return f"meter {inverter.system_setpoint:+d} W"
    return AUTOMATIC


@pytest.mark.parametrize("mode", Feature)
def test_each_mode_settles_on_its_command(
    monkeypatch: pytest.MonkeyPatch, mode: Feature
) -> None:
    told = []
    for solar, house in BALANCES:
        sim = Simulation(monkeypatch, export_cap=CAP, model=TRACKING)
        select(sim, mode)
        sim.run(polls=24, solar=solar, house=house)
        told.append(_told(sim))

    assert tuple(told) == SETTLED[mode]


# Weather and load that put the balance either side of zero, far past it, and
# flipping across it faster than the loop settles. (solar, house, polls)
SITUATIONS: Final = {
    "evening": (0, 1500, 120),
    "heavy evening load": (0, 4500, 120),
    "large surplus": (9500, 500, 120),
    "flickering across zero": (2000, [1600, 1600, 2400, 2400], 120),
    "passing clouds": ([9500] * 6 + [1500] * 6, 1000, 240),
}
GUARDS: Final = {
    "charge limit": {"soc": 80.0, "charge_limit": 80.0},
    "reserve": {"soc": 20.0, "reserve": 20.0},
}

# Guards treat a mode as moving the battery one way, but Export to Grid charges it
# with a surplus above its power and Import from Grid discharges it with a house
# above its power. Fixing that makes these pass, and strict means they then fail
# until taken off this list.
KNOWN_GAPS: Final = {
    (Feature.EXPORT_TO_GRID, "charge limit"),
    (Feature.IMPORT_FROM_GRID, "reserve"),
}


def _guard_cases() -> list[pytest.param]:
    return [
        pytest.param(
            mode,
            guard,
            marks=[pytest.mark.xfail(strict=True, reason="one-way guard")]
            if (mode, guard) in KNOWN_GAPS
            else [],
            id=f"{mode.value}-{guard.replace(' ', '_')}",
        )
        for mode in Feature
        for guard in GUARDS
    ]


@pytest.mark.parametrize(("mode", "guard"), _guard_cases())
def test_a_guard_never_lets_the_battery_move_the_way_it_forbids(
    monkeypatch: pytest.MonkeyPatch, mode: Feature, guard: str
) -> None:
    """A change the loop could not see coming may reach the battery for the one poll
    it takes to see it. A guard that holds rather than tracks may take its settle
    time to take control back. Never longer."""
    forbidden = 1.0 if guard == "charge limit" else -1.0
    for model in (TRACKING, HOLDING):
        allowed = 1 if model is TRACKING else int(const.GUARD_SETTLE_S / 5.0)
        for situation, (solar, house, polls) in SITUATIONS.items():
            sim = Simulation(monkeypatch, export_cap=CAP, model=model, **GUARDS[guard])
            select(sim, mode)
            run = sim.run(polls=polls, solar=solar, house=house)
            engaged = run.charge_guard if guard == "charge limit" else run.reserve_guard
            streak = 0
            for poll, watts in enumerate(run.battery):
                guarded = poll > 0 and engaged[poll - 1] and engaged[poll]
                streak = streak + 1 if guarded and watts * forbidden > DEADBAND else 0
                assert streak <= allowed, f"{model.name}, {situation}: poll {poll}"


def _jitter(
    monkeypatch: pytest.MonkeyPatch,
    mode: Feature,
    *,
    lead_in: tuple[float, float],
    solar: list[float] | float,
    house: list[float] | float,
    **guards: float,
) -> tuple[Run, int]:
    """Settle a mode, then return a jittering run and the method writes during it."""
    sim = Simulation(monkeypatch, export_cap=CAP, model=TRACKING, **guards)
    select(sim, mode)
    sim.run(polls=24, solar=lead_in[0], house=lead_in[1])
    before = sim.inverter.method_writes
    run = sim.run(polls=120, solar=solar, house=house)
    return run, sim.inverter.method_writes - before


def test_a_draw_dipping_under_the_hand_back_threshold_stays_tracked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under a charge limit, a draw is only handed to the inverter once it has stayed
    clearly above the threshold. One that keeps dipping under it stays tracked."""
    run, method_writes = _jitter(
        monkeypatch,
        Feature.AUTOMATIC,
        lead_in=(2000, 2300),
        solar=2000,
        house=[2300, 2300, 2800, 2800],
        soc=80.0,
        charge_limit=80.0,
    )

    assert method_writes == 0
    assert set(run.method) == {BATTERY_LIMITS}


def test_export_solar_first_rides_out_a_balance_jittering_around_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, method_writes = _jitter(
        monkeypatch,
        Feature.EXPORT_SOLAR_FIRST,
        lead_in=(0, 1500),
        solar=2000,
        house=[2000 - 0.75 * DEADBAND, 2000 + 0.75 * DEADBAND],
    )

    assert method_writes == 0
    assert set(run.status) == {Status.AUTOMATIC}


def test_export_solar_first_rides_out_a_surplus_jittering_around_its_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, method_writes = _jitter(
        monkeypatch,
        Feature.EXPORT_SOLAR_FIRST,
        lead_in=(9500, 500),
        solar=[SOLAR_FIRST_LIMIT + 500 + jitter for jitter in (-150, 150)],
        house=500,
    )

    assert method_writes == 0
    assert set(run.grid) == {-SOLAR_FIRST_LIMIT}
