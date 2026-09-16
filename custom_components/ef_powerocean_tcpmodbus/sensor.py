"""Sensor entities for EcoFlow PowerOcean Plus."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Final

from homeassistant.components.sensor import RestoreSensor, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    BATTERY_SOC_KEYS,
    CONF_BATTERY_COUNT,
    CONTROL_STATUS_SENSOR,
    DAILY_ENERGY_SENSORS_DEVICE_RAW,
    DOMAIN,
    ENERGY_SENSOR_MAP,
    SENSOR_MAP,
    UNIT_OF_RATIO,
    WRITABLE_NUMBERS_MAP,
)
from .coordinator import EcoflowCoordinator
from .entity import EcoFlowBaseEntity
from .models import EnergySensorDef, SensorDef

_LOGGER = logging.getLogger(__name__)


VALUE_PRECISION: Final = {
    UNIT_OF_RATIO: 0,
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
    entities: list[EcoflowSensor] = []

    empty_battery_slots = set(
        BATTERY_SOC_KEYS[coordinator.limits[CONF_BATTERY_COUNT] :]
    )
    # A writable number already shows its register's value, so a read-only twin would
    # only be a second row saying the same thing.
    controlled_keys = {
        number_def.read_key
        for number_def in WRITABLE_NUMBERS_MAP
        if coordinator.inverter_model not in number_def.unsupported_models
    }

    for sensor in SENSOR_MAP:
        if sensor.key in empty_battery_slots or sensor.key in controlled_keys:
            continue
        entities.append(EcoflowSensor(coordinator, entry, sensor))

    for sensor in ENERGY_SENSOR_MAP:
        entities.append(EcoflowSensor(coordinator, entry, sensor))

    for sensor in DAILY_ENERGY_SENSORS_DEVICE_RAW:
        entities.append(EcoflowSensor(coordinator, entry, sensor))

    async_add_entities([*entities, EcoFlowControlStatusSensor(coordinator, entry)])


class EcoFlowControlStatusSensor(EcoFlowBaseEntity, SensorEntity):
    """Whether the selected mode is achieving anything, and why not if it isn't.

    The select says what was asked for; this says what the inverter is managing to
    do about it, which is not the same thing on a device that silently ignores a
    target the battery has no headroom for.
    """

    def __init__(self, coordinator: EcoflowCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, CONTROL_STATUS_SENSOR)
        self._attr_device_class = CONTROL_STATUS_SENSOR.device_class
        self._attr_options = list(CONTROL_STATUS_SENSOR.options or ())
        if CONTROL_STATUS_SENSOR.icon:
            self._attr_icon = CONTROL_STATUS_SENSOR.icon

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        return str(self.coordinator.control.status)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        control = self.coordinator.control
        return {
            "mode": str(control.selected_feature),
            "commanded_power": control.power,
            "control_method": str(control.method),
            "in_control": control.in_control,
        }


class EcoflowSensor(EcoFlowBaseEntity, RestoreSensor):
    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: SensorDef | EnergySensorDef,
    ) -> None:
        super().__init__(coordinator, entry, definition)
        self._attr_native_unit_of_measurement = self._definition.unit
        self._attr_device_class = self._definition.device_class
        self._attr_state_class = self._definition.state_class
        if options := getattr(self._definition, "options", None):
            self._attr_options = list(options)
        self._restored_value: datetime | float | int | str | None = None
        self._last_written_value: datetime | float | int | str | None = None

        if self._definition.unit in VALUE_PRECISION:
            self._attr_suggested_display_precision = VALUE_PRECISION.get(
                self._definition.unit
            )

        self._attr_entity_category = self._definition.entity_category

        if self._definition.icon:
            self._attr_icon = self._definition.icon

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
    def available(self) -> bool:
        """Keep coordinator diagnostics available when normal entities are not."""
        if self._definition.key == "coordinator_status":
            return self.coordinator.status is not None
        return super().available

    @property
    def native_value(self) -> datetime | float | int | str | None:
        """Return the sensor value from coordinator, falling back to last value"""
        if self._definition.key == "coordinator_status":
            return self.coordinator.status
        if self.coordinator.data is not None:
            value = self.coordinator.data.get(self._definition.key, None)
            if value is not None:
                if isinstance(value, datetime):
                    return value
                if isinstance(value, str):
                    return value
                if self._definition.device_class == "enum":
                    return str(value)
                if precision := VALUE_PRECISION.get(self._definition.unit, None):
                    return (
                        round(value, precision)
                        if precision > 0
                        else int(round(value, 0))
                    )
                else:
                    return int(value)
        return self._last_written_value
