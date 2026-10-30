"""Sensor entities for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
)
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
from homeassistant.helpers.typing import StateType

from .const import (
    BATTERY_RESERVE_REGISTER_KEY,
    BATTERY_SOC_KEYS,
    CONF_BATTERY_COUNT,
    CONTROL_STATUS_SENSOR,
    CONTROL_TEST_SENSOR,
    DAILY_ENERGY_SENSORS_DEVICE_RAW,
    EFFECTIVE_RESERVE_SENSOR,
    ENERGY_SENSOR_MAP,
    FAULT_KEYS,
    SENSOR_MAP,
    TOTAL_ENERGY_SENSORS_DEVICE_RAW,
    UNIT_OF_RATIO,
    WRITABLE_NUMBERS_MAP,
)
from .coordinator import EcoflowConfigEntry, EcoflowCoordinator
from .entity import EcoFlowBaseEntity
from .fault_names import async_get_fault_names
from .models import EnergySensorDef, SensorDef
from .telemetry import active_fault_codes

type SensorValue = StateType | date | datetime | Decimal

_LOGGER = logging.getLogger(__name__)

ACTIVE_FAULTS_KEY: Final = "active_faults"


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
    hass: HomeAssistant,  # noqa: ARG001 - required by the platform API
    entry: EcoflowConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
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
    # A native Battery Reserve is that register, so the App Backup Reserve would
    # repeat it.
    if coordinator.control.reserve_native:
        controlled_keys.add(BATTERY_RESERVE_REGISTER_KEY)

    for sensor in SENSOR_MAP:
        if (
            sensor.key in empty_battery_slots
            or sensor.key in controlled_keys
            or coordinator.inverter_model in sensor.unsupported_models
        ):
            continue
        entity_class = (
            EcoFlowActiveFaultsSensor
            if sensor.key == ACTIVE_FAULTS_KEY
            else EcoflowSensor
        )
        entities.append(entity_class(coordinator, entry, sensor))

    energy_sensors: list[SensorDef | EnergySensorDef] = [
        *ENERGY_SENSOR_MAP,
        *DAILY_ENERGY_SENSORS_DEVICE_RAW,
        *TOTAL_ENERGY_SENSORS_DEVICE_RAW,
    ]
    entities.extend(
        EcoflowSensor(coordinator, entry, sensor) for sensor in energy_sensors
    )

    async_add_entities(
        [
            *entities,
            EcoFlowControlStatusSensor(coordinator, entry),
            EcoFlowEffectiveReserveSensor(coordinator, entry),
            EcoFlowControlTestSensor(coordinator, entry),
        ]
    )


class EcoFlowControlStatusSensor(EcoFlowBaseEntity[SensorDef], SensorEntity):
    """Whether the selected mode is achieving anything, and why not if it isn't.

    The select says what was asked for; this says what the inverter is managing to
    do about it, which is not the same thing on a device that silently ignores a
    target the battery has no headroom for.
    """

    # Don't record these, since they change every time and it fills up the logbook
    _unrecorded_attributes = frozenset({"expires_at"})

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
            "guard": str(guard) if (guard := control.active_guard) else None,
            "commanded_power": control.power,
            "control_method": str(control.method),
            "in_control": control.in_control,
            "expires_at": control.expires_at,
        }


class EcoFlowEffectiveReserveSensor(EcoFlowBaseEntity[SensorDef], SensorEntity):
    """The state of charge the battery is held at, whoever sets it.

    The inverter keeps the app's backup reserve whatever runs it. A native Battery
    Reserve is that same register; an emulated one is kept by the control manager
    on top of it, but only while Modbus control is on.
    """

    def __init__(self, coordinator: EcoflowCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, EFFECTIVE_RESERVE_SENSOR)
        self._attr_native_unit_of_measurement = EFFECTIVE_RESERVE_SENSOR.unit
        self._attr_state_class = EFFECTIVE_RESERVE_SENSOR.state_class
        self._attr_suggested_display_precision = 0
        if EFFECTIVE_RESERVE_SENSOR.icon:
            self._attr_icon = EFFECTIVE_RESERVE_SENSOR.icon

    def _reserves(self) -> tuple[float | None, float | None]:
        """Return the app's backup reserve and the Battery Reserve in force."""
        control = self.coordinator.control
        app = (self.coordinator.data or {}).get(BATTERY_RESERVE_REGISTER_KEY)
        app_reserve = None if app is None else float(app)
        if control.reserve_native:
            # The Battery Reserve is the app's register, so both say the same.
            return app_reserve, app_reserve
        if control.enabled and control.battery_reserve_soc > 0:
            return app_reserve, control.battery_reserve_soc
        return app_reserve, None

    @property
    def native_value(self) -> float | None:
        reserves = [value for value in self._reserves() if value is not None]
        return max(reserves) if reserves else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        app_reserve, battery_reserve = self._reserves()
        source: str | None
        if battery_reserve is not None and (
            app_reserve is None or battery_reserve > app_reserve
        ):
            source = "battery_reserve"
        else:
            source = "app" if app_reserve is not None else None
        return {"source": source}


class EcoFlowControlTestSensor(EcoFlowBaseEntity[SensorDef], SensorEntity):
    """Follows the control test the run_control_test action starts."""

    def __init__(self, coordinator: EcoflowCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, CONTROL_TEST_SENSOR)
        self._attr_device_class = CONTROL_TEST_SENSOR.device_class
        self._attr_entity_category = CONTROL_TEST_SENSOR.entity_category
        self._attr_options = list(CONTROL_TEST_SENSOR.options or ())
        if CONTROL_TEST_SENSOR.icon:
            self._attr_icon = CONTROL_TEST_SENSOR.icon

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        return str(self.coordinator.control_test.state)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.control_test.attributes


class EcoflowSensor(EcoFlowBaseEntity[SensorDef | EnergySensorDef], RestoreSensor):
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
        if isinstance(definition, SensorDef) and definition.options:
            self._attr_options = list(definition.options)
        self._last_written_value: SensorValue = None
        self._last_written_attributes: dict[str, Any] | None = None

        if definition.unit is not None:
            self._attr_suggested_display_precision = VALUE_PRECISION.get(
                definition.unit
            )

        self._attr_entity_category = self._definition.entity_category

        if self._definition.icon:
            self._attr_icon = self._definition.icon

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        new_value = self.native_value
        new_attributes = self.extra_state_attributes
        if (
            new_value != self._last_written_value
            or new_attributes != self._last_written_attributes
        ):
            self._last_written_value = new_value
            self._last_written_attributes = new_attributes
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (
            last_value := await self.async_get_last_sensor_data()
        ) and last_value.native_value is not None:
            _LOGGER.debug(
                "Restore sensor %s with value %s",
                self._definition.key,
                last_value.native_value,
            )
            self._last_written_value = last_value.native_value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Publish the data keys the definition names, such as a raw word."""
        if not isinstance(self._definition, SensorDef):
            return None
        keys = self._definition.attribute_keys
        if not keys:
            return None
        data = self.coordinator.data or {}
        return {key: data.get(key) for key in keys}

    @property
    def available(self) -> bool:
        """Keep coordinator diagnostics available when normal entities are not."""
        if self._definition.key == "coordinator_status":
            return self.coordinator.status is not None
        return super().available

    @property
    def native_value(self) -> SensorValue:
        """Return the value from the coordinator, else the last one shown."""
        if self._definition.key == "coordinator_status":
            return self.coordinator.status
        value = (self.coordinator.data or {}).get(self._definition.key)
        if value is None:
            return self._last_written_value
        if isinstance(value, datetime | str):
            return value
        if self._definition.device_class is SensorDeviceClass.ENUM:
            return str(value)
        number = float(value)
        unit = self._definition.unit
        precision = VALUE_PRECISION.get(unit) if unit is not None else None
        if precision is None:
            return int(number)
        return round(number, precision) if precision else round(number)


class EcoFlowActiveFaultsSensor(EcoflowSensor):
    """The fault codes the device reports, each named where the code is known."""

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: SensorDef | EnergySensorDef,
    ) -> None:
        super().__init__(coordinator, entry, definition)
        self._fault_names: dict[int, str] = {}

    async def async_added_to_hass(self) -> None:
        self._fault_names = await async_get_fault_names(self.hass)
        await super().async_added_to_hass()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Name each active code, or None for a code nobody has named yet."""
        data = self.coordinator.data or {}
        codes = active_fault_codes(data.get(key) for key in FAULT_KEYS)
        return {
            "faults": [
                {"code": code, "name": self._fault_names.get(code)} for code in codes
            ]
        }
