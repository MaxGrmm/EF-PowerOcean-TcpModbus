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
    ATTR_CHECK,
    ATTR_MODE,
    ATTR_STATUS,
    DOMAIN,
    EVENT_COMMAND_EXPIRED,
    EVENT_CONTROL_TEST_FINISHED,
    EVENT_CONTROL_TEST_STARTED,
    EVENT_READING_CHECK_CLEARED,
    EVENT_READING_CHECK_FAILED,
)

CONTROL_TEST_NAME = "Control test"
INVERTER_NAME = "Inverter"


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
            LOGBOOK_ENTRY_MESSAGE: (
                "returned to automatic because the "
                f"{event.data.get(ATTR_MODE)} action expired"
            ),
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

    @callback
    def async_describe_check_failed(event: Event) -> dict[str, Any]:
        status = str(event.data.get(ATTR_STATUS, "")).replace("_", " ")
        check = str(event.data.get(ATTR_CHECK, "")).replace("_", " ")
        return {
            LOGBOOK_ENTRY_NAME: INVERTER_NAME,
            LOGBOOK_ENTRY_MESSAGE: f"{status} (failed the {check} check)",
        }

    @callback
    def async_describe_check_cleared(event: Event) -> dict[str, Any]:
        status = str(event.data.get(ATTR_STATUS, "")).replace("_", " ")
        return {
            LOGBOOK_ENTRY_NAME: INVERTER_NAME,
            LOGBOOK_ENTRY_MESSAGE: f"no longer {status}",
        }

    async_describe_event(
        DOMAIN, EVENT_READING_CHECK_FAILED, async_describe_check_failed
    )
    async_describe_event(
        DOMAIN, EVENT_READING_CHECK_CLEARED, async_describe_check_cleared
    )
