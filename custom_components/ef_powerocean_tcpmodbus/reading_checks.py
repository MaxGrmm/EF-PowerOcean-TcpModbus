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
from dataclasses import dataclass, field, replace
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
# A house draws at least about this much at any hour: fridge, router, standby.
HOUSE_FLOOR_W: Final = 100
# Energy counters step in 0.01 kWh (#227 read 20719.85), so at HOUSE_FLOOR_W the
# counters that supply the house move every 6 minutes. Still for this long, they
# missed about 3 steps (33 Wh), more than a late update.
SUPPLY_STILL_AFTER: Final = timedelta(minutes=20)
# Readings that stood still for this long while the voltages are frozen show a
# stalled inverter even with no counter to prove it, as at night.
STALLED_WITHOUT_COUNTERS: Final = timedelta(minutes=30)
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

    A missing reading is not reported by this model, or not now, and is
    forgotten, so it never counts as still. So is a 0 where *zero_is_missing*,
    as for voltages a model does not measure, but a power can really stand at 0.
    """

    def __init__(self, keys: tuple[str, ...], *, zero_is_missing: bool) -> None:
        self._keys = keys
        self._zero_is_missing = zero_is_missing
        self._seen: dict[str, tuple[Any, datetime]] = {}

    def observe(self, data: Mapping[str, Any], now: datetime) -> None:
        for key in self._keys:
            value = data.get(key)
            if value is None or (self._zero_is_missing and not value):
                self._seen.pop(key, None)
                continue
            last = self._seen.get(key)
            if last is None or last[0] != value:
                self._seen[key] = (value, now)

    @property
    def reported(self) -> frozenset[str]:
        """Return the keys this model reports a value for."""
        return frozenset(self._seen)

    def still(self, now: datetime, after: timedelta = FROZEN_AFTER) -> frozenset[str]:
        """Return the reported keys that have not changed for *after*."""
        return frozenset(
            key for key, (_, since) in self._seen.items() if now - since >= after
        )

    def dump(self) -> dict[str, list[Any]]:
        return {
            key: [value, since.isoformat()]
            for key, (value, since) in self._seen.items()
        }

    def load(self, stored: Mapping[str, Any]) -> None:
        """Restore when each reading last changed.

        A reading that still has the stored value on the first poll keeps its
        time, so a restart does not hide how long it has stood still.
        """
        self._seen = {
            key: (value, datetime.fromisoformat(since))
            for key, (value, since) in stored.items()
            if key in self._keys
        }

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
    # Readings it judges without keeping them polled, kept in its snapshots.
    shows: ClassVar[frozenset[str]] = frozenset()
    # The repairs issue that tells the user what to do, by its translation key,
    # once a failure has lasted issue_after. None where there is nothing to do.
    remedy: ClassVar[str | None] = None
    issue_after: ClassVar[timedelta | None] = None
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

    def dump(self) -> dict[str, Any]:
        """Return what this check learned, to survive a restart, keyed uniquely."""
        return {}

    def load(self, stored: Mapping[str, Any]) -> None:
        """Restore what dump() returned before a restart."""


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

    def dump(self) -> dict[str, Any]:
        return {"grid_seen": self._grid_seen}

    def load(self, stored: Mapping[str, Any]) -> None:
        self._grid_seen = bool(stored.get("grid_seen"))


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

    def dump(self) -> dict[str, Any]:
        return {"bms_seen": self._bms_seen}

    def load(self, stored: Mapping[str, Any]) -> None:
        self._bms_seen = bool(stored.get("bms_seen"))


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
        self._movement = Movement(_JITTER_KEYS, zero_is_missing=True)
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

    def dump(self) -> dict[str, Any]:
        return {"frozen_moved": self._movement.dump()}

    def load(self, stored: Mapping[str, Any]) -> None:
        self._movement.load(stored.get("frozen_moved") or {})


class CounterStalled(Check):
    """Every counter that supplies the house stands still.

    A house always draws HOUSE_FLOOR_W or more, from the sun, the grid or the
    battery, so one of their counters keeps rising, day and night. When none has
    moved for SUPPLY_STILL_AFTER, they stopped counting. Judged on the counters
    alone, since in a stall the power readings are as frozen as they are.
    Evidence for TelemetryStalled only: a model that updates its counters late
    should not step control aside on its own.
    """

    name = "counter_stalled"
    status = ControlStatus.INCONSISTENT_READINGS
    steps_aside = False
    _SUPPLY: ClassVar[tuple[str, ...]] = (
        "solar_total",
        "grid_import_total",
        "bat_discharged_total",
    )
    keys = frozenset(_SUPPLY)

    def __init__(self) -> None:
        self._movement = Movement(self._SUPPLY, zero_is_missing=True)

    @property
    def reported(self) -> bool:
        """Return whether this model reports any counter that supplies the house."""
        return bool(self._movement.reported)

    def check(self, data: Mapping[str, Any], now: datetime, /) -> bool:
        # The device's own count, not the total held back from it.
        counts = {
            key: raw if (raw := data.get(f"{key}_raw")) is not None else data.get(key)
            for key in self._SUPPLY
        }
        self._movement.observe(counts, now)
        reported = self._movement.reported
        still = self._movement.still(now, SUPPLY_STILL_AFTER)
        return bool(reported) and still == reported

    def onset(self, now: datetime, /) -> datetime:
        return self._movement.still_since(self._movement.reported) or now

    def dump(self) -> dict[str, Any]:
        return {"supply_moved": self._movement.dump()}

    def load(self, stored: Mapping[str, Any]) -> None:
        self._movement.load(stored.get("supply_moved") or {})


class TelemetryStalled(Check):
    """The inverter answers, but its readings stopped updating (#227).

    Frozen voltages alone may be a coarse model or a short glitch, and still
    counters can be a model that counts late. Frozen voltages with the counters
    that supply the house still is the inverter serving readings it no longer
    updates. Only a model that reports none of those counters is judged by most
    readings standing still for STALLED_WITHOUT_COUNTERS instead: on a healthy
    system at night many stand still, such as the PV strings, so that alone is
    not enough where the counters can say more. Every measured reading that has not
    moved is then distrusted, while those that still move, such as the grid power
    from the meter, stay shown.
    """

    name = "telemetry_stalled"
    status = ControlStatus.INCONSISTENT_READINGS
    needs = (FrozenReadings, CounterStalled)
    # Judges whichever of _STALL_KEYS an enabled entity has polled, and keeps none
    # of them polled itself: the checks it weighs keep their own evidence polled,
    # and a reading nobody shows needs no verdict.
    keys = frozenset()
    shows = frozenset(_STALL_KEYS)
    remedy = "telemetry_stalled"
    issue_after = timedelta(minutes=30)

    def __init__(self, frozen: Check, counters: Check) -> None:
        self._evidence = (frozen, counters)
        self._movement = Movement(_STALL_KEYS, zero_is_missing=False)
        self._stalled: frozenset[str] = frozenset()

    def check(self, data: Mapping[str, Any], now: datetime, /) -> bool:
        self._movement.observe(data, now)
        frozen, counters = self._evidence
        if isinstance(counters, CounterStalled) and counters.reported:
            evidence = counters.failed
        else:
            quiet = self._movement.still(now, STALLED_WITHOUT_COUNTERS)
            evidence = 2 * len(quiet) > len(self._movement.reported)
        if not frozen.failed or not evidence:
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

    def dump(self) -> dict[str, Any]:
        return {"stall_moved": self._movement.dump()}

    def load(self, stored: Mapping[str, Any]) -> None:
        self._movement.load(stored.get("stall_moved") or {})


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

# The checks reported on their own, rather than as evidence for another.
REPORTED: Final = frozenset(check.name for check in CHECKS if check.steps_aside)

# Kept polled whatever entities are enabled, as the control inputs are.
KEYS: Final = frozenset[str]().union(*(check.keys for check in CHECKS))
# The readings each check judges, kept in the snapshots of its failures.
KEYS_BY_CHECK: Final = {check.name: check.keys | check.shows for check in CHECKS}


@dataclass(frozen=True, slots=True)
class Finding:
    """A check that has failed for long enough to act on, and since when.

    It carries what the consumers need to react to it, so none of them has to
    know one check from another: the readings it distrusts, and the repairs issue
    that tells the user what to do. A finding is the same finding while its
    check, state and start are, whatever its readings do.
    """

    status: ControlStatus
    check: str
    since: datetime
    keys: frozenset[str] = field(default=frozenset(), compare=False)
    remedy: str | None = field(default=None, compare=False)
    issue_after: timedelta | None = field(default=None, compare=False)


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
        # Every failure that holds, in the order of CHECKS. Evidence for another
        # check is not one: it is never reported on its own.
        self.findings: tuple[Finding, ...] = ()
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
        self.findings = tuple(
            Finding(
                check.status,
                check.name,
                self._since[check.name],
                keys=with_derived(check.distrusted()),
                remedy=check.remedy,
                issue_after=check.issue_after,
            )
            for check in failed
            if check.steps_aside
            and self._polls[check.name] >= CONTROL_STATUS_DAMPING_POLLS
        )
        held = self.findings[0] if self.findings else None
        if held is None:
            self.active = None
        elif self.active is None or self.active.status is not held.status:
            self.active = held
        else:
            # Another cause for the same state, or the same one with other
            # readings: it has lasted since the first.
            self.active = replace(held, since=self.active.since)

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

    def dump_state(self) -> dict[str, Any]:
        """Return what the checks learned, to survive a restart."""
        state: dict[str, Any] = {}
        for check in self._checks:
            state.update(check.dump())
        return state

    def load_state(self, stored: Mapping[str, Any]) -> None:
        """Restore what the checks learned before a restart.

        A check whose stored state cannot be read starts afresh, as on first use.
        """
        for check in self._checks:
            try:
                check.load(stored)
            except (TypeError, ValueError, KeyError):
                _LOGGER.debug("Could not restore the %s check", check.name)
