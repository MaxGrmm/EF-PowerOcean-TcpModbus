from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest

from tests.fake_inverter import HOST, FakeInverter


@pytest.fixture(autouse=True)
def restore_event_loop() -> Iterator[None]:
    """Give back the loop asyncio.run() unsets; harnesses before 2026.5 need it."""
    loop = asyncio.get_event_loop()
    yield
    asyncio.set_event_loop(loop)


@pytest.fixture(autouse=True)
def reset_shared_modbus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test with the sharing decision unmade, as at a fresh startup."""
    from custom_components.ef_powerocean_tcpmodbus import modbus

    monkeypatch.setattr(modbus, "_shared", None)


@pytest.fixture
async def inverter(
    socket_enabled: None,  # noqa: ARG001 - lets the fake inverter listen
) -> AsyncIterator[FakeInverter]:
    """Serve a fake inverter on localhost."""
    fake = FakeInverter()
    server = await asyncio.start_server(fake.serve, HOST, 0)
    fake.port = server.sockets[0].getsockname()[1]
    yield fake
    server.close()
    await server.wait_closed()
