"""Binary entities for EcoFlow PowerOcean Plus."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BINARY_SENSOR_MAP, DOMAIN, MODBUS_CONTROL_BINARY_SENSOR
from .coordinator import EcoflowCoordinator
from .entity import EcoFlowBaseEntity
from .models import BinarySensorDef


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow binary sensors from a config entry."""
    coordinator: EcoflowCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = [
        EcoFlowModbusControlBinarySensor(
            coordinator, entry, MODBUS_CONTROL_BINARY_SENSOR
        )
    ]

    for definition in BINARY_SENSOR_MAP:
        entities.append(EcoFlowBinarySensor(coordinator, entry, definition))

    async_add_entities(entities)


class EcoFlowModbusControlBinarySensor(EcoFlowBaseEntity, BinarySensorEntity):
    """Whether the inverter is currently following this integration, not the app.

    Commands are stored but ignored unless the heartbeat is current, and the device
    offers no read-back to prove it, so this is the one honest answer available.
    """

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: BinarySensorDef,
    ) -> None:
        super().__init__(coordinator, entry, definition)
        self._attr_device_class = definition.device_class
        self._attr_entity_category = definition.entity_category

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.in_control


class EcoFlowBinarySensor(EcoFlowBaseEntity, BinarySensorEntity):
    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: BinarySensorDef,
    ) -> None:
        super().__init__(coordinator, entry, definition)
        self._last_written_value: bool | None = None
        self._attr_device_class = definition.device_class
        self._attr_entity_category = definition.entity_category

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        new_value = self.is_on
        if new_value != self._last_written_value:
            self._last_written_value = new_value
            self.async_write_ha_state()

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        if self.coordinator.data is not None:
            value = self.coordinator.data.get(self._definition.key, None)

            if value is not None:
                return bool(value)

        return self._last_written_value
