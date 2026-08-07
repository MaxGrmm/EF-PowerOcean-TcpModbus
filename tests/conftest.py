"""Make the integration importable without installing Home Assistant.

Pytest loads this file before it imports the test modules. The coordinator imports
Home Assistant and pymodbus at module level, but to avoid including those dependencies
in the tests, we mock the least possible interface of them.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime


homeassistant = types.ModuleType("homeassistant")
homeassistant_util = types.ModuleType("homeassistant.util")
homeassistant_dt = types.ModuleType("homeassistant.util.dt")
homeassistant_dt.now = datetime.now
homeassistant_util.dt = homeassistant_dt

homeassistant_const = types.ModuleType("homeassistant.const")
homeassistant_const.Platform = type(
    "Platform", (), {"BINARY_SENSOR": "binary_sensor", "SENSOR": "sensor"}
)
homeassistant_core = types.ModuleType("homeassistant.core")
homeassistant_core.HomeAssistant = type("HomeAssistant", (), {})
homeassistant_config_entries = types.ModuleType("homeassistant.config_entries")
homeassistant_config_entries.ConfigEntry = type("ConfigEntry", (), {})
homeassistant_helpers = types.ModuleType("homeassistant.helpers")
homeassistant_update_coordinator = types.ModuleType(
    "homeassistant.helpers.update_coordinator"
)
homeassistant_update_coordinator.DataUpdateCoordinator = type(
    "DataUpdateCoordinator", (), {}
)
homeassistant_update_coordinator.UpdateFailed = type(
    "UpdateFailed", (Exception,), {}
)

pymodbus = types.ModuleType("pymodbus")
pymodbus.__version__ = "test"
pymodbus_client = types.ModuleType("pymodbus.client")
pymodbus_client.AsyncModbusTcpClient = type("AsyncModbusTcpClient", (), {})
pymodbus_exceptions = types.ModuleType("pymodbus.exceptions")
pymodbus_exceptions.ModbusException = type("ModbusException", (Exception,), {})

DEPENDENCY_STUBS = {
    "homeassistant": homeassistant,
    "homeassistant.util": homeassistant_util,
    "homeassistant.util.dt": homeassistant_dt,
    "homeassistant.const": homeassistant_const,
    "homeassistant.core": homeassistant_core,
    "homeassistant.config_entries": homeassistant_config_entries,
    "homeassistant.helpers": homeassistant_helpers,
    "homeassistant.helpers.update_coordinator": homeassistant_update_coordinator,
    "pymodbus": pymodbus,
    "pymodbus.client": pymodbus_client,
    "pymodbus.exceptions": pymodbus_exceptions,
}

for name, module in DEPENDENCY_STUBS.items():
    sys.modules.setdefault(name, module)
