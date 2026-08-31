"""Make the integration importable without installing Home Assistant.

Pytest loads this file before it imports the test modules. The coordinator imports
Home Assistant and pymodbus at module level, but to avoid including those dependencies
in the tests, we mock the least possible interface of them.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime
from enum import StrEnum

homeassistant = types.ModuleType("homeassistant")
homeassistant_util = types.ModuleType("homeassistant.util")
homeassistant_dt = types.ModuleType("homeassistant.util.dt")
homeassistant_dt.now = datetime.now
homeassistant_util.dt = homeassistant_dt

homeassistant_const = types.ModuleType("homeassistant.const")
homeassistant_const.Platform = type(
    "Platform",
    (),
    {"BINARY_SENSOR": "binary_sensor", "SENSOR": "sensor", "NUMBER": "number"},
)
homeassistant_const.UnitOfRatio = StrEnum("UnitOfRatio", {"PERCENTAGE": "%"})
homeassistant_const.UnitOfElectricCurrent = StrEnum(
    "UnitOfElectricCurrent", {"AMPERE": "A"}
)
homeassistant_const.UnitOfElectricPotential = StrEnum(
    "UnitOfElectricPotential", {"VOLT": "V"}
)
homeassistant_const.UnitOfEnergy = StrEnum(
    "UnitOfEnergy", {"WATT_HOUR": "Wh", "KILO_WATT_HOUR": "kWh"}
)
homeassistant_const.UnitOfFrequency = StrEnum("UnitOfFrequency", {"HERTZ": "Hz"})
homeassistant_const.UnitOfPower = StrEnum("UnitOfPower", {"WATT": "W"})
homeassistant_const.UnitOfTemperature = StrEnum("UnitOfTemperature", {"CELSIUS": "°C"})
homeassistant_core = types.ModuleType("homeassistant.core")
homeassistant_core.HomeAssistant = type("HomeAssistant", (), {})
homeassistant_config_entries = types.ModuleType("homeassistant.config_entries")
homeassistant_config_entries.ConfigEntry = type("ConfigEntry", (), {})
homeassistant_components = types.ModuleType("homeassistant.components")
homeassistant_diagnostics = types.ModuleType("homeassistant.components.diagnostics")
homeassistant_diagnostics.async_redact_data = lambda data, _to_redact: data
homeassistant_exceptions = types.ModuleType("homeassistant.exceptions")
homeassistant_exceptions.HomeAssistantError = type(
    "HomeAssistantError", (Exception,), {}
)
homeassistant_persistent_notification = types.ModuleType(
    "homeassistant.components.persistent_notification"
)
homeassistant_persistent_notification.async_create = lambda *args, **kwargs: None
homeassistant_persistent_notification.async_dismiss = lambda *args, **kwargs: None
homeassistant_components.persistent_notification = homeassistant_persistent_notification
homeassistant_helpers = types.ModuleType("homeassistant.helpers")
homeassistant_translation = types.ModuleType("homeassistant.helpers.translation")
homeassistant_translation.async_get_translations = None
homeassistant_update_coordinator = types.ModuleType(
    "homeassistant.helpers.update_coordinator"
)
homeassistant_update_coordinator.DataUpdateCoordinator = type(
    "DataUpdateCoordinator", (), {}
)


class UpdateFailed(Exception):
    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


homeassistant_update_coordinator.UpdateFailed = UpdateFailed

homeassistant_storage = types.ModuleType("homeassistant.helpers.storage")


class _Store:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def async_load(self):
        return None

    async def async_save(self, *args, **kwargs) -> None:
        pass

    def async_delay_save(self, *args, **kwargs) -> None:
        pass


homeassistant_storage.Store = _Store


pymodbus = types.ModuleType("pymodbus")
pymodbus.__version__ = "test"
pymodbus_client = types.ModuleType("pymodbus.client")
pymodbus_client.AsyncModbusTcpClient = type("AsyncModbusTcpClient", (), {})
pymodbus_exceptions = types.ModuleType("pymodbus.exceptions")


class ModbusException(Exception):
    """Mirror pymodbus, which exposes the message as ``.string``."""

    def __init__(self, string: str = "") -> None:
        self.string = string
        super().__init__(string)


pymodbus_exceptions.ModbusException = ModbusException

DEPENDENCY_STUBS = {
    "homeassistant": homeassistant,
    "homeassistant.util": homeassistant_util,
    "homeassistant.util.dt": homeassistant_dt,
    "homeassistant.const": homeassistant_const,
    "homeassistant.core": homeassistant_core,
    "homeassistant.config_entries": homeassistant_config_entries,
    "homeassistant.components": homeassistant_components,
    "homeassistant.components.diagnostics": homeassistant_diagnostics,
    "homeassistant.components.persistent_notification": homeassistant_persistent_notification,
    "homeassistant.helpers": homeassistant_helpers,
    "homeassistant.helpers.translation": homeassistant_translation,
    "homeassistant.helpers.update_coordinator": homeassistant_update_coordinator,
    "homeassistant.helpers.storage": homeassistant_storage,
    "homeassistant.exceptions": homeassistant_exceptions,
    "pymodbus": pymodbus,
    "pymodbus.client": pymodbus_client,
    "pymodbus.exceptions": pymodbus_exceptions,
}

for name, module in DEPENDENCY_STUBS.items():
    sys.modules.setdefault(name, module)
