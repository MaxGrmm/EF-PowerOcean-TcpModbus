"""An inverter that stops answering is told apart from one that is gone."""

from __future__ import annotations

import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.ef_powerocean_tcpmodbus import outage
from custom_components.ef_powerocean_tcpmodbus.outage import Cause
from tests.fake_inverter import HOST, FakeInverter


def _closed_port() -> int:
    """Return a port nothing listens on."""
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


# ── The cause ─────────────────────────────────────────────────────────────────


async def test_a_port_that_takes_connections_is_a_silent_inverter(
    inverter: FakeInverter,
) -> None:
    assert await outage.async_classify(HOST, inverter.port) is Cause.MODBUS_SILENT
    assert inverter.requests == 0


@pytest.mark.usefixtures("socket_enabled")
async def test_a_closed_port_is_an_unreachable_inverter() -> None:
    assert await outage.async_classify(HOST, _closed_port()) is Cause.UNREACHABLE


async def test_a_connection_held_without_answers_needs_no_probe() -> None:
    client = SimpleNamespace(connected=True)

    with patch.object(outage, "async_classify", side_effect=AssertionError):
        cause = await outage.async_cause(client, HOST, 502)  # type: ignore[arg-type]

    assert cause is Cause.MODBUS_SILENT


async def test_without_a_held_connection_the_probe_decides() -> None:
    client = SimpleNamespace(connected=False)

    with patch.object(
        outage, "async_classify", AsyncMock(return_value=Cause.UNREACHABLE)
    ) as probe:
        cause = await outage.async_cause(client, HOST, 502)  # type: ignore[arg-type]

    assert cause is Cause.UNREACHABLE
    probe.assert_awaited_once_with(HOST, 502)
