"""Checks of what each poll's readings say about the inverter.

Some conditions mean the inverter is best left to itself: it reports running
off-grid or its battery disconnected, or its readings contradict each other, such
as a system state of charge of 0 while every pack reports 99 % (#32, #200), or a
frame that no longer changes once its telemetry stops updating.

The checks run on every poll, whoever is in control, since a bad reading also
misleads the sensors and anything built on them. Control steps aside while one has
failed; the coordinator announces it, and Control Status shows it.

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

from .const import (
    BATTERY_SOC_KEYS,
    CONTROL_STATUS_DAMPING_POLLS,
    DERIVED_FROM,
    MODBUS_REGISTERS,
)
from .models import ControlStatus, DeviceReport, Signal

_LOGGER = logging.getLogger(__name__)

# A system level of 0 is only believed when the last level believed was at most
# twice this, since a battery running empty passes through the low levels and a bad
# read drops to 0 from anywhere. With no level believed yet, as after a start, it is
# believed while no pack in use reports more than this.
SOC_PACK_MARGIN: Final = 5.0
# Readings that have not moved at all for this long have stopped updating.
FROZEN_AFTER: Final = timedelta(minutes=5)
# A counter that has not moved while its power readings add up to this much
# energy has stopped counting.
COUNTER_STALLED_AFTER_WH: Final = 1000.0
# A gap between polls longer than this, as across an outage, counts as this much.
MAX_STEP: Final = timedelta(minutes=1)
_HOUR: Final = timedelta(hours=1)
# Grid-side voltage above this shows a grid is there; at or below the lower one,
# on every phase that reports, it is gone.
GRID_PRESENT_V: Final = 100.0
GRID_ABSENT_V: Final = 50.0

_STATUS_KEYS: Final = frozenset({"system_modes", "system_state_2"})
_GRID_VOLTAGE_KEYS: Final = ("grid_voltage_l1", "grid_voltage_l2", "grid_voltage_l3")


def keys_of(*signals: Signal) -> tuple[str, ...]:
    """Return the polled keys that carry one of *signals*, in register order."""
    return tuple(
        register.key for register in MODBUS_REGISTERS if register.signal in signals
    )


# On a live grid these jitter from poll to poll, whatever the power flows do.
_JITTER_KEYS: Final = keys_of(Signal.JITTER)
# What an inverter that stopped updating stops moving, settings and counters aside.
_STALL_KEYS: Final = keys_of(Signal.JITTER, Signal.MEASURE, Signal.LEVEL)


class Movement:
    """When each reading last changed, for the checks that look for stillness.

    A reading that is missing or 0 is not reported by this model, or not now, and
    is forgotten, so it never counts as still.
    """

    def __init__(self, keys: tuple[str, ...]) -> None:
        self._keys = keys
        self._seen: dict[str, tuple[Any, datetime]] = {}

    def observe(self, data: Mapping[str, Any], now: datetime) -> None:
        for key in self._keys:
            value = data.get(key)
            if not value:
                self._seen.pop(key, None)
                continue
            last = self._seen.get(key)
            if last is None or last[0] != value:
                self._seen[key] = (value, now)

    @property
    def reported(self) -> frozenset[str]:
        """Return the keys this model reports a value for."""
        return frozenset(self._seen)

    def still(self, now: datetime) -> frozenset[str]:
        """Return the reported keys that have not changed for FROZEN_AFTER."""
        return frozenset(
            key for key, (_, since) in self._seen.items() if now - since >= FROZEN_AFTER
        )

    def still_since(self, keys: frozenset[str]) -> datetime | None:
        """Return since when the stillest of *keys* has not changed."""
        return min(
            (since for key, (_, since) in self._seen.items() if key in keys),
            default=None,
        )


class Check:
    """One check of a poll's readings; see the module docstring."""

    name: ClassVar[str]
    status: ClassVar[ControlStatus]
    keys: ClassVar[frozenset[str]]
    # Readings a failure shows to be wrong, which their sensors do not show.
    distrusts: ClassVar[frozenset[str]] = frozenset()
    # Whether a failure on its own steps control aside. One that is only evidence
    # for another check counts towards that check and is never reported itself.
    steps_aside: ClassVar[bool] = True
    # Checks whose verdict this one weighs, run before it on every poll.
    needs: ClassVar[tuple[type[Check], ...]] = ()
    # Whether the last poll failed this check, as the runner counted it.
    failed = False

    def __init__(self, *_needed: Check) -> None:
        """Take the checks named in needs, already built."""

    def check(self, data: Mapping[str, Any], now: datetime, /) -> bool:
        raise NotImplementedError

    def distrusted(self) -> frozenset[str]:
        """Return the readings the last failure showed to be wrong."""
        return self.distrusts

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
    """The system state of charge drops to 0 from a level no battery empties from.

    The packs' own levels are no help once the read is bad: in #200 a pack read 0
    alongside the system level, and the packs may not be read at all.
    """

    name = "battery_soc"
    status = ControlStatus.INCONSISTENT_READINGS
    keys = frozenset({"battery_soc", "battery_count", *BATTERY_SOC_KEYS})
    distrusts = frozenset({"battery_soc"})

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
        if self._last_believed is None:
            implausible = any(pack > SOC_PACK_MARGIN for pack in packs)
        else:
            implausible = self._last_believed > 2 * SOC_PACK_MARGIN
        fails = float(soc) <= 0 and implausible
        # A failing level is never believed, so the drop stays a bad read until a
        # real level comes back.
        if not fails:
            self._last_believed = float(soc)
        return fails


class FrozenReadings(Check):
    """Most voltages and the frequency have not changed at all for a while.

    Judged per reading, so one that still jitters does not hide the others (#227),
    and only the ones that stopped are distrusted. It takes most of them, so one
    reading a model rounds coarsely never fails it on its own.
    """

    name = "frozen"
    status = ControlStatus.INCONSISTENT_READINGS
    keys = frozenset(_JITTER_KEYS)

    def __init__(self) -> None:
        self._movement = Movement(_JITTER_KEYS)
        self._frozen: frozenset[str] = frozenset()

    def check(self, data: Mapping[str, Any], now: datetime, /) -> bool:
        self._movement.observe(data, now)
        reported = self._movement.reported
        self._frozen = self._movement.still(now)
        # Nothing to judge by: a model that reports none of these never fails.
        return bool(reported) and 2 * len(self._frozen) > len(reported)

    def distrusted(self) -> frozenset[str]:
        return self._frozen

    def onset(self, now: datetime, /) -> datetime:
        # The readings stopped moving before the check could tell.
        return self._movement.still_since(self._frozen) or now


class CounterStalled(Check):
    """An energy counter stands still while the power it counts keeps flowing.

    Counted from the power readings themselves, so a counter only fails once they
    say COUNTER_STALLED_AFTER_WH went by without it moving at all. Evidence for
    TelemetryStalled only: a model that updates a counter in coarse steps should
    not step control aside on its own.
    """

    name = "counter_stalled"
    status = ControlStatus.INCONSISTENT_READINGS
    steps_aside = False
    # Each counter, the power it counts, and the sign that power has while it does.
    _PAIRS: ClassVar[tuple[tuple[str, str, int], ...]] = (
        ("solar_total", "solar_power", 1),
        ("grid_import_total", "grid_power", 1),
        ("grid_export_total", "grid_power", -1),
    )
    keys = frozenset(key for pair in _PAIRS for key in pair[:2])

    def __init__(self) -> None:
        self._at: datetime | None = None
        # Per counter: the value last seen, since when, and the energy since.
        self._last: dict[str, tuple[float, datetime, float]] = {}
        self._stalled: frozenset[str] = frozenset()

    def check(self, data: Mapping[str, Any], now: datetime, /) -> bool:
        step = timedelta() if self._at is None else min(now - self._at, MAX_STEP)
        self._at = now
        stalled: set[str] = set()
        for counter, power_key, sign in self._PAIRS:
            # The device's own count, not the total held back from it.
            raw = data.get(f"{counter}_raw")
            total = raw if raw is not None else data.get(counter)
            power = data.get(power_key)
            last = self._last.get(counter)
            if total is None or power is None:
                self._last.pop(counter, None)
                continue
            if last is None or last[0] != total:
                self._last[counter] = (float(total), now, 0.0)
                continue
            energy_wh = last[2] + max(sign * float(power), 0.0) * (step / _HOUR)
            self._last[counter] = (last[0], last[1], energy_wh)
            if energy_wh >= COUNTER_STALLED_AFTER_WH:
                stalled.add(counter)
        self._stalled = frozenset(stalled)
        return bool(stalled)

    def onset(self, now: datetime, /) -> datetime:
        return min((self._last[counter][1] for counter in self._stalled), default=now)


class TelemetryStalled(Check):
    """The inverter answers, but its readings stopped updating (#227).

    Frozen voltages alone can be a meter the inverter lost, and a still counter can
    be a model that counts coarsely. Both at once is the inverter serving readings
    it no longer updates. Every measured reading that has not moved is then
    distrusted, while those that still move, such as the grid power from the
    meter, stay shown.
    """

    name = "telemetry_stalled"
    status = ControlStatus.INCONSISTENT_READINGS
    needs = (FrozenReadings, CounterStalled)
    keys = frozenset(_STALL_KEYS)

    def __init__(self, *evidence: Check) -> None:
        self._evidence = evidence
        self._movement = Movement(_STALL_KEYS)
        self._stalled: frozenset[str] = frozenset()

    def check(self, data: Mapping[str, Any], now: datetime, /) -> bool:
        self._movement.observe(data, now)
        if not all(check.failed for check in self._evidence):
            self._stalled = frozenset()
            return False
        self._stalled = self._movement.still(now)
        return True

    def distrusted(self) -> frozenset[str]:
        return self._stalled

    def onset(self, now: datetime, /) -> datetime:
        return min(
            (check.onset(now) for check in self._evidence if check.failed),
            default=now,
        )


def with_derived(keys: frozenset[str]) -> frozenset[str]:
    """Add the values calculated from any of *keys*, as wrong as their sources."""
    return keys | {key for key, sources in DERIVED_FROM.items() if keys & sources}


# In order of precedence: where several fail, the first one is reported.
CHECKS: Final[tuple[type[Check], ...]] = (
    OffGridReported,
    GridVoltageLost,
    BatteryDisconnected,
    TelemetryStalled,
    SystemSocMismatch,
    FrozenReadings,
    CounterStalled,
)

# Kept polled whatever entities are enabled, as the control inputs are.
KEYS: Final = frozenset[str]().union(*(check.keys for check in CHECKS))
KEYS_BY_CHECK: Final = {check.name: check.keys for check in CHECKS}


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
        built: dict[type[Check], Check] = {}
        # A check that weighs others is built after them, and runs after them.
        for check in sorted(CHECKS, key=lambda check: bool(check.needs)):
            built[check] = check(*(built[need] for need in check.needs))
        self._run_order: tuple[Check, ...] = tuple(built.values())
        self._checks: tuple[Check, ...] = tuple(built[check] for check in CHECKS)
        # Polls in a row each check has failed, and since when.
        self._polls: dict[str, int] = {}
        self._since: dict[str, datetime] = {}
        # Checks whose error has been logged, so a broken one does not flood the log.
        self._broken: set[str] = set()
        # The failure that holds, if any: the first in CHECKS that has held.
        self.active: Finding | None = None
        # The checks this poll failed, whether or not that has held yet.
        self.failing: frozenset[str] = frozenset()
        # The readings this poll's failures show to be wrong.
        self.distrusted: frozenset[str] = frozenset()
        # Whether this poll's readings contradict each other, whether or not that
        # has held long enough to count: such a frame never moves a guard. Kept
        # until the next poll, so a user action re-applying that poll's inputs
        # inherits its verdict.
        self.suspect = False

    def run(self, data: Mapping[str, Any], now: datetime) -> None:
        # Every check sees every poll, since some keep a history.
        for check in self._run_order:
            check.failed = self._failed(check, data, now)
        failed = [check for check in self._checks if check.failed]
        self.failing = frozenset(check.name for check in failed)
        self.distrusted = with_derived(
            frozenset().union(*(check.distrusted() for check in failed))
        )
        self.suspect = any(
            check.steps_aside and check.status is ControlStatus.INCONSISTENT_READINGS
            for check in failed
        )
        held = next(
            (
                check
                for check in failed
                if check.steps_aside
                and self._polls[check.name] >= CONTROL_STATUS_DAMPING_POLLS
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
