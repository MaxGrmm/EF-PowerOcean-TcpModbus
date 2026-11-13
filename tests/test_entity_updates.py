"""Entities write their state when what they show changes, availability included.

Sensors, binary sensors and writable numbers skip the write when nothing they
show has changed. Whether they are available is part of that (#219): otherwise a
steady value, such as a full battery's level, stays unavailable after a failed
read until it next changes, and stays shown as current while reads fail.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import (
    binary_sensor,
    const,
    number,
    sensor,
)


def _definition(definitions: list[Any], key: str) -> Any:
    return next(definition for definition in definitions if definition.key == key)


MODES = _definition(const.SENSOR_MAP, "active_control_mode")
MODES_ATTRIBUTE = MODES.attribute_keys[0]


@dataclass(frozen=True)
class Kind:
    """One kind of entity, with a steady reading and the one it changes to."""

    make: Callable[[Any, Any], Any]
    key: str
    steady: Any
    steady_shown: Any
    changed: Any
    changed_shown: Any


KINDS = {
    "sensor": Kind(
        lambda coordinator, entry: sensor.EcoflowSensor(
            coordinator, entry, _definition(const.SENSOR_MAP, "battery_soc")
        ),
        key="battery_soc",
        steady=100.0,
        steady_shown=100,
        changed=99.0,
        changed_shown=99,
    ),
    "sensor_with_attributes": Kind(
        lambda coordinator, entry: sensor.EcoflowSensor(coordinator, entry, MODES),
        key=MODES.key,
        steady=MODES.options[0],
        steady_shown=MODES.options[0],
        changed=MODES.options[1],
        changed_shown=MODES.options[1],
    ),
    "binary_sensor": Kind(
        lambda coordinator, entry: binary_sensor.EcoFlowBinarySensor(
            coordinator,
            entry,
            _definition(const.BINARY_SENSOR_MAP, "self_use_mode_ena"),
        ),
        key="self_use_mode_ena",
        steady=1,
        steady_shown=True,
        changed=0,
        changed_shown=False,
    ),
    "number": Kind(
        lambda coordinator, entry: number.EcoFlowGenericNumber(
            coordinator, entry, const.WRITABLE_NUMBERS_MAP[0]
        ),
        key=const.WRITABLE_NUMBERS_MAP[0].read_key,
        steady=50.0,
        steady_shown=50.0,
        changed=60.0,
        changed_shown=60.0,
    ),
}
SENSORS = ("sensor", "sensor_with_attributes")

# What each write showed: whether available, and the steady or the changed value.
SHOWN, SHOWN_CHANGED = (True, "steady"), (True, "changed")
UNAVAILABLE, UNAVAILABLE_CHANGED = (False, "steady"), (False, "changed")

Scenario = tuple[tuple[str, ...], list[tuple[bool, str]]]

# Each step is "update", the coordinator telling its entities, or a change to the
# coordinator before one. The coordinator keeps its data when a read fails.
SCENARIOS: dict[str, Scenario] = {
    "first update writes, a repeat does not": (
        ("update", "update"),
        [SHOWN],
    ),
    "failed read, steady value": (
        ("update", "fail", "update", "update"),
        [SHOWN, UNAVAILABLE],
    ),
    "next read succeeds, steady value": (
        ("update", "fail", "update", "recover", "update", "update"),
        [SHOWN, UNAVAILABLE, SHOWN],
    ),
    "added after a failed read, holding its value (#219)": (
        ("fail", "hold", "update", "recover", "update"),
        [UNAVAILABLE, SHOWN],
    ),
    "added after a failed read, first update once reads succeed": (
        ("fail", "hold", "recover", "update"),
        [SHOWN],
    ),
    "connection lost and back, steady value": (
        ("update", "disconnect", "update", "reconnect", "update"),
        [SHOWN, UNAVAILABLE, SHOWN],
    ),
    "unavailable for two reasons, available once both clear": (
        (
            "update",
            "fail",
            "disconnect",
            "update",
            "recover",
            "update",
            "reconnect",
            "update",
        ),
        [SHOWN, UNAVAILABLE, SHOWN],
    ),
    "value changes": (
        ("update", "change", "update", "update"),
        [SHOWN, SHOWN_CHANGED],
    ),
    "value changes while reads fail": (
        ("update", "fail", "update", "change", "update", "recover", "update"),
        [SHOWN, UNAVAILABLE, UNAVAILABLE_CHANGED, SHOWN_CHANGED],
    ),
    "value changes in the read that succeeds": (
        ("update", "fail", "update", "change", "recover", "update"),
        [SHOWN, UNAVAILABLE, SHOWN_CHANGED],
    ),
    "poll leaves the value out": (
        ("update", "drop", "update"),
        [SHOWN],
    ),
    "poll leaves the value out, then reads fail": (
        ("update", "drop", "update", "fail", "update", "recover", "update"),
        [SHOWN, UNAVAILABLE, SHOWN],
    ),
}
SENSOR_SCENARIOS: dict[str, Scenario] = {
    "reading distrusted and trusted again, steady value (#200)": (
        ("update", "distrust", "update", "trust", "update"),
        [SHOWN, UNAVAILABLE, SHOWN],
    ),
}
ATTRIBUTE_SCENARIOS: dict[str, Scenario] = {
    "attribute changes, value steady": (
        ("update", "change_attribute", "update", "update"),
        [SHOWN, SHOWN],
    ),
    "attribute changes while reads fail": (
        ("update", "fail", "update", "change_attribute", "update", "recover", "update"),
        [SHOWN, UNAVAILABLE, UNAVAILABLE, SHOWN],
    ),
}


def _cases() -> list[Any]:
    groups: list[tuple[tuple[str, ...], dict[str, Scenario]]] = [
        (tuple(KINDS), SCENARIOS),
        (SENSORS, SENSOR_SCENARIOS),
        (("sensor_with_attributes",), ATTRIBUTE_SCENARIOS),
    ]
    return [
        pytest.param(kind, steps, expected, id=f"{kind}: {name}")
        for kinds, scenarios in groups
        for kind in kinds
        for name, (steps, expected) in scenarios.items()
    ]


def fake_coordinator(kind: Kind) -> SimpleNamespace:
    """A coordinator whose last read succeeded."""
    return SimpleNamespace(
        data={kind.key: kind.steady, MODES_ATTRIBUTE: "0x0001"},
        last_update_success=True,
        connected=True,
        status=None,
        control=SimpleNamespace(
            status=None,
            reading_checks=SimpleNamespace(distrusted=frozenset()),
        ),
    )


def _step(step: str, kind: Kind, coordinator: SimpleNamespace, entity: Any) -> None:
    checks = coordinator.control.reading_checks
    match step:
        case "update":
            entity._handle_coordinator_update()
        case "fail":
            coordinator.last_update_success = False
        case "recover":
            coordinator.last_update_success = True
        case "disconnect":
            coordinator.connected = False
        case "reconnect":
            coordinator.connected = True
        case "change":
            coordinator.data[kind.key] = kind.changed
        case "change_attribute":
            coordinator.data[MODES_ATTRIBUTE] = "0x0002"
        case "drop":
            del coordinator.data[kind.key]
        case "distrust":
            checks.distrusted = frozenset({kind.key})
        case "trust":
            checks.distrusted = frozenset()
        case "hold":
            # As added: a sensor restores its last state, a number takes the
            # coordinator's value, and Home Assistant writes it without us.
            entity._last_written_value = kind.steady_shown
        case _:
            raise AssertionError(step)


@pytest.mark.parametrize(("kind_name", "steps", "expected"), _cases())
async def test_writes_exactly_when_what_is_shown_changes(
    hass,  # noqa: ARG001 - sets up the Home Assistant test harness
    kind_name: str,
    steps: tuple[str, ...],
    expected: list[tuple[bool, str]],
) -> None:
    kind = KINDS[kind_name]
    coordinator = fake_coordinator(kind)
    entry = MockConfigEntry(domain=const.DOMAIN, entry_id="entry")
    entity = kind.make(coordinator, entry)
    shown = {kind.steady_shown: "steady", kind.changed_shown: "changed"}
    writes: list[tuple[bool, str]] = []

    def write() -> None:
        writes.append((entity.available, shown[entity._last_written_value]))

    with patch.object(entity, "async_write_ha_state", side_effect=write):
        for step in steps:
            _step(step, kind, coordinator, entity)

    assert writes == expected
