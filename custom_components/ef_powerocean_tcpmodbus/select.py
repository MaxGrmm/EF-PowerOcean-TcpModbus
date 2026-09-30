"""Select entities for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_MODE, BATTERY_MODE_SELECT, DOMAIN, EVENT_COMMAND_EXPIRED
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
        self._announced_expiry: tuple[ControlFeature, datetime] | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        expiry = self.coordinator.control.last_expiry
        if expiry is not None and expiry != self._announced_expiry:
            self._announced_expiry = expiry
            context = Context()
            self.hass.bus.async_fire(
                EVENT_COMMAND_EXPIRED,
                {
                    ATTR_ENTITY_ID: self.entity_id,
                    ATTR_DEVICE_ID: self.registry_entry.device_id
                    if self.registry_entry
                    else None,
                    ATTR_MODE: str(expiry[0]),
                },
                context=context,
            )
            # The logbook then shows the timeout as the cause of the mode change.
            self.async_set_context(context)
        super()._handle_coordinator_update()

    @property
    def current_option(self) -> str:
        return str(self.coordinator.control.selected_feature)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "status": str(self.coordinator.control.status),
            "commanded_power": self.coordinator.control.power,
        }

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.control.async_select_feature(ControlFeature(option))
