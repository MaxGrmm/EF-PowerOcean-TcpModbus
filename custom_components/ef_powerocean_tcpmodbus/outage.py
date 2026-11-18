"""Raise a repairs issue when the inverter stops answering, naming the likely cause.

A firmware update can switch Modbus TCP off. The inverter then stays silent
rather than answering with zeros, so the modbus_disabled issue never shows and
a timeout is all a read sees. Whether the port still takes connections tells a
silent inverter from one that is gone.

Failed polls and failed setups feed one outage per entry. Its issue waits until
the outage has lasted longer than the reboot of an update, and clears with the
first good read.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MODBUS_REQUEST_TIMEOUT_S

if TYPE_CHECKING:
    from .modbus import ModbusClient

_LOGGER = logging.getLogger(__name__)

# Well past the reboot of a firmware update, which takes about two minutes.
NOT_ANSWERING_AFTER_S: Final = 600
# Once the issue is up, how often its cause is looked at again. A poll can fail
# every few seconds, and each look may open a connection.
RECHECK_CAUSE_AFTER_S: Final = 300
LEARN_MORE_URL: Final = (
    "https://github.com/MaxGrmm/EF-PowerOcean-TcpModbus#troubleshooting"
)


class Cause(StrEnum):
    """Why the inverter does not answer, as far as the network shows."""

    # Nothing takes a connection on the port.
    UNREACHABLE = "unreachable"
    # The port takes connections, but no Modbus reply comes back.
    MODBUS_SILENT = "modbus_silent"


async def async_classify(host: str, port: int) -> Cause:
    """Connect to the port and close again at once, sending nothing.

    The handshake is all it takes: an inverter that limits its clients mostly
    does so by closing a connection it has already accepted.
    """
    try:
        async with asyncio.timeout(MODBUS_REQUEST_TIMEOUT_S):
            _, writer = await asyncio.open_connection(host, port)
    # ValueError: a host name that cannot be encoded, such as "a..b".
    except (OSError, TimeoutError, ValueError):
        return Cause.UNREACHABLE
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return Cause.MODBUS_SILENT


async def async_cause(client: ModbusClient, host: str, port: int) -> Cause:
    """Name the cause from the client's link where it can tell, else probe.

    A link that holds a connection its reads get no answer on has already shown
    the port open. One that holds none leaves the inverter free for the probe.
    """
    if client.connected:
        return Cause.MODBUS_SILENT
    return await async_classify(host, port)


@dataclass(slots=True)
class Outage:
    """An entry's inverter at *host*:*port* not answering since *since*."""

    since: datetime
    host: str
    port: int
    # What the issue names, once it is raised, and when that was looked at.
    cause: Cause | None = None
    checked_at: datetime | None = None


# By entry id. Held here rather than by the coordinator, so that an outage outlives
# a reload of its entry. A restart of Home Assistant starts over, as it also drops
# the issue.
_outages: dict[str, Outage] = {}


def issue_id(entry: ConfigEntry) -> str:
    """Return the stable id of the issue raised while the inverter is silent."""
    return f"not_answering_{entry.entry_id}"


async def async_failed(
    hass: HomeAssistant, entry: ConfigEntry, client: ModbusClient, host: str, port: int
) -> None:
    """Note that the inverter did not answer; raise the issue once it is overdue."""
    now = dt_util.utcnow()
    outage = _outages.get(entry.entry_id)
    if outage is None or (outage.host, outage.port) != (host, port):
        # A new address, after Reconfigure, starts over: the issue names the host.
        if outage is not None:
            ir.async_delete_issue(hass, DOMAIN, issue_id(entry))
        outage = _outages[entry.entry_id] = Outage(since=now, host=host, port=port)
    if (now - outage.since).total_seconds() < NOT_ANSWERING_AFTER_S:
        return
    if (
        outage.checked_at is not None
        and (now - outage.checked_at).total_seconds() < RECHECK_CAUSE_AFTER_S
    ):
        return

    cause = await async_cause(client, host, port)
    outage.checked_at = now
    if cause is outage.cause:
        return
    since = dt_util.as_local(outage.since).strftime("%Y-%m-%d %H:%M")
    if outage.cause is None:
        _LOGGER.warning(
            "The inverter at %s:%s has not answered since %s (%s)",
            host,
            port,
            since,
            cause,
        )
    outage.cause = cause
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry),
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=f"not_answering_{cause}",
        translation_placeholders={"host": host, "since": since},
        learn_more_url=LEARN_MORE_URL,
    )


@callback
def async_answered(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """End the outage, and its issue, with an answer from the inverter."""
    outage = _outages.pop(entry.entry_id, None)
    if outage is None or outage.cause is None:
        return
    _LOGGER.info(
        "The inverter answers again, after not answering since %s",
        dt_util.as_local(outage.since).strftime("%Y-%m-%d %H:%M"),
    )
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry))


@callback
def async_forget(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the outage and its issue of an entry that stops polling for good."""
    _outages.pop(entry.entry_id, None)
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry))
