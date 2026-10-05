"""The Battery Modes: what each one runs, depending on the solar surplus.

The surplus is solar power minus house load, negative when the house needs more.
A mode can run something different in each of four zones:

    deficit_above_limit   the house needs more than the solar, by more than the limit
    deficit               the house needs more than the solar
    surplus               there is spare solar
    surplus_above_limit   there is more spare solar than the limit

The limit is the mode's power setting. A zone left out runs the same as its
neighbour closer to zero, and ``always`` runs one thing in every zone.

What runs is one of the inverter's commands. It can also say ``never`` charge or
discharge the battery, which then holds whenever the command would move it that
way, just as the guards do.

The per-feature plans are defined in ``MODES`` below.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Final

from .const import GUARD_HANDBACK_MAX_S, GUARD_HANDBACK_S
from .models import ControlFeature, ControlStatus


class Way(Enum):
    """A way the battery can move."""

    CHARGE = auto()
    DISCHARGE = auto()


CHARGE: Final = Way.CHARGE
DISCHARGE: Final = Way.DISCHARGE


@dataclass(frozen=True)
class Step:
    """An inverter command, optionally forbidding the battery one way."""

    command: ControlFeature
    never: Way | None = None
    # Shown while never holds the battery, which the command alone would not explain.
    status: ControlStatus | None = None


def automatic(*, never: Way | None = None, status: ControlStatus | None = None) -> Step:
    """The inverter's own self-consumption."""
    return Step(ControlFeature.AUTOMATIC, never, status)


def hold_battery() -> Step:
    """The battery idles; the grid takes or covers the rest."""
    return Step(ControlFeature.HOLD_BATTERY)


def charge_battery() -> Step:
    """The battery charges at the mode's power."""
    return Step(ControlFeature.CHARGE_BATTERY)


def discharge_battery() -> Step:
    """The battery discharges at the mode's power."""
    return Step(ControlFeature.DISCHARGE_BATTERY)


def export_to_grid() -> Step:
    """The grid export is fixed at the mode's power; the battery takes or covers
    the difference."""
    return Step(ControlFeature.EXPORT_TO_GRID)


def import_from_grid() -> Step:
    """The grid import is fixed at the mode's power; the battery takes or covers
    the difference."""
    return Step(ControlFeature.IMPORT_FROM_GRID)


class Zone(Enum):
    """Where the surplus stands against zero and the mode's limit."""

    DEFICIT_ABOVE_LIMIT = auto()
    DEFICIT = auto()
    SURPLUS = auto()
    SURPLUS_ABOVE_LIMIT = auto()

    @property
    def is_surplus(self) -> bool:
        return self in (Zone.SURPLUS, Zone.SURPLUS_ABOVE_LIMIT)


@dataclass(frozen=True)
class Mode:
    """What a mode runs in each zone; see the module docstring."""

    deficit: Step
    surplus: Step | None = None
    surplus_above_limit: Step | None = None
    deficit_above_limit: Step | None = None

    @property
    def adapts(self) -> bool:
        return (
            self.surplus is not None
            or self.surplus_above_limit is not None
            or self.deficit_above_limit is not None
        )

    @property
    def uses_limit(self) -> bool:
        return (
            self.surplus_above_limit is not None or self.deficit_above_limit is not None
        )

    def step_for(self, zone: Zone) -> Step:
        surplus = self.surplus or self.deficit
        match zone:
            case Zone.DEFICIT_ABOVE_LIMIT:
                return self.deficit_above_limit or self.deficit
            case Zone.SURPLUS:
                return surplus
            case Zone.SURPLUS_ABOVE_LIMIT:
                return self.surplus_above_limit or surplus
        return self.deficit


def always(step: Step) -> Mode:
    return Mode(deficit=step)


# Mode definitions
MODES: Final[dict[ControlFeature, Mode]] = {
    ControlFeature.AUTOMATIC: always(automatic()),
    ControlFeature.HOLD_BATTERY: always(hold_battery()),
    ControlFeature.CHARGE_BATTERY: always(charge_battery()),
    ControlFeature.DISCHARGE_BATTERY: always(discharge_battery()),
    ControlFeature.EXPORT_TO_GRID: always(export_to_grid()),
    ControlFeature.IMPORT_FROM_GRID: always(import_from_grid()),
    ControlFeature.EXPORT_SOLAR_FIRST: Mode(
        deficit=automatic(),
        # Under the limit the battery may not take the surplus, so all of it is
        # exported.
        surplus=automatic(never=CHARGE, status=ControlStatus.BELOW_SOLAR_EXPORT_LIMIT),
        # Above it the export is fixed at the limit, and the battery stores the rest.
        surplus_above_limit=export_to_grid(),
    ),
}


def zone_for(surplus: float, limit: float, current: Zone, deadband: float) -> Zone:
    """Return the zone *surplus* is in, leaving *current* only once clearly outside.

    A limit is reached at the limit itself, because at the device's export cap the
    solar is curtailed and the surplus reads no higher than the cap.
    """
    if surplus >= limit or (
        current is Zone.SURPLUS_ABOVE_LIMIT and surplus >= limit - deadband
    ):
        return Zone.SURPLUS_ABOVE_LIMIT
    if surplus <= -limit or (
        current is Zone.DEFICIT_ABOVE_LIMIT and surplus <= -limit + deadband
    ):
        return Zone.DEFICIT_ABOVE_LIMIT
    if surplus < -deadband or (not current.is_surplus and surplus <= deadband):
        return Zone.DEFICIT
    return Zone.SURPLUS


def battery_power(
    command: ControlFeature, surplus: float | None, power: float
) -> float | None:
    """Return the battery power *command* at *power* leads to, positive charging.

    None when that depends on a surplus that is not known.
    """
    match command:
        case ControlFeature.HOLD_BATTERY:
            return 0.0
        case ControlFeature.CHARGE_BATTERY:
            return power
        case ControlFeature.DISCHARGE_BATTERY:
            return -power
    if surplus is None:
        return None
    match command:
        case ControlFeature.EXPORT_TO_GRID:
            return surplus - power
        case ControlFeature.IMPORT_FROM_GRID:
            return surplus + power
    return surplus


@dataclass
class ModeState:
    """The zone and step a mode is in, and how long each step must wait."""

    feature: ControlFeature
    zone: Zone = Zone.DEFICIT
    step: Step = field(init=False)
    since: datetime | None = None
    _cooldown_s: dict[Step, float] = field(default_factory=dict)
    _left_at: dict[Step, datetime] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.step = self.mode.deficit

    @property
    def mode(self) -> Mode:
        return MODES[self.feature]

    def choose(
        self,
        surplus: float | None,
        limit: float | None,
        now: datetime,
        deadband: float,
        *,
        battery_full: bool = False,
    ) -> Step:
        """Return and remember the step to run for *surplus*."""
        mode = self.mode
        if surplus is None or (mode.uses_limit and limit is None):
            zone = Zone.DEFICIT
        else:
            zone = zone_for(
                surplus, math.inf if limit is None else limit, self.zone, deadband
            )
            if battery_full and zone.is_surplus:
                # A full battery takes none of a surplus, so there is nothing to
                # decide about it.
                zone = Zone.DEFICIT
        step = mode.step_for(zone)
        if step != self.step and not self._may_switch_to(step, now):
            # Waiting out a cooldown counts as a deficit, so the mode switches again
            # only once the surplus is clearly above zero.
            zone, step = Zone.DEFICIT, mode.deficit
        self.zone = zone
        self._switch_to(step, now)
        return step

    def _may_switch_to(self, step: Step, now: datetime) -> bool:
        left_at = self._left_at.get(step)
        if left_at is None:
            return True
        return (now - left_at).total_seconds() >= self._cooldown_s.get(step, 0.0)

    def _switch_to(self, step: Step, now: datetime) -> None:
        if step == self.step:
            return
        leaving = self.step
        if leaving != self.mode.deficit and self.since is not None:
            # Each switch rewrites the inverter's method, so a step that lasted
            # under a minute waits twice as long as last time before it is used again.
            lasted = (now - self.since).total_seconds()
            previous = self._cooldown_s.get(leaving, 0.0)
            self._cooldown_s[leaving] = (
                min(max(2 * previous, GUARD_HANDBACK_S), GUARD_HANDBACK_MAX_S)
                if lasted < GUARD_HANDBACK_S
                else 0.0
            )
            self._left_at[leaving] = now
        self.step = step
        self.since = now
