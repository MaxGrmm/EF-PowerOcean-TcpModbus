"""How each Battery Mode picks the inverter command to run.

Most modes run one command all the time. Export Solar First adapts to the solar
surplus, which is solar power minus house load and negative when the house uses
more:

- below zero it runs Automatic, so the battery covers the house;
- under the Solar Export Limit it holds the battery, so all of the surplus is
  exported;
- at the limit it runs Export to Grid, so the battery only takes what is above it.

Two things keep it from switching back and forth: a zone is only left once the
surplus is clearly outside it, and a command left within a minute of switching to
it is not used again for a minute, then two, and so on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto

from .const import GUARD_HANDBACK_MAX_S, GUARD_HANDBACK_S
from .models import ControlFeature, ControlStatus


class Zone(Enum):
    """Where the surplus stands against zero and the mode's limit."""

    DEFICIT = auto()
    SURPLUS = auto()
    AT_LIMIT = auto()


@dataclass(frozen=True)
class Plan:
    """The command a mode runs in each zone. A deficit always runs the default."""

    default: ControlFeature
    surplus: ControlFeature | None = None
    at_limit: ControlFeature | None = None
    # Holding is how Export Solar First exports, so it gets its own status rather
    # than looking like a guard.
    hold_status: ControlStatus | None = None

    @property
    def adapts(self) -> bool:
        return self.surplus is not None or self.at_limit is not None

    def command_for(self, zone: Zone) -> ControlFeature:
        if zone is Zone.SURPLUS and self.surplus is not None:
            return self.surplus
        if zone is Zone.AT_LIMIT and self.at_limit is not None:
            return self.at_limit
        return self.default


EXPORT_SOLAR_FIRST_PLAN = Plan(
    default=ControlFeature.AUTOMATIC,
    surplus=ControlFeature.HOLD_BATTERY,
    at_limit=ControlFeature.EXPORT_TO_GRID,
    hold_status=ControlStatus.BELOW_SOLAR_EXPORT_LIMIT,
)


def plan_for(mode: ControlFeature) -> Plan:
    if mode is ControlFeature.EXPORT_SOLAR_FIRST:
        return EXPORT_SOLAR_FIRST_PLAN
    return Plan(default=mode)


def zone_for(surplus: float, limit: float, current: Zone, deadband: float) -> Zone:
    """Return the zone *surplus* is in, leaving *current* only once clearly outside.

    The limit is reached at the limit itself, because at the device's export cap
    the solar is curtailed and the surplus reads no higher than the cap.
    """
    if surplus >= limit or (current is Zone.AT_LIMIT and surplus >= limit - deadband):
        return Zone.AT_LIMIT
    if surplus < -deadband or (current is Zone.DEFICIT and surplus <= deadband):
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
class PlanState:
    """The command a mode is running, and how long each other command must wait."""

    mode: ControlFeature
    zone: Zone = Zone.DEFICIT
    command: ControlFeature = field(init=False)
    since: datetime | None = None
    _cooldown_s: dict[ControlFeature, float] = field(default_factory=dict)
    _left_at: dict[ControlFeature, datetime] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.command = self.plan.default

    @property
    def plan(self) -> Plan:
        return plan_for(self.mode)

    def choose(
        self, surplus: float | None, limit: float, now: datetime, deadband: float
    ) -> ControlFeature:
        """Return and remember the command for *surplus*; None runs the default."""
        zone = (
            Zone.DEFICIT
            if surplus is None
            else zone_for(surplus, limit, self.zone, deadband)
        )
        command = self.plan.command_for(zone)
        if command is not self.command and not self._may_switch_to(command, now):
            # Waiting out a cooldown counts as a deficit, so the mode switches again
            # only once the surplus is clearly above zero.
            zone, command = Zone.DEFICIT, self.plan.default
        self.zone = zone
        self._switch_to(command, now)
        return command

    def _may_switch_to(self, command: ControlFeature, now: datetime) -> bool:
        left_at = self._left_at.get(command)
        if left_at is None:
            return True
        return (now - left_at).total_seconds() >= self._cooldown_s.get(command, 0.0)

    def _switch_to(self, command: ControlFeature, now: datetime) -> None:
        if command is self.command:
            return
        leaving = self.command
        if leaving is not self.plan.default and self.since is not None:
            # Each switch rewrites the inverter's method, so a command that lasted
            # under a minute waits twice as long as last time before it is used again.
            lasted = (now - self.since).total_seconds()
            previous = self._cooldown_s.get(leaving, 0.0)
            self._cooldown_s[leaving] = (
                min(max(2 * previous, GUARD_HANDBACK_S), GUARD_HANDBACK_MAX_S)
                if lasted < GUARD_HANDBACK_S
                else 0.0
            )
            self._left_at[leaving] = now
        self.command = command
        self.since = now
