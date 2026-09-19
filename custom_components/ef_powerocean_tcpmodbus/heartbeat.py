"""Holds Modbus control authority over the inverter.

The inverter acts on a stored command only while the heartbeat register has been
written within the last minute. Beating runs on its own timer rather than riding
on the read poll, which stalls behind slow reads and backs off for two minutes
after a failed reconnect — both long enough to lose the window.
"""

from __future__ import annotations

import asyncio
import logging
import random
from contextlib import suppress
from datetime import datetime
from math import inf

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt

from .const import (
    HEARTBEAT_FRESH_S,
    HEARTBEAT_INTERVAL_S,
    HEARTBEAT_JITTER_S,
    HEARTBEAT_LAPSE_S,
    HEARTBEAT_REGISTER,
    HEARTBEAT_REPROBE_S,
    HEARTBEAT_RETRY_BUDGET_S,
    HEARTBEAT_VALUE,
)
from .modbus import ModbusClient, ModbusRejected

_LOGGER = logging.getLogger(__name__)


def retry_delays(scan_interval_s: float) -> tuple[float, ...]:
    """Return the waits between the attempts of one beat, the first immediate.

    A busy answer is usually the poll's own read still in the inverter, so a retry
    waits a whole poll cycle rather than asking again inside the one that caused it.
    """
    delay = max(1.0, min(float(scan_interval_s), HEARTBEAT_RETRY_BUDGET_S))
    return (0.0, *(delay,) * int(HEARTBEAT_RETRY_BUDGET_S // delay))


class Heartbeat:
    """Writes the heartbeat register on a timer and reports whether it holds."""

    def __init__(self, modbus_client: ModbusClient, *, scan_interval_s: float) -> None:
        self._modbus_client = modbus_client
        self._retry_delays = retry_delays(scan_interval_s)
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._last_success: datetime | None = None
        # None until the inverter has answered once, False only if it refused the
        # request itself rather than the moment.
        self._supported: bool | None = None

    @property
    def supported(self) -> bool | None:
        return self._supported

    @property
    def last_success(self) -> datetime | None:
        return self._last_success

    @property
    def in_control(self) -> bool:
        """Return whether a beat landed recently enough for commands to take effect."""
        return self._age() <= HEARTBEAT_LAPSE_S

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="powerocean-heartbeat")

    async def async_stop(self) -> None:
        """Stop beating. Nothing is written on the way out: letting the window lapse
        is how the inverter is handed back to its app settings."""
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def mark_stale(self) -> None:
        """Give up authority after a connection outage.

        The verdict on the register goes with it: a new socket can mean a different
        device state, so a refusal recorded against the old one is not held against it.
        """
        self._last_success = None
        self._supported = None

    async def async_ensure_fresh(self) -> bool:
        """Hold the window open for the command that follows.

        A beat seconds old already holds it, and skipping the write keeps a command
        from adding a frame the inverter has to answer while applying the last one.
        """
        if self._age() <= HEARTBEAT_FRESH_S:
            return True
        return await self._async_beat()

    def _age(self) -> float:
        if self._last_success is None:
            return inf
        return (dt.now() - self._last_success).total_seconds()

    async def _run(self) -> None:
        while True:
            try:
                await self._async_beat()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a supervisor may not die of surprises
                _LOGGER.exception("Unexpected error in the heartbeat loop")
            delay = (
                HEARTBEAT_REPROBE_S
                if self._supported is False
                else HEARTBEAT_INTERVAL_S
            )
            # Drift off the poll tick, which is when the inverter is busiest.
            await asyncio.sleep(delay + random.uniform(0.0, HEARTBEAT_JITTER_S))

    async def _async_beat(self) -> bool:
        async with self._lock:
            # Someone may have beaten while this call waited for the lock.
            if self._age() <= HEARTBEAT_FRESH_S:
                return True

            for delay in self._retry_delays:
                if delay:
                    await asyncio.sleep(delay)
                if not self._modbus_client.connected:
                    return False

                # Stamped from before the write, so a slow round trip shortens the
                # next interval rather than overrunning the window.
                sent_at = dt.now()
                try:
                    await self._modbus_client.async_write(
                        HEARTBEAT_REGISTER, [HEARTBEAT_VALUE], what="heartbeat"
                    )
                except ModbusRejected as err:
                    if not err.transient:
                        self._record_refusal(err)
                        return False
                    _LOGGER.debug(f"Heartbeat refused for now, retrying: {err}")
                except HomeAssistantError as err:
                    _LOGGER.debug(f"Heartbeat did not reach the inverter: {err!r}")
                else:
                    self._record_success(sent_at)
                    return True

        return False

    def _record_success(self, sent_at: datetime) -> None:
        if self._supported is not True:
            _LOGGER.info(
                "Heartbeat register %s accepted; Modbus control authority is being "
                "refreshed every %ss.",
                HEARTBEAT_REGISTER,
                int(HEARTBEAT_INTERVAL_S),
            )
        self._supported = True
        self._last_success = sent_at

    def _record_refusal(self, err: ModbusRejected) -> None:
        """Note a refusal of the request itself, which this firmware will repeat."""
        if self._supported is not False:
            _LOGGER.warning(
                "Heartbeat register %s was refused as an invalid request (%s). This "
                "firmware appears not to implement it, so commands will be stored "
                "but never acted on. Retrying every %s minutes.",
                HEARTBEAT_REGISTER,
                err,
                int(HEARTBEAT_REPROBE_S // 60),
            )
        self._supported = False
