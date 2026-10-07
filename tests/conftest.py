from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest


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
