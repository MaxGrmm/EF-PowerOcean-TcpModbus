"""Select entities for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BATTERY_MODE_SELECT, DOMAIN
from .coordinator import EcoflowCoordinator
from .entity import EcoFlowBaseEntity
from .models import ControlEntityDef, ControlFeature


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow selects from a config entry."""
    coordinator: EcoflowCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [EcoFlowBatteryModeSelect(coordinator, entry, BATTERY_MODE_SELECT)]
    )


class EcoFlowBatteryModeSelect(EcoFlowBaseEntity, SelectEntity):
    """What the inverter should be doing.

    The protocol follows one control method at a time, so this is a single choice
    rather than several toggles. Each mode's power and the two state-of-charge
    limits are separate entities that stay editable whatever is selected here.
    """

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: ControlEntityDef,
    ) -> None:
        super().__init__(coordinator, entry, definition)
        self._attr_options = [str(feature) for feature in ControlFeature]
        self._attr_entity_category = definition.entity_category
        if definition.icon:
            self._attr_icon = definition.icon

    @property
    def current_option(self) -> str:
        return str(self.coordinator.selected_feature)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "status": str(self.coordinator.control_status),
            "commanded_power": self.coordinator.control_power,
        }

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_select_feature(ControlFeature(option))
