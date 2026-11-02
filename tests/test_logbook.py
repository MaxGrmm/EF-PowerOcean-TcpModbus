"""The logbook entries for the events the integration fires."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.ef_powerocean_tcpmodbus import const, logbook


def _describe(
    hass: HomeAssistant, event_type: str, data: dict[str, Any]
) -> dict[str, Any]:
    described: dict[str, Any] = {}

    def register(domain: str, name: str, describer: Any) -> None:
        assert domain == const.DOMAIN
        described[name] = describer

    logbook.async_describe_events(hass, register)
    entry: dict[str, Any] = described[event_type](SimpleNamespace(data=data))
    return entry


def test_a_started_control_test_names_its_power(hass: HomeAssistant) -> None:
    entry = _describe(hass, const.EVENT_CONTROL_TEST_STARTED, {"power": 1499.6})

    assert entry == {"name": "Control test", "message": "started at 1500 W"}


def test_a_failed_reading_check_names_the_check(hass: HomeAssistant) -> None:
    entry = _describe(
        hass,
        const.EVENT_READING_CHECK_FAILED,
        {const.ATTR_STATUS: "battery_disconnected", const.ATTR_CHECK: "bms_state"},
    )

    assert entry == {
        "name": "Inverter",
        "message": "battery disconnected (failed the bms state check)",
    }


def test_a_cleared_reading_check_says_what_ended(hass: HomeAssistant) -> None:
    entry = _describe(
        hass, const.EVENT_READING_CHECK_CLEARED, {const.ATTR_STATUS: "off_grid"}
    )

    assert entry == {"name": "Inverter", "message": "no longer off grid"}
