"""The Battery Reserve number, native or emulated, and the number it replaced."""

from __future__ import annotations

from types import SimpleNamespace

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import const, number
from custom_components.ef_powerocean_tcpmodbus.models import InverterModel


def fake_coordinator(*, native: bool) -> SimpleNamespace:
    control = SimpleNamespace(
        reserve_native=native,
        battery_reserve_soc=20.0,
        charge_limit_soc=100.0,
        async_set_battery_reserve_soc=None,
        async_set_charge_limit_soc=None,
        feature_power=lambda feature: 0.0,
        feature_power_max=lambda feature: 0.0,
    )
    return SimpleNamespace(
        control=control, inverter_model=InverterModel.POWEROCEAN_THREE_PHASE
    )


async def set_up(hass, *, native: bool) -> list:
    entry = MockConfigEntry(domain=const.DOMAIN, entry_id="entry")
    entry.add_to_hass(hass)
    hass.data.setdefault(const.DOMAIN, {})[entry.entry_id] = fake_coordinator(
        native=native
    )
    added: list = []
    await number.async_setup_entry(hass, entry, added.extend)
    return added


def reserve(entities: list):
    return next(
        entity
        for entity in entities
        if entity._definition.key == const.BATTERY_RESERVE_SOC_NUMBER.key
    )


async def test_the_retired_minimum_soc_number_is_removed(hass) -> None:
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "number",
        const.DOMAIN,
        f"entry_{const.RETIRED_MIN_SOC_NUMBER_KEY}",
    )

    entities = await set_up(hass, native=False)

    assert not registry.async_get_entity_id(
        "number", const.DOMAIN, f"entry_{const.RETIRED_MIN_SOC_NUMBER_KEY}"
    )
    assert not any(
        entity._definition.key == const.RETIRED_MIN_SOC_NUMBER_KEY
        for entity in entities
    )


async def test_the_battery_reserve_says_whether_it_is_native(hass) -> None:
    emulated = reserve(await set_up(hass, native=False))
    assert emulated.extra_state_attributes == {"implementation": "emulated"}
    assert emulated._definition.availability is not None

    native = reserve(await set_up(hass, native=True))
    assert native.extra_state_attributes == {"implementation": "native"}
    # The inverter keeps it without us, so it needs no Modbus control.
    assert native._definition.availability is None
