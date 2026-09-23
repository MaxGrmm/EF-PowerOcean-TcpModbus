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
