"""What every Battery Mode does, written down so that a change in it shows.

Every test drives the control manager only the way the integration does, and checks
only what the inverter is told and what the battery and the grid then do. So they
keep passing however the logic is arranged inside, as long as it behaves the same.

- what each mode tells the inverter once it has settled;
- what the battery and the grid then do, with and without a guard;
- that no guard lets the battery move the way it forbids;
- that a balance close to a threshold does not switch the method back and forth;
- how a mode's power, an expiry and handing control back to the app work.

Guards run differently on single-phase models, so every check runs on one of each.
"""

from __future__ import annotations

import asyncio
from typing import Final

import pytest
from homeassistant.exceptions import HomeAssistantError
from simulation import (
    AUTOMATIC,
    BATTERY_LIMITS,
    SYSTEM_FEED,
    Run,
    Simulation,
    select,
)

from custom_components.ef_powerocean_tcpmodbus import models

Feature = models.ControlFeature
Status = models.ControlStatus
Model = models.InverterModel

MODELS: Final = (Model.POWEROCEAN_PLUS, Model.POWEROCEAN_SINGLE_PHASE)
CAP: Final = 6000.0
# Export Solar First keeps 100 W under a cap it is set at.
SOLAR_FIRST_LIMIT: Final = 5900
# Power changes this small are taken as noise rather than as the battery moving.
NOISE_W: Final = 200.0
# Settled: a mode has had three minutes, a guard its hand-back, and the last minute
# of the run is what it settled on.
SETTLE_POLLS: Final = 48
LAST_MINUTE: Final = slice(-12, None)

GUARDS: Final = {
    "no guard": {"soc": 50.0},
    "charge limit": {"soc": 80.0, "charge_limit": 80.0},
    "reserve": {"soc": 20.0, "reserve": 20.0},
}

# (solar, house) for each column of the tables below.
BALANCES: Final = {
    "deficit": (0, 1500),
    "heavy deficit": (0, 4500),
    "small surplus": (2500, 500),
    "large surplus": (9500, 500),
}


def _settle(
    mode: Feature, guard: str, balance: str, model: Model
) -> tuple[Simulation, Run]:
    with pytest.MonkeyPatch.context() as monkeypatch:
        sim = Simulation(monkeypatch, export_cap=CAP, model=model, **GUARDS[guard])
        select(sim, mode)
        solar, house = BALANCES[balance]
        return sim, sim.run(polls=SETTLE_POLLS, solar=solar, house=house)


def _table(text: str) -> dict[Feature, tuple[str, ...]]:
    """Read a table of one row per mode and one cell of two words per balance."""
    rows = {}
    for line in text.strip().splitlines():
        mode, *words = line.split()
        rows[Feature(mode)] = tuple(
            " ".join(pair) for pair in zip(words[::2], words[1::2], strict=True)
        )
    return rows


# What the inverter is told once each mode has settled with no guard on: Automatic,
# a battery setpoint (positive charges), or a meter setpoint (positive draws from
# the grid, negative exports).
TOLD: Final = _table("""
automatic           automatic -      automatic -      automatic -      automatic -
hold_battery        battery +1       battery +1       battery +1       battery +1
charge_battery      battery +2000    battery +2000    battery +2000    battery +2000
discharge_battery   battery -2000    battery -2000    battery -2000    battery -2000
export_to_grid      meter -3000      meter -3000      meter -3000      meter -3000
import_from_grid    meter +3000      meter +3000      meter +3000      meter +3000
export_solar_first  automatic -      automatic -      battery +1       meter -5900
""")


def _told(sim: Simulation) -> str:
    inverter = sim.inverter
    if inverter.method == BATTERY_LIMITS:
        return f"battery {inverter.setpoint:+d}"
    if inverter.method == SYSTEM_FEED:
        return f"meter {inverter.system_setpoint:+d}"
    return "automatic -"


@pytest.mark.parametrize("model", MODELS, ids=lambda model: model.name)
@pytest.mark.parametrize("mode", Feature)
def test_each_mode_tells_the_inverter_its_command(mode: Feature, model: Model) -> None:
    told = tuple(
        _told(_settle(mode, "no guard", balance, model)[0]) for balance in BALANCES
    )

    assert told == TOLD[mode]


# What the battery and the grid do once each mode has settled: the battery charges,
# discharges or idles, and the grid imports, exports or is balanced.
#                    deficit          heavy deficit    small surplus    large surplus
OUTCOMES: Final = {
    "no guard": _table("""
automatic           discharges bal   discharges bal   charges bal      charges exports
hold_battery        idle imports     idle imports     idle exports     idle exports
charge_battery      charges imports  charges imports  charges bal      charges exports
discharge_battery   discharges exports discharges imports discharges exports discharges exports
export_to_grid      discharges exports discharges exports discharges exports charges exports
import_from_grid    charges imports  discharges imports charges imports charges exports
export_solar_first  discharges bal   discharges bal   idle exports     charges exports
"""),
    "charge limit": _table("""
automatic           discharges bal   discharges bal   idle exports     idle exports
hold_battery        idle imports     idle imports     idle exports     idle exports
charge_battery      idle imports     idle imports     idle exports     idle exports
discharge_battery   discharges exports discharges imports discharges exports discharges exports
export_to_grid      discharges exports discharges exports discharges exports idle exports
import_from_grid    idle imports     discharges imports idle exports  idle exports
export_solar_first  discharges bal   discharges bal   idle exports     idle exports
"""),
    "reserve": _table("""
automatic           idle imports     idle imports     charges bal      charges exports
hold_battery        idle imports     idle imports     idle exports     idle exports
charge_battery      charges imports  charges imports  charges bal      charges exports
discharge_battery   idle imports     idle imports     idle exports     idle exports
export_to_grid      idle imports     idle imports     idle exports     charges exports
import_from_grid    charges imports  idle imports    charges imports  charges exports
export_solar_first  idle imports     idle imports     idle exports     charges exports
"""),
}


def _direction(watts: list[float], up: str, down: str, still: str) -> str:
    seen = {up if w > NOISE_W else down if w < -NOISE_W else still for w in watts}
    return seen.pop() if len(seen) == 1 else "varies"


def _outcome_cases() -> list[pytest.param]:
    return [
        pytest.param(
            mode,
            guard,
            balance,
            cell,
            id=f"{mode.value}-{guard}-{balance}".replace(" ", "_"),
        )
        for guard, rows in OUTCOMES.items()
        for mode, cells in rows.items()
        for balance, cell in zip(BALANCES, cells, strict=True)
    ]


@pytest.mark.parametrize(("mode", "guard", "balance", "expected"), _outcome_cases())
def test_each_mode_moves_the_battery_and_grid_as_intended(
    mode: Feature, guard: str, balance: str, expected: str
) -> None:
    for model in MODELS:
        run = _settle(mode, guard, balance, model)[1]
        battery = _direction(run.battery[LAST_MINUTE], "charges", "discharges", "idle")
        grid = _direction(run.grid[LAST_MINUTE], "imports", "exports", "bal")

        assert f"{battery} {grid}" == expected, model.name


# Weather and load that put the balance far either side of zero, and flip it across
# zero faster than a guard settles. (solar, house, polls)
WEATHER: Final = {
    "evening": (0, 1500, 120),
    "heavy evening load": (0, 4500, 120),
    "large surplus": (9500, 500, 120),
    "flickering across zero": (2000, [1600, 1600, 2400, 2400], 120),
    "passing clouds": ([9500] * 6 + [1500] * 6, 1000, 240),
}
# How many polls in a row a guard may let the battery move the forbidden way: the
# one it takes to see a change, or on a single-phase model the 30 s its guard waits
# after a change before it takes control back.
ALLOWED_POLLS: Final = {Model.POWEROCEAN_PLUS: 1, Model.POWEROCEAN_SINGLE_PHASE: 6}


@pytest.mark.parametrize("guard", ("charge limit", "reserve"))
@pytest.mark.parametrize("mode", Feature)
def test_a_guard_never_lets_the_battery_move_the_way_it_forbids(
    monkeypatch: pytest.MonkeyPatch, mode: Feature, guard: str
) -> None:
    """At or past its limit the battery is not moved further, whatever the mode."""
    charging = guard == "charge limit"
    limit = GUARDS[guard]["charge_limit" if charging else "reserve"]
    for model in MODELS:
        for weather, (solar, house, polls) in WEATHER.items():
            sim = Simulation(monkeypatch, export_cap=CAP, model=model, **GUARDS[guard])
            select(sim, mode)
            run = sim.run(polls=polls, solar=solar, house=house)

            streak = 0
            for poll, (soc, watts) in enumerate(zip(run.soc, run.battery, strict=True)):
                at_limit = soc >= limit if charging else soc <= limit
                forbidden = watts > NOISE_W if charging else watts < -NOISE_W
                streak = streak + 1 if at_limit and forbidden else 0
                assert streak <= ALLOWED_POLLS[model], (
                    f"{model.name}, {weather}: {poll}"
                )


def _jitter(
    monkeypatch: pytest.MonkeyPatch,
    mode: Feature,
    *,
    lead_in: tuple[float, float],
    solar: list[float] | float,
    house: list[float] | float,
    model: Model = Model.POWEROCEAN_PLUS,
    **guards: float,
) -> tuple[Run, int]:
    """Settle a mode, then return a jittering run and the method writes during it."""
    sim = Simulation(monkeypatch, export_cap=CAP, model=model, **guards)
    select(sim, mode)
    sim.run(polls=24, solar=lead_in[0], house=lead_in[1])
    before = sim.inverter.method_writes
    run = sim.run(polls=120, solar=solar, house=house)
    return run, sim.inverter.method_writes - before


def test_a_draw_dipping_under_the_hand_back_threshold_stays_with_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under a charge limit, a draw is only handed to the inverter once it has stayed
    clearly up for a while. One that keeps dipping stays with the guard."""
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


def test_a_single_phase_guard_leaves_a_draw_of_a_few_watts_to_the_grid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single-phase guard holds rather than tracks, and hands a draw to the
    inverter only past 20 W, so a few watts either way switch nothing."""
    run, method_writes = _jitter(
        monkeypatch,
        Feature.AUTOMATIC,
        lead_in=(2000, 2010),
        solar=2000,
        house=[2005, 2005, 2015, 2015],
        model=Model.POWEROCEAN_SINGLE_PHASE,
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
        house=[1850, 2150],
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
        solar=[SOLAR_FIRST_LIMIT + 500 - 150, SOLAR_FIRST_LIMIT + 500 + 150],
        house=500,
    )

    assert method_writes == 0
    assert set(run.grid) == {-SOLAR_FIRST_LIMIT}


def test_export_solar_first_exports_a_surplus_just_under_its_limit_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Export to Grid at the limit would make up the last 100 W from the battery, so
    it only takes over once the surplus reaches the limit."""
    sim = Simulation(monkeypatch, export_cap=CAP)
    select(sim, Feature.EXPORT_SOLAR_FIRST)

    run = sim.run(polls=60, solar=SOLAR_FIRST_LIMIT + 400, house=500)

    assert set(run.method) == {BATTERY_LIMITS}
    # All of it exported, less the watt the hold keeps in the battery.
    assert set(run.grid) == {-(SOLAR_FIRST_LIMIT - 100 - 1)}


def test_export_solar_first_covers_a_small_draw_under_a_reached_charge_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the charge limit reached it has nothing left to decide, so it covers even
    a draw too small to switch for from the battery, as Automatic does."""
    sim = Simulation(monkeypatch, soc=80.0, charge_limit=80.0, export_cap=CAP)
    select(sim, Feature.EXPORT_SOLAR_FIRST)
    sim.run(polls=12, solar=2500, house=500)

    run = sim.run(polls=24, solar=2500, house=2650)

    assert max(run.grid[1:]) <= 0


@pytest.mark.parametrize(
    ("mode", "power", "told"),
    (
        # Past what the battery takes or gives, which is 5 kW either way here.
        (Feature.CHARGE_BATTERY, 9000.0, "battery +5000"),
        (Feature.DISCHARGE_BATTERY, 9000.0, "battery -5000"),
        # Past the export cap.
        (Feature.EXPORT_TO_GRID, 9000.0, "meter -6000"),
        # Within every limit, as set.
        (Feature.CHARGE_BATTERY, 1200.0, "battery +1200"),
        (Feature.IMPORT_FROM_GRID, 4000.0, "meter +4000"),
    ),
)
def test_a_mode_runs_at_its_power_within_what_the_inverter_can_do(
    monkeypatch: pytest.MonkeyPatch, mode: Feature, power: float, told: str
) -> None:
    sim = Simulation(monkeypatch, export_cap=CAP)
    asyncio.run(sim.control.async_set_feature_power(mode, power))
    select(sim, mode)
    sim.run(polls=4, solar=0, house=1500)

    assert _told(sim) == told


def test_a_new_power_reaches_the_inverter_while_its_mode_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim = Simulation(monkeypatch, export_cap=CAP)
    select(sim, Feature.CHARGE_BATTERY)
    sim.run(polls=4, solar=0, house=1500)

    asyncio.run(sim.control.async_set_feature_power(Feature.CHARGE_BATTERY, 3500.0))
    sim.run(polls=1, solar=0, house=1500)

    assert _told(sim) == "battery +3500"


def test_a_command_with_an_expiry_returns_to_automatic_when_it_runs_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim = Simulation(monkeypatch, export_cap=CAP)
    asyncio.run(
        sim.control.async_set_command(
            Feature.CHARGE_BATTERY, power=1500.0, expire_in_s=60
        )
    )

    before = sim.run(polls=10, solar=0, house=1000)
    after = sim.run(polls=4, solar=0, house=1000)

    assert set(before.method) == {BATTERY_LIMITS}
    assert after.method[-1] == AUTOMATIC
    sim.command_expired.assert_called_once()


def test_handing_control_back_stops_every_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inverter returns to its app settings by itself once the heartbeat stops,
    and nothing more is written until control is taken again."""
    sim = Simulation(monkeypatch, export_cap=CAP)
    select(sim, Feature.CHARGE_BATTERY)
    sim.run(polls=4, solar=0, house=1000)

    asyncio.run(sim.control.async_set_enabled(False))
    writes = sim.inverter.method_writes + sim.inverter.setpoint_writes
    run = sim.run(polls=24, solar=0, house=1000)

    assert sim.inverter.method_writes + sim.inverter.setpoint_writes == writes
    assert run.method[-1] == AUTOMATIC
    with pytest.raises(HomeAssistantError):
        asyncio.run(sim.control.async_select_feature(Feature.CHARGE_BATTERY))


def test_charging_resumes_once_the_battery_falls_back_under_its_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A charge limit is a ceiling, not a lock: once the battery has drawn down past
    it by a margin, a surplus charges it again."""
    sim = Simulation(monkeypatch, soc=80.0, charge_limit=80.0, export_cap=CAP)
    evening = sim.run(polls=720, solar=0, house=3000)
    morning = sim.run(polls=24, solar=4000, house=500)

    assert evening.soc[-1] < 75
    assert min(morning.battery[LAST_MINUTE]) > NOISE_W
