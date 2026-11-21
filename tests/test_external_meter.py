"""The Grid-side sensors are disabled while no external meter has reported.

Without one the device reads 0 in all six, as on a PowerOcean Plus on its
built-in meter (#240). Home Assistant cannot say why a sensor is unavailable, so
they are disabled by the integration instead, and enabled once a meter reports.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import const, sensor
from custom_components.ef_powerocean_tcpmodbus.coordinator import (
    EcoflowConfigEntry,
    EcoflowCoordinator,
)

INTEGRATION = er.RegistryEntryDisabler.INTEGRATION
USER = er.RegistryEntryDisabler.USER


def _sync(
    hass: HomeAssistant, entry: MockConfigEntry, *, has_external_meter: bool
) -> None:
    coordinator = EcoflowCoordinator.__new__(EcoflowCoordinator)
    coordinator.hass = hass
    coordinator.config_entry = cast(EcoflowConfigEntry, entry)
    coordinator._has_external_meter = has_external_meter
    coordinator.async_sync_external_meter_entities()


def _register(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    key: str,
    disabled_by: er.RegistryEntryDisabler | None = None,
) -> str:
    return (
        er.async_get(hass)
        .async_get_or_create(
            "sensor",
            const.DOMAIN,
            f"{entry.entry_id}_{key}",
            config_entry=entry,
            disabled_by=disabled_by,
        )
        .entity_id
    )


def _disabled_by(
    hass: HomeAssistant, entity_id: str
) -> er.RegistryEntryDisabler | None:
    registry_entry = er.async_get(hass).async_get(entity_id)
    assert registry_entry is not None
    return registry_entry.disabled_by


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=const.DOMAIN, entry_id="entry")
    entry.add_to_hass(hass)
    return entry


async def test_meter_sensors_are_disabled_without_a_meter(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    meter = [_register(hass, entry, key) for key in const.EXTERNAL_METER_KEYS]
    other = _register(hass, entry, "battery_soc")

    _sync(hass, entry, has_external_meter=False)

    assert all(_disabled_by(hass, entity_id) is INTEGRATION for entity_id in meter)
    assert _disabled_by(hass, other) is None


async def test_meter_sensors_are_enabled_once_a_meter_reports(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    entity_id = _register(hass, entry, "grid_voltage_l1", INTEGRATION)

    _sync(hass, entry, has_external_meter=True)

    assert _disabled_by(hass, entity_id) is None


@pytest.mark.parametrize("has_external_meter", (False, True))
async def test_a_meter_sensor_the_user_disabled_stays_disabled(
    hass: HomeAssistant, entry: MockConfigEntry, has_external_meter: bool
) -> None:
    entity_id = _register(hass, entry, "grid_current_l2", USER)

    _sync(hass, entry, has_external_meter=has_external_meter)

    assert _disabled_by(hass, entity_id) is USER


@pytest.mark.parametrize("has_external_meter", (False, True))
async def test_a_new_meter_sensor_is_enabled_only_with_a_meter(
    entry: MockConfigEntry, has_external_meter: bool
) -> None:
    coordinator = SimpleNamespace(has_external_meter=has_external_meter)
    definition = next(
        definition
        for definition in const.SENSOR_MAP
        if definition.key == "grid_voltage_l2"
    )

    entity = sensor.EcoflowSensor(
        cast(EcoflowCoordinator, coordinator), entry, definition
    )

    assert entity.entity_registry_enabled_default is has_external_meter
