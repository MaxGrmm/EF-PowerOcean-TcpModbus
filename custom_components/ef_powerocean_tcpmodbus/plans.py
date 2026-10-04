"""What each Battery Mode runs, written as a plan the control manager carries out.

A plan says which of the inverter's own commands runs in each situation the balance
can be in, solar less the house: a deficit or a surplus, either one past the mode's
limit. A situation a plan leaves out runs its ``otherwise``. Every mode is carried
out by the same rules, so modes differ only in their plans:

- The balance is read the same whichever command runs, and a situation is left only
  once the balance is past its edge by the deadband. The limit is the exception on
  the way up: with the export held at a cap, the balance cannot read past it.
- Running something other than ``otherwise`` is a departure, and every switch is a
  method change, so a departure left within a minute is not taken again for a
  minute, doubling each time. ``otherwise`` runs meanwhile and is never held back.
- A full battery takes nothing, so a plan that departs runs ``otherwise`` then.
- A guard holds the battery wherever the command would move it the way the guard
  forbids, judged by what the command would do with the balance as it is. A plan
  whose departures only steer the very direction a guard forbids has nothing left
  to decide while that guard is on, and runs ``otherwise`` under it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final

from .const import GUARD_HANDBACK_MAX_S, GUARD_HANDBACK_S
from .models import ControlFeature, ControlStatus

AUTOMATIC = ControlFeature.AUTOMATIC
HOLD_BATTERY = ControlFeature.HOLD_BATTERY
CHARGE_BATTERY = ControlFeature.CHARGE_BATTERY
DISCHARGE_BATTERY = ControlFeature.DISCHARGE_BATTERY
EXPORT_TO_GRID = ControlFeature.EXPORT_TO_GRID
IMPORT_FROM_GRID = ControlFeature.IMPORT_FROM_GRID


class Situation(StrEnum):
    """Where the balance, solar less the house, stands against zero and the limit."""

    DEFICIT_OVER_LIMIT = "deficit_over_limit"
    DEFICIT = "deficit"
    SURPLUS = "surplus"
    SURPLUS_OVER_LIMIT = "surplus_over_limit"


SURPLUSES: Final = frozenset({Situation.SURPLUS, Situation.SURPLUS_OVER_LIMIT})


@dataclass(frozen=True)
class Plan:
    """What a mode runs in each situation, and ``otherwise`` in the rest."""

    otherwise: ControlFeature
    deficit_over_limit: ControlFeature | None = None
    deficit: ControlFeature | None = None
    surplus: ControlFeature | None = None
    surplus_over_limit: ControlFeature | None = None
    # What Control Status shows while the plan holds the battery, if it departs.
    held_status: ControlStatus | None = None

    def run(self, situation: Situation) -> ControlFeature:
        """Return the command for *situation*."""
        return getattr(self, situation.value) or self.otherwise

    @property
    def departs(self) -> bool:
        """Return whether any situation runs something other than ``otherwise``."""
        return any(self.run(situation) is not self.otherwise for situation in Situation)

    @property
    def steers(self) -> int:
        """Return +1 if the plan only departs with a surplus, -1 if only with a
        deficit, else 0: which way of the battery its departures decide on."""
        departing = {s for s in Situation if self.run(s) is not self.otherwise}
        if not departing:
            return 0
        if departing <= SURPLUSES:
            return 1
        if not departing & SURPLUSES:
            return -1
        return 0

    @property
    def start(self) -> Situation:
        """Return a situation that runs ``otherwise``, to begin as if it had been."""
        return next(s for s in Situation if self.run(s) is self.otherwise)


def always(feature: ControlFeature) -> Plan:
    """Return the plan of a mode that runs one command whatever the balance."""
    return Plan(otherwise=feature)


PLANS: Final[dict[ControlFeature, Plan]] = {
    AUTOMATIC: always(AUTOMATIC),
    HOLD_BATTERY: always(HOLD_BATTERY),
    CHARGE_BATTERY: always(CHARGE_BATTERY),
    DISCHARGE_BATTERY: always(DISCHARGE_BATTERY),
    EXPORT_TO_GRID: always(EXPORT_TO_GRID),
    IMPORT_FROM_GRID: always(IMPORT_FROM_GRID),
    ControlFeature.EXPORT_SOLAR_FIRST: Plan(
        otherwise=AUTOMATIC,
        surplus=HOLD_BATTERY,
        surplus_over_limit=EXPORT_TO_GRID,
        held_status=ControlStatus.BELOW_SOLAR_EXPORT_LIMIT,
    ),
}


def situation_for(
    surplus: float, limit: float, current: Situation | None, deadband: float
) -> Situation:
    """Return the situation *surplus* is in, leaving *current* only once past its edge
    by *deadband*. The limit is entered at the limit itself on the way up."""
    was_surplus = current in SURPLUSES if current is not None else surplus >= 0
    if surplus >= -deadband if was_surplus else surplus > deadband:
        stays_over = current is Situation.SURPLUS_OVER_LIMIT
        if surplus >= (limit - deadband if stays_over else limit):
            return Situation.SURPLUS_OVER_LIMIT
        return Situation.SURPLUS
    stays_over = current is Situation.DEFICIT_OVER_LIMIT
    if surplus <= (-limit + deadband if stays_over else -limit):
        return Situation.DEFICIT_OVER_LIMIT
    return Situation.DEFICIT


def battery_power(
    command: ControlFeature, surplus: float | None, power: float
) -> float | None:
    """Return what *command* at *power* does to the battery, positive charging.

    None when it depends on a balance that is not known.
    """
    if command is HOLD_BATTERY:
        return 0.0
    if command is CHARGE_BATTERY:
        return power
    if command is DISCHARGE_BATTERY:
        return -power
    if surplus is None:
        return None
    if command is EXPORT_TO_GRID:
        return surplus - power
    if command is IMPORT_FROM_GRID:
        return surplus + power
    return surplus


# Commands whose way of the battery follows the balance, rather than being chosen.
FOLLOWS_THE_BALANCE: Final = frozenset({AUTOMATIC, EXPORT_TO_GRID, IMPORT_FROM_GRID})


@dataclass
class PlanState:
    """Where a mode's plan stands: its situation, what runs, and the cooldowns."""

    mode: ControlFeature
    situation: Situation | None = None
    command: ControlFeature | None = None
    since: datetime | None = None
    # The guard holding the battery against the command, until it is clearly clear.
    held_by: ControlStatus | None = None
    cooldown_s: dict[ControlFeature, float] = field(default_factory=dict)
    left_at: dict[ControlFeature, datetime] = field(default_factory=dict)

    @classmethod
    def begin(cls, mode: ControlFeature) -> PlanState:
        """Start *mode*'s plan as if it had been running ``otherwise``."""
        plan = PLANS[mode]
        return cls(mode=mode, situation=plan.start, command=plan.otherwise)

    def may_depart(self, command: ControlFeature, now: datetime) -> bool:
        """Return whether *command* has waited out its cooldown."""
        left_at = self.left_at.get(command)
        if left_at is None:
            return True
        return (now - left_at).total_seconds() >= self.cooldown_s.get(command, 0.0)

    def switch(self, command: ControlFeature, now: datetime) -> None:
        """Run *command* from now on, noting how long a departure left lasted."""
        if command is self.command:
            return
        otherwise = PLANS[self.mode].otherwise
        if self.command not in (None, otherwise) and self.since is not None:
            lasted = (now - self.since).total_seconds()
            previous = self.cooldown_s.get(self.command, 0.0)
            self.cooldown_s[self.command] = (
                min(max(2 * previous, GUARD_HANDBACK_S), GUARD_HANDBACK_MAX_S)
                if lasted < GUARD_HANDBACK_S
                else 0.0
            )
            self.left_at[self.command] = now
        self.command = command
        self.since = now
