"""Binary sensor entities for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import EcoflowConfigEntry, EcoflowCoordinator
from .definitions import (
    BINARY_SENSOR_MAP,
    MODBUS_CONTROL_BINARY_SENSOR,
    READING_CHECK_BINARY_SENSOR,
)
from .entity import EcoFlowBaseEntity
from .powerocean.models import ControlStatus
from .powerocean.reading_checks import Finding

# The coordinator polls for every entity, so updates need no limit.
PARALLEL_UPDATES = 0


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
            EcoFlowReadingCheckBinarySensor(
                coordinator, entry, READING_CHECK_BINARY_SENSOR
            ),
            *(
                EcoFlowBinarySensor(coordinator, entry, description)
                for description in BINARY_SENSOR_MAP
            ),
        ]
    )


class EcoFlowModbusControlBinarySensor(EcoFlowBaseEntity, BinarySensorEntity):
    """Whether the inverter is currently following this integration, not the app.

    Commands are stored but ignored unless the heartbeat is current, and the device
    offers no read-back to prove it, so this is the one honest answer available.
    """

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        description: BinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, entry, description)

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        control = self.coordinator.control
        return control.in_control or control.handing_back


class EcoFlowReadingCheckBinarySensor(EcoFlowBaseEntity, BinarySensorEntity):
    """Whether a check shows readings to be wrong, which, since when and which.

    Only findings of wrong readings count: off-grid or a battery the battery saver
    turned off are the inverter's state, not a problem. Shown whether or not
    Modbus Control is on, where Control Status only shows a finding while it is.
    """

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        description: BinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, entry, description)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._async_write_if_changed(self.is_on, self.extra_state_attributes)

    def _findings(self) -> list[Finding]:
        return [
            finding
            for finding in self.coordinator.control.reading_checks.findings
            if finding.status is ControlStatus.INCONSISTENT_READINGS
        ]

    @property
    def is_on(self) -> bool:
        return bool(self._findings())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        findings = self._findings()
        first = findings[0] if findings else None
        return {
            "check": first.check if first else None,
            "since": first.since.isoformat() if first else None,
            "distrusted": sorted(set().union(*(f.keys for f in findings))),
        }


class EcoFlowBinarySensor(EcoFlowBaseEntity, BinarySensorEntity):
    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        description: BinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, entry, description)
        self._last_written_value: bool | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        self._last_written_value = self.is_on
        self._async_write_if_changed(self._last_written_value)

    @property
    def is_on(self) -> bool | None:
        value = (self.coordinator.data or {}).get(self.entity_description.key)
        return bool(value) if value is not None else self._last_written_value
