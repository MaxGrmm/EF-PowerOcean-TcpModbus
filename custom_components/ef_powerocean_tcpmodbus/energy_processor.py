"""Energy counter validation and daily derivation for PowerOcean telemetry."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.util import dt

from .const import (
    DEFAULT_MAX_POWER,
    ENERGY_RESOLUTION_KWH,
    ENERGY_SENSOR_MAP,
)
from .util import parse_datetime

_LOGGER = logging.getLogger(__name__)


class EnergyProcessor:
    """Processor to validate and derive energy sensors.

    - Lifetime _total counters are the source of truth. They are validated
      against a max believable power since the last accepted read.
    - Daily _today values are derived as total - snapshot, where the snapshot is
      taken at local midnight. The device's own daily registers are never used for
      derived values because we experienced illogical spikes indicating timezone/ghost
      bugs in the inverter itself.
    - The daily reset is triggered when the local calendar date advances.
    """

    def __init__(self, limits: dict[str, Any]) -> None:
        self.limits = limits
        self.accepted_at: dict[str, datetime] = {}
        self.daily_snapshots: dict[str, float] = {}
        self.last_rollover: datetime | None = None

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

    def validate_totals(
        self,
        raw_data: dict[str, Any],
        previous_data: dict[str, Any],
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
                    f"dt is less than one second. Return last data. Delta-t: {elapsed_seconds}"
                )
                return dict(previous_data)

        for energy_sensor in ENERGY_SENSOR_MAP:
            if energy_sensor.is_calculated or energy_sensor.resets_daily:
                continue

            key = energy_sensor.key
            current_energy = result.get(key)
            baseline = previous_data.get(key)
            accepted_at = self.accepted_at.get(key) or previous_time

            if current_energy is None:
                if baseline is not None:
                    result[key] = baseline
                    _LOGGER.debug(
                        f"Fill missing read of {key} with last validated value {baseline}"
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
                result[key] = baseline
                _LOGGER.debug(
                    f"Hold implausible {key}! (raw energy: {current_energy} baseline: {baseline} delta energy: {round(energy_delta, 2)} max delta: {round(max_believable_delta, 4)} accepted at: {accepted_at.time()})"
                )
            else:
                self.accepted_at[key] = now

        return result

    def _max_believable_energy_delta(
        self, energy_sensor: Any, accepted_at: datetime, now: datetime
    ) -> float:
        """Largest believable rise (kWh) for the elapsed time since last accepted value."""
        limit = self.limits.get(energy_sensor.max_power, DEFAULT_MAX_POWER)
        elapsed_hours = max((now - accepted_at).total_seconds(), 0) / 3600
        return limit * elapsed_hours / 1000 + ENERGY_RESOLUTION_KWH

    def derive_daily(self, data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Set each daily counter to its lifetime counter's growth since the
        last reset snapshot; also return whether a reset rolled this poll."""
        result = dict(data)
        is_daily_reset = self._should_roll_daily_snapshots()

        for energy_sensor in ENERGY_SENSOR_MAP:
            if energy_sensor.total_source is None:
                continue

            total_energy = result.get(energy_sensor.total_source)
            if total_energy is None:
                continue

            snapshot = self.daily_snapshots.get(energy_sensor.key)
            if is_daily_reset or snapshot is None:
                snapshot = total_energy
                _LOGGER.debug(
                    f"Snapshot {energy_sensor.key} at {snapshot} (total: {total_energy} reset: {is_daily_reset})"
                )

            snapshot = min(snapshot, total_energy)
            self.daily_snapshots[energy_sensor.key] = snapshot
            result[energy_sensor.key] = round(total_energy - snapshot, 2)

        return result, is_daily_reset

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
            _LOGGER.info(f"Daily reset triggered at local midnight ({now.date()})")
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
                    f"Calculated {energy_sensor.key} came out negative ({current_energy}); guarded to {guarded_energy}. (last: {last_energy} daily_reset: {accepts_decrease})"
                )
            elif guarded_energy - current_energy > ENERGY_RESOLUTION_KWH:
                _LOGGER.debug(
                    f"Keep last calculated {energy_sensor.key} {guarded_energy} (raw: {current_energy} last: {last_energy} daily_reset: {accepts_decrease})"
                )

            result[energy_sensor.key] = guarded_energy

        return result
