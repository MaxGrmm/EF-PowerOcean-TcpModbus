"""The Effective Battery Reserve shows what the control manager says holds."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import const, sensor
from custom_components.ef_powerocean_tcpmodbus.models import (
    DeviceIdentity,
    InverterModel,
    ReserveSource,
)


async def effective_reserve(hass, reserve_in_force):
    control = SimpleNamespace(
        reserve_native=False, reserve_in_force=reserve_in_force, status=None
    )
    coordinator = SimpleNamespace(
        control=control,
        data={},
        inverter_model=InverterModel.POWEROCEAN_THREE_PHASE,
        identity=DeviceIdentity(),
        limits={const.CONF_BATTERY_COUNT: 1},
        connected=True,
        last_update_success=True,
    )
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


async def test_the_sensor_shows_the_reserve_in_force(hass) -> None:
    entity = await effective_reserve(hass, (30.0, ReserveSource.BATTERY_RESERVE))

    assert entity.native_value == 30.0
    assert entity.extra_state_attributes == {"source": "battery_reserve"}


async def test_nothing_known_is_unknown_not_zero(hass) -> None:
    entity = await effective_reserve(hass, (None, None))

    assert entity.native_value is None
    assert entity.extra_state_attributes == {"source": None}
