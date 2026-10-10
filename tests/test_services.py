"""Tests for the set_battery_command action."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service import async_get_all_descriptions
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import services
from custom_components.ef_powerocean_tcpmodbus.const import DOMAIN
from custom_components.ef_powerocean_tcpmodbus.models import ControlFeature


@pytest.fixture
def inverter(hass: HomeAssistant) -> SimpleNamespace:
    """An inverter set up in the integration, with its control stubbed."""
    entry = MockConfigEntry(domain=DOMAIN, state=ConfigEntryState.LOADED)
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "HJ31")}
    )
    control = SimpleNamespace(enabled=True, async_set_command=AsyncMock())
    control_test = SimpleNamespace(
        async_run=AsyncMock(),
        async_start=Mock(),
        async_cancel=AsyncMock(),
    )
    entry.runtime_data = SimpleNamespace(control=control, control_test=control_test)
    services.async_setup_services(hass)
    return SimpleNamespace(
        device_id=device.id, control=control, control_test=control_test
    )


async def call(hass: HomeAssistant, **data) -> None:
    await hass.services.async_call(
        DOMAIN, services.SERVICE_SET_BATTERY_COMMAND, data, blocking=True
    )


async def test_setting_up_the_integration_offers_the_action(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """The fields shown in the UI must be the ones the action accepts."""
    assert await async_setup_component(hass, DOMAIN, {})

    description = (await async_get_all_descriptions(hass))[DOMAIN][
        services.SERVICE_SET_BATTERY_COMMAND
    ]

    assert set(description["fields"]) == {
        str(key) for key in services.SET_BATTERY_COMMAND_SCHEMA.schema
    }
    for name, schema in (
        (services.SERVICE_RUN_CONTROL_TEST, services.RUN_CONTROL_TEST_SCHEMA),
        (services.SERVICE_CANCEL_CONTROL_TEST, services.CANCEL_CONTROL_TEST_SCHEMA),
    ):
        fields = (await async_get_all_descriptions(hass))[DOMAIN][name]["fields"]
        assert set(fields) == {str(key) for key in schema.schema}


async def test_a_templated_command_reaches_the_inverter(
    hass: HomeAssistant, inverter: SimpleNamespace
) -> None:
    """Predbat fills in its templates as text, so the numbers arrive as strings."""
    await call(
        hass,
        device_id=inverter.device_id,
        mode="charge_battery",
        power="3000",
        charge_limit_soc="80",
        expire_in="900",
    )

    inverter.control.async_set_command.assert_awaited_once_with(
        ControlFeature.CHARGE_BATTERY,
        power=3000.0,
        charge_limit_soc=80.0,
        expire_in_s=900.0,
    )


@pytest.mark.parametrize(
    ("data", "error"),
    (
        ({"mode": "charge_battery"}, ServiceValidationError),
        # The inverter would read a zero setpoint as no limit.
        ({"mode": "charge_battery", "power": 0}, ServiceValidationError),
        ({"mode": "automatic", "power": 1000}, ServiceValidationError),
        ({"mode": "export_solar_first", "power": 0}, ServiceValidationError),
        ({"mode": "automatic", "device_id": ["one", "two"]}, vol.Invalid),
        ({"mode": "charge_battery", "power": 1000, "expire_in": 30}, vol.Invalid),
        (
            {"mode": "charge_battery", "power": 1000, "device_id": "not-an-inverter"},
            ServiceValidationError,
        ),
    ),
)
async def test_an_invalid_command_changes_nothing(
    hass: HomeAssistant, inverter: SimpleNamespace, data: dict, error: type
) -> None:
    with pytest.raises(error):
        await call(hass, **{"device_id": inverter.device_id, **data})

    inverter.control.async_set_command.assert_not_awaited()


async def test_export_solar_first_keeps_its_limit_when_the_power_is_left_out(
    hass: HomeAssistant, inverter: SimpleNamespace
) -> None:
    await call(
        hass, device_id=inverter.device_id, mode="export_solar_first", expire_in=3600
    )

    inverter.control.async_set_command.assert_awaited_once_with(
        ControlFeature.EXPORT_SOLAR_FIRST,
        power=None,
        charge_limit_soc=None,
        expire_in_s=3600.0,
    )


async def test_only_automatic_is_accepted_while_modbus_control_is_off(
    hass: HomeAssistant, inverter: SimpleNamespace
) -> None:
    """Limits are settings that apply without control, but a mode would be ignored."""
    inverter.control.enabled = False

    with pytest.raises(ServiceValidationError):
        await call(hass, device_id=inverter.device_id, mode="hold_battery")
    await call(
        hass, device_id=inverter.device_id, mode="automatic", charge_limit_soc=100
    )

    assert inverter.control.async_set_command.await_count == 1


async def test_the_control_test_needs_the_confirmation(
    hass: HomeAssistant, inverter: SimpleNamespace
) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            services.SERVICE_RUN_CONTROL_TEST,
            {"device_id": inverter.device_id, "confirm": False},
            blocking=True,
        )
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            services.SERVICE_RUN_CONTROL_TEST,
            {"device_id": inverter.device_id},
            blocking=True,
        )

    inverter.control_test.async_start.assert_not_called()
    inverter.control_test.async_run.assert_not_awaited()


async def test_the_control_test_returns_as_soon_as_it_has_started(
    hass: HomeAssistant, inverter: SimpleNamespace
) -> None:
    """The run outlives the call: a dropped connection must not look like a failure."""
    await hass.services.async_call(
        DOMAIN,
        services.SERVICE_RUN_CONTROL_TEST,
        {"device_id": inverter.device_id, "confirm": True, "power": "1000"},
        blocking=True,
    )

    inverter.control_test.async_start.assert_called_once_with(1000.0)
    inverter.control_test.async_run.assert_not_awaited()


async def test_a_control_test_power_out_of_range_is_refused(
    hass: HomeAssistant, inverter: SimpleNamespace
) -> None:
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            services.SERVICE_RUN_CONTROL_TEST,
            {"device_id": inverter.device_id, "confirm": True, "power": 5000},
            blocking=True,
        )


async def test_the_control_test_can_be_cancelled(
    hass: HomeAssistant, inverter: SimpleNamespace
) -> None:
    await hass.services.async_call(
        DOMAIN,
        services.SERVICE_CANCEL_CONTROL_TEST,
        {"device_id": inverter.device_id},
        blocking=True,
    )

    inverter.control_test.async_cancel.assert_awaited_once()


async def test_a_device_is_found_by_its_config_entry(
    hass: HomeAssistant, inverter: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """Without the deprecated DeviceEntry.config_entries on Home Assistant 2026.10."""
    await hass.services.async_call(
        DOMAIN,
        services.SERVICE_CANCEL_CONTROL_TEST,
        {"device_id": inverter.device_id},
        blocking=True,
    )

    inverter.control_test.async_cancel.assert_awaited_once()
    assert "config_entries" not in caplog.text
