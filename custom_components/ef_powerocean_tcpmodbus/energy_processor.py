"""Energy counter validation and daily derivation for PowerOcean telemetry."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from homeassistant.util import dt

from .const import (
    DEFAULT_MAX_POWER,
    ENERGY_RESOLUTION_KWH,
    ENERGY_SENSOR_MAP,
)
from .models import EnergySensorDef
from .util import parse_datetime

_LOGGER = logging.getLogger(__name__)

# A restored counter held since it was restored takes the device's counter over once
# the device's lifetime counter and its daily counter, a separate register, have
# risen together, plausibly, by at least this much over at least this many polls.
RESYNC_MIN_RISE_KWH = 1.0
RESYNC_MIN_POLLS = 3
# How far the two rises may differ: register resolution plus a small share, which
# no misscaled counter fits.
RESYNC_TOLERANCE_KWH = 0.02
RESYNC_TOLERANCE_SHARE = 0.02
DAILY_OF_TOTAL = {
    energy_sensor.total_source: energy_sensor.key
    for energy_sensor in ENERGY_SENSOR_MAP
    if energy_sensor.total_source is not None
}


@dataclass(slots=True)
class ResyncRun:
    """An unbroken run of plausible device reads of a held, restored counter."""

    first_value: float
    first_daily: float
    last_value: float
    last_daily: float
    last_seen: datetime
    polls: int = 1


# The counters the processor validates and derives the daily values from.
ENERGY_KEYS: Final = frozenset(
    key
    for energy_sensor in ENERGY_SENSOR_MAP
    for key in (energy_sensor.key, energy_sensor.total_source)
    if key is not None
)


def _rises_agree(rise: float, daily_rise: float) -> bool:
    """Whether a lifetime counter rose as much as its daily counter, never lower."""
    return daily_rise >= 0 and abs(rise - daily_rise) <= (
        RESYNC_TOLERANCE_KWH + RESYNC_TOLERANCE_SHARE * daily_rise
    )


class EnergyProcessor:
    """Processor to validate and derive energy sensors.

    - Lifetime _total counters are the source of truth. They are validated
      against a max believable power since the last accepted read.
    - Daily _today values are derived as total - snapshot, where the snapshot is
      taken at local midnight. The raw daily registers of the device is only used
      to reconstruct a missing snapshot or keep energy from before the first poll
      after midnight.
    - The daily reset is triggered when the local calendar date advances.
    """

    def __init__(self, limits: dict[str, Any]) -> None:
        self.limits = limits
        self.accepted_at: dict[str, datetime] = {}
        self.daily_snapshots: dict[str, float] = {}
        self.last_rollover: datetime | None = None
        # Counters restored without an acceptance time and held since (#169).
        # Saved, since the acceptance time recorded for them would otherwise hide
        # them after a restart.
        self.resync_pending: set[str] = set()
        self.resync_runs: dict[str, ResyncRun] = {}

    def dump_state(self) -> dict[str, Any]:
        """Return the processor state in a JSON-serializable form."""
        return {
            "energy_accepted_at": {
                key: value.isoformat() for key, value in self.accepted_at.items()
            },
            "daily_energy_snapshots": self.daily_snapshots,
            "last_daily_rollover": self.last_rollover.isoformat()
            if self.last_rollover is not None
            else None,
            "energy_resync_pending": sorted(self.resync_pending),
        }

    def load_state(self, stored: dict[str, Any]) -> None:
        """Restore the processor state from persisted storage."""
        self.accepted_at = {
            key: parsed
            for key, raw in (stored.get("energy_accepted_at") or {}).items()
            if (parsed := parse_datetime(raw)) is not None
        }
        self.daily_snapshots = stored.get("daily_energy_snapshots") or {}
        self.last_rollover = parse_datetime(stored.get("last_daily_rollover"))
        self.resync_pending = set(stored.get("energy_resync_pending") or ())
        self.resync_runs = {}

    def validate_totals(
        self,
        raw_data: Mapping[str, Any],
        previous_data: Mapping[str, Any],
        previous_time: datetime | None,
    ) -> dict[str, Any]:
        """Validate lifetime energy counters against physical plausibility.

        A lifetime counter never decreases, and it can rise at most as fast as
        the configured maximum power allows. Implausible or missing readings
        are replaced with the last validated value.
        """
        result: dict[str, Any] = dict(raw_data)

        now = dt.now()
        if previous_time is not None:
            elapsed_seconds = (now - previous_time).total_seconds()
            if elapsed_seconds < 1:
                _LOGGER.debug(
                    "dt is less than one second. Return last data. Delta-t: %s",
                    elapsed_seconds,
                )
                return dict(previous_data)

        for energy_sensor in ENERGY_SENSOR_MAP:
            if energy_sensor.is_calculated or energy_sensor.resets_daily:
                continue

            key = energy_sensor.key
            current_energy = result.get(key)
            baseline = previous_data.get(key)
            if (
                baseline is not None
                and previous_time is not None
                and key not in self.accepted_at
            ):
                # A counter restored from a state file written before 2.4.3 has no
                # acceptance time. Its baseline was accepted when it was saved, so
                # record that, as for any other counter. Falling back to the
                # previous poll instead holds it for good once it is held (#169).
                self.accepted_at[key] = previous_time
                self.resync_pending.add(key)
            accepted_at = self.accepted_at.get(key) or previous_time

            if current_energy is None:
                if baseline is not None:
                    result[key] = baseline
                    _LOGGER.debug(
                        "Fill missing read of %s with last validated value %s",
                        key,
                        baseline,
                    )
                continue

            if baseline is None or accepted_at is None:
                self.accepted_at[key] = now
                continue

            energy_delta = current_energy - baseline
            max_believable_delta = self._max_believable_energy_delta(
                energy_sensor, accepted_at, now
            )

            if energy_delta < 0 or energy_delta > max_believable_delta:
                if key in self.resync_pending and self._resync_confirmed(
                    energy_sensor,
                    current_energy,
                    raw_data.get(DAILY_OF_TOTAL.get(key, "")),
                    baseline,
                    now,
                ):
                    _LOGGER.info(
                        "%s was held at %s since it was restored; taking the "
                        "device's %s over",
                        key,
                        baseline,
                        current_energy,
                    )
                    self._accept(key, now)
                    # Rebuild today's counters from the device's daily counters
                    # rather than count the whole gap as today's.
                    for daily in ENERGY_SENSOR_MAP:
                        if daily.total_source == key:
                            self.daily_snapshots.pop(daily.key, None)
                    continue
                result[key] = baseline
                _LOGGER.debug(
                    "Hold implausible %s! (raw energy: %s baseline: %s "
                    "delta energy: %s "
                    "max delta: %s accepted at: %s)",
                    key,
                    current_energy,
                    baseline,
                    round(energy_delta, 2),
                    round(max_believable_delta, 4),
                    accepted_at.time(),
                )
            else:
                self._accept(key, now)

        return result

    def _accept(self, key: str, now: datetime) -> None:
        self.accepted_at[key] = now
        self.resync_pending.discard(key)
        self.resync_runs.pop(key, None)

    def _resync_confirmed(
        self,
        energy_sensor: EnergySensorDef,
        value: float,
        daily: Any,
        baseline: float,
        now: datetime,
    ) -> bool:
        """Track a held, restored counter's reads; return whether they confirm it.

        Each read must follow the one before as a counter would, within the rise
        budget and rising as much as the device's daily counter for the same
        energy, a separate register, rose. Once that has risen by at least a kWh,
        the whole run must agree too, so a spike, a repeating garbage value, a
        misscaled counter or a corrupted daily read is never taken for the counter.
        """
        key = energy_sensor.key
        if not isinstance(daily, (int, float)) or not math.isfinite(daily):
            self.resync_runs.pop(key, None)
            return False
        if value < baseline:
            self.resync_runs.pop(key, None)
            return False
        run = self.resync_runs.get(key)
        if (
            run is not None
            and 0
            <= value - run.last_value
            <= self._max_believable_energy_delta(energy_sensor, run.last_seen, now)
            and _rises_agree(value - run.last_value, daily - run.last_daily)
        ):
            run.last_value, run.last_daily = value, daily
            run.last_seen, run.polls = now, run.polls + 1
        else:
            run = self.resync_runs[key] = ResyncRun(value, daily, value, daily, now)
        daily_rise = run.last_daily - run.first_daily
        return (
            run.polls >= RESYNC_MIN_POLLS
            and daily_rise >= RESYNC_MIN_RISE_KWH
            and _rises_agree(run.last_value - run.first_value, daily_rise)
        )

    def _max_believable_energy_delta(
        self, energy_sensor: EnergySensorDef, accepted_at: datetime, now: datetime
    ) -> float:
        """Largest believable rise in kWh since the last accepted value."""
        limit = (
            self.limits.get(energy_sensor.max_power, DEFAULT_MAX_POWER)
            if energy_sensor.max_power
            else DEFAULT_MAX_POWER
        )
        elapsed_hours = max((now - accepted_at).total_seconds(), 0) / 3600
        return limit * elapsed_hours / 1000 + ENERGY_RESOLUTION_KWH

    def derive_daily(self, data: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        """Set each daily counter to its lifetime counter's growth since the
        last reset snapshot; also return whether a reset rolled this poll."""
        result = dict(data)
        is_daily_reset = self._should_roll_daily_snapshots()

        for energy_sensor in ENERGY_SENSOR_MAP:
            if energy_sensor.total_source is None:
                continue

            total_energy = result.get(energy_sensor.total_source)
            if total_energy is None:
                # Avoid publishing raw daily data or yesterday's snapshot
                result.pop(energy_sensor.key, None)
                if is_daily_reset:
                    self.daily_snapshots.pop(energy_sensor.key, None)
                continue

            snapshot = self.daily_snapshots.get(energy_sensor.key)
            if is_daily_reset or snapshot is None:
                raw_sensor_key = f"{energy_sensor.key}_raw"
                raw_daily = result.get(raw_sensor_key)
                if self._is_valid_raw_daily(energy_sensor, raw_daily, total_energy):
                    snapshot = total_energy - raw_daily
                    _LOGGER.debug(
                        "Snapshot %s set to %s using %s (total: %s reset: %s)",
                        energy_sensor.key,
                        snapshot,
                        raw_sensor_key,
                        total_energy,
                        is_daily_reset,
                    )
                else:
                    snapshot = total_energy
                    _LOGGER.debug(
                        "Snapshot %s set to %s (total: %s reset: %s)",
                        energy_sensor.key,
                        snapshot,
                        total_energy,
                        is_daily_reset,
                    )

            snapshot = min(snapshot, total_energy)
            self.daily_snapshots[energy_sensor.key] = snapshot
            result[energy_sensor.key] = round(total_energy - snapshot, 2)

        return result, is_daily_reset

    def _is_valid_raw_daily(
        self, energy_sensor: Any, raw_daily: Any, total_energy: float
    ) -> bool:
        """Return whether a device daily value can safely reconstruct a snapshot."""
        if not isinstance(raw_daily, (int, float)) or not math.isfinite(raw_daily):
            return False
        if raw_daily < 0 or raw_daily > total_energy:
            return False

        now = dt.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        max_believable_daily = self._max_believable_energy_delta(
            energy_sensor, midnight, now
        )
        if raw_daily > max_believable_daily:
            _LOGGER.debug(
                "Ignore implausible raw daily %s %s (max: %s)",
                energy_sensor.key,
                raw_daily,
                round(max_believable_daily, 2),
            )
            return False

        return True

    @staticmethod
    def raw_daily_values(raw_data: dict[str, Any]) -> dict[str, Any]:
        """Echo each device daily register under a *_raw diagnostic key.

        Independent of the derivation: it just exposes the device's own
        reading so it can be compared against the derived value in the UI.
        """
        return {
            f"{energy_sensor.key}_raw": raw_data.get(energy_sensor.key)
            for energy_sensor in ENERGY_SENSOR_MAP
            if energy_sensor.total_source is not None
        }

    def _should_roll_daily_snapshots(self) -> bool:
        """Return whether the local calendar date advanced since the last rollover."""
        now = dt.now()

        if self.last_rollover is None:
            self.last_rollover = now
            return False

        if now.date() > self.last_rollover.date():
            self.last_rollover = now
            _LOGGER.info("Daily reset triggered at local midnight (%s)", now.date())
            return True

        return False

    def clamp_calculated(
        self,
        data: dict[str, Any],
        previous_data: dict[str, Any],
        *,
        is_daily_reset: bool,
    ) -> dict[str, Any]:
        """Keep calculated total-increasing sensors monotonic between resets.

        Derived values sum independently validated counters, so rounding can
        make them dip by a small step without a real decrease. Hold the last
        value in that case, and only let a decrease through when the daily
        counters rolled over this poll. As a hard invariant, a
        total_increasing sensor must never be negative.
        """
        result = dict(data)

        for energy_sensor in ENERGY_SENSOR_MAP:
            if not energy_sensor.is_calculated:
                continue

            current_energy = result.get(energy_sensor.key)
            if current_energy is None:
                continue

            last_energy = previous_data.get(energy_sensor.key)

            accepts_decrease = energy_sensor.resets_daily and is_daily_reset
            # guarded_energy is the value we publish after enforcing the invariants
            guarded_energy = current_energy
            if (
                last_energy is not None
                and guarded_energy < last_energy
                and not accepts_decrease
            ):
                guarded_energy = last_energy

            guarded_energy = max(guarded_energy, 0)
            if current_energy < 0:
                _LOGGER.warning(
                    "Calculated %s came out negative (%s); guarded to %s. "
                    "(last: %s daily_reset: %s)",
                    energy_sensor.key,
                    current_energy,
                    guarded_energy,
                    last_energy,
                    accepts_decrease,
                )
            elif guarded_energy - current_energy > ENERGY_RESOLUTION_KWH:
                _LOGGER.debug(
                    "Keep last calculated %s %s (raw: %s last: %s daily_reset: %s)",
                    energy_sensor.key,
                    guarded_energy,
                    current_energy,
                    last_energy,
                    accepts_decrease,
                )

            result[energy_sensor.key] = guarded_energy

        return result
