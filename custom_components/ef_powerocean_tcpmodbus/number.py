"""Dynamic number entities configuration platform for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, WRITABLE_NUMBERS_MAP, NumberWritableDef
from .coordinator import EcoflowCoordinator
from .entity import EcoFlowBaseEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Automatically set up number entities from the WRITABLE_NUMBERS_MAP configuration list."""
    coordinator: EcoflowCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        EcoFlowGenericNumber(coordinator, entry, number_def)
        for number_def in WRITABLE_NUMBERS_MAP
    ]

    async_add_entities(entities)


class EcoFlowGenericNumber(EcoFlowBaseEntity, NumberEntity):
    """Generic configuration slider entity dynamically driven by NumberWritableDef specifications."""

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: NumberWritableDef,
    ) -> None:
        """Initialize the generic number slider entity."""
        super().__init__(coordinator, entry, definition)

        # Track the last written value to prevent redundant state updates
        self._last_written_value: float | None = None

        # Configure native Home Assistant number attributes
        self._attr_native_min_value = definition.min_value
        self._attr_native_max_value = definition.max_value
        self._attr_native_step = definition.step
        self._attr_native_unit_of_measurement = definition.unit
        self._attr_device_class = definition.device_class

        # Categorize writeable management controls into the diagnostic section of the UI
        self._attr_entity_category = EntityCategory.CONFIG

        if definition.icon:
            self._attr_icon = definition.icon

    async def async_added_to_hass(self) -> None:
        """Initialize the initial value from coordinator data when entity is added to Home Assistant."""
        await super().async_added_to_hass()
        # Pre-populate native value from current coordinator data state to prevent 0 on startup
        initial_value = self.native_value
        if initial_value is not None:
            self._last_written_value = initial_value

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator efficiently."""
        new_value = self.native_value
        if new_value != self._last_written_value:
            self._last_written_value = new_value
            self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """Dynamically fetch the active numerical value cached inside the coordinator data block by its read_key."""
        if self.coordinator.data is not None and isinstance(
            self.coordinator.data, dict
        ):
            # Read directly using the original sensor key defined in WRITABLE_NUMBERS_MAP
            val = self.coordinator.data.get(self._definition.read_key, None)
            if val is not None:
                return float(val)
        return self._last_written_value

    async def async_set_native_value(self, value: float) -> None:
        """Set new value asynchronously (overrides NumberEntity abstract method)."""
        await self.coordinator.async_write_modbus_register(
            entity_def=self._definition,
            value=int(value),
        )
