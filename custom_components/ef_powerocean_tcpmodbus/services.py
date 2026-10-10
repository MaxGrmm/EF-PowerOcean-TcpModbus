"""Actions for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

from typing import Final

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import ATTR_MODE, CONTROL_FEATURES, DOMAIN
from .control_test_core import DEFAULT_TEST_POWER_W, MAX_TEST_POWER_W, MIN_TEST_POWER_W
from .coordinator import EcoflowCoordinator
from .models import ControlFeature

SERVICE_SET_BATTERY_COMMAND: Final = "set_battery_command"
SERVICE_RUN_CONTROL_TEST: Final = "run_control_test"
SERVICE_CANCEL_CONTROL_TEST: Final = "cancel_control_test"
ATTR_CONFIRM: Final = "confirm"
ATTR_POWER: Final = "power"
ATTR_CHARGE_LIMIT_SOC: Final = "charge_limit_soc"
ATTR_EXPIRE_IN: Final = "expire_in"

# Numbers are coerced because external controllers such as Predbat send templated
# strings.
SET_BATTERY_COMMAND_SCHEMA: Final = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_MODE): vol.In([str(feature) for feature in ControlFeature]),
        vol.Optional(ATTR_POWER): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional(ATTR_CHARGE_LIMIT_SOC): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=100)
        ),
        vol.Optional(ATTR_EXPIRE_IN): vol.All(
            vol.Coerce(float), vol.Range(min=60, max=86_400)
        ),
    }
)


# The confirmation is a field of its own, and required, so the action is never
# started by leaving a default in place.
RUN_CONTROL_TEST_SCHEMA: Final = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_CONFIRM): cv.boolean,
        vol.Optional(ATTR_POWER, default=DEFAULT_TEST_POWER_W): vol.All(
            vol.Coerce(float), vol.Range(min=MIN_TEST_POWER_W, max=MAX_TEST_POWER_W)
        ),
    }
)

CANCEL_CONTROL_TEST_SCHEMA: Final = vol.Schema(
    {vol.Required(ATTR_DEVICE_ID): cv.string}
)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's actions."""

    async def async_set_battery_command(call: ServiceCall) -> None:
        feature = ControlFeature(call.data[ATTR_MODE])
        definition = CONTROL_FEATURES[feature]
        power = call.data.get(ATTR_POWER)
        placeholders = {"mode": str(feature)}
        # Handle zero specifically, since 0 means no limit at all. A mode whose power
        # is optional keeps the number's value when it is left out.
        if definition.has_power and (
            power == 0 or (power is None and not definition.power_optional)
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="power_required",
                translation_placeholders=placeholders,
            )
        if not definition.has_power and power is not None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="power_not_allowed",
                translation_placeholders=placeholders,
            )

        coordinator = _coordinator_for(hass, call.data[ATTR_DEVICE_ID])
        if feature is not ControlFeature.AUTOMATIC and not coordinator.control.enabled:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="modbus_control_off"
            )

        await coordinator.control.async_set_command(
            feature,
            power=power,
            charge_limit_soc=call.data.get(ATTR_CHARGE_LIMIT_SOC),
            expire_in_s=call.data.get(ATTR_EXPIRE_IN),
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_BATTERY_COMMAND,
        async_set_battery_command,
        schema=SET_BATTERY_COMMAND_SCHEMA,
    )

    async def async_run_control_test(call: ServiceCall) -> None:
        """Start the control test and return at once.

        The run takes about ten minutes. Waiting for it here would tie it to the
        caller: the app's connection drops when the phone locks, and Developer
        Tools then shows an error for a test that is in fact still running. The
        Control Test sensor follows the run, and a notification says when the
        report is ready.
        """
        if not call.data[ATTR_CONFIRM]:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="control_test_not_confirmed"
            )
        control_test = _coordinator_for(hass, call.data[ATTR_DEVICE_ID]).control_test
        control_test.async_start(call.data[ATTR_POWER])

    async def async_cancel_control_test(call: ServiceCall) -> None:
        await _coordinator_for(
            hass, call.data[ATTR_DEVICE_ID]
        ).control_test.async_cancel()

    hass.services.async_register(
        DOMAIN,
        SERVICE_RUN_CONTROL_TEST,
        async_run_control_test,
        schema=RUN_CONTROL_TEST_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CANCEL_CONTROL_TEST,
        async_cancel_control_test,
        schema=CANCEL_CONTROL_TEST_SCHEMA,
    )


def _entry_ids(device: dr.DeviceEntry) -> set[str]:
    """Return the config entries of a device.

    Home Assistant 2026.10 gives a device a single config_entry_id and deprecates
    config_entries, which older versions still need.
    """
    if hasattr(device, "config_entry_id"):
        return {device.config_entry_id} if device.config_entry_id else set()
    return set(device.config_entries)


def _coordinator_for(hass: HomeAssistant, device_id: str) -> EcoflowCoordinator:
    """Return the coordinator of a device this integration has set up."""
    if device := dr.async_get(hass).async_get(device_id):
        for entry_id in _entry_ids(device):
            entry = hass.config_entries.async_get_entry(entry_id)
            if (
                entry is not None
                and entry.domain == DOMAIN
                and entry.state is ConfigEntryState.LOADED
            ):
                return entry.runtime_data
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="unknown_device",
        translation_placeholders={"device_id": device_id},
    )
