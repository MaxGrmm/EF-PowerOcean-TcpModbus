"""What the control test checks, how it judges a result, and the report it writes.

Shared by the control_test action in Home Assistant and by
scripts/control_feature_scan.py, so both decide the same way and write the same
report. Nothing here talks to the inverter or imports Home Assistant: the scripts
load this module without it.

A frame is one reading of the inverter as a flat dict: the decoded registers with
the values derived from them on top, the form the coordinator polls into.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Final

from .const import (
    CONTROL_COMMAND_BATTERY_SAVER_BIT,
    CONTROL_COMMAND_METHOD_MASK,
    CONTROL_COMMAND_METHOD_SHIFT,
    CONTROL_FEATURES,
    CONTROL_STATUS_DAMPING_POLLS,
    FEED_IN_POWER_MAX_KEY,
    FEED_IN_POWER_MAX_SETTING_KEY,
    HEARTBEAT_WINDOW_S,
)
from .models import (
    BATTERY_FULL_SOC,
    POWER_TOLERANCE_FRACTION,
    POWER_TOLERANCE_W,
    ControlFeature,
    ControlMode,
    ControlStatus,
    GridFeedMode,
)

# Bumped whenever a field changes meaning or goes away; adding one does not.
REPORT_SCHEMA_VERSION: Final = 1

DEFAULT_TEST_POWER_W: Final = 1500
MIN_TEST_POWER_W: Final = 600
MAX_TEST_POWER_W: Final = 3000
# The PowerOcean Plus took up to 30s to ramp to 1500 W, so this leaves margin.
CONTROL_SETTLE_S: Final = 60
RETURN_SETTLE_S: Final = 9
STATUS_BIT_WAIT_S: Final = 15
HANDBACK_WAIT_S: Final = HEARTBEAT_WINDOW_S + 20
# Room left above the reserve before discharging, and below full before charging,
# so the battery can actually move the way the test asks.
SOC_MARGIN: Final = 5.0
MIN_ACHIEVABLE_W: Final = 300.0

SETPOINT_KEYS: Final = (
    "battery_power_setpoint",
    "system_power_setpoint",
    "inverter_power_setpoint",
)

# What a report keeps of each reading, to show how the inverter got where it did.
TRACE_KEYS: Final = (
    "battery_power",
    "grid_power",
    "solar_power",
    "house_power",
    "inverter_output_power",
    "battery_soc",
    "active_control_mode",
    "device_modbus_control",
)

# The conditions a report records at the start, which explain an inconclusive run.
CONDITION_KEYS: Final = (
    "battery_soc",
    "min_soc_limit",
    "battery_power",
    "grid_power",
    "solar_power",
    "house_power",
    "grid_feed_mode",
    FEED_IN_POWER_MAX_KEY,
    "system_modes_hex",
)

# Every register a run reads. The coordinator polls only what is asked for, so
# the test asks for these while it runs, whatever entities are enabled.
REQUIRED_KEYS: Final = frozenset(
    {
        *SETPOINT_KEYS,
        *TRACE_KEYS,
        *CONDITION_KEYS,
        "min_soc_limit",
        FEED_IN_POWER_MAX_SETTING_KEY,
        "system_modes",
    }
)


@dataclass(frozen=True)
class CoreTest:
    """One control method, commanded in one direction."""

    name: str
    method: ControlMode
    setpoint_key: str
    measure_key: str
    # +1 draws into the battery or from the grid, -1 feeds out.
    sign: int

    @property
    def charges(self) -> bool:
        return self.sign > 0


def from_feature(name: str, feature: ControlFeature) -> CoreTest:
    """Build a test from the integration's feature definition."""
    definition = CONTROL_FEATURES[feature]
    assert definition.setpoint_key is not None and definition.measure_key is not None
    return CoreTest(
        name,
        definition.method,
        definition.setpoint_key,
        definition.measure_key,
        definition.sign,
    )


# Each protocol control method in both directions.
CORE_TESTS: Final = (
    from_feature("battery charge", ControlFeature.CHARGE_BATTERY),
    from_feature("battery discharge", ControlFeature.DISCHARGE_BATTERY),
    from_feature("grid draw", ControlFeature.IMPORT_FROM_GRID),
    from_feature("grid feed", ControlFeature.EXPORT_TO_GRID),
    # Not an integration feature yet, so declared here against the protocol:
    # 40544 Inverter Power Draw/Feed Setting, positive draws from the grid, and
    # 40550 Inverter Output Power, rectification positive.
    CoreTest(
        "inverter draw",
        ControlMode.INVERTER_FEED,
        "inverter_power_setpoint",
        "inverter_output_power",
        1,
    ),
    CoreTest(
        "inverter feed",
        ControlMode.INVERTER_FEED,
        "inverter_power_setpoint",
        "inverter_output_power",
        -1,
    ),
)


class Verdict(StrEnum):
    """What the inverter did with one command."""

    FOLLOWED = "followed"
    NOT_FOLLOWED = "not_followed"
    # The battery could not take or give the power: full or at its reserve.
    UNREACHABLE = "unreachable"
    # The measured value was already at the target before the command.
    INCONCLUSIVE = "inconclusive"
    # The conditions did not allow the test, such as a full battery.
    SKIPPED = "skipped"
    WRITE_REFUSED = "write_refused"
    # Not run at all: no register on this model, or a part only the script runs.
    NOT_TESTED = "not_tested"

    @property
    def label(self) -> str:
        """Return the verdict as the script prints it."""
        if self in (Verdict.FOLLOWED, Verdict.NOT_FOLLOWED):
            return self.replace("_", " ").upper()
        return self.replace("_", " ")

    @property
    def decisive(self) -> bool:
        """Return whether the verdict says something about the firmware.

        The others depend on the battery, the sun or the model, so a change
        between two of them is not a change in the firmware.
        """
        return self in (Verdict.FOLLOWED, Verdict.NOT_FOLLOWED, Verdict.WRITE_REFUSED)


@dataclass
class FeatureResult:
    """The outcome of one core test."""

    name: str
    method: str
    setpoint_key: str
    measure_key: str
    verdict: Verdict
    detail: str = ""
    target_w: float | None = None
    baseline_w: float | None = None
    # Mean of the last readings, the value the verdict was reached on.
    achieved_w: float | None = None
    settle_s: float | None = None
    # Whether the inverter reported the method it was told, and how.
    method_reported: bool | None = None
    status_report: str = ""
    samples: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def for_test(
        cls, test: CoreTest, verdict: Verdict, detail: str = ""
    ) -> FeatureResult:
        return cls(
            test.name,
            str(test.method),
            test.setpoint_key,
            test.measure_key,
            verdict,
            detail,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["verdict"] = str(self.verdict)
        return data


@dataclass
class ControlTestReport:
    """A whole run, in the form attached to an issue and compared between firmware."""

    source: str
    created_at: str
    integration_version: str | None = None
    home_assistant_version: str | None = None
    model: str | None = None
    firmware_version: str | None = None
    protocol_version: int | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    conditions: dict[str, Any] = field(default_factory=dict)
    heartbeat: str = "not tested"
    manual_mode_bit: str = "not tested"
    # Seconds until the inverter reported Modbus control, None if it never did.
    manual_mode_bit_s: float | None = None
    features: list[FeatureResult] = field(default_factory=list)
    handback: str = "not tested"
    # Seconds after the last heartbeat until it reported the app back in charge.
    handback_s: float | None = None
    # Checks only the script runs, such as the ones that need the app open.
    extra: dict[str, Any] = field(default_factory=dict)
    outcome: str = "running"
    abort_reason: str | None = None
    finished_at: str | None = None
    schema_version: int = REPORT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["features"] = [result.to_dict() for result in self.features]
        return data

    def summary(self) -> dict[str, str]:
        """Return each feature's verdict, the line a sensor or logbook can show."""
        return {result.name: str(result.verdict) for result in self.features}


def compose_control_word(method: ControlMode, battery_saver: bool) -> int:
    """Build a control word from the method and battery-saver state."""
    word = (
        method.command_value & CONTROL_COMMAND_METHOD_MASK
    ) << CONTROL_COMMAND_METHOD_SHIFT
    if battery_saver:
        word |= 1 << CONTROL_COMMAND_BATTERY_SAVER_BIT
    return word


def tolerance(target: float) -> float:
    return max(POWER_TOLERANCE_W, abs(target) * POWER_TOLERANCE_FRACTION)


def measure(test: CoreTest, frame: Mapping[str, Any]) -> tuple[float | None, bool]:
    """Return the measured value, and whether it had to be derived.

    Inverter output is worked out from battery minus solar where its register is
    unreadable, since battery power includes solar.
    """
    value = frame.get(test.measure_key)
    if value is not None:
        return float(value), False
    if test.measure_key == "inverter_output_power":
        battery, solar = frame.get("battery_power"), frame.get("solar_power")
        if battery is not None and solar is not None:
            return float(battery) - float(solar), True
    return None, False


def export_limit(frame: Mapping[str, Any]) -> float | None:
    """Return the watt cap when the feed-in mode is limited."""
    if (
        GridFeedMode.from_register(frame.get("grid_feed_mode"))
        is not GridFeedMode.LIMITED
    ):
        return None
    return frame.get(FEED_IN_POWER_MAX_KEY)


def achievable_power(test: CoreTest, frame: Mapping[str, Any]) -> float | None:
    """Return the largest feed target the export cap allows, if it caps one.

    Inverter feed can use house load plus unused export capacity. Grid feed is
    limited by the cap alone.
    """
    if test.charges:
        return None
    cap = export_limit(frame)
    configured = frame.get(FEED_IN_POWER_MAX_SETTING_KEY)
    # Some models read 0 for the effective cap whatever the setting.
    if cap is not None and cap < MIN_ACHIEVABLE_W and configured:
        cap = configured
    if cap is None:
        return None
    if test.method is ControlMode.SYSTEM_FEED:
        return cap
    house = float(frame.get("house_power") or 0)
    solar = float(frame.get("solar_power") or 0)
    return max(0.0, house + cap - solar)


def skip_reason(test: CoreTest, frame: Mapping[str, Any]) -> str:
    """Return why the battery cannot run the test now, or an empty string."""
    soc = frame.get("battery_soc")
    floor = float(frame.get("min_soc_limit") or 0)
    if soc is None:
        return ""
    if test.charges and soc >= BATTERY_FULL_SOC - SOC_MARGIN:
        return f"battery at {soc:.0f}%, too full to take a charge"
    if not test.charges and soc <= floor + SOC_MARGIN:
        return f"battery at {soc:.0f}%, too close to its {floor:.0f}% reserve"
    return ""


def is_decisive(test: CoreTest, power: float, baseline: float) -> bool:
    """Return whether the target differs from the baseline beyond tolerance."""
    target = power * test.sign
    return abs(target - baseline) > tolerance(target)


def pick_decisive_power(
    test: CoreTest, power: float, baseline: float, limit: float | None
) -> float | None:
    """Choose the nearest test power that differs clearly from the baseline."""
    ceiling = min(MAX_TEST_POWER_W, limit) if limit is not None else MAX_TEST_POWER_W
    candidates = sorted(
        range(MIN_TEST_POWER_W, int(ceiling) + 1, 100),
        key=lambda candidate: abs(candidate - power),
    )
    return next((float(c) for c in candidates if is_decisive(test, c, baseline)), None)


@dataclass
class Plan:
    """The power one test runs at, decided from the frame before it."""

    power: float
    baseline: float | None
    decisive: bool
    notes: list[str] = field(default_factory=list)

    def signed_target(self, test: CoreTest) -> float:
        return self.power * test.sign


def plan_test(
    test: CoreTest, frame: Mapping[str, Any], power: float
) -> tuple[Plan | None, str]:
    """Return the plan for a test, or None and why it cannot run now."""
    if reason := skip_reason(test, frame):
        return None, reason

    notes: list[str] = []
    limit = achievable_power(test, frame)
    if limit is not None and limit < MIN_ACHIEVABLE_W:
        return None, f"the export cap leaves only {limit:.0f} W to feed"
    if limit is not None and limit < power:
        notes.append(f"tested at {limit:.0f} W, the most the export cap allows")
        power = limit

    baseline, derived = measure(test, frame)
    if derived:
        notes.append(f"{test.measure_key} unreadable, used battery minus solar")
    if baseline is not None and not is_decisive(test, power, baseline):
        decisive_power = pick_decisive_power(test, power, baseline, limit)
        if decisive_power is not None:
            notes.append(
                f"tested at {decisive_power:.0f} W, as {power:.0f} W was already "
                "flowing before the command"
            )
            power = decisive_power
    decisive = baseline is None or is_decisive(test, power, baseline)
    return Plan(power, baseline, decisive, notes), ""


def judge(
    test: CoreTest,
    plan: Plan,
    settle_s: float | None,
    last_state: ControlStatus | None,
    measured: Sequence[float],
) -> tuple[Verdict, str, float | None]:
    """Return the verdict, its detail and the value it was reached on."""
    tail = statistics.fmean(measured[-3:]) if measured else None
    target = plan.signed_target(test)
    if settle_s is not None and plan.decisive:
        verdict, detail = Verdict.FOLLOWED, ""
    elif settle_s is not None:
        verdict = Verdict.INCONCLUSIVE
        detail = f"{test.measure_key} was already near the target before the command"
    elif last_state in (
        ControlStatus.UNREACHABLE_BATTERY_FULL,
        ControlStatus.UNREACHABLE_BATTERY_EMPTY,
    ):
        verdict, detail = Verdict.UNREACHABLE, str(last_state)
    else:
        verdict = Verdict.NOT_FOLLOWED
        shown = "-" if tail is None else f"{tail:+.0f}"
        detail = f"{test.measure_key} averaged {shown} W against {target:+.0f} W"
    if plan.notes:
        detail = "; ".join(filter(None, (detail, *plan.notes)))
    return verdict, detail, tail


def judge_modes(expected: str, modes: Sequence[tuple[float, str]]) -> tuple[bool, str]:
    """Check the last readings for the requested method; the status can lag a poll."""
    tail = [mode for _, mode in modes[-CONTROL_STATUS_DAMPING_POLLS:]]
    if tail and all(mode == expected for mode in tail):
        first = next(at for at, mode in modes if mode == expected)
        return True, f"reports {expected} within {first:.0f}s"
    seen = ", ".join(dict.fromkeys(tail)) or "nothing"
    return False, f"reports {seen}, NOT {expected}"


def trace_sample(elapsed_s: float, frame: Mapping[str, Any]) -> dict[str, Any]:
    """Return the part of a reading a report keeps."""
    sample: dict[str, Any] = {"t": round(elapsed_s, 1)}
    for key in TRACE_KEYS:
        value = frame.get(key)
        sample[key] = round(value) if isinstance(value, float) else value
    return sample


def conditions_of(frame: Mapping[str, Any]) -> dict[str, Any]:
    """Return the readings that explain what a run could test."""
    return {
        key: str(value) if isinstance(value, StrEnum) else value
        for key in CONDITION_KEYS
        if (value := frame.get(key)) is not None
    }


# ── Comparing two reports ────────────────────────────────────────────────────

# A settle time has to move this much, and by this fraction, to count as changed:
# the poll interval alone makes it jump by a few seconds.
SETTLE_CHANGE_S: Final = 10.0
SETTLE_CHANGE_FRACTION: Final = 0.5


@dataclass(frozen=True)
class Difference:
    """One way a feature behaved differently in the newer report."""

    feature: str
    what: str
    old: Any
    new: Any
    # Whether the firmware is the likely cause rather than the conditions.
    significant: bool

    def __str__(self) -> str:
        marker = "!" if self.significant else "~"
        return f"{marker} {self.feature}: {self.what} {self.old} -> {self.new}"


def compare_reports(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[Difference]:
    """Return how the features in *new* behaved differently from those in *old*.

    A verdict that flips between two decisive ones, followed to not followed say,
    is significant. One that becomes skipped or inconclusive is noted but not: the
    battery or the sun decided that, not the firmware.
    """
    differences: list[Difference] = []
    old_features = {item["name"]: item for item in old.get("features", [])}
    new_features = {item["name"]: item for item in new.get("features", [])}

    for name in sorted(old_features.keys() | new_features.keys()):
        before, after = old_features.get(name), new_features.get(name)
        if before is None or after is None:
            differences.append(
                Difference(
                    name,
                    "presence",
                    "tested" if before else "absent",
                    "tested" if after else "absent",
                    False,
                )
            )
            continue
        old_verdict, new_verdict = Verdict(before["verdict"]), Verdict(after["verdict"])
        if old_verdict is not new_verdict:
            differences.append(
                Difference(
                    name,
                    "verdict",
                    str(old_verdict),
                    str(new_verdict),
                    old_verdict.decisive and new_verdict.decisive,
                )
            )
            continue
        if before.get("method_reported") != after.get("method_reported"):
            differences.append(
                Difference(
                    name,
                    "method reported",
                    before.get("method_reported"),
                    after.get("method_reported"),
                    True,
                )
            )
        old_settle, new_settle = before.get("settle_s"), after.get("settle_s")
        if old_settle is not None and new_settle is not None:
            change = abs(new_settle - old_settle)
            if (
                change >= SETTLE_CHANGE_S
                and change >= old_settle * SETTLE_CHANGE_FRACTION
            ):
                differences.append(
                    Difference(
                        name,
                        "settle time",
                        f"{old_settle:.0f}s",
                        f"{new_settle:.0f}s",
                        True,
                    )
                )

    if old.get("heartbeat") != new.get("heartbeat"):
        differences.append(
            Difference(
                "heartbeat", "result", old.get("heartbeat"), new.get("heartbeat"), True
            )
        )
    # Only whether the bit was seen at all: its timing follows the poll interval.
    for key, what in (
        ("manual_mode_bit_s", "manual mode bit"),
        ("handback_s", "hand back"),
    ):
        seen_before, seen_after = old.get(key) is not None, new.get(key) is not None
        if seen_before != seen_after:
            differences.append(
                Difference(
                    what,
                    "reported",
                    "seen" if seen_before else "not seen",
                    "seen" if seen_after else "not seen",
                    True,
                )
            )
    return differences
