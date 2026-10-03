"""Closed-loop scenarios: the real control manager against an inverter that answers.

The unit tests feed one frame and check one command. A control loop goes wrong over a
sequence instead, so these run minutes of simulated weather and load and assert what
must never happen across the whole run.
"""

from __future__ import annotations

import asyncio
import math
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
SYSTEM_SETPOINT_REGISTER: Final = const.REGISTERS_BY_KEY[
    "system_power_setpoint"
].address
SYSTEM_FEED: Final = models.ControlMode.SYSTEM_FEED.command_value
AUTOMATIC: Final = models.ControlMode.DEFAULT.command_value


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
    # The meter setpoint, positive for a draw, which the system feed method holds.
    system_setpoint: int = 0
    system_setpoint_writes: int = 0
    connected: bool = True
    reachable: bool = True
    # Polls the battery stays put after each new setpoint.
    setback_polls: int = 0
    setback: int = 0
    # New setpoints sent before the battery reached the previous one.
    interruptions: int = 0
    # Most the battery power can change per poll, if limited.
    ramp_w: float | None = None
    # The export limit, above which the solar is curtailed: infinite for the
    # unlimited feed mode, and None for a frame that does not report one at all.
    export_cap: float | None = None
    # What the export limit took from the solar on the last poll.
    curtailed: float = 0.0

    async def async_write(
        self, address: int, words: list[int], *, what: str = ""
    ) -> None:
        if not self.reachable:
            raise control_module.HomeAssistantError("the inverter is unreachable")
        if address not in (
            const.CONTROL_COMMAND_REGISTER,
            SETPOINT_REGISTER,
            SYSTEM_SETPOINT_REGISTER,
        ):
            return  # the heartbeat, which carries nothing this simulation needs
        value = (words[0] << 16) | words[1]
        if address == SYSTEM_SETPOINT_REGISTER:
            self.system_setpoint_writes += 1
            self.system_setpoint = value - (1 << 32) if value >> 31 else value
            return
        if address == const.CONTROL_COMMAND_REGISTER:
            self.method_writes += 1
            self.method = (
                value >> const.CONTROL_COMMAND_METHOD_SHIFT
            ) & const.CONTROL_COMMAND_METHOD_MASK
        elif address == SETPOINT_REGISTER:
            self.setpoint_writes += 1
            self.setpoint = value - (1 << 32) if value >> 31 else value
            self.interruptions += self.setback > 0
            self.setback = self.setback_polls

    def settle(self) -> None:
        """Obey the standing command, or run self-consumption where there is none."""
        commanded = self.method == BATTERY_LIMITS and self.setpoint != 0
        if commanded and self.setback:
            self.setback -= 1
            target = self.battery
        elif commanded:
            target = float(self.setpoint)
        elif self.method == SYSTEM_FEED and self.system_setpoint != 0:
            # The grid pinned at the setpoint: the battery takes whatever balances.
            target = self.solar + self.system_setpoint - self.house
        else:
            target = self.solar - self.house
        if self.ramp_w is not None:
            step = max(-self.ramp_w, min(self.ramp_w, target - self.battery))
            target = self.battery + step
        ceiling = self.charge_max if self.soc < 100.0 else 0.0
        floor = -self.discharge_max if self.soc > 0.0 else 0.0
        self.battery = max(floor, min(ceiling, target))
        moved = self.battery * POLL_S / 36.0 / self.capacity_wh
        self.soc = max(0.0, min(100.0, self.soc + moved))
        export = self.solar - self.house - self.battery
        self.curtailed = (
            max(0.0, export - self.export_cap) if self.export_cap is not None else 0.0
        )

    def frame(self) -> dict[str, float | models.GridFeedMode]:
        solar = self.solar - self.curtailed
        frame: dict[str, float | models.GridFeedMode] = {
            "solar_power": solar,
            "house_power": self.house,
            "battery_power": self.battery,
            "grid_power": self.house - solar + self.battery,
            # Whole percent, as the inverter reports it.
            "battery_soc": float(round(self.soc)),
        }
        if self.export_cap is not None and math.isinf(self.export_cap):
            frame["grid_feed_mode"] = models.GridFeedMode.UNLIMITED
        elif self.export_cap is not None:
            frame["grid_feed_mode"] = models.GridFeedMode.LIMITED
            frame[const.FEED_IN_POWER_MAX_KEY] = self.export_cap
        return frame


@dataclass
class Run:
    """What the house did over one stretch of weather, one entry per poll."""

    battery: list[float] = field(default_factory=list)
    grid: list[float] = field(default_factory=list)
    soc: list[float] = field(default_factory=list)
    status: list[models.ControlStatus] = field(default_factory=list)
    curtailed: list[float] = field(default_factory=list)


class Simulation:
    """A control manager wired to the fake inverter and stepped one poll at a time."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        soc: float = 50.0,
        charge_limit: float = 100.0,
        reserve: float = 0.0,
        export_cap: float | None = None,
    ) -> None:
        self.inverter = FakeInverter(soc=soc, export_cap=export_cap)
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
            write_setting=AsyncMock(),
            on_command_expired=Mock(),
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
            run.curtailed.append(self.inverter.curtailed)
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
    """A charge limit only forbids charging, and the inverter never charges while the
    house uses more than the solar. So once such a draw has lasted a while, the
    inverter is left to cover it by itself, and we take over again as soon as the sun
    comes back."""
    sim = Simulation(monkeypatch, soc=67.0, charge_limit=60.0)

    held = sim.run(polls=12, solar=2000, house=400)
    # One run, since a new run's first poll sees the load before the inverter reacts.
    run = sim.run(
        polls=180,
        solar=[1400] * 60 + [3000] * 30 + [1400] * 30 + [3000] * 30 + [1400] * 30,
        # A heavy draw, a lighter one, the sun, and the heavy draw and sun again.
        house=[3600] * 40 + [1800] * 20 + ([400] * 30 + [3600] * 30) * 2,
    )

    assert max(held.battery) <= const.HOLD_SETPOINT_W
    # The grid imports only on the first poll of each later draw.
    assert [step for step, watts in enumerate(run.grid) if watts > 0] == [90, 150]
    # The battery charges for one poll each time the sun returns.
    charging = [
        step for step, watts in enumerate(run.battery) if watts > const.HOLD_SETPOINT_W
    ]
    assert charging == [60, 120]
    assert set(run.status) == {models.ControlStatus.CHARGE_LIMIT_REACHED}
    # Take control, then hand back and take back three times.
    assert sim.inverter.method_writes == 6


def test_a_cycling_load_settles_into_one_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oven switching its heater on for 90 s and off for 60 s would otherwise make
    us let go and take over on every cycle. Each time it is taken back that soon, the
    wait doubles, so it soon stays in one mode."""
    # High enough that the hour's draw never releases the guard.
    sim = Simulation(monkeypatch, soc=80.0, charge_limit=60.0)
    sim.run(polls=12, solar=2000, house=400)

    run = sim.run(polls=720, solar=1000, house=[3500] * 18 + [300] * 12)

    assert set(run.status) == {models.ControlStatus.CHARGE_LIMIT_REACHED}
    # Take control, hand back once, take back, then no more switching.
    assert sim.inverter.method_writes == 3
    assert sum(watts > const.HOLD_SETPOINT_W for watts in run.battery) <= 1


def test_a_battery_at_its_limit_is_not_held_against_the_inverter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the house uses more than the battery can give, the grid covers the rest
    whoever is in control, so the inverter is left alone. The same goes for the few
    seconds a slower inverter needs to follow a bigger draw."""
    sim = Simulation(monkeypatch, soc=67.0, charge_limit=60.0)
    sim.run(polls=12, solar=2000, house=400)
    too_much = sim.run(polls=120, solar=0, house=7000)

    slow = Simulation(monkeypatch, soc=67.0, charge_limit=60.0)
    slow.inverter.ramp_w = 400.0
    slow.run(polls=12, solar=2000, house=400)
    slow.run(polls=240, solar=0, house=[1000] * 60 + [3000] * 60)

    assert min(too_much.battery) == -sim.inverter.discharge_max
    # Take control in the sun, then hand back for the whole draw.
    assert sim.inverter.method_writes == 2
    assert slow.inverter.method_writes == 2


def test_a_guard_retunes_as_fast_as_the_inverter_can_follow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some inverters start over on every new setpoint, so correcting on every small
    change of the house kept the battery from ever getting there (issue #107). Small
    corrections now wait until the battery has reached the last setpoint, which an
    inverter that reacts within a poll always has by the next one."""
    noisy = [300, 450, 320, 470, 310, 440]

    fast = Simulation(monkeypatch, soc=67.0, charge_limit=60.0)
    fast.run(polls=12, solar=2000, house=400)
    quick = fast.run(polls=120, solar=0, house=noisy)

    slow = Simulation(monkeypatch, soc=67.0, charge_limit=60.0)
    slow.run(polls=12, solar=2000, house=400)
    slow.inverter.setback_polls = 2
    patient = slow.run(polls=120, solar=0, house=noisy)

    # The grid never imports two polls in a row.
    late = [step for step, watts in enumerate(quick.grid) if watts > 0]
    assert all(later - earlier > 1 for earlier, later in zip(late, late[1:]))

    imported = sum(max(watts, 0.0) for watts in patient.grid) / len(patient.grid)
    assert imported < 100
    assert slow.inverter.interruptions == 0


def test_a_guard_never_imports_what_the_battery_could_have_covered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tracked setpoint is what the house draws through, so anything it leaves
    behind is bought from the grid every poll it stays behind. Erring the other way
    spills the remainder into the grid instead, which costs nothing to buy."""
    for solar, house in (
        # A fractional draw, as the float registers report one, so which way the odd
        # watt is rounded shows up in the grid rather than cancelling out.
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


def test_a_guard_never_imports_across_a_hand_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A draw large enough to hand back must be covered while it is tracked, while
    the hand-back is pending and once the inverter runs itself."""
    sim = Simulation(monkeypatch, soc=60.0, charge_limit=1.0)

    run = sim.run(polls=60, solar=147.0, house=1280.4)

    assert sim.control._handback.phase is control_module.HandbackPhase.HANDED_BACK
    assert max(run.grid) <= 0
    assert min(run.grid) >= -const.GUARD_TRACKING_STEP_W


def test_a_guard_only_falls_behind_the_poll_an_unexpected_load_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A load nothing could have seen coming is carried by the grid for the one poll
    it takes to measure it. What must not happen is that shortfall settling in and
    being bought again on every poll after the setpoint has caught up."""
    for house, late_polls in (
        # A 2 kW appliance switching on six times, each one poll late.
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


def test_a_reserve_leaves_a_lasting_surplus_to_the_inverter_until_dusk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror of a charge limit: a surplus can only charge, which the reserve
    allows, and the house must not reach the battery once the sun is gone."""
    sim = Simulation(monkeypatch, soc=18.0, reserve=20.0)

    day = sim.run(polls=60, solar=3000, house=800)
    assert sim.control._handback.phase is control_module.HandbackPhase.HANDED_BACK

    dusk = sim.run(polls=60, solar=0, house=1000)

    assert max(day.grid) <= 0
    assert min(dusk.battery) >= 0
    assert sim.control._handback.phase is control_module.HandbackPhase.TRACKING


def test_a_reserve_raised_as_the_battery_fills_keeps_its_hand_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A planner that freezes the battery raises the reserve a percent above the state
    of charge every few minutes while the sun fills it. The guard stays latched
    through every rewrite, so the inverter keeps running itself rather than being
    taken back for a minute each time."""
    sim = Simulation(monkeypatch, soc=50.0, reserve=51.0)

    runs = []
    for _ in range(12):
        runs.append(sim.run(polls=60, solar=3000, house=800))
        reserve = round(sim.inverter.soc) + 1
        asyncio.run(sim.control.async_set_battery_reserve_soc(reserve))

    assert all(min(run.battery) >= 0 for run in runs)
    assert runs[-1].soc[-1] > 60
    # Take control, then hand back once for the whole hour.
    assert sim.inverter.method_writes == 2


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
    # No draw lasts long enough to hand back, so the method is written only once.
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


def select(sim: Simulation, feature: models.ControlFeature) -> None:
    """Choose a mode by hand, against the frame the inverter shows right now."""
    sim.control._data = sim.inverter.frame()
    asyncio.run(sim.control.async_select_feature(feature))


def sunny_day(polls: int, peak: float) -> list[float]:
    """Return a clear day's solar, rising from nothing to the peak and back."""
    return [peak * math.sin(math.pi * step / polls) for step in range(polls)]


CAP = 6000.0
# Where Export Solar First pins the meter with its limit left at a 6 kW cap.
AT_THE_CAP = CAP - const.SOLAR_EXPORT_CAP_MARGIN_W


def test_export_solar_first_hands_the_overflow_to_export_to_grid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 9 kW surplus against a 6 kW cap: once the hold shows the export at the cap,
    the inverter holds it just under there by itself and the battery takes the rest,
    with one write for the meter and none after."""
    sim = Simulation(monkeypatch, soc=40.0, export_cap=CAP)
    select(sim, models.ControlFeature.EXPORT_SOLAR_FIRST)

    run = sim.run(polls=120, solar=9500, house=500)

    settled = slice(2, None)
    assert sim.inverter.method == SYSTEM_FEED
    assert sim.inverter.system_setpoint == -AT_THE_CAP
    assert sim.inverter.system_setpoint_writes == 1
    assert set(run.grid[settled]) == {-AT_THE_CAP}
    assert max(run.curtailed[settled]) == 0
    assert set(run.status[settled]) == {models.ControlStatus.ACTIVE}
    # The mode at work is not a guard, so automations reading one see none.
    assert sim.control.active_guard is None


def test_export_solar_first_holds_the_battery_while_the_surplus_fits_under_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim = Simulation(monkeypatch, soc=40.0, export_cap=CAP)
    select(sim, models.ControlFeature.EXPORT_SOLAR_FIRST)

    run = sim.run(polls=60, solar=4500, house=500)

    assert max(run.battery) <= const.HOLD_SETPOINT_W
    assert min(run.grid) >= -4000.0 - const.HOLD_SETPOINT_W
    assert set(run.status) == {models.ControlStatus.BELOW_SOLAR_EXPORT_LIMIT}
    assert sim.inverter.method_writes == 1


def test_export_solar_first_follows_a_limit_set_below_the_cap_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The margin only keeps clear of the device's cap. A limit of 3 kW on an install
    without one exports 3 kW and banks the rest."""
    sim = Simulation(monkeypatch, soc=40.0, export_cap=math.inf)
    asyncio.run(
        sim.control.async_set_feature_power(
            models.ControlFeature.EXPORT_SOLAR_FIRST, 3000.0
        )
    )
    select(sim, models.ControlFeature.EXPORT_SOLAR_FIRST)

    run = sim.run(polls=60, solar=6500, house=500)

    assert set(run.grid[2:]) == {-3000.0}
    assert run.battery[-1] == 3000.0


def test_export_solar_first_exports_all_of_it_without_a_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the limit left at the inverter's maximum and nothing capping the export,
    solar goes to the grid first and the battery takes none of it."""
    sim = Simulation(monkeypatch, soc=40.0, export_cap=math.inf)
    select(sim, models.ControlFeature.EXPORT_SOLAR_FIRST)

    run = sim.run(polls=60, solar=4500, house=500)

    assert max(run.battery) <= const.HOLD_SETPOINT_W
    assert set(run.status) == {models.ControlStatus.BELOW_SOLAR_EXPORT_LIMIT}


def test_export_solar_first_holds_again_before_the_battery_exports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Export to Grid would keep the export up from the battery once a cloud takes the
    overflow. The hold is back on the next poll, so that lasts one poll at most."""
    sim = Simulation(monkeypatch, soc=40.0, export_cap=CAP)
    select(sim, models.ControlFeature.EXPORT_SOLAR_FIRST)
    sim.run(polls=20, solar=9500, house=500)

    run = sim.run(polls=24, solar=4500, house=500)

    assert sum(watts < -const.GUARD_POWER_DEADBAND_W for watts in run.battery) <= 1
    assert sim.inverter.method == BATTERY_LIMITS
    assert run.grid[-1] == pytest.approx(-4000.0, abs=const.HOLD_SETPOINT_W)
    assert run.status[-1] is models.ControlStatus.BELOW_SOLAR_EXPORT_LIMIT


def test_export_solar_first_leaves_the_evening_to_the_inverter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the house draws from the grid, Automatic covers it from the battery."""
    sim = Simulation(monkeypatch, soc=70.0, export_cap=CAP)
    select(sim, models.ControlFeature.EXPORT_SOLAR_FIRST)
    sim.run(polls=12, solar=4500, house=500)

    run = sim.run(polls=60, solar=0, house=1500)

    # The grid only covers the poll the load arrives on.
    assert sum(watts > 0 for watts in run.grid) <= 1
    assert sim.inverter.method == AUTOMATIC
    assert run.status[-1] is models.ControlStatus.AUTOMATIC


def test_a_mode_left_early_waits_before_it_is_picked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing clouds would switch the method every poll. A mode left within a minute
    waits a minute before it may be picked again, and twice that the next time."""
    sim = Simulation(monkeypatch, soc=40.0, export_cap=CAP)
    select(sim, models.ControlFeature.EXPORT_SOLAR_FIRST)
    sim.run(polls=4, solar=9500, house=500)
    sim.run(polls=2, solar=4500, house=500)

    waiting = sim.run(polls=10, solar=9500, house=500)
    assert sim.inverter.method == BATTERY_LIMITS
    assert set(waiting.status) == {models.ControlStatus.BELOW_SOLAR_EXPORT_LIMIT}

    sim.run(polls=4, solar=9500, house=500)
    assert sim.inverter.method == SYSTEM_FEED
    sim.run(polls=2, solar=4500, house=500)
    cooldown = sim.control._solar_first.cooldown_s[models.ControlFeature.EXPORT_TO_GRID]
    assert cooldown == 2 * const.GUARD_HANDBACK_S


def test_export_solar_first_with_the_export_off_is_left_as_automatic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the Grid Feed-in switch off there is nothing to export first, so filling
    the battery first is all there is, and the inverter does that by itself."""
    sim = Simulation(monkeypatch, soc=40.0, export_cap=0.0)
    select(sim, models.ControlFeature.EXPORT_SOLAR_FIRST)

    run = sim.run(polls=60, solar=4500, house=500)

    assert min(run.battery) > 3000.0
    assert max(run.curtailed) == 0
    assert sim.inverter.method_writes == 0
    assert set(run.status) == {models.ControlStatus.AUTOMATIC}


def test_export_solar_first_holds_at_the_charge_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Export to Grid charges the battery, so the charge limit holds it instead."""
    sim = Simulation(monkeypatch, soc=75.0, charge_limit=80.0, export_cap=CAP)
    select(sim, models.ControlFeature.EXPORT_SOLAR_FIRST)
    sim.run(polls=20, solar=9500, house=500)
    assert sim.inverter.method == SYSTEM_FEED

    run = sim.run(polls=120, solar=9500, house=500)

    assert run.status[-1] is models.ControlStatus.CHARGE_LIMIT_REACHED
    assert sim.inverter.method == BATTERY_LIMITS
    assert run.battery[-1] <= const.HOLD_SETPOINT_W


def test_export_solar_first_holds_at_the_battery_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Automatic discharges the battery, so the reserve holds it instead."""
    sim = Simulation(monkeypatch, soc=10.0, reserve=10.0, export_cap=CAP)
    select(sim, models.ControlFeature.EXPORT_SOLAR_FIRST)

    run = sim.run(polls=60, solar=0, house=1500)

    assert max(-watts for watts in run.battery) <= const.HOLD_SETPOINT_W
    assert sim.inverter.method == BATTERY_LIMITS
    assert set(run.status) == {models.ControlStatus.RESERVE_REACHED}


def test_export_solar_first_saves_the_midday_solar_that_automatic_curtails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over a clear day with a 6 kW export cap, Automatic fills the battery by late
    morning and curtails the midday peak. Export Solar First keeps the room for it,
    switching between the inverter's own modes a handful of times a day."""
    polls = 8 * 720
    # About 5.8 kWh above the limit, which a battery at 20% has room for.
    solar = sunny_day(polls, peak=8500)

    automatic = Simulation(monkeypatch, soc=20.0, export_cap=CAP)
    automatic_run = automatic.run(polls=polls, solar=solar, house=500)

    solar_first = Simulation(monkeypatch, soc=20.0, export_cap=CAP)
    select(solar_first, models.ControlFeature.EXPORT_SOLAR_FIRST)
    solar_first_run = solar_first.run(polls=polls, solar=solar, house=500)

    def kwh(watts: list[float]) -> float:
        return sum(watts) * POLL_S / 3_600_000

    assert kwh(automatic_run.curtailed) > 4.0
    assert kwh(solar_first_run.curtailed) < 0.01
    # The trade-off: without an expiry the battery ends the day less full.
    assert solar_first_run.soc[-1] < automatic_run.soc[-1]
    inverter = solar_first.inverter
    assert inverter.setpoint_writes + inverter.system_setpoint_writes <= 6
    assert inverter.method_writes <= 6
