"""Switch entities for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

from typing import Any, Final

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .control import ControlManager
from .coordinator import EcoflowCoordinator
from .entity import EcoFlowBaseEntity
from .models import SwitchDef, requires_modbus_control

MODBUS_CONTROL_SWITCH: Final = SwitchDef(
    key="modbus_control",
    # Switching off only takes effect once the inverter's heartbeat window runs out.
    icon=lambda coordinator: (
        "mdi:timer-sand" if coordinator.control.handing_back else "mdi:remote"
    ),
    is_on=lambda coordinator: coordinator.control.enabled,
    turn=ControlManager.async_set_enabled,
    attributes=lambda coordinator: {
        "hands_back_at": coordinator.control.hands_back_at,
    },
)

BATTERY_SAVER_SWITCH: Final = SwitchDef(
    key="battery_saver_mode_control",
    icon="mdi:leaf",
    # The device's own bit only rises once it has gone idle, so it cannot confirm.
    is_on=lambda coordinator: coordinator.control.battery_saver_commanded,
    turn=ControlManager.async_set_battery_saver,
    attributes=lambda coordinator: {
        "device_reports_battery_saver": (coordinator.data or {}).get(
            "battery_saver_mode_ena"
        ),
        "commanded_word": f"0x{coordinator.control.command:08X}",
    },
)

GRID_FEED_SWITCH: Final = SwitchDef(
    key="grid_feed",
    icon="mdi:transmission-tower-export",
    is_on=lambda coordinator: coordinator.control.grid_feed_allowed(coordinator.data),
    turn=ControlManager.async_set_grid_feed,
    attributes=lambda coordinator: coordinator.control.grid_feed_restore_attributes,
    available=lambda coordinator: (
        coordinator.control.grid_feed_switchable
        and requires_modbus_control(coordinator.control.status)
    ),
    # The PowerOcean Plus and Three Phase both accept writes to 40537 and 40538
    # and act on them while Modbus control is on (issue #89).
)

RESERVE_CHARGE_SWITCH: Final = SwitchDef(
    key="battery_reserve_charge",
    icon="mdi:battery-arrow-up",
    # A Battery Reserve above the SOC then charges from the grid up to it, at the
    # Charge Battery power, as the app's backup reserve does.
    is_on=lambda coordinator: coordinator.control.reserve_charge,
    turn=ControlManager.async_set_reserve_charge,
    available=lambda coordinator: requires_modbus_control(coordinator.control.status),
)

SWITCHES: Final = (
    MODBUS_CONTROL_SWITCH,
    BATTERY_SAVER_SWITCH,
    GRID_FEED_SWITCH,
    RESERVE_CHARGE_SWITCH,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow switches from a config entry."""
    coordinator: EcoflowCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        EcoFlowSwitch(coordinator, entry, definition) for definition in SWITCHES
    )


class EcoFlowSwitch(EcoFlowBaseEntity, SwitchEntity):
    """A switch whose state and action come from its definition."""

    _definition: SwitchDef

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: SwitchDef,
    ) -> None:
        super().__init__(coordinator, entry, definition)
        self._attr_entity_category = definition.entity_category

    @property
    def icon(self) -> str:
        icon = self._definition.icon
        return icon(self.coordinator) if callable(icon) else icon

    @property
    def available(self) -> bool:
        rule = self._definition.available
        return super().available and (rule is None or rule(self.coordinator))

    @property
    def is_on(self) -> bool:
        return self._definition.is_on(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attributes = self._definition.attributes
        return attributes(self.coordinator) if attributes else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._definition.turn(self.coordinator.control, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._definition.turn(self.coordinator.control, False)
