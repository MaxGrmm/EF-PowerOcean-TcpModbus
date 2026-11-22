"""Types of the integration itself, as opposed to those of the device."""

from __future__ import annotations

from enum import StrEnum


class AlertVerbosity(StrEnum):
    """How much the integration raises in repairs, from least to most.

    Each level raises everything the one before it does.
    """

    # What the user must act on: the inverter stopped updating, Modbus is off, it
    # does not answer, totals are held, another inverter answers.
    ESSENTIAL = "essential"
    # Also readings that cannot be trusted: a failed reading check, a counter
    # going back, sensors unavailable unexplained, control not accepted.
    READINGS = "readings"
    # Also the connection's health: reconnects, late polls, control writes with
    # control off, power that does not add up.
    DETAILED = "detailed"
    # Also every warning and error the integration logs.
    DEBUG = "debug"

    @property
    def rank(self) -> int:
        return list(AlertVerbosity).index(self)


class CoordinatorStatus(StrEnum):
    SUCCESS = "success"
    READ_FAILED = "read_failed"
    RECONNECT_FAILED = "reconnect_failed"
    PROCESSING_FAILED = "processing_failed"
    IDENTITY_MISMATCH = "identity_mismatch"
    # The poll succeeded, but the inverter stopped updating what it serves.
    READINGS_STALLED = "readings_stalled"
