"""Sensor base entity for EcoFlow PowerOcean Plus."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, BinarySensorDef, EnergySensorDef, SensorDef
from .coordinator import EcoflowCoordinator


class EcoFlowBaseEntity(CoordinatorEntity[EcoflowCoordinator]):
    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: SensorDef | EnergySensorDef | BinarySensorDef,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry.entry_id
        self._attr_has_entity_name = True
        self._definition = definition
        self._attr_unique_id = f"{self._entry_id}_{self._definition.key}"
        self._attr_translation_key = self._definition.key

    @property
    def device_info(self) -> DeviceInfo:
        """Return Home Assistant device info."""
        info = {
            "identifiers": {(DOMAIN, self._entry_id)},
            "name": "EcoFlow PowerOcean",
            "manufacturer": "EcoFlow",
            "model": self.coordinator.inverter_model.display_name,
            "serial_number": self.coordinator.serial_number,
            "entry_type": DeviceEntryType.SERVICE,
        }
        if self.coordinator.firmware_version:
            info["sw_version"] = self.coordinator.firmware_version

        return DeviceInfo(**info)

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.connected
