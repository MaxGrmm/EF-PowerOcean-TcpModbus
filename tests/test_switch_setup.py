"""The Charge to Battery Reserve switch, which only an emulated reserve has."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import const, switch

RESERVE_CHARGE_ID = f"entry_{switch.RESERVE_CHARGE_SWITCH.key}"


def fake_coordinator(
    *, native: bool, option: bool = False, forced: bool = False
) -> SimpleNamespace:
    control = SimpleNamespace(
        reserve_native=native,
        reserve_charge=option,
        reserve_charge_forced=forced,
    )
    return SimpleNamespace(control=control)


async def set_up(hass, coordinator: SimpleNamespace) -> list:
    entry = MockConfigEntry(domain=const.DOMAIN, entry_id="entry")
    entry.add_to_hass(hass)
    entry.runtime_data = coordinator
    added: list = []
    await switch.async_setup_entry(hass, entry, cast(AddEntitiesCallback, added.extend))
    return added


def reserve_charge(entities: list):
    return next(
        (
            entity
            for entity in entities
            if entity._definition is switch.RESERVE_CHARGE_SWITCH
        ),
        None,
    )


async def test_a_native_reserve_has_no_switch_and_drops_a_leftover_one(hass) -> None:
    registry = er.async_get(hass)
    registry.async_get_or_create("switch", const.DOMAIN, RESERVE_CHARGE_ID)

    entities = await set_up(hass, fake_coordinator(native=True))

    assert reserve_charge(entities) is None
    assert not registry.async_get_entity_id("switch", const.DOMAIN, RESERVE_CHARGE_ID)


async def test_an_emulated_reserve_keeps_its_switch(hass) -> None:
    registry = er.async_get(hass)
    registry.async_get_or_create("switch", const.DOMAIN, RESERVE_CHARGE_ID)

    entities = await set_up(hass, fake_coordinator(native=False))

    assert reserve_charge(entities) is not None
    assert registry.async_get_entity_id("switch", const.DOMAIN, RESERVE_CHARGE_ID)


async def test_the_switch_is_on_below_the_app_reserve_whatever_it_is_set_to(
    hass,
) -> None:
    off = reserve_charge(await set_up(hass, fake_coordinator(native=False)))
    assert off.is_on is False
    assert off.extra_state_attributes == {"set_by_app_backup_reserve": False}

    forced = reserve_charge(
        await set_up(hass, fake_coordinator(native=False, forced=True))
    )
    assert forced.is_on is True
    assert forced.extra_state_attributes == {"set_by_app_backup_reserve": True}
