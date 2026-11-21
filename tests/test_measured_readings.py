"""Readings only some installations measure are unavailable until they read.

A PowerOcean Plus without an EcoFlow Smart Meter reads 0 in every grid-side
voltage and current (#240), which is no reading. Once any of them reads anything
else, the group shows, 0 included.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import const, sensor
from custom_components.ef_powerocean_tcpmodbus.coordinator import EcoflowCoordinator


def _definition(key: str) -> Any:
    return next(definition for definition in const.SENSOR_MAP if definition.key == key)


def fake_coordinator(measured: frozenset[str]) -> SimpleNamespace:
    coordinator = SimpleNamespace(
        data=dict.fromkeys(const.GRID_SIDE_KEYS, 0.0) | {"battery_soc": 80.0},
        last_update_success=True,
        connected=True,
        status=None,
        control=SimpleNamespace(
            status=None,
            reading_checks=SimpleNamespace(distrusted=frozenset()),
        ),
        _measured_keys=measured,
    )
    coordinator.measures = lambda keys: EcoflowCoordinator.measures(
        cast(EcoflowCoordinator, coordinator), keys
    )
    return coordinator


def make(coordinator: SimpleNamespace, key: str) -> sensor.EcoflowSensor:
    entry = MockConfigEntry(domain=const.DOMAIN, entry_id="entry")
    return sensor.EcoflowSensor(coordinator, entry, _definition(key))


@pytest.mark.parametrize("key", const.GRID_SIDE_KEYS)
async def test_a_grid_side_never_measured_is_unavailable(
    hass,  # noqa: ARG001 - sets up the Home Assistant test harness
    key: str,
) -> None:
    entity = make(fake_coordinator(frozenset()), key)

    assert not entity.available


@pytest.mark.parametrize("key", const.GRID_SIDE_KEYS)
async def test_a_measured_grid_side_shows_even_at_zero(
    hass,  # noqa: ARG001 - sets up the Home Assistant test harness
    key: str,
) -> None:
    entity = make(fake_coordinator(frozenset({"grid_voltage_l1"})), key)

    assert entity.available
    assert entity.native_value == 0


async def test_the_grid_side_comes_back_once_it_reads(
    hass,  # noqa: ARG001 - sets up the Home Assistant test harness
) -> None:
    coordinator = fake_coordinator(frozenset())
    entity = make(coordinator, "grid_current_l1")

    coordinator._measured_keys = frozenset({"grid_voltage_l3"})
    coordinator.data["grid_current_l1"] = 4.2

    assert entity.available
    assert entity.native_value == 4.2


async def test_a_reading_every_installation_measures_is_unaffected(
    hass,  # noqa: ARG001 - sets up the Home Assistant test harness
) -> None:
    entity = make(fake_coordinator(frozenset()), "battery_soc")

    assert entity.available
