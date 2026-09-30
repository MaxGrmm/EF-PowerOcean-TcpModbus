"""Tests for how a command timeout shows in the device's activity."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Event, HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.ef_powerocean_tcpmodbus.const import (
    ATTR_MODE,
    BATTERY_MODE_SELECT,
    DOMAIN,
    EVENT_COMMAND_EXPIRED,
)
from custom_components.ef_powerocean_tcpmodbus.logbook import async_describe_events
from custom_components.ef_powerocean_tcpmodbus.models import ControlFeature
from custom_components.ef_powerocean_tcpmodbus.select import (
    EcoFlowBatteryModeSelect,
)


async def test_a_timeout_is_recorded_as_the_cause_of_the_change_to_automatic(
    hass: HomeAssistant,
) -> None:
    """The mode change is written under the event's context, and only once."""
    control = SimpleNamespace(last_expiry=None)
    select = EcoFlowBatteryModeSelect(
        SimpleNamespace(control=control),
        MockConfigEntry(domain=DOMAIN),
        BATTERY_MODE_SELECT,
    )
    select.hass = hass
    select.entity_id = "select.ecoflow_powerocean_battery_mode"
    select.async_write_ha_state = Mock()
    events = async_capture_events(hass, EVENT_COMMAND_EXPIRED)

    select._handle_coordinator_update()
    control.last_expiry = (ControlFeature.CHARGE_BATTERY, dt_util.utcnow())
    select._handle_coordinator_update()
    select._handle_coordinator_update()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data[ATTR_MODE] == "charge_battery"
    assert select._context is events[0].context


def test_the_logbook_names_the_command_that_timed_out(hass: HomeAssistant) -> None:
    describers = {}
    async_describe_events(
        hass,
        lambda domain, event_type, describe: describers.update({event_type: describe}),
    )

    entry = describers[EVENT_COMMAND_EXPIRED](
        Event(
            EVENT_COMMAND_EXPIRED,
            {ATTR_ENTITY_ID: "select.battery_mode", ATTR_MODE: "charge_battery"},
        )
    )

    assert "charge_battery" in entry["message"]
    assert entry["entity_id"] == "select.battery_mode"
