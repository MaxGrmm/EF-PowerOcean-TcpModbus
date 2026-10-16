"""The repairs flow that takes the inverter's value for long-held energy totals."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from custom_components.ef_powerocean_tcpmodbus import const
from custom_components.ef_powerocean_tcpmodbus.repairs import (
    HeldTotalsRepairFlow,
    async_create_fix_flow,
)


def _flow(coordinator: Any) -> HeldTotalsRepairFlow:
    flow = asyncio.run(
        async_create_fix_flow(Any, "issue", {"entry_id": "entry"})  # type: ignore[arg-type]
    )
    assert isinstance(flow, HeldTotalsRepairFlow)
    entry = SimpleNamespace(domain=const.DOMAIN, runtime_data=coordinator)
    flow.hass = SimpleNamespace(  # type: ignore[assignment]
        config_entries=SimpleNamespace(async_get_entry=lambda _: entry)
    )
    flow.flow_id, flow.handler = "flow", const.DOMAIN
    return flow


def _held_coordinator() -> SimpleNamespace:
    return SimpleNamespace(
        long_held_totals={"grid_export_total": object()},
        describe_held_totals=lambda: "- sensor.grid_export_total: ...",
        async_take_device_totals=AsyncMock(return_value={"grid_export_total": 507.5}),
    )


def test_the_flow_shows_what_it_will_take_before_taking_it() -> None:
    coordinator = _held_coordinator()
    result = asyncio.run(_flow(coordinator).async_step_init())

    assert result["type"] == "form"
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"] == {
        "totals": "- sensor.grid_export_total: ..."
    }
    coordinator.async_take_device_totals.assert_not_called()


def test_confirming_takes_the_device_totals() -> None:
    coordinator = _held_coordinator()
    result = asyncio.run(_flow(coordinator).async_step_confirm({}))

    assert result["type"] == "create_entry"
    coordinator.async_take_device_totals.assert_awaited_once()


def test_the_flow_aborts_when_nothing_is_held_anymore() -> None:
    coordinator = _held_coordinator()
    coordinator.long_held_totals = {}
    result = asyncio.run(_flow(coordinator).async_step_init())

    assert result["type"] == "abort"
    assert result["reason"] == "not_held"
