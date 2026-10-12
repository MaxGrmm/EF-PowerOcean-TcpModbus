"""Base entity for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EcoflowCoordinator
from .models import ControlEntityDef, EntityDef, shown_keys


class EcoFlowBaseEntity[DefinitionT: EntityDef](CoordinatorEntity[EcoflowCoordinator]):
    """An entity of the inverter's device, made from one definition."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: DefinitionT,
    ) -> None:
        # The keys this entity shows, which the coordinator reads on demand.
        super().__init__(coordinator, context=shown_keys(definition))
        self._entry_id = entry.entry_id
        self._definition = definition
        self._attr_unique_id = f"{self._entry_id}_{definition.key}"
        self._attr_translation_key = definition.key

    @property
    def device_info(self) -> DeviceInfo:
        """Return Home Assistant device info."""
        identity = self.coordinator.identity
        info = DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name="EcoFlow PowerOcean",
            manufacturer="EcoFlow",
            model=self.coordinator.inverter_model.traits.display_name,
            serial_number=identity.serial_number,
        )
        if identity.firmware_version:
            info["sw_version"] = identity.firmware_version
        return info

    @property
    def available(self) -> bool:
        if not super().available or not self.coordinator.connected:
            return False
        # Controls that only act while the device follows us carry a rule.
        if (
            isinstance(self._definition, ControlEntityDef)
            and self._definition.availability is not None
        ):
            return self._definition.availability(self.coordinator.control.status)
        return True
