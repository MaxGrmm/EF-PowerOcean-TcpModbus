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

from .const import (
    ATTR_MODE,
    DOMAIN,
    EVENT_COMMAND_EXPIRED,
    EVENT_CONTROL_TEST_FINISHED,
    EVENT_CONTROL_TEST_STARTED,
)

CONTROL_TEST_NAME = "Control test"


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, Any]]], None],
) -> None:
    """Describe the events this integration fires."""

    @callback
    def async_describe_command_expired(event: Event) -> dict[str, Any]:
        entity_id = event.data.get(ATTR_ENTITY_ID)
        state = hass.states.get(entity_id) if entity_id else None
        described = {
            LOGBOOK_ENTRY_NAME: state.name if state else "Battery Mode",
            LOGBOOK_ENTRY_MESSAGE: f"returned to automatic because the {event.data.get(ATTR_MODE)} action expired",
        }
        if entity_id:
            described[LOGBOOK_ENTRY_ENTITY_ID] = entity_id
        return described

    async_describe_event(DOMAIN, EVENT_COMMAND_EXPIRED, async_describe_command_expired)

    @callback
    def async_describe_control_test_started(event: Event) -> dict[str, Any]:
        return {
            LOGBOOK_ENTRY_NAME: CONTROL_TEST_NAME,
            LOGBOOK_ENTRY_MESSAGE: f"started at {event.data.get('power', 0):.0f} W",
        }

    @callback
    def async_describe_control_test_finished(event: Event) -> dict[str, Any]:
        data = event.data
        if data.get("outcome") != "done":
            message = f"{data.get('outcome')}: {data.get('abort_reason')}"
        else:
            counts: dict[str, int] = {}
            for verdict in (data.get("verdicts") or {}).values():
                counts[verdict] = counts.get(verdict, 0) + 1
            message = "finished: " + ", ".join(
                f"{count} {verdict.replace('_', ' ')}"
                for verdict, count in sorted(counts.items())
            )
        return {LOGBOOK_ENTRY_NAME: CONTROL_TEST_NAME, LOGBOOK_ENTRY_MESSAGE: message}

    async_describe_event(
        DOMAIN, EVENT_CONTROL_TEST_STARTED, async_describe_control_test_started
    )
    async_describe_event(
        DOMAIN, EVENT_CONTROL_TEST_FINISHED, async_describe_control_test_finished
    )
