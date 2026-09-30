"""Describe EF-PowerOcean-TcpModbus logbook events."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Event, HomeAssistant, callback

from .const import ATTR_MODE, DOMAIN, EVENT_COMMAND_EXPIRED


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, Any]]], None],
) -> None:
    """Describe the events this integration fires."""

    @callback
    def async_describe_command_expired(event: Event) -> dict[str, Any]:
        entity_id = event.data[ATTR_ENTITY_ID]
        state = hass.states.get(entity_id)
        return {
            LOGBOOK_ENTRY_NAME: state.name if state else "Battery Mode",
            LOGBOOK_ENTRY_MESSAGE: (
                f"returned to automatic because the {event.data[ATTR_MODE]} "
                "command was not renewed within its timeout"
            ),
            LOGBOOK_ENTRY_ENTITY_ID: entity_id,
        }

    async_describe_event(DOMAIN, EVENT_COMMAND_EXPIRED, async_describe_command_expired)
