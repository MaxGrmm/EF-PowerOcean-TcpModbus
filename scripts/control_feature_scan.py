#!/usr/bin/env python3
"""Test which Modbus controls an inverter supports.

Before running it, turn off Modbus Control in the integration and wait a minute,
so nothing else is commanding the inverter. Have the EcoFlow app open, battery
saver and the settings are confirmed by you there.

    uv pip install -r requirements-development.txt
    uv run python scripts/control_feature_scan.py <inverter_ip>

It takes 5 to 10 minutes. The battery charges and discharges briefly at the test
power and the inverter is handed back to the EcoFlow app at the end.

    uv run python scripts/control_feature_scan.py <inverter_ip> --reserve-probe

Instead tests the backup reserve. Set in the app, a reserve above the SOC makes the
inverter charge from the grid up to it, and one at the SOC stops it discharging.
The probe does both by writing the register (40536) under each control state, and
asks you to do the same in the app as the reference, so it finds the state the
firmware acts in, if any. Run it while the battery is covering the house.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from pymodbus.client import ModbusTcpClient
from utils import (
    EXCEPTION_MEANINGS,
    MANIFEST,
    RegisterReader,
    const,
    core,
    models,
    render_address,
    report_device,
    telemetry,
)

ControlMode = models.ControlMode
Feature = models.ControlFeature
RegisterType = models.RegisterType

SETPOINT_KEYS: Final = core.SETPOINT_KEYS

# The registers sampled while testing; a small set, so a sample takes a second.
WATCHED_KEYS: Final = (
    "house_power",
    "grid_power",
    "solar_power",
    "battery_power",
    "battery_soc",
    "system_modes",
    "min_soc_limit",
    "grid_feed_mode",
    const.FEED_IN_POWER_MAX_SETTING_KEY,
    const.FEED_IN_POWER_MAX_EFFECTIVE_KEY,
    "device_led_brightness",
    "inverter_output_power",
    *SETPOINT_KEYS,
)

# The control tests, their judgement and the report are shared with the
# integration's run_control_test action, so both reach the same verdicts.
DEFAULT_TEST_POWER_W: Final = core.DEFAULT_TEST_POWER_W
MIN_TEST_POWER_W: Final = core.MIN_TEST_POWER_W
MAX_TEST_POWER_W: Final = core.MAX_TEST_POWER_W
PROBE_SETPOINT_W: Final = 500
# A 32-bit write is echoed as sent and only republished in read order a few
# seconds later, so the word order is judged on the reading after this.
WRITE_ORDER_SETTLE_S: Final = 15
SAMPLE_GAP_S: Final = 3
CONTROL_SETTLE_S: Final = core.CONTROL_SETTLE_S
RETURN_SETTLE_S: Final = core.RETURN_SETTLE_S
STATUS_BIT_WAIT_S: Final = core.STATUS_BIT_WAIT_S
HANDBACK_WAIT_S: Final = core.HANDBACK_WAIT_S
SOC_MARGIN: Final = core.SOC_MARGIN
MIN_ACHIEVABLE_W: Final = core.MIN_ACHIEVABLE_W
# The grid power above which the export counts as stopped. The inverter holds a
# zero-export limit to within a few tens of watts, not exactly.
EXPORT_STOPPED_W: Final = const.GUARD_POWER_DEADBAND_W
GRID_FEED_SETTLE_S: Final = 45
BUSY_RETRIES: Final = 3
# Illegal function, address and value: the request itself is wrong, so the device
# will refuse it again. Anything else, device busy above all, is worth retrying.
PERMANENT_CODES: Final = frozenset({1, 2, 3})

# The backup reserve probe (--reserve-probe). In the app, a reserve above the SOC
# makes the inverter charge from the grid up to it, and a reserve at the SOC stops
# it discharging. The probe asks whether a write to the register does the same,
# and under which control state, against the app doing it as the reference.
RESERVE_RAISE_PCT: Final = 10
# Grid charging to the reserve started within a minute in the app; this leaves
# margin for a setting read on a slower schedule.
RESERVE_SETTLE_S: Final = 120
# After the reserve goes back, how long to wait for the effect to end.
RESERVE_RELEASE_S: Final = 60
# Charging counts once the battery and the grid both rise this much above their
# baseline, so solar that happened to come out is not mistaken for it.
RESERVE_CHARGE_W: Final = 300.0
# The floor cases need the battery discharging at least this much to start with,
# so that it stopping is a visible change.
RESERVE_FLOOR_MIN_DISCHARGE_W: Final = 300.0
# Unmapped on every model; it held the same value as 40536 in an Ocean 2 Plus scan.
# Read alongside in case it is the copy the firmware enforces; never written.
RESERVE_SHADOW_ADDRESS: Final = 40518


class Aborted(Exception):
    """Stop the scan when its preconditions are not met."""

    pass


CoreTest = core.CoreTest
CORE_TESTS: Final = core.CORE_TESTS
Verdict = core.Verdict


class ReserveEffect(StrEnum):
    """What a raised reserve is expected to make the inverter do."""

    # Reserve above the SOC: charge from the grid up to it.
    CHARGE = "charge"
    # Reserve at the SOC while discharging: stop discharging.
    FLOOR = "floor"


@dataclass(frozen=True)
class ReserveCase:
    """One way of setting the backup reserve, under one control state."""

    name: str
    effect: ReserveEffect
    # Whether the heartbeat runs and a control word is written first.
    session: bool
    # Set by the user in the app instead of written to the register: the
    # reference the register cases are judged against.
    via_app: bool = False
    method: models.ControlMode = ControlMode.DEFAULT
    # Write the control word again after the reserve, in case the firmware only
    # reads its settings on a command.
    resend_control_word: bool = False
    # Send the hold the integration's Hold battery sends, to see whether a manual
    # battery command takes precedence over the reserve.
    hold: bool = False


# Grouped by session so the heartbeat starts once, after the cases without it.
# Each control state starts with the app as its reference where there is one.
RESERVE_CASES: Final = (
    ReserveCase(
        "app, no session (reference)", ReserveEffect.CHARGE, False, via_app=True
    ),
    ReserveCase("register, no session", ReserveEffect.CHARGE, False),
    ReserveCase("register floor, no session", ReserveEffect.FLOOR, False),
    ReserveCase("register, session on default method", ReserveEffect.CHARGE, True),
    ReserveCase(
        "register, session, control word re-sent after the write",
        ReserveEffect.CHARGE,
        True,
        resend_control_word=True,
    ),
    ReserveCase("register floor, session on default method", ReserveEffect.FLOOR, True),
    ReserveCase(
        "app, session on default method", ReserveEffect.CHARGE, True, via_app=True
    ),
    ReserveCase(
        "register, session under a battery hold (method 3, +1 W)",
        ReserveEffect.CHARGE,
        True,
        method=ControlMode.BATTERY_LIMITS,
        hold=True,
    ),
)


@dataclass
class ManualResult:
    """Write result and optional app confirmation."""

    register: str
    app: str = "not asked"

    def __str__(self) -> str:
        return f"register {self.register}; app {self.app}"


@dataclass
class Report:
    write_order: str = "not tested"
    heartbeat: str = "not tested"
    manual_mode_bit: str = "not tested"
    manual_mode_bit_s: float | None = None
    grid_feed: str = "not tested"
    handback: str = "not tested"
    handback_s: float | None = None
    manual: dict[str, ManualResult | str] = field(default_factory=dict)
    controls: list[core.FeatureResult] = field(default_factory=list)
    # --reserve-probe: case name -> what the inverter did.
    reserve: dict[str, str] = field(default_factory=dict)


def refusal_code(reason: str) -> int | None:
    match = re.match(r"exception code (\d+)", reason)
    return int(match.group(1)) if match else None


class Inverter:
    """Synchronous Modbus access and state for one scan."""

    def __init__(
        self,
        client: ModbusTcpClient,
        reader: RegisterReader,
        slave: int,
        model: models.InverterModel,
    ) -> None:
        self._client = client
        self._reader = reader
        self._slave = slave
        self.model = model
        self.registers = {
            key: const.REGISTERS_BY_KEY[key].for_model(model) for key in WATCHED_KEYS
        }
        self.refused: set[str] = set()
        # The integration sends 32-bit values high word first on every model. The
        # probe below checks that, and the rest of the run follows what it finds.
        self.high_word_first_writes = True
        self.last_heartbeat: float | None = None
        self.heartbeat_running = False
        self.writes = 0
        # Values to restore if a check is interrupted.
        self.pending_settings: dict[str, int] = {}
        # A reserve the user raised in the app, which only they can put back.
        self.pending_app_reserve: int | None = None
        self.started = time.monotonic()

    def sample(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Read watched registers and calculate their derived values."""
        self.keep_alive()
        raw: dict[str, Any] = {}
        for key, register in self.registers.items():
            if key in self.refused:
                continue
            value, reason = self._reader.read_value(register)
            if refusal_code(reason) in PERMANENT_CODES:
                self.refused.add(key)
            raw[key] = value
        derived = telemetry.calculate_derived_values(
            telemetry.TelemetryData.from_mapping(raw),
            calculate_solar_power=False,
            startup_voltage=self.model.traits.startup_voltage,
            reports_effective_feed_cap=self.model.traits.reports_effective_feed_cap,
        )
        return raw, derived

    def read_int(self, key: str) -> int | None:
        return self.read_int_reason(key)[0]

    def read_int_reason(self, key: str) -> tuple[int | None, str]:
        """Return an integer value or the reason it could not be read."""
        register = const.REGISTERS_BY_KEY[key].for_model(self.model)
        value, reason = self._reader.read_value(register)
        if value is None:
            return None, reason or "undecodable"
        return int(value), ""

    def read_words_reason(
        self, address: int, count: int
    ) -> tuple[list[int] | None, str]:
        return self._reader.read(address, count)

    def decode(self, words: list[int], register: models.RegisterDef) -> float | None:
        return self._reader.decode(words, register)

    def encode(self, value: int, data_type: RegisterType) -> list[int]:
        words = models.encode_register(value, data_type)
        return words if self.high_word_first_writes else list(reversed(words))

    def write(self, address: int, words: list[int]) -> tuple[bool, str, int | None]:
        """Write with FC6 or FC16; return success, reason, and exception code."""
        reason, code = "", None
        for attempt in range(BUSY_RETRIES):
            if attempt:
                time.sleep(1)
            self.writes += 1
            try:
                if len(words) == 1:
                    response = self._client.write_register(
                        address=address, value=words[0], device_id=self._slave
                    )
                else:
                    response = self._client.write_registers(
                        address=address, values=words, device_id=self._slave
                    )
            except Exception as error:  # a dropped connection rather than a refusal
                reason, code = str(error), None
                self._client.connect()
                continue
            if not response.isError():
                return True, "", None
            code = getattr(response, "exception_code", None)
            reason = (
                f"exception code {code}, {EXCEPTION_MEANINGS.get(code, '?')}"
                if code
                else str(response)
            )
            if code in PERMANENT_CODES:
                break
        return False, reason, code

    def write_value(self, key: str, value: int) -> tuple[bool, str, int | None]:
        register = const.REGISTERS_BY_KEY[key].for_model(self.model)
        address = register.write_address or register.address
        return self.write(address, self.encode(value, register.data_type))

    def write_control_word(self, value: int) -> tuple[bool, str, int | None]:
        if value & const.CONTROL_COMMAND_UNSAFE_BITS:
            raise Aborted(f"Refusing control word 0x{value:08X}: off-grid or shutdown")
        return self.write(
            const.CONTROL_COMMAND_REGISTER, self.encode(value, RegisterType.UINT32)
        )

    def heartbeat(self) -> tuple[bool, str, int | None]:
        sent_at = time.monotonic()
        result = self.write(const.HEARTBEAT_REGISTER, [const.HEARTBEAT_VALUE])
        if result[0]:
            self.last_heartbeat = sent_at
        return result

    def keep_alive(self) -> None:
        """Send a heartbeat when its interval has elapsed."""
        if not self.heartbeat_running:
            return
        age = (
            float("inf")
            if self.last_heartbeat is None
            else time.monotonic() - self.last_heartbeat
        )
        if age >= const.HEARTBEAT_INTERVAL_S:
            self.heartbeat()

    def ensure_fresh(self) -> None:
        """Send a heartbeat before a command if the last one is too old."""
        if not self.heartbeat_running:
            return
        if (
            self.last_heartbeat is None
            or time.monotonic() - self.last_heartbeat > const.HEARTBEAT_REUSE_S
        ):
            self.heartbeat()

    def wait(self, seconds: float) -> None:
        """Wait in short intervals so the heartbeat stays current."""
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            time.sleep(min(1.0, left))
            self.keep_alive()

    def elapsed(self) -> str:
        seconds = int(time.monotonic() - self.started)
        return f"{seconds // 60}:{seconds % 60:02d}"


compose_control_word = core.compose_control_word


def fmt_power(value: Any) -> str:
    return "     -" if value is None else f"{value:>+6.0f}"


def sample_line(inverter: Inverter, raw: dict, derived: dict, note: str = "") -> str:
    soc = raw.get("battery_soc")
    control = derived.get("device_modbus_control")
    return (
        f"    [{inverter.elapsed()}] battery {fmt_power(raw.get('battery_power'))} W"
        f"  grid {fmt_power(raw.get('grid_power'))} W"
        f"  solar {fmt_power(raw.get('solar_power'))} W"
        f"  inverter {fmt_power(raw.get('inverter_output_power'))} W"
        f"  soc {'-' if soc is None else f'{soc:.0f}'}%"
        f"  method {derived.get('active_control_mode', '-')}"
        f"  modbus {'-' if control is None else ('on' if control else 'off')}"
        + (f"  {note}" if note else "")
    )


def preflight(inverter: Inverter, force: bool) -> tuple[dict, dict]:
    """Check telemetry and control status before writing anything."""
    print("== Before writing anything ==")
    raw, derived = inverter.sample()
    print(sample_line(inverter, raw, derived))
    if raw.get("system_modes") is None:
        raise Aborted(
            "System Status (40530) is unreadable, so the test could not see what "
            "the inverter does. Run the register scan first."
        )
    if all(not raw.get(key) for key in ("battery_power", "grid_power", "battery_soc")):
        raise Aborted(
            "Power and SOC all read zero, the signature of Modbus TCP being off "
            "in the EcoFlow app."
        )

    busy = derived.get("device_modbus_control") or derived.get(
        "active_control_mode"
    ) not in (str(ControlMode.DEFAULT), None)
    if busy:
        message = (
            "Another controller is active. Turn off Modbus Control in Home "
            "Assistant, wait a minute, then try again."
        )
        if not force:
            raise Aborted(message)
        print(f"  Warning: {message} Continuing because --force was set.")
    else:
        print("  No other controller is active.")
    print()
    return raw, derived


def test_write_order(inverter: Inverter, report: Report) -> None:
    """Probe 32-bit word order before taking control; the probe setpoint is inactive."""
    print("== 32-bit write word order ==")
    key = "battery_power_setpoint"
    register = const.REGISTERS_BY_KEY[key].for_model(inverter.model)
    original = inverter.read_int(key)
    print(f"  {key} at {render_address(register.address)} holds {original}")

    sent = models.encode_register(PROBE_SETPOINT_W, RegisterType.INT32)
    ok, reason, _ = inverter.write(register.address, sent)
    if not ok:
        report.write_order = f"untested, the setpoint write was refused ({reason})"
        print(f"  Write refused: {reason or 'no response'}\n")
        return

    print(
        f"  Sent {PROBE_SETPOINT_W} W as {[f'0x{w:04X}' for w in sent]} "
        f"(high word first); checking readback for {WRITE_ORDER_SETTLE_S}s"
    )
    readings: list[int | None] = []
    end = time.monotonic() + WRITE_ORDER_SETTLE_S
    while time.monotonic() < end:
        readings.append(inverter.read_int(key))
        time.sleep(2)
    settled = inverter.read_int(key)
    readings.append(settled)
    print(f"  Readback: {' -> '.join(str(r) for r in dict.fromkeys(readings))}")

    if settled == PROBE_SETPOINT_W:
        report.write_order = "high word first, as the integration sends"
        inverter.high_word_first_writes = True
    elif settled == PROBE_SETPOINT_W << 16:
        report.write_order = (
            "LOW word first: the integration's writes arrive 65536x too large"
        )
        inverter.high_word_first_writes = False
    else:
        report.write_order = f"unclear, read back {settled} after writing 500"
    print(f"  Result: {report.write_order}")

    restore = original if original is not None else 0
    ok, reason, _ = inverter.write_value(key, restore)
    print(
        f"  restored {restore}"
        + ("" if ok else f" FAILED ({reason or 'no answer'})")
        + "\n"
    )


def wait_for(
    inverter: Inverter, check: Callable[[dict], bool | None], seconds: float
) -> float | None:
    """Wait for a condition to pass, returning elapsed seconds or None."""
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        _, derived = inverter.sample()
        if check(derived):
            return time.monotonic() - start
        inverter.wait(1.5)
    return None


def ask_app(question: str) -> str:
    """Record what the user sees in the EcoFlow app."""
    while True:
        try:
            answer = input(f"  {question} [y]es / [n]o / [c]an't see: ")
        except EOFError:
            return "no answer"
        answer = answer.strip().lower()
        if answer in ("y", "yes"):
            return "confirmed"
        if answer in ("n", "no"):
            return "NOT confirmed"
        if answer in ("c", "cant", "can't", "cannot"):
            return "not visible"


def test_battery_saver(
    inverter: Inverter, report: Report, saver: bool, manual: bool
) -> None:
    """Toggle the saver bit before the heartbeat, then restore its original value."""
    print("== Battery saver (control word bit 3), without the heartbeat ==")
    toggled = not saver
    word = compose_control_word(ControlMode.DEFAULT, toggled)
    ok, reason, _ = inverter.write_control_word(word)
    if not ok:
        report.manual["battery saver"] = f"control word refused ({reason})"
        print(f"  control word 0x{word:08X} refused: {reason}\n")
        return
    state = "ON" if toggled else "OFF"
    print(f"  Battery saver: {state} (control word 0x{word:08X})")
    took = wait_for(
        inverter,
        lambda d: d.get("battery_saver_mode_ena") == toggled,
        STATUS_BIT_WAIT_S,
    )
    result = ManualResult(
        f"status bit 3 followed after {took:.0f}s"
        if took is not None
        else f"status bit 3 unchanged after {STATUS_BIT_WAIT_S}s"
    )
    print(f"  {result.register}")
    if manual:
        result.app = ask_app(f"Does the app show battery saver {state}?")
    report.manual["battery saver"] = result

    inverter.write_control_word(compose_control_word(ControlMode.DEFAULT, saver))
    back = wait_for(
        inverter,
        lambda d: d.get("battery_saver_mode_ena") == saver,
        STATUS_BIT_WAIT_S,
    )
    print(
        f"  restored battery saver {'ON' if saver else 'OFF'}"
        + ("" if back is not None or took is None else ", status bit not back yet")
        + "\n"
    )


@dataclass(frozen=True)
class SettingChange:
    key: str
    app_label: str
    choose: Callable[[int, dict], tuple[int | None, str]]


def _led_value(current: int, _raw: dict) -> tuple[int | None, str]:
    return (20 if current >= 60 else 100), ""


def _reserve_value(current: int, raw: dict) -> tuple[int | None, str]:
    # Raise only when SOC leaves room; avoid zero, which the app may reject.
    soc = raw.get("battery_soc")
    if soc is not None and current + 5 <= soc - SOC_MARGIN:
        return current + 5, ""
    if current >= 10:
        return current - 5, ""
    return None, "the reserve can neither go down nor safely up"


SETTING_CHANGES: Final = (
    SettingChange("device_led_brightness", "LED brightness", _led_value),
    SettingChange("min_soc_limit", "backup reserve / minimum SOC", _reserve_value),
)

FEED_MODE_NAMES: Final = {
    mode.register_value: str(mode) for mode in models.GridFeedMode
}


def render_setting(key: str, value: int | None) -> str:
    if key == "grid_feed_mode" and value is not None:
        return f"{value} ({FEED_MODE_NAMES.get(value, '?')})"
    unit = {"device_led_brightness": " %", "min_soc_limit": " %"}.get(key, " W")
    return f"{value if value is None else int(value)}{unit}"


def test_settings(inverter: Inverter, report: Report, manual: bool) -> None:
    """Change and restore settings before the heartbeat; ask for app confirmation."""
    print("== Settings, without the heartbeat ==")
    raw, _ = inverter.sample()
    for change in SETTING_CHANGES:
        key = change.key
        register = const.REGISTERS_BY_KEY[key].for_model(inverter.model)
        is_wide = register.data_type is not RegisterType.UINT16
        current = inverter.read_int(key)
        print(f"  {change.app_label} at {render_address(register.address)}")
        if current is None:
            report.manual[key] = "unreadable, skipped"
            print("    Unreadable; skipped.")
            continue
        if is_wide and report.write_order.startswith(("unclear", "untested")):
            report.manual[key] = "skipped, the write word order is unknown"
            print("    Write order unknown; skipped.")
            continue
        target, why = change.choose(current, raw)
        if target is None:
            report.manual[key] = f"skipped, {why}"
            print(f"    Skipped: {why}.")
            continue

        inverter.pending_settings[key] = current
        ok, reason, _ = inverter.write_value(key, target)
        if not ok:
            del inverter.pending_settings[key]
            report.manual[key] = f"write refused ({reason})"
            print(f"    Write refused: {reason}")
            continue
        inverter.wait(WRITE_ORDER_SETTLE_S if is_wide else 3)
        after = inverter.read_int(key)
        result = ManualResult(
            f"reads back {render_setting(key, after)}"
            + ("" if after == target else f", not {render_setting(key, target)}")
        )
        print(
            f"    {render_setting(key, current)} -> {render_setting(key, target)}, "
            f"{result.register}"
        )
        if manual:
            result.app = ask_app(
                f"Does the app show {change.app_label} "
                f"{render_setting(key, target)}? Refresh the page first."
            )
        report.manual[key] = result

        ok, reason, _ = inverter.write_value(key, current)
        if ok:
            del inverter.pending_settings[key]
        inverter.wait(WRITE_ORDER_SETTLE_S if is_wide else 2)
        restored = inverter.read_int(key)
        print(
            f"    restored {render_setting(key, current)}"
            + (
                ""
                if ok and restored == current
                else f"; restore failed (readback: {restored})"
            )
        )
    print(
        "  These settings may not appear in the app. 'Can't see' is a valid answer.\n"
    )


def take_control(inverter: Inverter, report: Report) -> bool:
    print("== Modbus control authority ==")
    ok, reason, code = inverter.heartbeat()
    if not ok:
        unsupported = code in PERMANENT_CODES
        report.heartbeat = (
            f"refused as invalid ({reason}): commands would be stored, never acted on"
            if unsupported
            else f"failed ({reason})"
        )
        print(f"  heartbeat {render_address(const.HEARTBEAT_REGISTER)}: {reason}\n")
        return False
    inverter.heartbeat_running = True
    report.heartbeat = "accepted"
    print(f"  heartbeat {render_address(const.HEARTBEAT_REGISTER)} accepted")
    took = wait_for(
        inverter, lambda d: d.get("device_modbus_control"), STATUS_BIT_WAIT_S
    )
    report.manual_mode_bit_s = None if took is None else round(took, 1)
    report.manual_mode_bit = (
        f"set {took:.0f}s after the first heartbeat"
        if took is not None
        else f"not set within {STATUS_BIT_WAIT_S}s"
    )
    print(f"  Manual Mode Status (bit 11): {report.manual_mode_bit}\n")
    return True


def send(inverter: Inverter, test: CoreTest, power: float, saver: bool) -> str:
    """Write the setpoint, then the control word."""
    inverter.ensure_fresh()
    ok, reason, _ = inverter.write_value(
        test.setpoint_key, int(round(power)) * test.sign
    )
    if not ok:
        return f"setpoint write refused ({reason})"
    ok, reason, _ = inverter.write_control_word(
        compose_control_word(test.method, saver)
    )
    if not ok:
        return f"control word refused ({reason})"
    return ""


def return_to_default(inverter: Inverter, saver: bool) -> None:
    """Clear the setpoints and select the default method."""
    inverter.ensure_fresh()
    for key in SETPOINT_KEYS:
        if key not in inverter.refused:
            inverter.write_value(key, 0)
    inverter.write_control_word(compose_control_word(ControlMode.DEFAULT, saver))


measure = core.measure
skip_reason = core.skip_reason
judge_modes = core.judge_modes
tolerance = core.tolerance


def achievable_power(test: CoreTest, raw: dict, derived: dict) -> float | None:
    """Return the largest feed target the export cap allows, if it caps one."""
    return core.achievable_power(test, {**raw, **derived})


def observe(
    inverter: Inverter, test: CoreTest, signed_target: float, reserve: float
) -> tuple[
    float | None,
    list[float],
    models.ControlStatus | None,
    list[tuple[float, str]],
    list[dict[str, Any]],
]:
    """Sample until active for three polls.

    Return the time that took, the readings, the last status, the methods reported
    and the trace the report keeps.
    """
    start = time.monotonic()
    measured: list[float] = []
    modes: list[tuple[float, str]] = []
    samples: list[dict[str, Any]] = []
    streak, state = 0, None
    inverter.wait(SAMPLE_GAP_S)
    while time.monotonic() - start < CONTROL_SETTLE_S:
        raw, derived = inverter.sample()
        elapsed = time.monotonic() - start
        value, _ = measure(test, raw)
        soc = raw.get("battery_soc")
        state = models.deviation_state(
            signed_target=signed_target,
            measured=value,
            soc=None if soc is None else float(soc),
            min_soc=reserve,
            battery=None
            if (battery := raw.get("battery_power")) is None
            else float(battery),
        )
        modes.append((elapsed, str(derived.get("active_control_mode"))))
        samples.append(core.trace_sample(elapsed, {**raw, **derived}))
        if value is not None:
            measured.append(value)
        print(sample_line(inverter, raw, derived, str(state)))
        streak = streak + 1 if state is models.ControlStatus.ACTIVE else 0
        if streak >= const.CONTROL_STATUS_DAMPING_POLLS:
            return time.monotonic() - start, measured, state, modes, samples
        inverter.wait(SAMPLE_GAP_S)
    return None, measured, state, modes, samples


def test_control(
    inverter: Inverter, report: Report, test: CoreTest, power: float, saver: bool
) -> None:
    print(f"== {test.name}: method {test.method}, {test.setpoint_key} ==")
    raw, derived = inverter.sample()
    plan, reason = core.plan_test(test, {**raw, **derived}, power)
    if plan is None:
        report.controls.append(
            core.FeatureResult.for_test(test, Verdict.SKIPPED, reason)
        )
        print(f"  Skipped: {reason}.\n")
        return
    signed_target = plan.signed_target(test)
    print(
        f"  Target: {test.measure_key} {signed_target:+.0f} W "
        f"(before: {fmt_power(plan.baseline).strip()} W)"
    )

    if failure := send(inverter, test, plan.power, saver):
        report.controls.append(
            core.FeatureResult.for_test(test, Verdict.WRITE_REFUSED, failure)
        )
        print(f"  Write refused: {failure}\n")
        return_to_default(inverter, saver)
        return

    reserve = float(raw.get("min_soc_limit") or 0)
    took, measured, state, modes, samples = observe(
        inverter, test, signed_target, reserve
    )
    verdict, detail, achieved = core.judge(test, plan, took, state, measured)
    method_ok, mode_report = judge_modes(str(test.method), modes)
    result = core.FeatureResult(
        test.name,
        str(test.method),
        test.setpoint_key,
        test.measure_key,
        verdict,
        detail,
        target_w=signed_target,
        baseline_w=plan.baseline,
        achieved_w=None if achieved is None else round(achieved, 1),
        settle_s=None if took is None else round(took, 1),
        method_reported=method_ok,
        status_report=mode_report,
        samples=samples,
    )
    print(
        f"  Result: {result.verdict.label}"
        + (f" after {took:.0f}s" if result.settle_s is not None else "")
        + (f", {result.detail}" if result.detail else "")
    )
    print(f"  Status: {result.status_report}")

    return_to_default(inverter, saver)
    inverter.wait(RETURN_SETTLE_S)
    _, derived = inverter.sample()
    back = str(derived.get("active_control_mode"))
    if back != str(ControlMode.DEFAULT):
        result.status_report += (
            f"; still reports {back} {RETURN_SETTLE_S}s after default"
        )
    print(f"  Default method restored; status: {back}\n")
    report.controls.append(result)


def watch_grid(
    inverter: Inverter,
    check: Callable[[float], bool],
    seconds: float,
    since: float | None = None,
) -> tuple[float | None, float | None, dict]:
    """Wait for three passing grid samples; return elapsed time and the last reading."""
    start = since if since is not None else time.monotonic()
    # Skip the first gap to avoid judging a pre-command reading.
    inverter.wait(SAMPLE_GAP_S)
    streak, grid, raw = 0, None, {}
    while time.monotonic() - start < seconds:
        raw, derived = inverter.sample()
        grid = raw.get("grid_power")
        passed = grid is not None and check(float(grid))
        print(sample_line(inverter, raw, derived, "ok" if passed else ""))
        streak = streak + 1 if passed else 0
        if streak >= const.CONTROL_STATUS_DAMPING_POLLS:
            return time.monotonic() - start, grid, raw
        inverter.wait(SAMPLE_GAP_S)
    return None, grid, raw


def write_setting_like_integration(inverter: Inverter, key: str, value: int) -> str:
    """Write a setting and accept either the echoed words or decoded readback."""
    register = const.REGISTERS_BY_KEY[key].for_model(inverter.model)
    words = inverter.encode(value, register.data_type)
    ok, reason, _ = inverter.write(register.write_address or register.address, words)
    if not ok:
        return f"{key} write refused ({reason})"
    readback, reason = inverter.read_words_reason(register.address, register.size)
    if readback is None:
        return f"{key} read-back right after the write failed ({reason})"
    decoded = inverter.decode(readback, register)
    if readback != words and (decoded is None or int(decoded) != value):
        return f"{key} read back {decoded} right after writing {value}"
    return ""


def check_settled(inverter: Inverter, key: str, value: int, written: float) -> str:
    """Check the value after the inverter republishes 32-bit words in read order."""
    inverter.wait(WRITE_ORDER_SETTLE_S - (time.monotonic() - written))
    after, reason = inverter.read_int_reason(key)
    if after == value:
        return ""
    return f"{key} reads {after if after is not None else reason} later, not {value}"


def test_grid_feed(
    inverter: Inverter, report: Report, power: float, saver: bool
) -> None:
    """Stop an export, then restore its cap and mode in the integration's order."""
    print("== Grid feed switch: stop and restore the export ==")
    export = next(test for test in CORE_TESTS if test.name == "grid feed")
    raw, derived = inverter.sample()
    mode = derived.get("grid_feed_mode")
    raw_mode = raw.get("grid_feed_mode")
    cap = raw.get(const.FEED_IN_POWER_MAX_SETTING_KEY)

    def skip(reason: str) -> None:
        report.grid_feed = f"skipped, {reason}"
        print(f"  Skipped: {reason}.\n")

    if mode is None or raw_mode is None or cap is None:
        return skip("the feed-in mode or cap is unreadable")
    if not mode.switchable:
        return skip(f"the feed-in mode is {mode}, which the switch leaves alone")
    if mode is models.GridFeedMode.LIMITED and cap < MIN_ACHIEVABLE_W:
        return skip(f"the export is already capped at {cap:.0f} W")
    if report.write_order.startswith(("unclear", "untested")):
        return skip("the write word order is unknown")
    if reason := skip_reason(export, raw):
        return skip(reason)
    limit = achievable_power(export, raw, derived)
    if limit is not None:
        power = min(power, limit)
    original_mode, original_cap = int(raw_mode), int(cap)
    print(f"  Feed-in mode: {mode}; cap: {original_cap} W")

    print(f"  Set export target to {power:.0f} W")
    if failure := send(inverter, export, power, saver):
        return_to_default(inverter, saver)
        return skip(failure)
    took, grid, _ = watch_grid(
        inverter,
        lambda grid: abs(grid + power) <= tolerance(power),
        CONTROL_SETTLE_S,
    )
    if took is None:
        return_to_default(inverter, saver)
        return skip(
            f"no export to stop, the grid stayed at {fmt_power(grid).strip()} W"
        )

    print("  Stopping export (limited mode, 0 W cap)")
    inverter.ensure_fresh()
    limited = models.GridFeedMode.LIMITED.register_value
    inverter.pending_settings["grid_feed_mode"] = original_mode
    inverter.pending_settings[const.FEED_IN_POWER_MAX_SETTING_KEY] = original_cap
    switched_off = time.monotonic()
    problems = [
        problem
        for problem in (
            write_setting_like_integration(inverter, "grid_feed_mode", limited),
            write_setting_like_integration(
                inverter, const.FEED_IN_POWER_MAX_SETTING_KEY, 0
            ),
        )
        if problem
    ]
    stopped, grid_off, raw_off = watch_grid(
        inverter,
        lambda grid: grid >= -EXPORT_STOPPED_W,
        GRID_FEED_SETTLE_S,
        since=switched_off,
    )
    effective = raw_off.get(const.FEED_IN_POWER_MAX_EFFECTIVE_KEY)
    # By now the 32-bit cap has been republished in read order.
    problems += filter(
        None,
        (
            check_settled(inverter, "grid_feed_mode", limited, switched_off),
            check_settled(
                inverter, const.FEED_IN_POWER_MAX_SETTING_KEY, 0, switched_off
            ),
        ),
    )

    print("  Restoring export (cap, then mode)")
    inverter.ensure_fresh()
    switched_on = time.monotonic()
    restores = (
        (const.FEED_IN_POWER_MAX_SETTING_KEY, original_cap),
        ("grid_feed_mode", original_mode),
    )
    for key, value in restores:
        if problem := write_setting_like_integration(inverter, key, value):
            problems.append(problem)
    resumed, grid_on, _ = watch_grid(
        inverter,
        lambda grid: abs(grid + power) <= tolerance(power),
        GRID_FEED_SETTLE_S,
        since=switched_on,
    )
    for key, value in restores:
        if problem := check_settled(inverter, key, value, switched_on):
            problems.append(problem)
        else:
            del inverter.pending_settings[key]
    return_to_default(inverter, saver)

    parts = [
        f"stopped, the grid at {fmt_power(grid_off).strip()} W after {stopped:.0f}s"
        if stopped is not None
        else f"NOT stopped, the grid still at {fmt_power(grid_off).strip()} W after "
        f"{GRID_FEED_SETTLE_S}s",
        f"resumed after {resumed:.0f}s"
        if resumed is not None
        else f"NOT resumed, the grid at {fmt_power(grid_on).strip()} W after "
        f"{GRID_FEED_SETTLE_S}s",
    ]
    # Only where 40609 is known to carry the cap; elsewhere it reads 0 regardless.
    if effective is not None and inverter.model.traits.reports_effective_feed_cap:
        parts.append(f"effective cap (40609) read {effective:.0f} W while off")
    parts += problems
    report.grid_feed = "; ".join(parts)
    print(f"  Result: {report.grid_feed}\n")


def read_shadow(inverter: Inverter) -> str:
    """Read the unmapped word beside the reserve, if the model answers for it."""
    words, _ = inverter.read_words_reason(RESERVE_SHADOW_ADDRESS, 1)
    if words is None:
        return ""
    return f"{render_address(RESERVE_SHADOW_ADDRESS)}={words[0]}"


def reserve_target(case: ReserveCase, raw: dict) -> tuple[int | None, str]:
    """Return the reserve the case sets, or why it cannot run now."""
    soc = raw.get("battery_soc")
    battery = raw.get("battery_power")
    if soc is None or battery is None:
        return None, "the SOC or battery power is unreadable"
    if case.effect is ReserveEffect.FLOOR:
        if float(battery) > -RESERVE_FLOOR_MIN_DISCHARGE_W:
            return None, (
                f"the battery is not discharging (at {battery:+.0f} W), so there is "
                "nothing for the floor to stop; run it when the battery covers the "
                "house"
            )
        # At the SOC, not above it, so the floor is tested without asking for a
        # charge as well.
        return int(soc), ""
    target = int(soc) + RESERVE_RAISE_PCT
    if target > models.BATTERY_FULL_SOC - SOC_MARGIN:
        return None, f"battery at {soc:.0f}%, no room to raise the reserve above it"
    return target, ""


def reserve_acted(effect: ReserveEffect, baseline: dict, raw: dict) -> bool:
    """Whether the sample shows the inverter doing what the reserve asks."""
    battery, grid = raw.get("battery_power"), raw.get("grid_power")
    if battery is None or grid is None:
        return False
    base_battery = float(baseline.get("battery_power") or 0)
    if effect is ReserveEffect.FLOOR:
        return float(battery) >= -const.GUARD_POWER_DEADBAND_W
    base_grid = float(baseline.get("grid_power") or 0)
    return (
        float(battery) - base_battery >= RESERVE_CHARGE_W
        and float(grid) - base_grid >= RESERVE_CHARGE_W
    )


def watch_reserve(
    inverter: Inverter,
    effect: ReserveEffect,
    baseline: dict,
    seconds: float,
    *,
    expect: bool,
) -> tuple[float | None, dict]:
    """Wait until the effect shows (or, with expect=False, has ended).

    Returns the seconds it took, or None, and the last sample.
    """
    start = time.monotonic()
    streak, raw = 0, {}
    inverter.wait(SAMPLE_GAP_S)
    while time.monotonic() - start < seconds:
        raw, derived = inverter.sample()
        acted = reserve_acted(effect, baseline, raw)
        reserve = raw.get("min_soc_limit")
        note = "  ".join(
            filter(
                None,
                (
                    f"reserve {render_setting('min_soc_limit', reserve)}"
                    if reserve is not None
                    else "",
                    read_shadow(inverter),
                    (
                        "charging from grid"
                        if effect is ReserveEffect.CHARGE
                        else "discharge stopped"
                    )
                    if acted
                    else "",
                ),
            )
        )
        print(sample_line(inverter, raw, derived, note))
        streak = streak + 1 if acted == expect else 0
        if streak >= const.CONTROL_STATUS_DAMPING_POLLS:
            return time.monotonic() - start, raw
        inverter.wait(SAMPLE_GAP_S)
    return None, raw


def enter_case(inverter: Inverter, case: ReserveCase, saver: bool) -> str:
    """Put the inverter in the case's control state; return a failure reason."""
    if not case.session:
        return ""
    inverter.ensure_fresh()
    if case.hold:
        ok, reason, _ = inverter.write_value(
            "battery_power_setpoint", int(const.HOLD_SETPOINT_W)
        )
        if not ok:
            return f"hold setpoint refused ({reason})"
    ok, reason, _ = inverter.write_control_word(
        compose_control_word(case.method, saver)
    )
    return "" if ok else f"control word refused ({reason})"


@contextmanager
def heartbeat_in_background(inverter: Inverter) -> Iterator[None]:
    """Keep the heartbeat going while the main thread waits on the user.

    Only safe while the main thread does no Modbus traffic of its own, which holds
    for a blocking input().
    """
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(1.0):
            inverter.keep_alive()

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def ask_app_change(inverter: Inverter, instruction: str) -> bool:
    """Ask the user to make a change in the app; False if they skip it."""
    print(f"  >>> {instruction}")
    try:
        with heartbeat_in_background(inverter):
            answer = input("      Press Enter once the app shows it, or s to skip: ")
    except EOFError:
        return False
    return answer.strip().lower() not in ("s", "skip")


def set_reserve(
    inverter: Inverter, case: ReserveCase, value: int, *, restoring: bool
) -> str:
    """Set the reserve the way the case does; return why it failed, if it did."""
    if case.via_app:
        verb = "back to" if restoring else "to"
        if not ask_app_change(
            inverter, f"In the EcoFlow app, set the backup reserve {verb} {value}%."
        ):
            return "skipped in the app"
        inverter.pending_app_reserve = None if restoring else value
        return ""
    ok, reason, _ = inverter.write_value("min_soc_limit", value)
    if not ok:
        return f"write refused ({reason})"
    if restoring:
        inverter.pending_settings.pop("min_soc_limit", None)
    return ""


def probe_reserve_case(
    inverter: Inverter,
    report: Report,
    case: ReserveCase,
    original: int,
    saver: bool,
    manual: bool,
) -> None:
    key = "min_soc_limit"
    print(f"== Backup reserve: {case.name} ==")
    if case.via_app and not manual:
        report.reserve[case.name] = "skipped, needs the app (no --skip-manual)"
        print("  Skipped: needs a change in the app.\n")
        return
    if failure := enter_case(inverter, case, saver):
        report.reserve[case.name] = failure
        print(f"  {failure}\n")
        return
    if case.session:
        inverter.wait(RETURN_SETTLE_S)
    baseline, derived = inverter.sample()
    print(sample_line(inverter, baseline, derived, "before"))
    target, why = reserve_target(case, baseline)
    if target is None:
        report.reserve[case.name] = f"skipped, {why}"
        print(f"  Skipped: {why}.\n")
        return

    # Any cleanup puts a written reserve back; one set in the app is left to the user.
    if not case.via_app:
        inverter.pending_settings[key] = original
    if failure := set_reserve(inverter, case, target, restoring=False):
        if not case.via_app:
            inverter.pending_settings.pop(key, None)
        report.reserve[case.name] = failure
        print(f"  {failure}\n")
        return
    if case.resend_control_word:
        inverter.ensure_fresh()
        inverter.write_control_word(compose_control_word(case.method, saver))
    readback = inverter.read_int(key)
    goal = (
        "grid charging"
        if case.effect is ReserveEffect.CHARGE
        else "the discharge to stop"
    )
    print(
        f"  Reserve {original}% -> {target}%, register reads {readback}%; "
        f"watching {RESERVE_SETTLE_S}s for {goal}"
    )
    took, last = watch_reserve(
        inverter, case.effect, baseline, RESERVE_SETTLE_S, expect=True
    )
    power = (
        f"battery {fmt_power(last.get('battery_power')).strip()} W, "
        f"grid {fmt_power(last.get('grid_power')).strip()} W"
    )
    acted = (
        "CHARGED from the grid"
        if case.effect is ReserveEffect.CHARGE
        else ("STOPPED discharging")
    )
    verdict = (
        f"{acted} after {took:.0f}s, {power}"
        if took is not None
        else f"NOT acted on within {RESERVE_SETTLE_S}s, {power}"
    )
    if readback != target:
        verdict += f"; register read {readback}%, not {target}%"
    report.reserve[case.name] = verdict
    print(f"  Result: {verdict}")

    if failure := set_reserve(inverter, case, original, restoring=True):
        print(f"  Could not put the reserve back: {failure}")
    if case.session:
        return_to_default(inverter, saver)
    after = inverter.read_int(key)
    print(f"  Reserve back to {original}%, register reads {after}%")
    if took is not None:
        ended, _ = watch_reserve(
            inverter, case.effect, baseline, RESERVE_RELEASE_S, expect=False
        )
        print(
            f"  Effect ended after {ended:.0f}s"
            if ended is not None
            else f"  Effect still going {RESERVE_RELEASE_S}s later"
        )
    print()


def probe_reserve_persistence(
    inverter: Inverter, report: Report, original: int, saver: bool
) -> None:
    """Write the reserve in a session and see what the register holds after it ends."""
    key = "min_soc_limit"
    label = "register value after the session ends"
    print(f"== Backup reserve: {label} ==")
    if not inverter.heartbeat_running:
        report.reserve[label] = "skipped, no session"
        print("  Skipped: no session.\n")
        return
    probe = original + 1 if original < 99 else original - 1
    inverter.ensure_fresh()
    inverter.pending_settings[key] = original
    ok, reason, _ = inverter.write_value(key, probe)
    if not ok:
        del inverter.pending_settings[key]
        report.reserve[label] = f"write refused ({reason})"
        print(f"  Write refused: {reason}\n")
        return
    print(f"  Wrote {probe}% in the session; ending it (no more heartbeats)")
    return_to_default(inverter, saver)
    inverter.heartbeat_running = False
    took = wait_for(
        inverter, lambda d: d.get("device_modbus_control") is False, HANDBACK_WAIT_S
    )
    after = inverter.read_int(key)
    if after == probe:
        held = f"still holds our {probe}%"
    elif after == original:
        held = f"reverted to the app's {original}%"
    else:
        held = f"reads {after}%"
    ended = (
        f"bit 11 cleared after {took:.0f}s"
        if took is not None
        else f"bit 11 still set after {HANDBACK_WAIT_S}s"
    )
    report.reserve[label] = f"{held} ({ended})"
    report.handback = ended
    print(f"  Result: {report.reserve[label]}")
    ok, _, _ = inverter.write_value(key, original)
    if ok:
        del inverter.pending_settings[key]
    print(f"  Reserve put back to {original}%\n")


def run_reserve_probe(
    inverter: Inverter, report: Report, saver: bool, manual: bool
) -> None:
    """Set the reserve under each control state, by the app and by the register."""
    original = inverter.read_int("min_soc_limit")
    if original is None:
        raise Aborted("The backup reserve (40536) is unreadable.")
    print(
        f"Backup reserve reads {original}%. Charge cases raise it "
        f"{RESERVE_RAISE_PCT}% above the SOC and watch for grid charging; floor "
        "cases set it to the SOC while discharging and watch the discharge stop."
    )
    if manual:
        print(
            "The app cases ask you to change the reserve in the EcoFlow app, as "
            "the reference."
        )
    print()

    session_ok: bool | None = None
    for case in RESERVE_CASES:
        if case.session and session_ok is None:
            session_ok = take_control(inverter, report)
        if case.session and not session_ok:
            report.reserve[case.name] = "skipped, the heartbeat was refused"
            continue
        probe_reserve_case(inverter, report, case, original, saver, manual)
    probe_reserve_persistence(inverter, report, original, saver)


def hand_back(inverter: Inverter, report: Report, saver: bool, wait: bool) -> None:
    print("== Handing back to the EcoFlow app ==")
    return_to_default(inverter, saver)
    inverter.heartbeat_running = False
    print("  Default method selected; setpoints cleared; heartbeat stopped.")
    if not wait:
        report.handback = "not observed (--no-handback-wait)"
        print()
        return
    took = wait_for(
        inverter, lambda d: d.get("device_modbus_control") is False, HANDBACK_WAIT_S
    )
    since = time.monotonic() - (inverter.last_heartbeat or time.monotonic())
    report.handback_s = round(since, 1) if took is not None else None
    report.handback = (
        f"bit 11 cleared {since:.0f}s after the last heartbeat"
        if took is not None
        else f"bit 11 still set {since:.0f}s after the last heartbeat"
    )
    print(f"  Result: {report.handback}\n")


def restore_originals(inverter: Inverter, originals: dict[str, int | None]) -> None:
    """Restore original setpoints and any setting left changed by a check."""
    for key, value in originals.items():
        if value is not None and key not in inverter.refused:
            inverter.write_value(key, value)
    # Newest first: the grid feed cap goes back before its mode, the order the
    # integration's grid feed switch restores them in.
    for key, value in reversed(list(inverter.pending_settings.items())):
        if inverter.write_value(key, value)[0]:
            del inverter.pending_settings[key]
            print(f"  {key} put back to {render_setting(key, value)}")


def print_summary(inverter: Inverter, report: Report) -> None:
    rows: list[tuple[str, str]] = [
        ("model", inverter.model.traits.display_name),
        ("32-bit write word order", report.write_order),
    ]
    rows += [(label, str(result)) for label, result in report.manual.items()]
    rows += [
        ("heartbeat", report.heartbeat),
        ("manual mode status bit", report.manual_mode_bit),
    ]
    for result in report.controls:
        line = result.verdict.label
        if result.settle_s is not None:
            line += f" in {result.settle_s:.0f}s"
        if result.detail:
            line += f" ({result.detail})"
        if result.status_report:
            line += f"; status {result.status_report}"
        rows.append((result.name, line))
    rows += [(f"reserve: {case}", result) for case, result in report.reserve.items()]
    if report.grid_feed != "not tested":
        rows.append(("grid feed switch", report.grid_feed))
    rows.append(("hand back", report.handback))

    print("== Summary ==")
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"  {label:<{width}}  {value}")
    print()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("host", help="the inverter's IP address")
    parser.add_argument("--port", type=int, default=const.DEFAULT_PORT)
    parser.add_argument("--slave", type=int, default=const.DEFAULT_SLAVE)
    parser.add_argument(
        "--model",
        choices=[model.value for model in models.InverterModel],
        help="model to test as. Default: whatever the device reports. Required "
        "when the device is not recognised.",
    )
    parser.add_argument(
        "--power",
        type=int,
        default=DEFAULT_TEST_POWER_W,
        metavar="WATTS",
        help=f"power to test each control method at. Default "
        f"{DEFAULT_TEST_POWER_W}, between {MIN_TEST_POWER_W} and {MAX_TEST_POWER_W}.",
    )
    parser.add_argument(
        "--reserve-probe",
        action="store_true",
        help="instead of the control methods, test whether writing the backup "
        "reserve (40536) does what setting it in the app does: charge from the "
        "grid when above the SOC, stop discharging when at it. --skip-manual "
        "leaves out the app reference cases.",
    )
    parser.add_argument(
        "--yes", action="store_true", help="start without asking for confirmation"
    )
    parser.add_argument(
        "--skip-manual",
        action="store_true",
        help="skip battery saver and the settings, which need checking in the app",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="start even though the inverter already reports Modbus control",
    )
    parser.add_argument(
        "--no-handback-wait",
        action="store_true",
        help="skip watching the inverter return to the app at the end (saves 80s)",
    )
    parser.add_argument(
        "--show-serial", action="store_true", help="the serial is masked by default"
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        help="also write the report as JSON, the form the run_control_test action "
        "saves, to attach to an issue or compare with scripts/compare_reports.py",
    )
    arguments = parser.parse_args()
    if not MIN_TEST_POWER_W <= arguments.power <= MAX_TEST_POWER_W:
        parser.error(
            f"--power must be between {MIN_TEST_POWER_W} and {MAX_TEST_POWER_W}"
        )
    return arguments


def build_report(
    arguments: argparse.Namespace,
    version: str,
    identity: dict,
    model: models.InverterModel | None,
    conditions: dict,
    report: Report,
    outcome: str,
    abort_reason: str | None,
) -> core.ControlTestReport:
    """Return the run in the form shared with the integration's action."""
    now = datetime.now(timezone.utc).astimezone().isoformat()
    return core.ControlTestReport(
        source="script",
        created_at=now,
        finished_at=now,
        integration_version=version,
        model=str(model) if model else None,
        firmware_version=identity.get("firmware_version"),
        protocol_version=identity.get("protocol_version"),
        parameters={
            "power_w": arguments.power,
            "settle_s": CONTROL_SETTLE_S,
            "reserve_probe": arguments.reserve_probe,
        },
        conditions=conditions,
        heartbeat=report.heartbeat,
        manual_mode_bit=report.manual_mode_bit,
        manual_mode_bit_s=report.manual_mode_bit_s,
        features=report.controls,
        handback=report.handback,
        handback_s=report.handback_s,
        extra={
            "write_order": report.write_order,
            "battery_saver_and_settings": {
                key: str(value) for key, value in report.manual.items()
            },
            "grid_feed_switch": report.grid_feed,
            "reserve_probe": report.reserve,
        },
        outcome=outcome,
        abort_reason=abort_reason,
    )


def write_json(path: str, report: core.ControlTestReport) -> None:
    Path(path).write_text(
        json.dumps(report.to_dict(), indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(f"Report written to {path}.")


def confirm(arguments: argparse.Namespace, manual: bool) -> bool:
    if arguments.reserve_probe:
        minutes = len(RESERVE_CASES) * (RESERVE_SETTLE_S + RESERVE_RELEASE_S) // 60 + 3
        print(f"This probe writes to the inverter. It takes up to {minutes} minutes:")
        print("  - write a battery setpoint to check the 32-bit word order,")
        print(
            f"  - set the backup reserve {RESERVE_RAISE_PCT}% above the SOC and "
            "watch for grid charging, and to the SOC while discharging and watch "
            "the discharge stop,"
        )
        print(
            "    by writing 40536 under each control state, and by asking you to "
            "set it in the app as the reference; it goes back after each,"
        )
        print("  - check whether a value written in a session survives its end,")
        print("  - return control to the EcoFlow app.")
        print(
            "The battery may charge briefly from the grid. Run it when the battery "
            "is covering the house, so the floor cases can run too."
        )
        print("Turn off Modbus Control in Home Assistant first. Ctrl+C stops the probe")
        print("and restores the reserve.")
        if arguments.yes:
            return True
        try:
            return input("Type yes to start: ").strip().lower() == "yes"
        except EOFError:
            return False
    print("This test writes to the inverter. It takes about 5 to 10 minutes:")
    print("  - write a battery setpoint to check the 32-bit word order,")
    if manual:
        print("  - change battery saver, LED brightness and backup reserve; ask you to")
        print("    check each in the app; then restore them,")
    print(
        f"  - test the battery, system and inverter control methods at "
        f"{arguments.power} W in both directions (up to {CONTROL_SETTLE_S}s each),"
    )
    print("  - stop and restore grid export using the integration's switch sequence,")
    print("  - return control to the EcoFlow app.")
    print("Turn off Modbus Control in Home Assistant first. Ctrl+C stops the test")
    print("and restores the inverter.")
    if arguments.yes:
        return True
    try:
        return input("Type yes to start: ").strip().lower() == "yes"
    except EOFError:
        return False


def main() -> int:
    arguments = parse_arguments()
    manual = not arguments.skip_manual and sys.stdin.isatty()
    version = json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]
    print("EcoFlow PowerOcean control test")
    print(
        f"Version {version}; {arguments.host}:{arguments.port}; "
        f"unit {arguments.slave}\n"
    )
    if not confirm(arguments, manual):
        print("Nothing was written.")
        return 1
    print()

    client = ModbusTcpClient(arguments.host, port=arguments.port, timeout=10)
    if not client.connect():
        print(f"Could not connect to {arguments.host}:{arguments.port}.")
        print("Check the IP address and enable Modbus TCP in the EcoFlow app.")
        return 1

    reader = RegisterReader(client, arguments.slave)
    report = Report()
    identity: dict = {}
    model: models.InverterModel | None = None
    conditions: dict = {}
    abort_reason: str | None = None
    inverter: Inverter | None = None
    saver = False
    originals: dict[str, int | None] = {}
    # Set from the first write until everything is put back, so an interruption
    # anywhere in between still restores the inverter.
    needs_cleanup = False
    interrupted = False
    try:
        detected = report_device(reader, arguments.show_serial, identity)
        if arguments.model:
            model = models.InverterModel(arguments.model)
        elif detected is not None:
            model = detected
        else:
            raise Aborted(
                "The model is not recognised. Writing to an unknown map is unsafe; "
                "pass --model to say which one to test as."
            )
        print(f"Testing as the {model.traits.display_name}.\n")
        reader.high_word_first = model.traits.high_word_first
        inverter = Inverter(client, reader, arguments.slave, model)

        raw, derived = preflight(inverter, arguments.force)
        conditions = core.conditions_of({**raw, **derived})
        saver = bool(derived.get("battery_saver_mode_ena"))
        originals = {key: inverter.read_int(key) for key in SETPOINT_KEYS}

        needs_cleanup = True
        test_write_order(inverter, report)
        if arguments.reserve_probe:
            report.manual["battery saver and settings"] = "skipped (--reserve-probe)"
            run_reserve_probe(inverter, report, saver, manual)
            if inverter.heartbeat_running:
                hand_back(inverter, report, saver, not arguments.no_handback_wait)
        else:
            if manual:
                print("Have the EcoFlow app open on this inverter for the next part.\n")
                test_battery_saver(inverter, report, saver, manual)
                test_settings(inverter, report, manual)
            else:
                reason = (
                    "skipped (--skip-manual)"
                    if arguments.skip_manual
                    else "skipped, no terminal to answer in"
                )
                report.manual["battery saver and settings"] = reason
            if take_control(inverter, report):
                for test in CORE_TESTS:
                    test_control(inverter, report, test, arguments.power, saver)
                test_grid_feed(inverter, report, arguments.power, saver)
                hand_back(inverter, report, saver, not arguments.no_handback_wait)
        restore_originals(inverter, originals)
        needs_cleanup = False
    except KeyboardInterrupt:
        interrupted = True
        abort_reason = "interrupted"
        print("\n\nInterrupted.")
    except Aborted as reason:
        abort_reason = str(reason)
        print(f"Stopped: {reason}\n")
    finally:
        if inverter is not None and needs_cleanup:
            print("Restoring the inverter...")
            try:
                return_to_default(inverter, saver)
                # The grid feed settings are only acted on under control, so they
                # go back before the heartbeat stops.
                restore_originals(inverter, originals)
                inverter.heartbeat_running = False
                print(
                    "  Default method selected and setpoints restored. The app takes "
                    f"control within {const.HEARTBEAT_WINDOW_S}s."
                )
                for key, value in inverter.pending_settings.items():
                    print(
                        f"  Could not restore {key} to {render_setting(key, value)}; "
                        "set it in the app."
                    )
                if inverter.pending_app_reserve is not None:
                    print(
                        "  The backup reserve was raised in the app to "
                        f"{inverter.pending_app_reserve}%; set it back there."
                    )
                print()
            except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001
                print(
                    f"  Restore failed ({error!r}). The inverter returns to the app "
                    f"without a heartbeat within "
                    f"{const.HEARTBEAT_WINDOW_S}s.\n"
                )
        if inverter is not None:
            print_summary(inverter, report)
            print(f"{reader.reads} reads; {inverter.writes} writes.")
        client.close()
        if arguments.json:
            write_json(
                arguments.json,
                build_report(
                    arguments,
                    version,
                    identity,
                    model,
                    conditions,
                    report,
                    "done" if abort_reason is None else "aborted",
                    abort_reason,
                ),
            )

    if interrupted:
        print("The test was interrupted, so the summary is incomplete.")
    print("Attach this report to the GitHub issue.")
    return 0 if inverter is not None and not interrupted else 1


if __name__ == "__main__":
    sys.exit(main())
