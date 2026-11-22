"""Switch entities for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .control import ControlManager
from .coordinator import EcoflowConfigEntry, EcoflowCoordinator
from .entity import EcoFlowBaseEntity
from .powerocean.models import requires_modbus_control

# One write to the inverter at a time.
PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class EcoflowSwitchEntityDescription(SwitchEntityDescription):
    """A switch over a setting the control manager keeps."""

    is_on_fn: Callable[[EcoflowCoordinator], bool]
    turn_fn: Callable[[ControlManager, bool], Awaitable[None]]
    attributes_fn: Callable[[EcoflowCoordinator], dict[str, Any]] | None = None
    available_fn: Callable[[EcoflowCoordinator], bool] | None = None
    entity_category: EntityCategory | None = EntityCategory.CONFIG
    # An icon that overrides the one in icons.json while it returns one, for a
    # state the switch's on and off cannot show.
    icon_fn: Callable[[EcoflowCoordinator], str | None] | None = None


MODBUS_CONTROL_SWITCH: Final = EcoflowSwitchEntityDescription(
    key="modbus_control",
    # Switching off only takes effect once the inverter's heartbeat window runs out.
    icon_fn=lambda coordinator: (
        "mdi:timer-sand" if coordinator.control.handing_back else None
    ),
    is_on_fn=lambda coordinator: coordinator.control.enabled,
    turn_fn=ControlManager.async_set_enabled,
    attributes_fn=lambda coordinator: {
        "hands_back_at": coordinator.control.hands_back_at,
    },
)

BATTERY_SAVER_SWITCH: Final = EcoflowSwitchEntityDescription(
    key="battery_saver_mode_control",
    # The device's own bit only rises once it has gone idle, so it cannot confirm.
    is_on_fn=lambda coordinator: coordinator.control.battery_saver_commanded,
    turn_fn=ControlManager.async_set_battery_saver,
    attributes_fn=lambda coordinator: {
        "device_reports_battery_saver": (coordinator.data or {}).get(
            "battery_saver_mode_ena"
        ),
        "commanded_word": f"0x{coordinator.control.command:08X}",
    },
)

GRID_FEED_SWITCH: Final = EcoflowSwitchEntityDescription(
    key="grid_feed",
    is_on_fn=lambda coordinator: coordinator.control.grid_feed_allowed(
        coordinator.data
    ),
    turn_fn=ControlManager.async_set_grid_feed,
    attributes_fn=lambda coordinator: coordinator.control.grid_feed_restore_attributes,
    available_fn=lambda coordinator: (
        coordinator.control.grid_feed_switchable
        and requires_modbus_control(coordinator.control.status)
    ),
    # The PowerOcean Plus and Three Phase both accept writes to 40537 and 40538
    # and act on them while Modbus control is on (issue #89).
)

RESERVE_CHARGE_SWITCH: Final = EcoflowSwitchEntityDescription(
    key="battery_reserve_charge",
    # A Battery Reserve above the SOC then charges from the grid up to it, at the
    # Charge Battery power, as the app's backup reserve does. Below the app's own
    # reserve it is on anyway, since the inverter charges up to that by itself.
    is_on_fn=lambda coordinator: (
        coordinator.control.reserve_charge or coordinator.control.reserve_charge_forced
    ),
    turn_fn=ControlManager.async_set_reserve_charge,
    available_fn=lambda coordinator: requires_modbus_control(
        coordinator.control.status
    ),
    attributes_fn=lambda coordinator: {
        "set_by_app_backup_reserve": coordinator.control.reserve_charge_forced,
    },
)

SWITCHES: Final = (
    MODBUS_CONTROL_SWITCH,
    BATTERY_SAVER_SWITCH,
    GRID_FEED_SWITCH,
    RESERVE_CHARGE_SWITCH,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcoflowConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow switches from a config entry."""
    coordinator = entry.runtime_data
    native = coordinator.control.reserve_native

    async_add_entities(
        EcoFlowSwitch(coordinator, entry, definition)
        for definition in SWITCHES
        # A native reserve charges up to itself in the inverter.
        if not (definition is RESERVE_CHARGE_SWITCH and native)
    )
    # One left from when the reserve was emulated would stay as unavailable.
    registry = er.async_get(hass)
    if native and (
        stale := registry.async_get_entity_id(
            "switch", DOMAIN, f"{entry.entry_id}_{RESERVE_CHARGE_SWITCH.key}"
        )
    ):
        registry.async_remove(stale)


class EcoFlowSwitch(EcoFlowBaseEntity, SwitchEntity):
    """A switch whose state and action come from its description."""

    entity_description: EcoflowSwitchEntityDescription

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        description: EcoflowSwitchEntityDescription,
    ) -> None:
        super().__init__(coordinator, entry, description)

    @property
    def icon(self) -> str | None:
        """Return an override of the icon in icons.json, if the state needs one."""
        icon = self.entity_description.icon_fn
        return icon(self.coordinator) if icon is not None else None

    @property
    def available(self) -> bool:
        rule = self.entity_description.available_fn
        return super().available and (rule is None or rule(self.coordinator))

    @property
    def is_on(self) -> bool:
        return self.entity_description.is_on_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attributes = self.entity_description.attributes_fn
        return attributes(self.coordinator) if attributes else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.entity_description.turn_fn(self.coordinator.control, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.entity_description.turn_fn(self.coordinator.control, False)
