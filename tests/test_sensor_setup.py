"""The Effective Battery Reserve: the reserve the battery is actually held at."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import const, sensor
from custom_components.ef_powerocean_tcpmodbus.models import (
    DeviceIdentity,
    InverterModel,
)


def fake_coordinator(
    *, app: float | None, reserve: float, native: bool = False, enabled: bool = True
) -> SimpleNamespace:
    control = SimpleNamespace(
        reserve_native=native,
        enabled=enabled,
        battery_reserve_soc=reserve,
        status=None,
    )
    return SimpleNamespace(
        control=control,
        data={const.BATTERY_RESERVE_REGISTER_KEY: app},
        inverter_model=InverterModel.POWEROCEAN_THREE_PHASE,
        identity=DeviceIdentity(),
        limits={const.CONF_BATTERY_COUNT: 1},
        connected=True,
        last_update_success=True,
    )


async def effective_reserve(hass, coordinator: SimpleNamespace):
    entry = MockConfigEntry(domain=const.DOMAIN, entry_id="entry")
    entry.add_to_hass(hass)
    entry.runtime_data = coordinator
    added: list = []
    await sensor.async_setup_entry(hass, entry, cast(AddEntitiesCallback, added.extend))
    return next(
        entity
        for entity in added
        if entity._definition.key == const.EFFECTIVE_RESERVE_SENSOR.key
    )


@pytest.mark.parametrize(
    ("app", "reserve", "enabled", "expected", "source"),
    (
        (10.0, 30.0, True, 30.0, "battery_reserve"),  # ours is higher and kept
        (40.0, 30.0, True, 40.0, "app"),  # the inverter still keeps the app's
        (10.0, 30.0, False, 10.0, "app"),  # ours is only kept under Modbus control
        (10.0, 0.0, True, 10.0, "app"),  # an unset Battery Reserve keeps nothing
    ),
)
async def test_the_higher_reserve_in_force_wins(
    hass, app, reserve, enabled, expected, source
) -> None:
    entity = await effective_reserve(
        hass, fake_coordinator(app=app, reserve=reserve, enabled=enabled)
    )

    assert entity.native_value == expected
    assert entity.extra_state_attributes["source"] == source


async def test_a_native_reserve_is_the_inverter_register(hass) -> None:
    entity = await effective_reserve(
        hass, fake_coordinator(app=25.0, reserve=25.0, native=True, enabled=False)
    )

    assert entity.native_value == 25.0


async def test_nothing_known_is_unknown_not_zero(hass) -> None:
    entity = await effective_reserve(
        hass, fake_coordinator(app=None, reserve=0.0, enabled=False)
    )

    assert entity.native_value is None


async def test_the_app_reserve_stays_polled(hass) -> None:
    entity = await effective_reserve(hass, fake_coordinator(app=10.0, reserve=0.0))

    assert const.BATTERY_RESERVE_REGISTER_KEY in entity.coordinator_context
