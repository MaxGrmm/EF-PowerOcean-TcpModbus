"""The control test, run from Home Assistant when the user asks for it.

It does what scripts/control_feature_scan.py does for the control methods: takes
control with the heartbeat, commands each method in each direction, watches what
the inverter does, and hands control back to the EcoFlow app. The decisions and the
report come from control_test_core, so a run here and a run of the script can be
compared. The checks that need someone watching the EcoFlow app, and the ones that
change device settings, stay in the script and show as not tested.

Nothing happens unless the run_control_test action is called with the
confirmation set. With Modbus Control on, the test takes over from the control manager,
which refuses every command while it runs and sends the selected mode again after.
It refuses to start only while another controller holds the inverter.

The readings come from the coordinator's own polls, so the test adds writes but no
reads of its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components import persistent_notification
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry
from homeassistant.loader import async_get_integration
from homeassistant.util import dt

from .const import (
    CONTROL_COMMAND_REGISTER,
    CONTROL_COMMAND_UNSAFE_BITS,
    CONTROL_STATUS_DAMPING_POLLS,
    DOMAIN,
    EVENT_CONTROL_TEST_FINISHED,
    EVENT_CONTROL_TEST_STARTED,
)
from .control_test_core import (
    CONTROL_SETTLE_S,
    CORE_TESTS,
    DEFAULT_TEST_POWER_W,
    HANDBACK_WAIT_S,
    REQUIRED_KEYS,
    RETURN_SETTLE_S,
    SETPOINT_KEYS,
    STATUS_BIT_WAIT_S,
    ControlTestReport,
    CoreTest,
    FeatureResult,
    Verdict,
    compose_control_word,
    conditions_of,
    judge,
    judge_modes,
    measure,
    plan_test,
    trace_sample,
)
from .heartbeat import Heartbeat
from .modbus import ModbusRejected
from .models import (
    ControlMode,
    ControlStatus,
    GridMode,
    RegisterType,
    deviation_state,
    encode_register,
)

if TYPE_CHECKING:
    from .coordinator import EcoflowCoordinator

_LOGGER = logging.getLogger(__name__)

REPORT_DIRECTORY: Final = DOMAIN
WRITE_ATTEMPTS: Final = 3
WRITE_RETRY_S: Final = 1.0

# Parts of the script that need the app open or change settings.
SCRIPT_ONLY: Final = {
    "write_order": "not tested here; the integration writes high word first",
    "battery_saver_and_settings": "not tested here; needs the EcoFlow app open",
    "grid_feed_switch": "not tested here; it changes the export settings",
    "reserve_probe": "not tested here; run the script with --reserve-probe",
}


class ControlTestState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    ABORTED = "aborted"
    FAILED = "failed"


@dataclass(frozen=True)
class Timing:
    """How long each wait lasts. Tests shorten them; the defaults match the script."""

    settle_s: float = CONTROL_SETTLE_S
    return_s: float = RETURN_SETTLE_S
    status_bit_s: float = STATUS_BIT_WAIT_S
    handback_s: float = HANDBACK_WAIT_S
    # Longest wait for one poll before the connection counts as lost. None for a
    # few poll intervals.
    frame_timeout_s: float | None = None


class AbortTest(Exception):
    """Stop the run, hand control back, and report why."""


class ControlTest:
    """Runs the control test for one inverter, one run at a time."""

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        *,
        timing: Timing | None = None,
        heartbeat_factory: Callable[[], Heartbeat] | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._timing = timing or Timing()
        self._heartbeat_factory = heartbeat_factory or (
            lambda: Heartbeat(
                coordinator.modbus_client,
                scan_interval_s=coordinator.scan_interval,
                # As the control manager's: a background task, so a run does not
                # hold up Home Assistant's startup or shutdown.
                start_task=coordinator.hass.async_create_background_task,
            )
        )
        self._task: asyncio.Task[dict[str, Any]] | None = None
        self._state = ControlTestState.IDLE
        self._step = ""
        self._progress = (0, 0)
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None
        self._report: ControlTestReport | None = None
        self._report_path: str | None = None
        self._heartbeat: Heartbeat | None = None
        self._wrote = False
        self._loop_start = 0.0

    # ── State shown by the sensor and the diagnostics ─────────────────────────

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def state(self) -> ControlTestState:
        return self._state

    @property
    def last_report(self) -> dict[str, Any] | None:
        return self._report.to_dict() if self._report else None

    @property
    def attributes(self) -> dict[str, Any]:
        done, total = self._progress
        report = self._report
        return {
            "step": self._step or None,
            "progress": f"{done}/{total}" if total else None,
            "started_at": self._started_at,
            "finished_at": self._finished_at,
            "firmware_version": report.firmware_version if report else None,
            "report_file": self._report_path,
            "abort_reason": report.abort_reason if report else None,
            "verdicts": report.summary() if report else None,
        }

    # ── Starting and stopping ─────────────────────────────────────────────────

    def check_preconditions(self) -> None:
        """Refuse to start, before anything is written, when the test cannot run."""
        coordinator = self._coordinator
        control = coordinator.control
        if self.running:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="control_test_running"
            )
        data = coordinator.data or {}
        if not coordinator.connected or not data:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="control_test_no_data"
            )
        if coordinator.is_modbus_disabled or data.get("system_modes_hex") is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="control_test_no_status"
            )
        # Held by us, the test takes over; held by anything else, it would fight it.
        held = bool(data.get("device_modbus_control")) or data.get(
            "active_control_mode"
        ) not in (str(ControlMode.DEFAULT), None)
        if held and not control.holds_control:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="control_test_other_controller",
            )

    def async_start(self, power: float = DEFAULT_TEST_POWER_W) -> None:
        """Start a run in the background."""
        self.check_preconditions()
        self._task = self._coordinator.hass.async_create_background_task(
            self._async_run(power), name=f"{DOMAIN} control test"
        )

    async def async_run(self, power: float = DEFAULT_TEST_POWER_W) -> dict[str, Any]:
        """Run the test to its end and return the report, for callers that wait."""
        self.async_start(power)
        assert self._task is not None
        return await asyncio.shield(self._task)

    async def async_cancel(self) -> None:
        """Stop a run; it hands control back before it ends."""
        if not self.running:
            return
        assert self._task is not None
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    # ── The run ──────────────────────────────────────────────────────────────

    async def _async_run(self, power: float) -> dict[str, Any]:
        coordinator = self._coordinator
        loop = asyncio.get_running_loop()
        self._loop_start = loop.time()
        self._state = ControlTestState.RUNNING
        self._started_at, self._finished_at = dt.now(), None
        self._report_path = None
        self._wrote = False
        tests = list(CORE_TESTS)
        self._progress = (0, len(tests) + 2)
        report = self._report = await self._new_report(power)
        saver = False
        originals: dict[str, int] = {}

        report.parameters["took_over_modbus_control"] = coordinator.control.enabled
        await coordinator.control.async_begin_test()
        # Polled for the run even with their entities disabled; the next poll
        # follows at once, and is the first one read below.
        release = coordinator.async_require(REQUIRED_KEYS)
        persistent_notification.async_dismiss(coordinator.hass, self._notification_id())
        self._fire(EVENT_CONTROL_TEST_STARTED, {"power": power})
        try:
            frame = await self._next_frame()
            report.conditions = conditions_of(frame)
            saver = bool(frame.get("battery_saver_mode_ena"))
            originals = {
                key: int(value)
                for key in SETPOINT_KEYS
                if key in coordinator.registers_by_key
                and (value := frame.get(key)) is not None
            }

            self._set_step("taking control")
            if await self._take_control(report):
                self._advance()
                for test in tests:
                    self._set_step(test.name)
                    report.features.append(await self._run_feature(test, power, saver))
                    self._advance()
                self._set_step("handing back")
                await self._hand_back(report, saver, originals)
                self._advance()
            else:
                report.features = [
                    FeatureResult.for_test(
                        test, Verdict.NOT_TESTED, "the heartbeat was refused"
                    )
                    for test in tests
                ]
            report.outcome = str(ControlTestState.DONE)
            self._state = ControlTestState.DONE
        except asyncio.CancelledError:
            # Taken as the request to stop: what follows still has to run.
            if task := asyncio.current_task():
                task.uncancel()
            report.outcome = str(ControlTestState.ABORTED)
            report.abort_reason = "cancelled"
            self._state = ControlTestState.ABORTED
        except AbortTest as reason:
            report.outcome = str(ControlTestState.ABORTED)
            report.abort_reason = str(reason)
            self._state = ControlTestState.ABORTED
        except Exception as err:  # noqa: BLE001 - the inverter is put back regardless
            _LOGGER.exception("The control test failed")
            report.outcome = str(ControlTestState.FAILED)
            report.abort_reason = repr(err)
            self._state = ControlTestState.FAILED
        finally:
            await self._restore(saver, originals)
            release()
            coordinator.control.end_test()

        self._finished_at = dt.now()
        report.finished_at = self._finished_at.isoformat()
        self._set_step("")
        await self._save(report)
        self._notify(report)
        self._fire(
            EVENT_CONTROL_TEST_FINISHED,
            {
                "outcome": report.outcome,
                "abort_reason": report.abort_reason,
                "verdicts": report.summary(),
                "report_file": self._report_path,
            },
        )
        coordinator.async_update_listeners()
        return report.to_dict()

    async def _new_report(self, power: float) -> ControlTestReport:
        coordinator = self._coordinator
        try:
            version = str(
                (await async_get_integration(coordinator.hass, DOMAIN)).version
            )
        except Exception:  # noqa: BLE001 - only the report misses it
            version = None
        identity = coordinator.identity
        return ControlTestReport(
            source="home_assistant",
            created_at=dt.now().isoformat(),
            integration_version=version,
            home_assistant_version=HA_VERSION,
            model=str(coordinator.inverter_model),
            firmware_version=identity.firmware_version,
            protocol_version=identity.protocol_version,
            parameters={
                "power_w": power,
                "scan_interval_s": coordinator.scan_interval,
                "settle_s": self._timing.settle_s,
            },
            extra=dict(SCRIPT_ONLY),
        )

    async def _take_control(self, report: ControlTestReport) -> bool:
        heartbeat = self._heartbeat = self._heartbeat_factory()
        self._wrote = True
        if not await heartbeat.async_ensure_fresh():
            report.heartbeat = (
                "refused as invalid: commands would be stored, never acted on"
                if heartbeat.supported is False
                else "failed"
            )
            return False
        heartbeat.start()
        report.heartbeat = "accepted"
        took = await self._wait_for(
            lambda frame: bool(frame.get("device_modbus_control")),
            self._timing.status_bit_s,
        )
        report.manual_mode_bit_s = None if took is None else round(took, 1)
        report.manual_mode_bit = (
            f"set {took:.0f}s after the first heartbeat"
            if took is not None
            else f"not set within {self._timing.status_bit_s:.0f}s"
        )
        return True

    async def _run_feature(
        self, test: CoreTest, power: float, saver: bool
    ) -> FeatureResult:
        registers = self._coordinator.registers_by_key
        if test.setpoint_key not in registers:
            return FeatureResult.for_test(
                test, Verdict.NOT_TESTED, f"{test.setpoint_key} is not mapped"
            )

        frame = await self._next_frame()
        plan, reason = plan_test(test, frame, power)
        if plan is None:
            return FeatureResult.for_test(test, Verdict.SKIPPED, reason)
        signed_target = plan.signed_target(test)

        if failure := await self._send(test, plan.power, saver):
            await self._return_to_default(saver)
            return FeatureResult.for_test(test, Verdict.WRITE_REFUSED, failure)

        reserve = float(frame.get("min_soc_limit") or 0)
        start = self._now()
        measured: list[float] = []
        modes: list[tuple[float, str]] = []
        samples: list[dict[str, Any]] = []
        state: ControlStatus | None = None
        streak, settle_s = 0, None
        # The first poll may have been read before the command landed.
        await self._next_frame()
        while self._now() - start < self._timing.settle_s:
            frame = await self._next_frame()
            elapsed = self._now() - start
            value, _ = measure(test, frame)
            soc = frame.get("battery_soc")
            battery = frame.get("battery_power")
            state = deviation_state(
                signed_target=signed_target,
                measured=value,
                soc=None if soc is None else float(soc),
                min_soc=reserve,
                battery=None if battery is None else float(battery),
            )
            modes.append((elapsed, str(frame.get("active_control_mode"))))
            samples.append(trace_sample(elapsed, frame))
            if value is not None:
                measured.append(value)
            streak = streak + 1 if state is ControlStatus.ACTIVE else 0
            if streak >= CONTROL_STATUS_DAMPING_POLLS:
                settle_s = elapsed
                break

        verdict, detail, achieved = judge(test, plan, settle_s, state, measured)
        method_ok, status_report = judge_modes(str(test.method), modes)
        result = FeatureResult(
            test.name,
            str(test.method),
            test.setpoint_key,
            test.measure_key,
            verdict,
            detail,
            target_w=signed_target,
            baseline_w=plan.baseline,
            achieved_w=None if achieved is None else round(achieved, 1),
            settle_s=None if settle_s is None else round(settle_s, 1),
            method_reported=method_ok,
            status_report=status_report,
            samples=samples,
        )

        await self._return_to_default(saver)
        await self._sleep_polls(self._timing.return_s)
        back = str((await self._next_frame()).get("active_control_mode"))
        if back != str(ControlMode.DEFAULT):
            result.status_report += (
                f"; still reports {back} {self._timing.return_s:.0f}s after default"
            )
        return result

    async def _hand_back(
        self, report: ControlTestReport, saver: bool, originals: dict[str, int]
    ) -> None:
        await self._return_to_default(saver)
        await self._restore_setpoints(originals)
        last_beat = self._heartbeat.last_success if self._heartbeat else None
        await self._stop_heartbeat()
        took = await self._wait_for(
            lambda frame: frame.get("device_modbus_control") is False,
            self._timing.handback_s,
        )
        # Handed back: nothing is left for the clean-up to undo, and a write from
        # it would renew the window.
        self._wrote = False
        since = (dt.now() - last_beat).total_seconds() if last_beat else None
        if took is not None and since is not None:
            report.handback_s = round(since, 1)
            report.handback = f"bit 11 cleared {since:.0f}s after the last heartbeat"
        else:
            report.handback = (
                f"bit 11 still set {self._timing.handback_s:.0f}s after the heartbeat "
                "stopped"
            )

    async def _restore(self, saver: bool, originals: dict[str, int]) -> None:
        """Leave the inverter as found, whatever ended the run."""
        if not self._wrote:
            return
        try:
            if self._heartbeat is not None and self._heartbeat.in_control:
                await self._return_to_default(saver)
                await self._restore_setpoints(originals)
        except Exception as err:  # noqa: BLE001 - the window hands back regardless
            _LOGGER.warning(
                "Could not restore the inverter after the control test (%r); it "
                "returns to the EcoFlow app within a minute without a heartbeat.",
                err,
            )
        finally:
            await self._stop_heartbeat()
            self._wrote = False

    # ── Writes ───────────────────────────────────────────────────────────────

    async def _send(self, test: CoreTest, power: float, saver: bool) -> str:
        """Write the setpoint, then the control word; return why one failed."""
        await self._ensure_fresh()
        register = self._coordinator.registers_by_key[test.setpoint_key]
        if failure := await self._write(
            register.address,
            encode_register(int(round(power)) * test.sign, RegisterType.INT32),
            f"{test.setpoint_key} {int(round(power)) * test.sign} W",
        ):
            return f"setpoint write refused ({failure})"
        if failure := await self._write_control_word(
            compose_control_word(test.method, saver)
        ):
            return f"control word refused ({failure})"
        return ""

    async def _return_to_default(self, saver: bool) -> None:
        """Clear the setpoints and select the default method."""
        await self._ensure_fresh()
        registers = self._coordinator.registers_by_key
        for key in SETPOINT_KEYS:
            if key in registers:
                await self._write(
                    registers[key].address, encode_register(0, RegisterType.INT32), key
                )
        await self._write_control_word(compose_control_word(ControlMode.DEFAULT, saver))

    async def _restore_setpoints(self, originals: dict[str, int]) -> None:
        registers = self._coordinator.registers_by_key
        for key, value in originals.items():
            if value:
                await self._write(
                    registers[key].address,
                    encode_register(value, RegisterType.INT32),
                    key,
                )

    async def _write_control_word(self, value: int) -> str:
        if value & CONTROL_COMMAND_UNSAFE_BITS:
            raise AbortTest(f"refused to write control word 0x{value:08X}")
        return await self._write(
            CONTROL_COMMAND_REGISTER,
            encode_register(value, RegisterType.UINT32),
            f"control command 0x{value:08X}",
        )

    async def _write(self, address: int, words: list[int], what: str) -> str:
        """Write, retrying a refusal for the moment; return why it failed, if it did."""
        failure = ""
        for attempt in range(WRITE_ATTEMPTS):
            if attempt:
                await asyncio.sleep(WRITE_RETRY_S)
            try:
                await self._coordinator.modbus_client.async_write(
                    address, words, what=f"control test {what}"
                )
            except ModbusRejected as err:
                failure = f"exception code {err.exception_code}"
                if not err.transient:
                    break
            except HomeAssistantError as err:
                failure = str(err)
            else:
                return ""
        return failure

    async def _ensure_fresh(self) -> None:
        if self._heartbeat is not None:
            await self._heartbeat.async_ensure_fresh()

    async def _stop_heartbeat(self) -> None:
        if self._heartbeat is not None:
            await self._heartbeat.async_stop()

    # ── Readings ─────────────────────────────────────────────────────────────

    def _now(self) -> float:
        return asyncio.get_running_loop().time() - self._loop_start

    def _frame_timeout(self) -> float:
        if self._timing.frame_timeout_s is not None:
            return self._timing.frame_timeout_s
        return self._coordinator.scan_interval * 4 + 30

    async def _next_frame(self) -> dict[str, Any]:
        """Wait for the coordinator's next poll and return it.

        A poll replaces the data with a new dict, which is how a fresh reading is
        told from a listener update that only re-announces the old one.
        """
        coordinator = self._coordinator
        previous = coordinator.data
        fresh = asyncio.Event()

        @callback
        def _updated() -> None:
            if coordinator.data is not None and coordinator.data is not previous:
                fresh.set()

        remove: CALLBACK_TYPE = coordinator.async_add_listener(_updated)
        try:
            async with asyncio.timeout(self._frame_timeout()):
                await fresh.wait()
        except TimeoutError as err:
            raise AbortTest("no reading from the inverter; connection lost") from err
        finally:
            remove()

        frame = dict(coordinator.data)
        if frame.get("system_fault"):
            raise AbortTest("the inverter reports a system fault")
        if frame.get("grid_mode") == GridMode.ISLANDED:
            raise AbortTest("the inverter went off-grid")
        return frame

    async def _wait_for(
        self, check: Callable[[dict[str, Any]], bool], seconds: float
    ) -> float | None:
        """Wait for a reading that passes, returning the seconds it took."""
        start = self._now()
        while self._now() - start < seconds:
            if check(await self._next_frame()):
                return self._now() - start
        return None

    async def _sleep_polls(self, seconds: float) -> None:
        """Let at least *seconds* pass, counted in polls so a slow one still counts."""
        start = self._now()
        while self._now() - start < seconds:
            await self._next_frame()

    # ── Reporting ────────────────────────────────────────────────────────────

    def _set_step(self, step: str) -> None:
        self._step = step
        self._coordinator.async_update_listeners()

    def _advance(self) -> None:
        done, total = self._progress
        self._progress = (done + 1, total)
        self._coordinator.async_update_listeners()

    def _notification_id(self) -> str:
        return f"{DOMAIN}_control_test_{self._coordinator.config_entry.entry_id}"

    def _notify(self, report: ControlTestReport) -> None:
        """Say the run the user started has ended, as they may have left the page."""
        counts: dict[str, int] = {}
        for verdict in report.summary().values():
            counts[verdict] = counts.get(verdict, 0) + 1
        lines = [
            f"Firmware {report.firmware_version}: "
            + (
                ", ".join(
                    f"{count} {verdict.replace('_', ' ')}"
                    for verdict, count in sorted(counts.items())
                )
                if report.outcome == ControlTestState.DONE
                else f"{report.outcome}, {report.abort_reason}"
            )
            + "."
        ]
        if self._report_path:
            lines.append(f"The report is saved as `{self._report_path}`.")
        lines.append(
            "To share it, download the diagnostics from the inverter's device page "
            "and attach them to a "
            "[Control test report](https://github.com/MaxGrmm/EF-PowerOcean-TcpModbus"
            "/issues/new?template=control_test_report.yml) issue."
        )
        persistent_notification.async_create(
            self._coordinator.hass,
            "\n\n".join(lines),
            title="Control test finished",
            notification_id=self._notification_id(),
        )

    def _fire(self, event: str, data: dict[str, Any]) -> None:
        coordinator = self._coordinator
        devices = device_registry.async_entries_for_config_entry(
            device_registry.async_get(coordinator.hass),
            coordinator.config_entry.entry_id,
        )
        coordinator.hass.bus.async_fire(
            event, {"device_id": devices[0].id if devices else None, **data}
        )

    async def _save(self, report: ControlTestReport) -> None:
        """Keep the report beside the configuration, named for what was tested."""
        hass = self._coordinator.hass
        stamp = dt.now().strftime("%Y%m%d-%H%M%S")
        firmware = (report.firmware_version or "unknown").replace("/", "-")
        name = f"control_test_{report.model}_{firmware}_{stamp}.json"
        path = Path(hass.config.path(REPORT_DIRECTORY, name))
        content = json.dumps(report.to_dict(), indent=2, default=str)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        try:
            await hass.async_add_executor_job(_write)
        except OSError as err:
            _LOGGER.warning("Could not save the control test report: %s", err)
            return
        self._report_path = str(path)
