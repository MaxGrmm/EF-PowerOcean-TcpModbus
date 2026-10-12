"""Binary sensor entities for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BINARY_SENSOR_MAP, MODBUS_CONTROL_BINARY_SENSOR
from .coordinator import EcoflowConfigEntry, EcoflowCoordinator
from .entity import EcoFlowBaseEntity
from .models import BinarySensorDef


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 - required by the platform API
    entry: EcoflowConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow binary sensors from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            EcoFlowModbusControlBinarySensor(
                coordinator, entry, MODBUS_CONTROL_BINARY_SENSOR
            ),
            *(
                EcoFlowBinarySensor(coordinator, entry, definition)
                for definition in BINARY_SENSOR_MAP
            ),
        ]
    )


class EcoFlowModbusControlBinarySensor(
    EcoFlowBaseEntity[BinarySensorDef], BinarySensorEntity
):
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
        control = self.coordinator.control
        return control.in_control or control.handing_back


class EcoFlowBinarySensor(EcoFlowBaseEntity[BinarySensorDef], BinarySensorEntity):
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
        new_value = self.is_on
        if new_value != self._last_written_value:
            self._last_written_value = new_value
            self.async_write_ha_state()

    @property
    def is_on(self) -> bool | None:
        value = (self.coordinator.data or {}).get(self._definition.key)
        return bool(value) if value is not None else self._last_written_value
