"""The read plan follows the entities: a register is polled while one shows it."""

from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import const
from custom_components.ef_powerocean_tcpmodbus.coordinator import EcoflowCoordinator
from custom_components.ef_powerocean_tcpmodbus.entity import EcoFlowBaseEntity
from custom_components.ef_powerocean_tcpmodbus.models import (
    EnergySensorDef,
    SensorDef,
)
from custom_components.ef_powerocean_tcpmodbus.telemetry import TelemetryData


def test_control_reads_nothing_that_could_be_left_out_of_the_poll() -> None:
    """control.py names the data keys it reads; each must be one that is always polled."""
    source = Path(const.__file__).with_name("control.py").read_text(encoding="utf-8")
    read = set(
        re.findall(r"""(?:data|self\._data)\.get\(\s*"([a-z_0-9]+)"\s*[,)]""", source)
    )
    # Values the control loop keeps for itself, not ones taken from the poll.
    read -= {"mode", "power", "feature_power", "grid_feed_restore", "grid_feed_stopped"}
    read -= {
        "battery_saver",
        "charge_limit_soc",
        "battery_reserve_soc",
        "reserve_charge",
    }

    assert read
    assert read <= const.CONTROL_READ_KEYS
    assert not const.ON_DEMAND_REGISTER_KEYS & const.CONTROL_READ_KEYS


def test_on_demand_registers_are_those_only_an_entity_reads() -> None:
    on_demand = const.ON_DEMAND_REGISTER_KEYS

    assert on_demand
    assert on_demand <= set(const.REGISTERS_BY_KEY)
    assert not on_demand & {field.name for field in fields(TelemetryData)}
    assert not on_demand & {
        key
        for sensor in const.ENERGY_SENSOR_MAP
        for key in (sensor.key, sensor.total_source)
    }
    assert not on_demand & {f"fault_{n}" for n in range(1, const.MAX_FAULT_EVENTS + 1)}
    # Taken from the definitions, so a sensor added for a register joins them.
    assert {"frequency", "battery_voltage", "soc_battery_12"} <= on_demand
    assert {"battery_soc", "house_power", "solar_total"}.isdisjoint(on_demand)


@pytest.fixture
async def coordinator(hass: HomeAssistant) -> EcoflowCoordinator:
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        data={const.CONF_HOST: "127.0.0.1", const.CONF_PORT: 5020},
    )
    entry.add_to_hass(hass)
    return EcoflowCoordinator(hass, config_entry=entry)


def _all_registers(coordinator: EcoflowCoordinator) -> frozenset[str]:
    return frozenset(
        register.key
        for block in const.register_blocks_for(coordinator.inverter_model)
        for register in block.registers
    )


async def test_reads_everything_until_the_entities_have_said(coordinator) -> None:
    """The first poll runs before the entities exist, and they all need a value."""
    assert coordinator.polled_registers == _all_registers(coordinator)


async def test_polls_an_on_demand_register_while_an_entity_wants_it(
    coordinator,
) -> None:
    everything = _all_registers(coordinator)
    always = everything - const.ON_DEMAND_REGISTER_KEYS

    remove_voltage = coordinator.async_add_listener(
        lambda: None, frozenset({"battery_voltage", "house_power"})
    )
    assert coordinator.polled_registers == always | {"battery_voltage"}

    remove_soc = coordinator.async_add_listener(lambda: None, {"soc_battery_3"})
    assert coordinator.polled_registers == always | {"battery_voltage", "soc_battery_3"}

    remove_voltage()
    assert coordinator.polled_registers == always | {"soc_battery_3"}

    remove_soc()
    assert coordinator.polled_registers == always


async def test_a_listener_without_keys_wants_no_register(coordinator) -> None:
    """The entry's own listener, and a control entity, carry no register keys."""
    remove = coordinator.async_add_listener(lambda: None)
    assert coordinator.polled_registers == (
        _all_registers(coordinator) - const.ON_DEMAND_REGISTER_KEYS
    )
    remove()


async def test_a_register_wanted_after_the_first_poll_is_fetched_at_once(
    hass: HomeAssistant, coordinator
) -> None:
    """An entity enabled later would otherwise wait a whole interval for a value."""
    coordinator.async_request_refresh = AsyncMock()
    remove_first = coordinator.async_add_listener(lambda: None, frozenset())
    coordinator.data = {}

    remove_second = coordinator.async_add_listener(lambda: None, {"frequency"})
    await hass.async_block_till_done()

    coordinator.async_request_refresh.assert_awaited_once()
    remove_first()
    remove_second()


async def test_a_refused_register_stays_out_whatever_the_entities_want(
    coordinator,
) -> None:
    coordinator._unsupported_keys = frozenset({"breaker_capacity"})
    remove = coordinator.async_add_listener(
        lambda: None, {"breaker_capacity", "frequency"}
    )
    assert "frequency" in coordinator.polled_registers
    assert "breaker_capacity" not in coordinator.polled_registers
    remove()


@pytest.mark.parametrize(
    ("definition", "keys"),
    [
        (SensorDef("frequency"), {"frequency"}),
        (
            SensorDef("fault_count", attribute_keys=("system_state_2_hex",)),
            {"fault_count", "system_state_2_hex"},
        ),
        (
            EnergySensorDef("solar_today", total_source="solar_total"),
            {"solar_today", "solar_total"},
        ),
        (
            const.WRITABLE_NUMBERS_MAP[0],
            {const.WRITABLE_NUMBERS_MAP[0].key, "device_led_brightness"},
        ),
    ],
)
def test_an_entity_asks_for_the_keys_it_shows(definition, keys) -> None:
    entity = EcoFlowBaseEntity(
        SimpleNamespace(), SimpleNamespace(entry_id="entry"), definition
    )
    assert entity.coordinator_context == frozenset(keys)
