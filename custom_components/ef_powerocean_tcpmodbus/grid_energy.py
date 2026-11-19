"""Grid energy integrated from Grid Power, for inverters whose counters read 0."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .const import (
    GRID_ENERGY_ISSUE_AFTER_S,
    GRID_ENERGY_ISSUE_MIN_KWH,
    GRID_ENERGY_MAX_GAP_S,
    GRID_ENERGY_REGISTER_KEYS,
)
from .util import parse_datetime


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def split_energy(
    power_before_w: float, power_after_w: float, hours: float
) -> tuple[float, float]:
    """Return the import and export in kWh between two Grid Power readings.

    Grid Power is positive while importing. The power is taken to change linearly
    between the readings, so an interval in which it changes sign is split where
    it crosses 0.
    """
    if power_before_w * power_after_w >= 0:
        energy = (power_before_w + power_after_w) / 2 * hours / 1000
        return (energy, 0.0) if energy >= 0 else (0.0, -energy)
    crossing = power_before_w / (power_before_w - power_after_w)
    before = power_before_w / 2 * crossing * hours / 1000
    after = power_after_w / 2 * (1 - crossing) * hours / 1000
    return (before, -after) if before > 0 else (after, -before)


@dataclass(slots=True)
class GridEnergyIntegrator:
    """Integrates Grid Power into lifetime grid import and export totals.

    Integrates every poll, whether the totals are published or not: the energy is
    what tells the grid energy registers reading 0 apart from a house that drew
    nothing from the grid. The totals only count while active, and start from the
    totals Home Assistant last published, so turning it on is no meter reset.
    """

    active: bool = False
    import_kwh: float = 0.0
    export_kwh: float = 0.0
    # Since when every grid energy register has read 0, and the energy that has
    # flowed through the grid since.
    zero_since: datetime | None = None
    kwh_while_zero: float = 0.0
    # The last Grid Power reading. Not saved: the gap after a restart is too long
    # to integrate across anyway.
    _last: tuple[float, datetime] | None = None

    def start(self, published: Mapping[str, Any]) -> None:
        """Start counting from the totals published last, or from 0."""
        self.active = True
        self.import_kwh = _number(published.get("grid_import_total")) or 0.0
        self.export_kwh = _number(published.get("grid_export_total")) or 0.0

    def stop(self) -> None:
        self.active = False

    def add(self, power_w: Any, now: datetime) -> tuple[float, float]:
        """Integrate a Grid Power reading; return the import and export it adds.

        A missing reading, or one after a gap longer than a few polls, adds
        nothing: the energy of an outage is unknown, not made up.
        """
        power = _number(power_w)
        last = self._last
        self._last = None if power is None else (power, now)
        if power is None or last is None:
            return 0.0, 0.0
        seconds = (now - last[1]).total_seconds()
        if not 0 < seconds <= GRID_ENERGY_MAX_GAP_S:
            return 0.0, 0.0
        imported, exported = split_energy(last[0], power, seconds / 3600)
        if self.active:
            self.import_kwh += imported
            self.export_kwh += exported
        return imported, exported

    def totals(self) -> dict[str, float]:
        """Return the lifetime totals, in place of the device's."""
        return {
            "grid_import_total": round(self.import_kwh, 3),
            "grid_export_total": round(self.export_kwh, 3),
        }

    def observe_registers(
        self, raw_data: Mapping[str, Any], kwh: float, now: datetime
    ) -> None:
        """Track how long the grid energy registers have all read 0.

        A poll in which one was not read tells nothing either way.
        """
        values = [_number(raw_data.get(key)) for key in GRID_ENERGY_REGISTER_KEYS]
        if None in values:
            return
        if any(values):
            self.zero_since, self.kwh_while_zero = None, 0.0
            return
        if self.zero_since is None:
            self.zero_since, self.kwh_while_zero = now, 0.0
            return
        self.kwh_while_zero += kwh

    def registers_not_counting(self, now: datetime) -> bool:
        """Whether the grid energy registers stayed at 0 while the grid was used."""
        return (
            self.zero_since is not None
            and (now - self.zero_since).total_seconds() >= GRID_ENERGY_ISSUE_AFTER_S
            and self.kwh_while_zero >= GRID_ENERGY_ISSUE_MIN_KWH
        )

    def dump_state(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "import_kwh": self.import_kwh,
            "export_kwh": self.export_kwh,
            "zero_since": self.zero_since.isoformat() if self.zero_since else None,
            "kwh_while_zero": self.kwh_while_zero,
        }

    def load_state(self, stored: Any) -> None:
        if not isinstance(stored, Mapping):
            return
        self.active = stored.get("active") is True
        self.import_kwh = _number(stored.get("import_kwh")) or 0.0
        self.export_kwh = _number(stored.get("export_kwh")) or 0.0
        self.zero_since = parse_datetime(stored.get("zero_since"))
        self.kwh_while_zero = _number(stored.get("kwh_while_zero")) or 0.0
