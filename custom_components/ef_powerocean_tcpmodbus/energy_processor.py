"""Energy counter validation and daily derivation for PowerOcean telemetry."""

from __future__ import annotations

import logging
import math
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, TypeIs

from homeassistant.util import dt

from .const import (
    DEFAULT_MAX_POWER,
    ENERGY_RESOLUTION_KWH,
    ENERGY_SENSOR_MAP,
)
from .models import EnergySensorDef
from .util import parse_datetime

_LOGGER = logging.getLogger(__name__)


# The counters the processor validates and derives the daily values from.
ENERGY_KEYS: Final = frozenset(
    key
    for energy_sensor in ENERGY_SENSOR_MAP
    for key in (energy_sensor.key, energy_sensor.total_source)
    if key is not None
)


# The lifetime counters the device reports, as opposed to the calculated ones.
DEVICE_TOTAL_KEYS: Final = tuple(
    energy_sensor.key
    for energy_sensor in ENERGY_SENSOR_MAP
    if not energy_sensor.is_calculated and not energy_sensor.resets_daily
)


# The device's daily counter for each lifetime total, a separate register.
DAILY_OF_TOTAL: Final = {
    energy_sensor.total_source: energy_sensor.key
    for energy_sensor in ENERGY_SENSOR_MAP
    if energy_sensor.total_source is not None
}

# A 0 baseline held against a large device total gives way once the device's total
# has risen in step with its daily counter by this much, over this many reads.
WITNESS_MIN_RISE_KWH: Final = 1.0
WITNESS_MIN_READS: Final = 3
# How far the two rises may differ: register resolution plus a share no misscaled
# counter fits.
WITNESS_TOLERANCE_KWH: Final = 0.02
WITNESS_TOLERANCE_SHARE: Final = 0.02


@dataclass(slots=True)
class WitnessRun:
    """Reads of a held total and its daily counter that have risen together."""

    first_total: float
    first_daily: float
    last_total: float
    last_daily: float
    last_seen: datetime
    reads: int = 1


@dataclass(frozen=True, slots=True)
class HeldTotal:
    """A lifetime total the processor holds while the device reports another value."""

    published: float
    device: float
    since: datetime | None


def offsets_from(
    stored: Any, keys: Collection[str] = DEVICE_TOTAL_KEYS
) -> dict[str, float]:
    """Return the usable offsets of a stored mapping, by the total they correct.

    Keys not among the given totals and values that are not a finite, non-zero
    number are left out, so a damaged entry corrects nothing.
    """
    if not isinstance(stored, Mapping):
        return {}
    return {
        key: float(value)
        for key, value in stored.items()
        if key in keys
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value != 0
    }


def _is_reading(value: Any) -> TypeIs[float]:
    """Whether a device total is a real reading: a number above 0."""
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def _rises_agree(total_rise: float, daily_rise: float) -> bool:
    """Whether a lifetime counter rose as much as its daily counter, never lower."""
    return daily_rise >= 0 and abs(total_rise - daily_rise) <= (
        WITNESS_TOLERANCE_KWH + WITNESS_TOLERANCE_SHARE * daily_rise
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
    - A first read of 0 is published but not trusted as a baseline, since a
      lifetime counter has been seen reading a bogus 0 for hours (#169). The
      first non-zero read after it is taken once the next read follows it as a
      counter would, so a one-off spike cannot take its place.
    - A 0 baseline that an earlier version accepted gives way to the device's total
      once that total rises in step with the device's daily counter (#169).
    - A user's offset is added to a device total before anything else, so a total
      the inverter reset continues where it was. A changed offset moves its total
      once, without the move being held as implausible.
    """

    def __init__(self, limits: dict[str, Any]) -> None:
        self.limits = limits
        # The offsets the saved totals include, by total.
        self.offsets: dict[str, float] = {}
        self.accepted_at: dict[str, datetime] = {}
        self.daily_snapshots: dict[str, float] = {}
        self.last_rollover: datetime | None = None
        # The first non-zero read after an unconfirmed 0, waiting for the next read
        # to follow it as a counter would. Not saved: after a restart the 0 is
        # still unconfirmed, and the next two reads confirm again.
        self.first_reads: dict[str, tuple[float, datetime]] = {}
        self.witness_runs: dict[str, WitnessRun] = {}
        # Totals held on their latest read, with what the device reported. Not
        # saved: the next poll after a restart finds them again.
        self.held: dict[str, HeldTotal] = {}

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
            "energy_offsets": self.offsets,
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
        self.offsets = offsets_from(stored.get("energy_offsets"))

    def apply_offsets(self, raw_data: Mapping[str, Any]) -> dict[str, Any]:
        """Add each total's offset to what the device reports for it.

        A read of 0 is left as it is: it is the glitch the validation holds back,
        and with the offset added it would pass for a real value (#169).
        """
        result = dict(raw_data)
        for key, offset in self.offsets.items():
            if _is_reading(value := result.get(key)):
                result[key] = round(value + offset, 3)
        return result

    def change_offsets(
        self, offsets: Mapping[str, float], last_data: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Switch to new offsets; return the last data with their totals moved.

        A total whose offset changed moves to its last device read plus the new
        offset and is accepted there, so the correction is not held as a jump.
        That also keeps a held total at its published value when its offset was
        worked out from it. Today's value stays as it was.
        """
        result = dict(last_data)
        now = dt.now()
        for key in DEVICE_TOTAL_KEYS:
            old, new = self.offsets.get(key, 0.0), offsets.get(key, 0.0)
            baseline = result.get(key)
            if new == old or not isinstance(baseline, (int, float)):
                continue
            device = result.get(f"{key}_raw")
            moved = round(
                device + new if _is_reading(device) else baseline + new - old, 3
            )
            result[key] = moved
            daily_key = DAILY_OF_TOTAL.get(key)
            if daily_key in self.daily_snapshots:
                self.daily_snapshots[daily_key] = round(
                    self.daily_snapshots[daily_key] + moved - baseline, 3
                )
            self.accepted_at[key] = now
            self.held.pop(key, None)
            self.first_reads.pop(key, None)
            self.witness_runs.pop(key, None)
            _LOGGER.info(
                "Offset of %s changed from %s to %s kWh; it continues from %s kWh",
                key,
                old,
                new,
                moved,
            )
        self.offsets = dict(offsets)
        return result

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
            # A 0 that was read first and never followed by a real value has no
            # acceptance time: it is shown, but never holds back the next read.
            unconfirmed_zero = baseline == 0 and key not in self.accepted_at
            if (
                baseline is not None
                and previous_time is not None
                and not unconfirmed_zero
            ):
                # A counter restored from a state file written by 2.4.0 has no
                # acceptance time. Its baseline was accepted when it was saved, so
                # record that, as for any other counter. Falling back to the
                # previous poll instead holds it for good once it is held (#169).
                self.accepted_at.setdefault(key, previous_time)
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

            if baseline is None:
                # A first read, taken as the device reports it. A 0 gets no
                # acceptance time, so it never holds back the real value (#169).
                if current_energy != 0:
                    self.accepted_at[key] = now
                self.held.pop(key, None)
                continue

            if unconfirmed_zero:
                if current_energy > 0 and self._confirms_first_read(
                    energy_sensor, current_energy, now
                ):
                    self.accepted_at[key] = now
                    self._restart_daily_from_device(key)
                else:
                    result[key] = baseline
                self.held.pop(key, None)
                continue

            if accepted_at is None:
                self.accepted_at[key] = now
                continue

            energy_delta = current_energy - baseline
            max_believable_delta = self._max_believable_energy_delta(
                energy_sensor, accepted_at, now
            )

            if energy_delta < 0 or energy_delta > max_believable_delta:
                if (
                    baseline == 0
                    and energy_delta > 0
                    and self._daily_confirms(
                        energy_sensor,
                        current_energy,
                        raw_data.get(DAILY_OF_TOTAL.get(key, "")),
                        now,
                    )
                ):
                    _LOGGER.info(
                        "%s was held at 0 while the device counted %s in step with "
                        "its daily counter; taking the device's total",
                        key,
                        current_energy,
                    )
                    self.accepted_at[key] = now
                    self.held.pop(key, None)
                    self._restart_daily_from_device(key)
                    continue
                result[key] = baseline
                self.held[key] = HeldTotal(
                    baseline, current_energy, self.accepted_at.get(key)
                )
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
                self.accepted_at[key] = now
                self.held.pop(key, None)
                self.witness_runs.pop(key, None)

        return result

    def _daily_confirms(
        self,
        energy_sensor: EnergySensorDef,
        total: float,
        daily: Any,
        now: datetime,
    ) -> bool:
        """Track a total held at 0; return whether its daily counter confirms it.

        Each read must follow the one before as a counter would, within the rise
        budget and rising as much as the device's daily counter for the same
        energy, a separate register, rose. Once that has risen by a kWh, the whole
        run must agree too, so a spike, a repeating garbage value or a misscaled
        counter is never taken for the counter.
        """
        key = energy_sensor.key
        if not isinstance(daily, (int, float)) or not math.isfinite(daily):
            self.witness_runs.pop(key, None)
            return False
        run = self.witness_runs.get(key)
        if (
            run is not None
            and 0
            <= total - run.last_total
            <= self._max_believable_energy_delta(energy_sensor, run.last_seen, now)
            and _rises_agree(total - run.last_total, daily - run.last_daily)
        ):
            run.last_total, run.last_daily = total, daily
            run.last_seen, run.reads = now, run.reads + 1
        else:
            run = self.witness_runs[key] = WitnessRun(total, daily, total, daily, now)
        daily_rise = run.last_daily - run.first_daily
        if (
            run.reads >= WITNESS_MIN_READS
            and daily_rise >= WITNESS_MIN_RISE_KWH
            and _rises_agree(run.last_total - run.first_total, daily_rise)
        ):
            del self.witness_runs[key]
            return True
        return False

    def take_device_totals(self, keys: Iterable[str]) -> dict[str, float]:
        """Accept what the device reports for held totals, as the user asked.

        Returns the totals taken, for the caller to use as their new baseline. A
        device value of 0 is never taken: it is the glitch these holds exist for.
        """
        now = dt.now()
        taken: dict[str, float] = {}
        for key in keys:
            held = self.held.pop(key, None)
            if held is None or held.device <= 0:
                continue
            self.accepted_at[key] = now
            self.witness_runs.pop(key, None)
            self._restart_daily_from_device(key)
            taken[key] = held.device
        return taken

    def _confirms_first_read(
        self, energy_sensor: EnergySensorDef, value: float, now: datetime
    ) -> bool:
        """Whether this read confirms the non-zero read before it as the counter.

        It must follow the earlier read as a counter would: never lower, and no
        higher than the configured power allows for the time between them.
        Otherwise this read becomes the one waiting for confirmation.
        """
        key = energy_sensor.key
        earlier = self.first_reads.get(key)
        if earlier is not None:
            earlier_value, earlier_time = earlier
            if (
                0
                <= value - earlier_value
                <= self._max_believable_energy_delta(energy_sensor, earlier_time, now)
            ):
                del self.first_reads[key]
                return True
        self.first_reads[key] = (value, now)
        return False

    def _restart_daily_from_device(self, total_key: str) -> None:
        """Rebuild the daily counters of a total from the device's daily counters.

        Their snapshot was taken from a 0 that turned out not to be the counter,
        so today would otherwise count the whole lifetime total.
        """
        for energy_sensor in ENERGY_SENSOR_MAP:
            if energy_sensor.total_source == total_key:
                self.daily_snapshots.pop(energy_sensor.key, None)

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
    def raw_total_values(raw_data: Mapping[str, Any]) -> dict[str, Any]:
        """Echo each device lifetime register under a *_raw diagnostic key.

        Shows what the device reports next to the validated total, so a held
        total can be told apart from a device that reports the same.
        """
        return {f"{key}_raw": raw_data.get(key) for key in DEVICE_TOTAL_KEYS}

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
