"""Closed-loop scenarios: the real control manager against an inverter that answers.

The unit tests feed one frame and check one command. A control loop goes wrong over a
sequence instead, so these run minutes of simulated weather and load and assert what
must never happen across the whole run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Final
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.ef_powerocean_tcpmodbus import const, models
from custom_components.ef_powerocean_tcpmodbus import control as control_module

POLL_S: Final = 5.0
START: Final = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)
SETPOINT_REGISTER: Final = const.REGISTERS_BY_KEY["battery_power_setpoint"].address
BATTERY_LIMITS: Final = models.ControlMode.BATTERY_LIMITS.command_value


def at(watts: float | list[float], step: int) -> float:
    """Return a steady value, or one poll of a pattern that repeats."""
    if isinstance(watts, list):
        return float(watts[step % len(watts)])
    return float(watts)


@dataclass
class FakeInverter:
    """The most basic inverter possible, to be used in closed-loop simulations."""

    soc: float = 50.0
    charge_max: float = 5000.0
    discharge_max: float = 5000.0
    capacity_wh: float = 10_000.0
    solar: float = 0.0
    house: float = 0.0
    battery: float = 0.0
    method: int = 0
    setpoint: int = 0
    method_writes: int = 0
    setpoint_writes: int = 0
    connected: bool = True
    reachable: bool = True
    # Polls the battery stays put after a setpoint write, as the Ocean 2's does.
    setback_polls: int = 0
    setback: int = 0
    # What an app schedule charges at while the inverter runs itself, if one is set.
    app_charge: float | None = None

    async def async_write(
        self, address: int, words: list[int], *, what: str = ""
    ) -> None:
        if not self.reachable:
            raise control_module.HomeAssistantError("the inverter is unreachable")
        if address not in (const.CONTROL_COMMAND_REGISTER, SETPOINT_REGISTER):
            return  # the heartbeat, which carries nothing this simulation needs
        value = (words[0] << 16) | words[1]
        if address == const.CONTROL_COMMAND_REGISTER:
            self.method_writes += 1
            self.method = (
                value >> const.CONTROL_COMMAND_METHOD_SHIFT
            ) & const.CONTROL_COMMAND_METHOD_MASK
        elif address == SETPOINT_REGISTER:
            self.setpoint_writes += 1
            self.setpoint = value - (1 << 32) if value >> 31 else value
            self.setback = self.setback_polls

    def settle(self) -> None:
        """Obey the standing command, or run self-consumption where there is none."""
        commanded = self.method == BATTERY_LIMITS and self.setpoint != 0
        if commanded and self.setback:
            self.setback -= 1
            target = self.battery
        elif commanded:
            target = float(self.setpoint)
        elif self.app_charge is not None:
            target = self.app_charge
        else:
            target = self.solar - self.house
        ceiling = self.charge_max if self.soc < 100.0 else 0.0
        floor = -self.discharge_max if self.soc > 0.0 else 0.0
        self.battery = max(floor, min(ceiling, target))
        moved = self.battery * POLL_S / 36.0 / self.capacity_wh
        self.soc = max(0.0, min(100.0, self.soc + moved))

    def frame(self) -> dict[str, float]:
        return {
            "solar_power": self.solar,
            "house_power": self.house,
            "battery_power": self.battery,
            "grid_power": self.house - self.solar + self.battery,
            # Whole percent, as the inverter reports it.
            "battery_soc": float(round(self.soc)),
        }


@dataclass
class Run:
    """What the house did over one stretch of weather, one entry per poll."""

    battery: list[float] = field(default_factory=list)
    grid: list[float] = field(default_factory=list)
    soc: list[float] = field(default_factory=list)
    status: list[models.ControlStatus] = field(default_factory=list)


class Simulation:
    """A control manager wired to the fake inverter and stepped one poll at a time."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        soc: float = 50.0,
        charge_limit: float = 100.0,
        reserve: float = 0.0,
    ) -> None:
        self.inverter = FakeInverter(soc=soc)
        self.now = START
        monkeypatch.setattr(control_module.dt, "now", lambda: self.now)

        blocks = const.register_blocks_for(const.DEFAULT_INVERTER_MODEL)
        self.control = control_module.ControlManager(
            self.inverter,
            registers_by_key={
                register.key: register
                for block in blocks
                for register in block.registers
            },
            limits={
                const.CONF_MAX_BATTERY_CHARGED_POWER: self.inverter.charge_max,
                const.CONF_MAX_BATTERY_DISCHARGED_POWER: self.inverter.discharge_max,
            },
            inverter_model=const.DEFAULT_INVERTER_MODEL,
            enabled=True,
            scan_interval_s=POLL_S,
            on_update=Mock(),
            on_refresh=AsyncMock(),
        )
        self.control._heartbeat._supported = True
        self.control._heartbeat._last_success = self.now
        # A failed beat would otherwise wait out a real poll cycle between retries.
        self.control._heartbeat._retry_delays = (0.0,)
        # Already settled, as after the first poll of a run that is underway.
        self.control._control_stale = False
        self.control._charge_limit_soc = charge_limit
        self.control._battery_reserve_soc = reserve

    def run(
        self,
        *,
        polls: int,
        solar: float | list[float],
        house: float | list[float],
        reachable: bool = True,
    ) -> Run:
        return asyncio.run(self._async_run(polls, solar, house, reachable))

    async def _async_run(
        self,
        polls: int,
        solar: float | list[float],
        house: float | list[float],
        reachable: bool,
    ) -> Run:
        self.inverter.reachable = reachable
        self.inverter.solar, self.inverter.house = at(solar, 0), at(house, 0)
        # One command before recording, so the run starts as any other poll would.
        await self.control.async_poll(self.inverter.frame())

        run = Run()
        for step in range(polls):
            self.inverter.solar, self.inverter.house = at(solar, step), at(house, step)
            if not self.control.in_control:
                # Past its deadline the inverter drops the command and runs itself.
                self.inverter.method, self.inverter.setpoint = 0, 0
            self.inverter.settle()

            self.now += timedelta(seconds=POLL_S)
            if reachable:
                self.control._heartbeat._last_success = self.now

            frame = self.inverter.frame()
            await self.control.async_poll(frame)

            run.battery.append(frame["battery_power"])
            run.grid.append(frame["grid_power"])
            run.soc.append(frame["battery_soc"])
            run.status.append(self.control.status)
        return run


def test_a_charge_limit_holds_through_a_cycling_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tests that a 2 kW appliance cycling under a steady sun doesn't make the guard
    engage and release over and over, charging a little each time."""
    sim = Simulation(monkeypatch, soc=46.0, charge_limit=1.0, reserve=5.0)

    run = sim.run(polls=240, solar=2400, house=[700] * 8 + [2700] * 8)

    assert max(run.battery) <= const.HOLD_SETPOINT_W
    assert set(run.status) == {models.ControlStatus.CHARGE_LIMIT_REACHED}
    assert sim.inverter.method_writes == 1


def test_a_long_draw_is_left_to_the_inverter_until_the_sun_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #107: a charge limit only forbids charging, which self-consumption never
    does under a draw, and the inverter meets that draw on the meter rather than a
    poll behind. So a draw that stays clear is handed back, and the guard takes over
    again on the poll the balance turns."""
    sim = Simulation(monkeypatch, soc=67.0, charge_limit=60.0)

    held = sim.run(polls=12, solar=2000, house=400)
    # One run, so each change arrives between polls rather than before a warm-up.
    run = sim.run(
        polls=120,
        solar=[1400] * 60 + [3000] * 30 + [1400] * 30,
        # A heavy draw, a lighter one, the sun, and the heavy draw again.
        house=[3600] * 40 + [1800] * 20 + [400] * 30 + [3600] * 30,
    )

    assert max(held.battery) <= const.HOLD_SETPOINT_W
    # Only the poll the second draw arrives on is behind; the warm-up saw the first.
    assert [step for step, watts in enumerate(run.grid) if watts > 0] == [90]
    # The sun outruns the poll once: a few Wh against a band of several percent.
    charging = [
        step for step, watts in enumerate(run.battery) if watts > const.HOLD_SETPOINT_W
    ]
    assert charging == [60]
    assert set(run.status) == {models.ControlStatus.CHARGE_LIMIT_REACHED}
    # Taken, left for the draw and through the lighter one, taken for the sun, and
    # left again: each switch waits for a lasting change.
    assert sim.inverter.method_writes == 4


def test_a_hand_back_caught_charging_is_not_repeated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Left to itself the inverter follows the app, and an app schedule charging from
    the grid is exactly what a charge limit forbids. Seen once, the guard keeps the
    inverter until it releases rather than handing it back every minute."""
    sim = Simulation(monkeypatch, soc=67.0, charge_limit=60.0)
    sim.inverter.app_charge = 1500.0

    sim.run(polls=12, solar=2000, house=400)
    run = sim.run(polls=120, solar=1400, house=3600)

    assert sum(watts > const.HOLD_SETPOINT_W for watts in run.battery) == 1
    assert sim.inverter.method_writes == 3


def test_a_guard_gives_a_slow_inverter_time_to_follow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #107 again: the Ocean 2 sets its battery back after every setpoint write,
    and a guard retuning on every wobble of the house kept it there, importing most
    of what it asked for. A retune within tolerance now waits out the last one."""
    sim = Simulation(monkeypatch, soc=67.0, charge_limit=60.0)
    sim.run(polls=12, solar=2000, house=400)
    sim.inverter.setback_polls = 2

    run = sim.run(polls=120, solar=0, house=[300, 450, 320, 470, 310, 440])

    imported = sum(max(watts, 0.0) for watts in run.grid) / len(run.grid)
    assert imported < 100
    assert sim.inverter.setpoint_writes <= 1 + len(run.grid) * POLL_S / (
        const.GUARD_RETUNE_S
    )


def test_a_guard_never_imports_what_the_battery_could_have_covered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tracked setpoint is what the house draws through, so anything it leaves
    behind is bought from the grid every poll it stays behind. Erring the other way
    spills the remainder into the grid instead, which costs nothing to buy."""
    for solar, house in (
        # A fractional draw, as the float registers report one, so which way the odd
        # watt is rounded shows up in the grid rather than cancelling out. Small
        # enough to stay tracked rather than left to the inverter.
        (147.0, 280.4),
        # And a draw smaller than the rewrite step, which still has to be covered
        # rather than rounded away in the grid's favour.
        (460.0, 500.0),
    ):
        sim = Simulation(monkeypatch, soc=60.0, charge_limit=1.0)
        run = sim.run(polls=60, solar=solar, house=house)

        assert max(run.grid) <= 0
        # Spilling is the lesser evil, not a licence to empty the battery to the grid.
        assert min(run.grid) >= -const.GUARD_TRACKING_STEP_W
        # A steady house is worth one setpoint, not one per poll.
        assert sim.inverter.setpoint_writes == 1


def test_a_guard_only_falls_behind_the_poll_an_unexpected_load_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A load nothing could have seen coming is carried by the grid for the one poll
    it takes to measure it. What must not happen is that shortfall settling in and
    being bought again on every poll after the setpoint has caught up."""
    for house, late_polls in (
        # A 2 kW appliance: one poll behind on each of the six times it switches on,
        # and left to the inverter once it has run for a minute.
        ([500.0] * 20 + [2500.0] * 20, 6),
        # A step smaller than the rewrite deadband, which is caught once and then
        # covered for good, the setpoint spilling into the grid on the low half.
        ([500.0] * 20 + [560.0] * 20, 1),
    ):
        sim = Simulation(monkeypatch, soc=60.0, charge_limit=1.0)
        run = sim.run(polls=240, solar=400, house=house)

        assert max(run.battery) <= const.HOLD_SETPOINT_W
        assert set(run.status) == {models.ControlStatus.CHARGE_LIMIT_REACHED}
        late = [step for step, watts in enumerate(run.grid) if watts > 0]
        # Never two polls running: a catch-up, not a standing shortfall.
        assert len(late) == late_polls
        assert all(later - earlier > 1 for earlier, later in zip(late, late[1:]))


def test_a_reserve_lets_the_battery_refill_once_the_sun_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tests that setting a reserve allows the battery to charge when the sun is back."""
    sim = Simulation(monkeypatch, soc=20.0, reserve=20.0)

    evening = sim.run(polls=60, solar=0, house=1000)

    assert min(evening.battery) >= 0
    assert max(evening.grid) >= 900

    morning = sim.run(polls=240, solar=3000.6, house=800.0)

    assert morning.soc[-1] > 20
    # Charging takes less than the surplus, so the remainder leaves rather than
    # being topped up from the grid.
    assert max(morning.grid) <= 0


def test_an_untouched_install_never_touches_the_inverter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both guards are off by default, so a whole day must leave the app alone."""
    sim = Simulation(monkeypatch)

    run = sim.run(
        polls=240,
        solar=[0] * 20 + [4000] * 20,
        house=[400] * 7 + [2500] * 7,
    )

    assert sim.inverter.method_writes == 0
    assert set(run.status) == {models.ControlStatus.AUTOMATIC}


def test_a_charge_limit_survives_an_hour_of_broken_weather(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Solar and load crossing each other at different periods put the balance either
    side of zero hundreds of times, and none of it may reach the battery."""
    sim = Simulation(monkeypatch, soc=60.0, charge_limit=1.0)
    sim.run(polls=12, solar=2000, house=400)

    run = sim.run(
        polls=720,
        solar=[0] * 37 + [5000] * 37,
        house=[400] * 11 + [3000] * 11,
    )

    assert max(run.battery) <= const.HOLD_SETPOINT_W
    assert run.soc[-1] <= run.soc[0]
    # Every one of those crossings is a setpoint, and none of them a method: no draw
    # here lasts the minute it takes to be worth handing back.
    assert sim.inverter.method_writes == 1


def test_the_guard_comes_back_after_the_link_drops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing the link past the inverter's deadline hands it to the app, which charges
    from the surplus. What matters is that the guard returns with the link."""
    sim = Simulation(monkeypatch, soc=46.0, charge_limit=1.0)

    held = sim.run(polls=30, solar=3000, house=800)
    lost = sim.run(polls=30, solar=3000, house=800, reachable=False)
    regained = sim.run(polls=30, solar=3000, house=800)

    assert max(held.battery) <= const.HOLD_SETPOINT_W
    assert max(lost.battery) > 2000
    assert lost.status[-1] is models.ControlStatus.NO_MODBUS_CONTROL
    assert max(regained.battery) <= const.HOLD_SETPOINT_W


def test_a_reserve_above_the_charge_limit_freezes_the_battery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing stops the two guards being set to overlap, and between them the battery
    is pinned in both directions. Worth knowing about rather than discovering."""
    sim = Simulation(monkeypatch, soc=55.0, charge_limit=50.0, reserve=60.0)

    run = sim.run(polls=120, solar=[0] * 13 + [4000] * 13, house=1000)

    assert max(abs(watts) for watts in run.battery) <= const.HOLD_SETPOINT_W
    assert run.soc[-1] == run.soc[0]


def test_a_full_battery_leaves_the_inverter_to_balance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Holding a full battery forbids nothing and leaves curtailing the array as the
    only way to balance, so the guard steps aside: the one hand-back left."""
    sim = Simulation(monkeypatch, soc=100.0, charge_limit=80.0)

    run = sim.run(polls=60, solar=4000, house=800)

    assert set(run.status) == {models.ControlStatus.CHARGE_LIMIT_REACHED}
    assert max(run.battery) == 0
    assert sim.inverter.method_writes == 0
