"""Keeps the inverter acting on the commands it has been sent.

It obeys them only while the heartbeat register keeps being written, so writing it
runs on its own timer rather than inside the read poll. The poll stalls behind slow
reads and backs off for two minutes after a failed reconnect, and either is long
enough to miss the deadline.
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
    HEARTBEAT_INTERVAL_S,
    HEARTBEAT_JITTER_S,
    HEARTBEAT_REGISTER,
    HEARTBEAT_RETRY_TOTAL_S,
    HEARTBEAT_REUSE_S,
    HEARTBEAT_UNSUPPORTED_RETRY_S,
    HEARTBEAT_VALUE,
    HEARTBEAT_WINDOW_S,
)
from .modbus import ModbusClient, ModbusRejected

_LOGGER = logging.getLogger(__name__)


def retry_delays(scan_interval_s: float) -> tuple[float, ...]:
    """Return how long to wait before each attempt of one write, the first nothing.

    A busy answer usually means the inverter is still serving the poll's own read,
    so a retry waits out a whole poll cycle instead of asking again during the cycle
    that caused it. Long intervals are capped, or the retries would outlast the write
    they belong to.
    """
    delay = max(1.0, min(float(scan_interval_s), HEARTBEAT_RETRY_TOTAL_S))
    return (0.0, *(delay,) * int(HEARTBEAT_RETRY_TOTAL_S // delay))


class Heartbeat:
    """Writes the heartbeat register on a timer, and reports whether it is landing."""

    def __init__(self, modbus_client: ModbusClient, *, scan_interval_s: float) -> None:
        self._modbus_client = modbus_client
        self._retry_delays = retry_delays(scan_interval_s)
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._last_success: datetime | None = None
        # None until the inverter has answered once. False means it called the
        # request invalid, which is a different answer from a refusal to do it now.
        self._supported: bool | None = None

    @property
    def supported(self) -> bool | None:
        return self._supported

    @property
    def last_success(self) -> datetime | None:
        return self._last_success

    @property
    def in_control(self) -> bool:
        """Return whether the last write is recent enough for commands to take effect."""
        return self._age() <= HEARTBEAT_WINDOW_S

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="powerocean-heartbeat")

    async def async_stop(self) -> None:
        """Stop writing.

        Nothing is sent on the way out: letting the deadline pass is how the inverter
        is handed back to its app settings.
        """
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def mark_stale(self) -> None:
        """Give up on the inverter obeying us, after a connection outage.

        What it last said about the register is dropped too: a new socket can mean a
        different device state, so an earlier refusal is not held against it.
        """
        self._last_success = None
        self._supported = None

    async def async_ensure_fresh(self) -> bool:
        """Make sure the inverter will act on the command that follows.

        A write seconds old already keeps the deadline off, and reusing it spares the
        inverter a frame to answer while it is still applying the previous write.
        """
        if self._age() <= HEARTBEAT_REUSE_S:
            return True
        return await self._async_write_heartbeat()

    def _age(self) -> float:
        if self._last_success is None:
            return inf
        return (dt.now() - self._last_success).total_seconds()

    async def _run(self) -> None:
        while True:
            try:
                await self._async_write_heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one failed write must not stop the timer
                _LOGGER.exception("Unexpected error in the heartbeat loop")
            delay = (
                HEARTBEAT_UNSUPPORTED_RETRY_S
                if self._supported is False
                else HEARTBEAT_INTERVAL_S
            )
            # The jitter stops the write settling onto the same second as the poll.
            await asyncio.sleep(delay + random.uniform(0.0, HEARTBEAT_JITTER_S))

    async def _async_write_heartbeat(self) -> bool:
        async with self._lock:
            # Another caller may have written while this one waited for the lock.
            if self._age() <= HEARTBEAT_REUSE_S:
                return True

            for delay in self._retry_delays:
                if delay:
                    await asyncio.sleep(delay)
                if not self._modbus_client.connected:
                    return False

                # Timed from before the write, so a slow round trip is counted
                # against the deadline rather than ignored.
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
        """Note the inverter calling the request invalid, which it will keep doing."""
        if self._supported is not False:
            _LOGGER.warning(
                "Heartbeat register %s was refused as an invalid request (%s). This "
                "firmware appears not to implement it, so commands will be stored "
                "but never acted on. Retrying every %s minutes.",
                HEARTBEAT_REGISTER,
                err,
                int(HEARTBEAT_UNSUPPORTED_RETRY_S // 60),
            )
        self._supported = False
