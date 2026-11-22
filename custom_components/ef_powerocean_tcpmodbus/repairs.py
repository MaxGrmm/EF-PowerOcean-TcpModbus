"""Repairs flows for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

from homeassistant.components.repairs import RepairsFlow
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry
from homeassistant.loader import async_get_integration

from . import issues, watchdog
from .const import BUG_REPORT_URL, DOMAIN
from .coordinator import EcoflowCoordinator

if TYPE_CHECKING:
    # Only for annotations: Home Assistant 2025.12, the oldest supported, does not
    # export it yet.
    from homeassistant.components.repairs import RepairsFlowResult


class HeldTotalsRepairFlow(RepairsFlow):
    """Continue energy totals held for over a day, or take the inverter's value."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    def _coordinator(self) -> EcoflowCoordinator | None:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or entry.domain != DOMAIN:
            return None
        coordinator: EcoflowCoordinator | None = getattr(entry, "runtime_data", None)
        return coordinator

    def _held_coordinator(self) -> EcoflowCoordinator | None:
        coordinator = self._coordinator()
        if coordinator is None or not coordinator.long_held_totals:
            return None
        return coordinator

    async def async_step_init(
        self,
        user_input: dict[str, str] | None = None,  # noqa: ARG002 - not a choice
    ) -> RepairsFlowResult:
        """Show the held totals and the choices.

        Home Assistant passes the issue id as this step's input, which must never
        count as the user choosing.
        """
        if (coordinator := self._held_coordinator()) is None:
            return self.async_abort(reason="not_held")
        return self.async_show_menu(
            step_id="init",
            menu_options=["keep_shown", "take_device"],
            description_placeholders={
                "totals": coordinator.describe_held_totals(),
                "device_page": self._device_page(),
                "report_url": await self._report_url(coordinator),
                "settings_page": f"/config/integrations/integration/{DOMAIN}",
            },
        )

    async def async_step_keep_shown(
        self,
        user_input: dict[str, str] | None = None,  # noqa: ARG002 - a menu choice
    ) -> RepairsFlowResult:
        """Keep each held total at its value in Home Assistant, with an offset."""
        if (coordinator := self._held_coordinator()) is None:
            return self.async_abort(reason="not_held")
        coordinator.async_keep_shown_totals()
        return self.async_create_entry(data={})

    async def async_step_take_device(
        self,
        user_input: dict[str, str] | None = None,  # noqa: ARG002 - a menu choice
    ) -> RepairsFlowResult:
        """Switch each held total to the inverter's value."""
        if (coordinator := self._held_coordinator()) is None:
            return self.async_abort(reason="not_held")
        await coordinator.async_take_device_totals()
        return self.async_create_entry(data={})

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


class AlertRepairFlow(RepairsFlow):
    """Ignore an alert this time, for a day, until the next release, or for good."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def _watchdog(self) -> watchdog.Watchdog | None:
        return watchdog.running(str(self._data.get("entry_id", "")))

    async def async_step_init(
        self,
        user_input: dict[str, str] | None = None,  # noqa: ARG002 - not a choice
    ) -> RepairsFlowResult:
        """Show what was seen, and the choices."""
        if self._watchdog() is None:
            return self.async_abort(reason="not_watching")
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "ignore_once",
                "ignore_day",
                "ignore_release",
                "ignore_forever",
            ],
            description_placeholders={
                key: str(self._data.get(key, ""))
                for key in ("title", "message", "count", "first", "last")
            },
        )

    async def async_step_ignore_release(
        self,
        user_input: dict[str, str] | None = None,  # noqa: ARG002 - a menu choice
    ) -> RepairsFlowResult:
        """Ignore the alert until another version of the integration runs."""
        if (running := self._watchdog()) is None:
            return self.async_abort(reason="not_watching")
        running.async_ignore_until_next_release(str(self._data.get("alert", "")))
        return self.async_create_entry(data={})

    async def async_step_ignore_once(
        self,
        user_input: dict[str, str] | None = None,  # noqa: ARG002 - a menu choice
    ) -> RepairsFlowResult:
        """Ignore the alert until what raised it goes away and comes back."""
        if (running := self._watchdog()) is None:
            return self.async_abort(reason="not_watching")
        running.async_ignore_this_time(str(self._data.get("alert", "")))
        return self.async_create_entry(data={})

    async def async_step_ignore_forever(
        self,
        user_input: dict[str, str] | None = None,  # noqa: ARG002 - a menu choice
    ) -> RepairsFlowResult:
        """Ignore the alert until the verbosity is set back to Essential."""
        if (running := self._watchdog()) is None:
            return self.async_abort(reason="not_watching")
        running.async_ignore_permanently(str(self._data.get("alert", "")))
        return self.async_create_entry(data={})

    async def async_step_ignore_day(
        self,
        user_input: dict[str, str] | None = None,  # noqa: ARG002 - a menu choice
    ) -> RepairsFlowResult:
        """Ignore the alert for 24 hours."""
        if (running := self._watchdog()) is None:
            return self.async_abort(reason="not_watching")
        running.async_ignore_for_a_day(str(self._data.get("alert", "")))
        return self.async_create_entry(data={})


class GridEnergyRepairFlow(RepairsFlow):
    """Turn on calculating grid energy from Grid Power, once the user confirms."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    def _coordinator(self) -> EcoflowCoordinator | None:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or entry.domain != DOMAIN:
            return None
        coordinator: EcoflowCoordinator | None = getattr(entry, "runtime_data", None)
        if coordinator is None or not coordinator.grid_energy_not_counted:
            return None
        return coordinator

    async def async_step_init(
        self,
        user_input: dict[str, str] | None = None,  # noqa: ARG002 - not a choice
    ) -> RepairsFlowResult:
        """Explain the choice before anything changes.

        Home Assistant passes the issue id as this step's input, which must never
        count as the user confirming.
        """
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Turn the calculation on once the user submits the form."""
        if (coordinator := self._coordinator()) is None:
            return self.async_abort(reason="not_needed")
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                description_placeholders={
                    "settings_page": f"/config/integrations/integration/{DOMAIN}"
                },
            )
        coordinator.async_enable_grid_energy_calculation()
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant,  # noqa: ARG001 - required by the repairs platform
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create the fix flow for a repairs issue raised by this integration."""
    data = data or {}
    if data.get("flow") == "alert":
        return AlertRepairFlow(data)
    entry_id = str(data.get("entry_id", ""))
    if issues.is_kind(issues.GRID_ENERGY, issue_id):
        return GridEnergyRepairFlow(entry_id)
    return HeldTotalsRepairFlow(entry_id)
