"""Repairs flows for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import EcoflowCoordinator


class HeldTotalsRepairFlow(RepairsFlow):
    """Take the inverter's value for energy totals held for over a day."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    def _coordinator(self) -> EcoflowCoordinator | None:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or entry.domain != DOMAIN:
            return None
        coordinator: EcoflowCoordinator | None = getattr(entry, "runtime_data", None)
        return coordinator

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        return await self.async_step_confirm(user_input)

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        coordinator = self._coordinator()
        if coordinator is None or not coordinator.long_held_totals:
            return self.async_abort(reason="not_held")
        if user_input is not None:
            await coordinator.async_take_device_totals()
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"totals": coordinator.describe_held_totals()},
        )


async def async_create_fix_flow(
    hass: HomeAssistant,  # noqa: ARG001 - required by the repairs platform
    issue_id: str,  # noqa: ARG001 - required by the repairs platform
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create the fix flow for a repairs issue raised by this integration."""
    return HeldTotalsRepairFlow(str((data or {}).get("entry_id", "")))
