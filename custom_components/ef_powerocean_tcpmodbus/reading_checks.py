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

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, ClassVar, Final

from .const import BATTERY_SOC_KEYS, CONTROL_STATUS_DAMPING_POLLS
from .models import ControlStatus, DeviceReport

_LOGGER = logging.getLogger(__name__)

# A system level of 0 is only believed while no pack in use reports more than this,
# or when the last level believed was at most twice it: a battery running empty
# passes through the low levels, a bad read drops to 0 from anywhere.
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

    def onset(self, now: datetime, /) -> datetime:
        """Return when what this check fails on began, once it fails at *now*."""
        return now


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

    def __init__(self) -> None:
        # The last system level that passed; unknown after a start.
        self._last_believed: float | None = None

    def check(self, data: Mapping[str, Any], _now: datetime, /) -> bool:
        soc = data.get("battery_soc")
        if soc is None:
            return False
        count = int(data.get("battery_count") or 0)
        packs = [
            float(pack)
            for key in BATTERY_SOC_KEYS[:count]
            if (pack := data.get(key)) is not None
        ]
        fails = (
            float(soc) <= 0
            and any(pack > SOC_PACK_MARGIN for pack in packs)
            and (
                self._last_believed is None or self._last_believed > 2 * SOC_PACK_MARGIN
            )
        )
        if not fails:
            self._last_believed = float(soc)
        return fails


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

    def onset(self, now: datetime, /) -> datetime:
        # The readings stopped moving before the check could tell.
        return self._since or now


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

    A check counts once it fails this many polls in a row, each counted on its own,
    so one odd read neither interrupts control nor raises anything, and a check
    that keeps failing holds even while one above it comes and goes.
    """

    def __init__(self) -> None:
        self._checks: tuple[Check, ...] = tuple(check() for check in CHECKS)
        # Polls in a row each check has failed, and since when.
        self._polls: dict[str, int] = {}
        self._since: dict[str, datetime] = {}
        # Checks whose error has been logged, so a broken one does not flood the log.
        self._broken: set[str] = set()
        # The failure that holds, if any: the first in CHECKS that has held.
        self.active: Finding | None = None
        # Whether this poll's readings contradict each other, whether or not that
        # has held long enough to count: such a frame never moves a guard. Kept
        # until the next poll, so a user action re-applying that poll's inputs
        # inherits its verdict.
        self.suspect = False

    def run(self, data: Mapping[str, Any], now: datetime) -> None:
        # Every check sees every poll, since some keep a history.
        failed = [check for check in self._checks if self._failed(check, data, now)]
        self.suspect = any(
            check.status is ControlStatus.INCONSISTENT_READINGS for check in failed
        )
        held = next(
            (
                check
                for check in failed
                if self._polls[check.name] >= CONTROL_STATUS_DAMPING_POLLS
            ),
            None,
        )
        if held is None:
            self.active = None
        elif self.active is None or self.active.status is not held.status:
            self.active = Finding(held.status, held.name, self._since[held.name])
        elif self.active.check != held.name:
            # Another cause for the same state: it has lasted since the first.
            self.active = replace(self.active, check=held.name)

    def _failed(self, check: Check, data: Mapping[str, Any], now: datetime) -> bool:
        """Run one check, counting its failures; an error in it passes the poll."""
        try:
            failed = check.check(data, now)
        except Exception:
            if check.name not in self._broken:
                self._broken.add(check.name)
                _LOGGER.exception("The %s reading check could not run", check.name)
            failed = False
        if failed:
            self._polls[check.name] = self._polls.get(check.name, 0) + 1
            self._since.setdefault(check.name, check.onset(now))
        else:
            self._polls.pop(check.name, None)
            self._since.pop(check.name, None)
        return failed

    def dump_state(self) -> dict[str, bool]:
        """Return what the checks learned about the model, to survive a restart."""
        state = {"grid_seen": False, "bms_seen": False}
        for check in self._checks:
            if isinstance(check, GridVoltageLost):
                state["grid_seen"] = check._grid_seen
            if isinstance(check, BatteryDisconnected):
                state["bms_seen"] = check._bms_seen
        return state

    def load_state(self, stored: Mapping[str, Any]) -> None:
        """Restore what the checks learned about the model before a restart."""
        for check in self._checks:
            if isinstance(check, GridVoltageLost) and stored.get("grid_seen"):
                check._grid_seen = True
            if isinstance(check, BatteryDisconnected) and stored.get("bms_seen"):
                check._bms_seen = True
