"""Icons come from icons.json, keyed by translation key, not from the entities."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import const
from custom_components.ef_powerocean_tcpmodbus.models import InverterModel
from custom_components.ef_powerocean_tcpmodbus.switch import (
    BATTERY_SAVER_SWITCH,
    MODBUS_CONTROL_SWITCH,
)
from tests.fake_inverter import HOST, FakeInverter
from tests.test_startup import _enable_every_entity, _fill_registers

INTEGRATION = Path(const.__file__).parent
ICONS: dict[str, Any] = json.loads((INTEGRATION / "icons.json").read_text())


async def _entities(
    hass: HomeAssistant, inverter: FakeInverter, model: InverterModel
) -> list[tuple[str, Any]]:
    """Return every entity of a model, with control on, as (platform, entity)."""
    _fill_registers(inverter)
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        data={const.CONF_HOST: HOST, const.CONF_PORT: inverter.port},
        options={
            const.CONF_INVERTER_MODEL: model,
            const.CONF_MODBUS_CONTROL: True,
        },
    )
    entry.add_to_hass(hass)
    _enable_every_entity(hass, entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entities = [
        (platform.domain, entity)
        for platform in entity_platform.async_get_platforms(hass, const.DOMAIN)
        for entity in platform.entities.values()
    ]
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    return entities


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_icons_name_only_entities_that_exist(
    hass: HomeAssistant, inverter: FakeInverter
) -> None:
    """A key in icons.json that no entity has would be an icon that never shows."""
    keys: set[tuple[str, str]] = set()
    for model in InverterModel:
        for platform, entity in await _entities(hass, inverter, model):
            assert entity.translation_key, entity.entity_id
            keys.add((platform, entity.translation_key))
            # Only an override for a state on and off cannot show.
            assert entity.icon is None, entity.entity_id

    named = {
        (platform, key)
        for platform, entities in ICONS["entity"].items()
        for key in entities
    }
    assert named <= keys


def test_icons_name_every_action() -> None:
    services = load_yaml_dict(str(INTEGRATION / "services.yaml"))
    assert ICONS["services"].keys() == services.keys()


def test_battery_mode_icons_cover_its_options() -> None:
    translations = json.loads((INTEGRATION / "translations" / "en.json").read_text())
    options = translations["entity"]["select"]["battery_mode"]["state"]
    assert ICONS["entity"]["select"]["battery_mode"]["state"].keys() == options.keys()


def test_modbus_control_shows_an_hourglass_only_while_handing_back() -> None:
    """Off, but the inverter still obeys until its window runs out."""
    icon = MODBUS_CONTROL_SWITCH.icon
    assert icon is not None
    handing_back = SimpleNamespace(control=SimpleNamespace(handing_back=True))
    in_control = SimpleNamespace(control=SimpleNamespace(handing_back=False))
    assert icon(handing_back) == "mdi:timer-sand"  # type: ignore[arg-type]
    assert icon(in_control) is None  # type: ignore[arg-type]
    assert BATTERY_SAVER_SWITCH.icon is None
