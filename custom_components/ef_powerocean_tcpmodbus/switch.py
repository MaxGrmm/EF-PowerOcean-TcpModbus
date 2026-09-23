"""Switch entities for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BATTERY_SAVER_SWITCH, DOMAIN, GRID_FEED_SWITCH
from .coordinator import EcoflowCoordinator
from .entity import EcoFlowBaseEntity
from .models import GridFeedMode, SwitchDef

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow switches from a config entry."""
    coordinator: EcoflowCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            EcoFlowBatterySaverSwitch(coordinator, entry, BATTERY_SAVER_SWITCH),
            EcoFlowGridFeedSwitch(coordinator, entry, GRID_FEED_SWITCH),
        ]
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
        return self.coordinator.control.battery_saver_commanded

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "device_reports_battery_saver": data.get("battery_saver_mode_ena"),
            "commanded_word": f"0x{self.coordinator.control.command:08X}",
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.control.async_set_battery_saver(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.control.async_set_battery_saver(False)


class EcoFlowGridFeedSwitch(EcoFlowSwitch):
    """Whether the inverter may export to the grid.

    Turning it off caps the feed-in power at zero, which the inverter honours by
    curtailing the PV when the battery cannot take the surplus. Turning it back on
    writes the inverter's own last export setting, never a value of ours, so an
    inverter configured for a legal export limit keeps it.
    """

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.grid_feed_switchable

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        if data.get("grid_feed_mode") == GridFeedMode.UNLIMITED:
            return True
        return (data.get("feed_in_power_max") or 0) > 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        restore = self.coordinator.grid_feed_restore or {}
        return {
            "restores_feed_mode": GridFeedMode.from_register(restore.get("mode")),
            "restores_feed_in_power_max": restore.get("power"),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_grid_feed(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_grid_feed(False)
