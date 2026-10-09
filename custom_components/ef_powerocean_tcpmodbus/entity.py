"""Sensor base entity for EcoFlow PowerOcean Plus."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EcoflowCoordinator
from .models import (
    BinarySensorDef,
    ControlEntityDef,
    EnergySensorDef,
    NumberWritableDef,
    SensorDef,
    SwitchDef,
)


def _shown_keys(definition: Any) -> frozenset[str]:
    """Return the data keys an entity made from *definition* shows."""
    keys = {definition.key}
    keys.update(getattr(definition, "attribute_keys", ()))
    if read_key := getattr(definition, "read_key", None):
        keys.add(read_key)
    if total_source := getattr(definition, "total_source", None):
        keys.add(total_source)
    return frozenset(keys)


class EcoFlowBaseEntity(CoordinatorEntity[EcoflowCoordinator]):
    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: SensorDef
        | EnergySensorDef
        | BinarySensorDef
        | NumberWritableDef
        | ControlEntityDef
        | SwitchDef,
    ) -> None:
        # The keys this entity shows, which the coordinator reads on demand.
        super().__init__(coordinator, context=_shown_keys(definition))
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
            "model": self.coordinator.inverter_model.traits.display_name,
            "serial_number": self.coordinator.identity.serial_number,
        }
        if self.coordinator.identity.firmware_version:
            info["sw_version"] = self.coordinator.identity.firmware_version

        return DeviceInfo(**info)

    @property
    def available(self) -> bool:
        if not super().available or not self.coordinator.connected:
            return False

        # Controls that only act while the device follows us carry a rule; the
        # rest of the entities have none and stay available.
        availability = getattr(self._definition, "availability", None)
        if availability is not None:
            return availability(self.coordinator.control.status)

        return True
