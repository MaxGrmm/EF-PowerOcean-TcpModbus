"""Sensor entities for EcoFlow PowerOcean Plus."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import RestoreSensor
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import DeviceInfo, DeviceEntryType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    SensorDef,
    EnergySensorDef,
    SENSOR_MAP,
    ENERGY_SENSOR_MAP,
)
from .coordinator import EcoflowCoordinator

_LOGGER = logging.getLogger(__name__)


VALUE_PRECISION = {
    PERCENTAGE: 0,
    UnitOfPower.WATT: 0,
    UnitOfEnergy.KILO_WATT_HOUR: 2,
    UnitOfTemperature.CELSIUS: 1,
    UnitOfFrequency.HERTZ: 2,
    UnitOfElectricPotential.VOLT: 1,
    UnitOfElectricCurrent.AMPERE: 2,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EcoflowCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorDef | EnergySensorDef] = []

    for sensor in SENSOR_MAP:
        entities.append(EcoflowSensor(coordinator, entry, sensor))

    for sensor in ENERGY_SENSOR_MAP:
        entities.append(EcoflowSensor(coordinator, entry, sensor))

    async_add_entities(entities)


class EcoflowSensor(CoordinatorEntity[EcoflowCoordinator], RestoreSensor):
    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: SensorDef | EnergySensorDef,
    ) -> None:
        super().__init__(coordinator)
        self._definition = definition
        self._attr_unique_id = f"{entry.entry_id}_{self._definition.key}"
        self._attr_native_unit_of_measurement = self._definition.unit
        self._attr_device_class = self._definition.device_class
        self._attr_state_class = self._definition.state_class
        self._attr_translation_key = self._definition.key
        self._attr_has_entity_name = True
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="EcoFlow PowerOcean",
            manufacturer="EcoFlow",
            model="PowerOcean",
            serial_number=coordinator.serial_number,
            entry_type=DeviceEntryType.SERVICE,
        )
        self._restored_value: float | int | str | None = None
        self._last_written_value: float | int | str | None = None

        if self._definition.unit in VALUE_PRECISION:
            self._attr_suggested_display_precision = VALUE_PRECISION.get(
                self._definition.unit
            )

        if self._definition.entity_category == "diagnostic":
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        new_value = self.native_value
        if new_value != self._last_written_value:
            self._last_written_value = new_value
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore last known value when sensor is added."""
        await super().async_added_to_hass()
        if (
            last_value := await self.async_get_last_sensor_data()
        ) and last_value.native_value is not None:
            _LOGGER.debug(
                f"Restore Sensor '{self._definition.key}' with value: {last_value.native_value}"
            )
            self._restored_value = last_value.native_value
            self._last_written_value = self._restored_value

    @property
    def native_value(self) -> float | int | str | None:
        """Return the sensor value from coordinator, falling back to last value"""
        if self.coordinator.data is not None:
            value = self.coordinator.data.get(self._definition.key, None)
            if value is not None:
                if precision := VALUE_PRECISION.get(self._definition.unit, None):
                    return (
                        round(value, precision)
                        if precision > 0
                        else int(round(value, 0))
                    )
                else:
                    return int(value)
        return self._last_written_value
