"""Actions for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

from typing import Final

import voluptuous as vol
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import ATTR_MODE, CONTROL_FEATURES, DOMAIN
from .coordinator import EcoflowCoordinator
from .models import ControlFeature

SERVICE_SET_BATTERY_COMMAND: Final = "set_battery_command"
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


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's actions."""

    async def async_set_battery_command(call: ServiceCall) -> None:
        feature = ControlFeature(call.data[ATTR_MODE])
        power = call.data.get(ATTR_POWER)
        placeholders = {"mode": str(feature)}
        # Handle zero specifically, since 0 means no limit at all.
        if CONTROL_FEATURES[feature].has_power and not power:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="power_required",
                translation_placeholders=placeholders,
            )
        if not CONTROL_FEATURES[feature].has_power and power is not None:
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


def _coordinator_for(hass: HomeAssistant, device_id: str) -> EcoflowCoordinator:
    """Return the coordinator of a device this integration has set up."""
    coordinators = hass.data.get(DOMAIN, {})
    if device := dr.async_get(hass).async_get(device_id):
        for entry_id in device.config_entries:
            if entry_id in coordinators:
                return coordinators[entry_id]
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="unknown_device",
        translation_placeholders={"device_id": device_id},
    )
