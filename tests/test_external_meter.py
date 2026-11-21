"""Readings of an external EcoFlow Smart Meter are unavailable until it reports.

Without one the device reads 0 in every grid-side voltage and current, as on a
PowerOcean Plus on its built-in meter (#240), which is no reading. Once any of them
reads anything else, all of them show, 0 included.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import const, sensor
from custom_components.ef_powerocean_tcpmodbus.coordinator import EcoflowCoordinator
from custom_components.ef_powerocean_tcpmodbus.models import EXTERNAL_METER_KEYS


def _definition(key: str) -> Any:
    return next(definition for definition in const.SENSOR_MAP if definition.key == key)


def fake_coordinator(*, has_external_meter: bool) -> SimpleNamespace:
    return SimpleNamespace(
        data=dict.fromkeys(EXTERNAL_METER_KEYS, 0.0) | {"battery_soc": 80.0},
        last_update_success=True,
        connected=True,
        status=None,
        control=SimpleNamespace(
            status=None,
            reading_checks=SimpleNamespace(distrusted=frozenset()),
        ),
        has_external_meter=has_external_meter,
    )


def make(coordinator: SimpleNamespace, key: str) -> sensor.EcoflowSensor:
    entry = MockConfigEntry(domain=const.DOMAIN, entry_id="entry")
    return sensor.EcoflowSensor(
        cast(EcoflowCoordinator, coordinator), entry, _definition(key)
    )


@pytest.mark.parametrize("key", EXTERNAL_METER_KEYS)
async def test_meter_readings_without_a_meter_are_unavailable(
    hass,  # noqa: ARG001 - sets up the Home Assistant test harness
    key: str,
) -> None:
    entity = make(fake_coordinator(has_external_meter=False), key)

    assert not entity.available


@pytest.mark.parametrize("key", EXTERNAL_METER_KEYS)
async def test_meter_readings_show_even_at_zero(
    hass,  # noqa: ARG001 - sets up the Home Assistant test harness
    key: str,
) -> None:
    entity = make(fake_coordinator(has_external_meter=True), key)

    assert entity.available
    assert entity.native_value == 0


async def test_meter_readings_come_back_once_the_meter_reports(
    hass,  # noqa: ARG001 - sets up the Home Assistant test harness
) -> None:
    coordinator = fake_coordinator(has_external_meter=False)
    entity = make(coordinator, "grid_current_l1")

    coordinator.has_external_meter = True
    coordinator.data["grid_current_l1"] = 4.2

    assert entity.available
    assert entity.native_value == 4.2


async def test_a_reading_every_installation_measures_is_unaffected(
    hass,  # noqa: ARG001 - sets up the Home Assistant test harness
) -> None:
    entity = make(fake_coordinator(has_external_meter=False), "battery_soc")

    assert entity.available
