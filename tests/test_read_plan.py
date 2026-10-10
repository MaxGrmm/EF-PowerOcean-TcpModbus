"""A register is polled because something asks for it.

Entities ask for the keys they show, the control loop for the fields of
ControlInputs, and the coordinator for what its derived values and energy
counters are made from.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import const
from custom_components.ef_powerocean_tcpmodbus.control import ControlInputs
from custom_components.ef_powerocean_tcpmodbus.coordinator import EcoflowCoordinator
from custom_components.ef_powerocean_tcpmodbus.energy_processor import ENERGY_KEYS
from custom_components.ef_powerocean_tcpmodbus.entity import EcoFlowBaseEntity
from custom_components.ef_powerocean_tcpmodbus.models import (
    EnergySensorDef,
    SensorDef,
    shown_keys,
)
from custom_components.ef_powerocean_tcpmodbus.telemetry import TelemetryData

REGISTERS = frozenset(const.REGISTERS_BY_KEY)
OWN = (TelemetryData.keys(REGISTERS) | ENERGY_KEYS) & REGISTERS


def test_control_inputs_hold_every_key_the_features_read() -> None:
    """A feature looks its values up by key, which must be an input field."""
    keys = {
        key
        for definition in const.CONTROL_FEATURES.values()
        for key in (
            definition.measure_key,
            definition.limit_key,
            definition.capacity_key,
        )
        if key is not None
    } | {
        const.BATTERY_RESERVE_REGISTER_KEY,
        const.FEED_IN_POWER_MAX_KEY,
        const.FEED_IN_POWER_MAX_SETTING_KEY,
    }
    assert keys <= ControlInputs.keys()


def test_telemetry_asks_for_the_fault_registers() -> None:
    keys = TelemetryData.keys(REGISTERS)
    assert {"fault_1", "fault_20", "inverter_rated_power", "limit_inv_max"} <= keys
    assert "fault_count" not in keys
    assert "fault_codes" not in keys


def test_every_register_has_something_that_asks_for_it() -> None:
    """A register nothing asks for is never read, so it should not be mapped."""
    shown = frozenset().union(
        *(
            shown_keys(definition)
            for definition in (
                *const.SENSOR_MAP,
                *const.ENERGY_SENSOR_MAP,
                *const.DAILY_ENERGY_SENSORS_DEVICE_RAW,
                *const.BINARY_SENSOR_MAP,
                *const.WRITABLE_NUMBERS_MAP,
            )
        )
    )
    assert REGISTERS - OWN - ControlInputs.keys() - shown == set()


@pytest.fixture
async def coordinator(hass: HomeAssistant) -> EcoflowCoordinator:
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        data={const.CONF_HOST: "127.0.0.1", const.CONF_PORT: 5020},
    )
    entry.add_to_hass(hass)
    coordinator = EcoflowCoordinator(hass, config_entry=entry)
    coordinator.async_poll_on_demand()
    return coordinator


def _model_registers(coordinator: EcoflowCoordinator) -> frozenset[str]:
    return frozenset(
        register.key
        for block in const.register_blocks_for(coordinator.inverter_model)
        for register in block.registers
    )


async def test_reads_everything_until_the_entities_have_all_asked(
    hass: HomeAssistant,
) -> None:
    """A poll before the last entity is added would leave out what it shows."""
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        data={const.CONF_HOST: "127.0.0.1", const.CONF_PORT: 5020},
    )
    entry.add_to_hass(hass)
    coordinator = EcoflowCoordinator(hass, config_entry=entry)
    everything = _model_registers(coordinator)

    remove = coordinator.async_add_listener(lambda: None, {"frequency"})
    assert coordinator.polled_registers == everything

    coordinator.async_poll_on_demand()
    assert coordinator.polled_registers == OWN & everything | {"frequency"}
    remove()


async def test_without_entities_reads_what_the_coordinator_needs(coordinator) -> None:
    remove = coordinator.async_add_listener(lambda: None)
    assert coordinator.polled_registers == OWN & _model_registers(coordinator)
    remove()


async def test_the_control_loop_keeps_its_inputs_polled(coordinator) -> None:
    remove = coordinator.async_require(ControlInputs.keys())
    polled = coordinator.polled_registers
    assert ControlInputs.keys() & REGISTERS <= polled
    assert "frequency" not in polled
    remove()


async def test_polls_a_register_while_an_entity_shows_it(coordinator) -> None:
    base = OWN & _model_registers(coordinator)

    remove_voltage = coordinator.async_add_listener(
        lambda: None, frozenset({"battery_voltage", "house_power"})
    )
    assert coordinator.polled_registers == base | {"battery_voltage", "house_power"}

    remove_soc = coordinator.async_add_listener(lambda: None, {"soc_battery_3"})
    assert "soc_battery_3" in coordinator.polled_registers

    remove_voltage()
    assert "battery_voltage" not in coordinator.polled_registers
    assert "soc_battery_3" in coordinator.polled_registers

    remove_soc()
    assert coordinator.polled_registers == base


async def test_a_key_asked_for_after_the_first_poll_is_fetched_at_once(
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


async def test_a_refused_register_stays_out_whatever_is_asked(coordinator) -> None:
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
