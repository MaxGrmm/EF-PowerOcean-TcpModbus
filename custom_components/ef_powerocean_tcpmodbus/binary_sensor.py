"""Binary entities for EcoFlow PowerOcean Plus."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo, DeviceEntryType
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, BinarySensorDef, BINARY_SENSOR_MAP
from .coordinator import EcoflowCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow binary sensors from a config entry."""
    coordinator: EcoflowCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorDef] = []

    for definition in BINARY_SENSOR_MAP:
        entities.append(EcoFlowBinarySensor(coordinator, entry, definition))

    async_add_entities(entities)


class EcoFlowBinarySensor(CoordinatorEntity[EcoflowCoordinator], BinarySensorEntity):
    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: BinarySensorDef,
    ) -> None:
        super().__init__(coordinator)
        self._definition = definition
        self._attr_unique_id = f"{entry.entry_id}_{definition.key}"
        self._attr_translation_key = definition.key
        self._attr_has_entity_name = True
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="EcoFlow PowerOcean",
            manufacturer="EcoFlow",
            model="PowerOcean",
            serial_number=coordinator.serial_number,
            # sw_version=f"pymodbus: {coordinator.get_pymodbus_version()}",
            entry_type=DeviceEntryType.SERVICE,
        )

        self._last_written_value: bool | None = None

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
