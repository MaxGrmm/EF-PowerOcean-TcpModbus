"""A control manager against a fake inverter that answers, stepped one poll at a time.

Shared by the closed-loop scenarios and the behaviour tests. The fake follows every
command at once and exactly, so what a run shows is the control manager's decisions.
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
    # The method word the inverter was told after each poll.
    method: list[int] = field(default_factory=list)
    # Whether each guard is engaged, its state of charge at its limit, whether or not
    # anything the mode does is being stopped by it.
    charge_guard: list[bool] = field(default_factory=list)
    reserve_guard: list[bool] = field(default_factory=list)


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
        model: models.InverterModel = const.DEFAULT_INVERTER_MODEL,
    ) -> None:
        self.inverter = FakeInverter(soc=soc, export_cap=export_cap)
        self.now = START
        monkeypatch.setattr(control_module.dt, "now", lambda: self.now)

        blocks = const.register_blocks_for(model)
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
            inverter_model=model,
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
            run.method.append(self.inverter.method)
            run.charge_guard.append(self.control._charge_guard)
            run.reserve_guard.append(self.control._reserve_guard)
        return run


def select(sim: Simulation, feature: models.ControlFeature) -> None:
    """Choose a mode by hand, against the frame the inverter shows right now."""
    sim.control._data = sim.inverter.frame()
    asyncio.run(sim.control.async_select_feature(feature))


def sunny_day(polls: int, peak: float) -> list[float]:
    """Return a clear day's solar, rising from nothing to the peak and back."""
    return [peak * math.sin(math.pi * step / polls) for step in range(polls)]
