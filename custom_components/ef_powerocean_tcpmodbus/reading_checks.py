"""Checks of what each poll's readings say about the inverter.

Some conditions mean the inverter is best left to itself: it reports running
off-grid or its battery disconnected, or its readings contradict each other, such
as a system state of charge of 0 while every pack reports 99 % (#32, #200), or a
frame that no longer changes once its telemetry stops updating.

The checks run on every poll, whoever is in control, since a bad reading also
misleads the sensors and anything built on them. Control steps aside while one has
failed; the coordinator announces it and raises it in repairs once it lasts.

Each check is one class: the data keys it reads, the Control Status it stands for,
and check(), which returns whether the frame fails it. A false alarm only hands the
inverter its own logic for a while, so a check errs on the side of staying quiet in
normal operation rather than catching every case.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, ClassVar, Final

from .const import BATTERY_SOC_KEYS, CONTROL_STATUS_DAMPING_POLLS
from .models import ControlStatus, DeviceReport

# A system level of 0 is only believed while no pack in use reports more than this.
SOC_PACK_MARGIN: Final = 5.0
# Readings that have not moved at all for this long have stopped updating.
FROZEN_AFTER: Final = timedelta(minutes=2)
# Grid-side voltage above this shows a grid is there; at or below the lower one,
# on every phase that reports, it is gone.
GRID_PRESENT_V: Final = 100.0
GRID_ABSENT_V: Final = 50.0

_STATUS_KEYS: Final = frozenset({"system_modes", "system_state_2"})
_GRID_VOLTAGE_KEYS: Final = ("grid_voltage_l1", "grid_voltage_l2", "grid_voltage_l3")
# On a live grid these jitter from poll to poll, whatever the power flows do.
_JITTER_KEYS: Final = (
    "frequency",
    "voltage_l1",
    "voltage_l2",
    "voltage_l3",
    *_GRID_VOLTAGE_KEYS,
    "battery_voltage",
)


class Check:
    """One check of a poll's readings; see the module docstring."""

    name: ClassVar[str]
    status: ClassVar[ControlStatus]
    keys: ClassVar[frozenset[str]]

    def check(self, data: Mapping[str, Any], now: datetime, /) -> bool:
        raise NotImplementedError


class OffGridReported(Check):
    """The inverter reports running off-grid, as during an outage."""

    name = "off_grid"
    status = ControlStatus.OFF_GRID
    keys = _STATUS_KEYS

    def check(self, data: Mapping[str, Any], _now: datetime, /) -> bool:
        report = DeviceReport.from_data(data)
        return report is not None and report.off_grid


class GridVoltageLost(Check):
    """The grid-side voltage is gone while the inverter still reports on-grid.

    Only armed once a grid-side voltage has been seen, so a model that reads
    these registers as 0 throughout never fails it.
    """

    name = "grid_voltage"
    status = ControlStatus.OFF_GRID
    keys = frozenset(_GRID_VOLTAGE_KEYS)

    def __init__(self) -> None:
        self._grid_seen = False

    def check(self, data: Mapping[str, Any], _now: datetime, /) -> bool:
        voltages = [
            float(value)
            for key in _GRID_VOLTAGE_KEYS
            if (value := data.get(key)) is not None
        ]
        if not voltages:
            return False
        if max(voltages) > GRID_PRESENT_V:
            self._grid_seen = True
            return False
        return self._grid_seen and max(voltages) <= GRID_ABSENT_V


class BatteryDisconnected(Check):
    """The inverter reports its battery management system gone.

    Only once it has reported one, so a model that never sets the bit never fails.
    """

    name = "bms"
    status = ControlStatus.BATTERY_DISCONNECTED
    keys = _STATUS_KEYS

    def __init__(self) -> None:
        self._bms_seen = False

    def check(self, data: Mapping[str, Any], _now: datetime, /) -> bool:
        report = DeviceReport.from_data(data)
        if report is None:
            return False
        if report.bms_connected:
            self._bms_seen = True
        return self._bms_seen and not report.bms_connected


class SystemSocMismatch(Check):
    """The system state of charge reads 0 while a pack in use reports more."""

    name = "battery_soc"
    status = ControlStatus.INCONSISTENT_READINGS
    keys = frozenset({"battery_soc", "battery_count", *BATTERY_SOC_KEYS})

    def check(self, data: Mapping[str, Any], _now: datetime, /) -> bool:
        soc = data.get("battery_soc")
        if soc is None or float(soc) > 0:
            return False
        count = int(data.get("battery_count") or len(BATTERY_SOC_KEYS))
        packs = (data.get(key) for key in BATTERY_SOC_KEYS[:count])
        return any(pack is not None and float(pack) > SOC_PACK_MARGIN for pack in packs)


class FrozenReadings(Check):
    """Voltages and frequency that have not changed at all for a while."""

    name = "frozen"
    status = ControlStatus.INCONSISTENT_READINGS
    keys = frozenset(_JITTER_KEYS)

    def __init__(self) -> None:
        self._last: tuple[Any, ...] | None = None
        self._since: datetime | None = None

    def check(self, data: Mapping[str, Any], now: datetime, /) -> bool:
        readings = tuple(data.get(key) for key in _JITTER_KEYS)
        # Nothing to judge by: a model that reports none of these never fails.
        if not any(readings):
            self._last = self._since = None
            return False
        if readings != self._last or self._since is None:
            self._last, self._since = readings, now
            return False
        return now - self._since >= FROZEN_AFTER


# In order of precedence: where several fail, the first one is reported.
CHECKS: Final[tuple[type[Check], ...]] = (
    OffGridReported,
    GridVoltageLost,
    BatteryDisconnected,
    SystemSocMismatch,
    FrozenReadings,
)

# Kept polled whatever entities are enabled, as the control inputs are.
KEYS: Final = frozenset[str]().union(*(check.keys for check in CHECKS))


@dataclass(frozen=True, slots=True)
class Finding:
    """A check that has failed for long enough to act on, and since when."""

    status: ControlStatus
    check: str
    since: datetime


class ReadingChecks:
    """Runs every check on each poll and keeps the failure that holds.

    A failure counts once the same check fails this many polls in a row, so one
    odd read neither interrupts control nor raises anything.
    """

    def __init__(self) -> None:
        self._checks: tuple[Check, ...] = tuple(check() for check in CHECKS)
        self._candidate: str | None = None
        self._polls = 0
        # The failure that holds, if any.
        self.active: Finding | None = None
        # Whether this poll's readings contradict each other, whether or not that
        # has held long enough to count: such a frame never moves a guard.
        self.suspect = False

    def run(self, data: Mapping[str, Any], now: datetime) -> None:
        # Every check sees every poll, since some keep a history.
        failed = [check for check in self._checks if check.check(data, now)]
        self.suspect = any(
            check.status is ControlStatus.INCONSISTENT_READINGS for check in failed
        )
        if not failed:
            self._candidate, self._polls, self.active = None, 0, None
            return
        first = failed[0]
        if first.name == self._candidate:
            self._polls += 1
        else:
            self._candidate, self._polls = first.name, 1
        held = self._polls >= CONTROL_STATUS_DAMPING_POLLS
        if held and (self.active is None or self.active.check != first.name):
            self.active = Finding(first.status, first.name, now)
