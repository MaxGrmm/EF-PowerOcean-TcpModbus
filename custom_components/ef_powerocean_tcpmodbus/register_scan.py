"""The register scan, run from Home Assistant when the user asks for it.

It reads what scripts/register_scan.py reads, once: every register the model's map
declares, the candidate registers from EcoFlow's register schema, and the address
ranges around them. Nothing is written. The words are kept as the device sent them,
so the report can be read the same way as a run of the script, and the diagnostics
download carries it. The script's repeated sampling and its --watch mode stay in the
script.

Each request goes through the coordinator's client, one at a time between the polls
and the heartbeat, with a short pause so neither is kept waiting. The heartbeat
register is never read: the inverter answers a write to a register it is serving a
read for with "device busy", and a lost beat costs a minute of control.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components import persistent_notification
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry
from homeassistant.loader import async_get_integration
from homeassistant.util import dt

from .const import (
    CANDIDATE_REGISTERS,
    DOMAIN,
    HEARTBEAT_REGISTER,
    MODBUS_REQUEST_TIMEOUT_S,
    REGISTER_SCAN_RANGES,
    SERIAL_NUMBER,
    register_blocks_for,
)
from .modbus import TRANSPORT_ERRORS, ModbusReadRejected
from .models import RegisterDef
from .telemetry import decode_register

if TYPE_CHECKING:
    from .coordinator import EcoflowCoordinator

_LOGGER = logging.getLogger(__name__)

# Words per range read, as the script reads them. A refused chunk is read again one
# address at a time, to tell the addresses the device has from those it lacks.
SCAN_CHUNK: Final = 16
# Pause before each request, which leaves the poll and the heartbeat room between them.
SCAN_REQUEST_GAP_S: Final = 0.1
# Never read, as the integration's poll never reads it.
SCAN_AVOID: Final = frozenset({HEARTBEAT_REGISTER})


class RegisterScanState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    ABORTED = "aborted"
    FAILED = "failed"


class ScanAborted(Exception):
    """The connection was lost, so the rest of the scan would read nothing."""


class RegisterScan:
    """Runs the register scan for one inverter, one run at a time."""

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        *,
        request_gap_s: float = SCAN_REQUEST_GAP_S,
    ) -> None:
        self._coordinator = coordinator
        self._request_gap_s = request_gap_s
        self._task: asyncio.Task[dict[str, Any]] | None = None
        self._state = RegisterScanState.IDLE
        self._report: dict[str, Any] | None = None
        self._requests = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def state(self) -> RegisterScanState:
        return self._state

    @property
    def last_report(self) -> dict[str, Any] | None:
        return self._report

    # ── Starting and stopping ─────────────────────────────────────────────────

    def check_preconditions(self) -> None:
        """Refuse to start while a scan runs or the inverter cannot be reached."""
        if self.running:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="register_scan_running"
            )
        # Its requests would sit between the test's, and skew what the test times.
        if self._coordinator.control_test.running:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="register_scan_control_test"
            )
        if not self._coordinator.connected or not self._coordinator.data:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="register_scan_no_data"
            )

    def async_start(self) -> None:
        """Start a scan in the background."""
        self.check_preconditions()
        self._task = self._coordinator.hass.async_create_background_task(
            self._async_run(), name=f"{DOMAIN} register scan"
        )

    async def async_run(self) -> dict[str, Any]:
        """Run a scan to its end and return the report, for callers that wait."""
        self.async_start()
        assert self._task is not None
        return await asyncio.shield(self._task)

    async def async_cancel(self) -> None:
        if not self.running:
            return
        assert self._task is not None
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    # ── The run ──────────────────────────────────────────────────────────────

    async def _async_run(self) -> dict[str, Any]:
        coordinator = self._coordinator
        hass = coordinator.hass
        self._state = RegisterScanState.RUNNING
        self._requests = 0
        report = self._report = self._new_report()
        persistent_notification.async_dismiss(hass, self._notification_id())
        try:
            report["integration_version"] = await self._integration_version()
            report["blocks"], report["mapped"] = await self._read_map()
            report["candidates"] = await self._read_candidates()
            words, refused = await self._read_ranges()
            report["words"] = _redact_serial(_spans(words))
            report["refused"] = {
                first: {"count": last - first + 1, "reason": reason}
                for first, last, reason in _refused_spans(refused)
            }
            self._state = RegisterScanState.DONE
        except asyncio.CancelledError:
            report["abort_reason"] = "cancelled"
            self._state = RegisterScanState.ABORTED
            raise
        except ScanAborted as reason:
            report["abort_reason"] = str(reason)
            self._state = RegisterScanState.ABORTED
        except Exception as err:
            _LOGGER.exception("The register scan failed")
            report["abort_reason"] = repr(err)
            self._state = RegisterScanState.FAILED
        finally:
            report["outcome"] = str(self._state)
            report["requests"] = self._requests
            report["finished_at"] = dt.now().isoformat()

        self._notify(report)
        return report

    async def _integration_version(self) -> str | None:
        try:
            integration = await async_get_integration(self._coordinator.hass, DOMAIN)
        except Exception:
            return None
        return str(integration.version)

    def _new_report(self) -> dict[str, Any]:
        coordinator = self._coordinator
        identity = coordinator.identity
        model = coordinator.inverter_model
        return {
            "source": "home_assistant",
            "created_at": dt.now().isoformat(),
            "integration_version": None,
            "home_assistant_version": HA_VERSION,
            "model": str(model),
            "high_word_first": model.traits.high_word_first,
            "firmware_version": identity.firmware_version,
            "protocol_version": identity.protocol_version,
            "ranges": [list(span) for span in REGISTER_SCAN_RANGES],
        }

    async def _read(self, address: int, count: int) -> tuple[list[int] | None, str]:
        """Read once, again if the device was only busy, and return the words or why
        the device refused them. Raises ScanAborted when the device is gone."""
        reason = ""
        for attempt in range(2):
            await asyncio.sleep(self._request_gap_s * (attempt + 1))
            self._requests += 1
            try:
                async with asyncio.timeout(2 * MODBUS_REQUEST_TIMEOUT_S):
                    words = await self._coordinator.modbus_client.async_read(
                        address, count
                    )
            except ModbusReadRejected as err:
                reason = f"exception code {err.exception_code}"
                if err.permanent:
                    break
                continue
            except (*TRANSPORT_ERRORS, HomeAssistantError) as err:
                raise ScanAborted(f"connection lost at {address}: {err!r}") from err
            return list(words), ""
        return None, reason

    def _decode(self, words: list[int], register: RegisterDef) -> float | None:
        high_word_first = self._coordinator.inverter_model.traits.high_word_first
        return decode_register(words, register.data_type, high_word_first)

    def _entry(
        self, register: RegisterDef, words: list[int] | None, reason: str
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "address": register.address,
            "type": str(register.data_type),
        }
        if words is None:
            entry["error"] = reason
        else:
            entry["value"] = self._decode(words, register)
            entry["raw"] = words
        return entry

    async def _read_map(
        self,
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        """Read every block of the model's whole map, as the poll would with every
        entity enabled, and each register of a refused block on its own."""
        blocks: list[dict[str, Any]] = []
        mapped: dict[str, dict[str, Any]] = {}
        for block in register_blocks_for(self._coordinator.inverter_model):
            words, reason = await self._read(block.start, block.count)
            blocks.append(
                {
                    "start": block.start,
                    "count": block.count,
                    "result": "ok" if words is not None else reason,
                }
            )
            for register in block.registers:
                own: list[int] | None
                if words is not None:
                    own, own_reason = block.registers_for(words, register), ""
                else:
                    own, own_reason = await self._read(register.address, register.size)
                mapped[register.key] = self._entry(register, own, own_reason)
        return blocks, mapped

    async def _read_candidates(self) -> dict[str, dict[str, Any]]:
        candidates: dict[str, dict[str, Any]] = {}
        for register in CANDIDATE_REGISTERS:
            words, reason = await self._read(register.address, register.size)
            candidates[register.key] = self._entry(register, words, reason)
        return candidates

    async def _read_ranges(self) -> tuple[dict[int, int], dict[int, str]]:
        """Read the scan ranges in chunks, one address at a time where refused."""
        words: dict[int, int] = {}
        refused: dict[int, str] = {}
        for low, high in REGISTER_SCAN_RANGES:
            for base in range(low, high + 1, SCAN_CHUNK):
                last = min(base + SCAN_CHUNK - 1, high)
                for first, end in _spans_avoiding(base, last, SCAN_AVOID):
                    chunk, _ = await self._read(first, end - first + 1)
                    if chunk is not None:
                        words.update(zip(range(first, end + 1), chunk, strict=False))
                        continue
                    for address in range(first, end + 1):
                        single, reason = await self._read(address, 1)
                        if single is None:
                            refused[address] = reason
                        else:
                            words[address] = single[0]
        return words, refused

    # ── Telling the user ─────────────────────────────────────────────────────

    def _notification_id(self) -> str:
        return f"{DOMAIN}_register_scan_{self._coordinator.config_entry.entry_id}"

    def _notify(self, report: dict[str, Any]) -> None:
        """Say the scan the user started has ended, as they may have left the page."""
        if report["outcome"] == RegisterScanState.DONE:
            candidates = report["candidates"].values()
            answered = sum(1 for entry in candidates if "error" not in entry)
            readable = sum(len(words) for words in report["words"].values())
            refused = sum(run["count"] for run in report["refused"].values())
            summary = (
                f"Firmware {report['firmware_version']}: {answered} of "
                f"{len(candidates)} candidate registers answered; {readable} "
                f"addresses read and {refused} refused in the scanned ranges."
            )
        else:
            summary = (
                f"Firmware {report['firmware_version']}: {report['outcome']}, "
                f"{report.get('abort_reason')}."
            )
        # The diagnostics download needs a signed URL that expires within a minute,
        # so the link goes to the device page, whose menu offers it.
        device_id = self._device_id()
        device_page = (
            f"[device page](/config/devices/device/{device_id})"
            if device_id
            else "device page"
        )
        persistent_notification.async_create(
            self._coordinator.hass,
            "\n\n".join(
                (
                    summary,
                    f"To share it, open the inverter's {device_page}, choose "
                    "**Download diagnostics** from its ⋮ menu, and attach the file "
                    "to an issue. The report is kept until Home Assistant restarts.",
                )
            ),
            title="Register scan finished",
            notification_id=self._notification_id(),
        )

    def _device_id(self) -> str | None:
        coordinator = self._coordinator
        devices = device_registry.async_entries_for_config_entry(
            device_registry.async_get(coordinator.hass),
            coordinator.config_entry.entry_id,
        )
        return devices[0].id if devices else None


def _spans_avoiding(
    first: int, last: int, avoid: frozenset[int]
) -> list[tuple[int, int]]:
    """Split first..last into the runs that leave out every address in *avoid*."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for address in range(first, last + 1):
        if address in avoid:
            if start is not None:
                spans.append((start, address - 1))
                start = None
        elif start is None:
            start = address
    if start is not None:
        spans.append((start, last))
    return spans


def _spans(words: dict[int, int]) -> dict[int, list[int | None]]:
    """Group words by the address each contiguous run starts at."""
    spans: dict[int, list[int | None]] = {}
    run: list[int | None] = []
    next_address = -1
    for address in sorted(words):
        if address != next_address:
            run = spans[address] = []
        run.append(words[address])
        next_address = address + 1
    return spans


def _refused_spans(refused: dict[int, str]) -> list[tuple[int, int, str]]:
    """Group refused addresses into runs that share their reason."""
    runs: list[tuple[int, int, str]] = []
    for address in sorted(refused):
        reason = refused[address]
        if runs and runs[-1][1] == address - 1 and runs[-1][2] == reason:
            runs[-1] = (runs[-1][0], address, reason)
        else:
            runs.append((address, address, reason))
    return runs


def _redact_serial(spans: dict[int, list[int | None]]) -> dict[int, list[int | None]]:
    """Blank every word of the serial number, which the ranges read."""
    serial = range(SERIAL_NUMBER.address, SERIAL_NUMBER.end)
    return {
        start: [
            None if start + offset in serial else word
            for offset, word in enumerate(words)
        ]
        for start, words in spans.items()
    }
