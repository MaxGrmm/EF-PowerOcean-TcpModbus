"""Switch entities for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BATTERY_SAVER_SWITCH, DOMAIN
from .coordinator import EcoflowCoordinator
from .entity import EcoFlowBaseEntity
from .models import SwitchDef

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow switches from a config entry."""
    coordinator: EcoflowCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [EcoFlowBatterySaverSwitch(coordinator, entry, BATTERY_SAVER_SWITCH)]
    )


class EcoFlowSwitch(EcoFlowBaseEntity, SwitchEntity):
    """Shared setup for the integration's switches."""

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: SwitchDef,
    ) -> None:
        super().__init__(coordinator, entry, definition)
        self._attr_device_class = definition.device_class
        self._attr_entity_category = definition.entity_category
        if definition.icon:
            self._attr_icon = definition.icon


class EcoFlowBatterySaverSwitch(EcoFlowSwitch):
    """Battery saver mode, bit 3 of the write-only control command.

    The coordinator composes the control word from this bit and whichever mode is
    selected, so toggling here never disturbs the mode.
    """

    @property
    def is_on(self) -> bool:
        # System Modes bit 3 is a status, not an echo: it only rises once the
        # inverter has actually gone idle, so it cannot confirm the command.
        return self.coordinator.battery_saver_commanded

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "device_reports_battery_saver": data.get("battery_saver_mode_ena"),
            "commanded_word": f"0x{self.coordinator.control_command:08X}",
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_battery_saver(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_battery_saver(False)
