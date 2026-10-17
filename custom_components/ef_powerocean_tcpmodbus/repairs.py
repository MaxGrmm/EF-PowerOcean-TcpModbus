"""Repairs flows for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry
from homeassistant.loader import async_get_integration

from .const import BUG_REPORT_URL, DOMAIN
from .coordinator import EcoflowCoordinator

if TYPE_CHECKING:
    # Only for annotations: Home Assistant 2025.12, the oldest supported, does not
    # export it yet.
    from homeassistant.components.repairs import RepairsFlowResult


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
        self,
        user_input: dict[str, str] | None = None,  # noqa: ARG002 - not a submission
    ) -> RepairsFlowResult:
        """Always show the confirmation first.

        Home Assistant passes the issue id as this step's input, which must never
        count as the user confirming.
        """
        return await self.async_step_confirm()

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
            description_placeholders={
                "totals": coordinator.describe_held_totals(),
                "device_page": self._device_page(),
                "report_url": await self._report_url(coordinator),
            },
        )

    def _device_page(self) -> str:
        """Return the inverter's device page, whose menu offers the diagnostics.

        The diagnostics download itself needs a signed URL that expires within a
        minute, so the link can only lead to the page that offers it.
        """
        devices = device_registry.async_entries_for_config_entry(
            device_registry.async_get(self.hass), self._entry_id
        )
        if devices:
            return f"/config/devices/device/{devices[0].id}"
        return f"/config/integrations/integration/{DOMAIN}"

    async def _report_url(self, coordinator: EcoflowCoordinator) -> str:
        """Return a bug report with the form's fields filled in from the device."""
        integration = await async_get_integration(self.hass, DOMAIN)
        held = sorted(coordinator.long_held_totals)
        fields = {
            "template": "bug_report.yml",
            "title": f"Energy totals held for over a day: {', '.join(held)}",
            "ha_version": HA_VERSION,
            "device_type": coordinator.inverter_model.traits.display_name,
            "integration_version": str(integration.version),
            "device_firmware": coordinator.identity.firmware_version or "",
            "description": (
                "Repairs reported these energy totals as held for over a day, "
                "which I believe is wrong:\n\n" + coordinator.describe_held_totals()
            ),
            "steps": (
                "Repairs showed the issue that energy totals have been held for "
                "over a day. The diagnostics download is attached."
            ),
        }
        return f"{BUG_REPORT_URL}?{urlencode(fields, quote_via=quote)}"


async def async_create_fix_flow(
    hass: HomeAssistant,  # noqa: ARG001 - required by the repairs platform
    issue_id: str,  # noqa: ARG001 - required by the repairs platform
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create the fix flow for a repairs issue raised by this integration."""
    return HeldTotalsRepairFlow(str((data or {}).get("entry_id", "")))
