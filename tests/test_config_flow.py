"""The config flow keys an inverter by its serial number."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import config_flow, const
from tests.fake_inverter import HOST, FakeInverter

SERIAL = "HJ31ZAS2TEST0001"


@pytest.fixture
def device_settings():
    """What the device reports during the flow, without a connection."""
    with patch.object(
        config_flow,
        "async_read_device_settings",
        new=AsyncMock(return_value={config_flow.DEVICE_SERIAL_NUMBER: SERIAL}),
    ) as read:
        yield read


async def _start(hass: HomeAssistant, host: str, port: int = 502):
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_HOST: host, const.CONF_PORT: port}
    )


@pytest.mark.usefixtures("enable_custom_integrations", "device_settings")
async def test_the_serial_number_is_the_unique_id(hass: HomeAssistant) -> None:
    result = await _start(hass, "192.168.1.10")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "parameters"

    with patch(
        "custom_components.ef_powerocean_tcpmodbus.async_setup_entry",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == SERIAL


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_falls_back_to_the_address_without_a_serial_number(
    hass: HomeAssistant, device_settings
) -> None:
    device_settings.return_value = {}

    result = await _start(hass, "192.168.1.10", 5020)
    with patch(
        "custom_components.ef_powerocean_tcpmodbus.async_setup_entry",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert result["result"].unique_id == "192.168.1.10:5020"


@pytest.mark.usefixtures("enable_custom_integrations", "device_settings")
async def test_an_inverter_that_moved_gets_its_new_address(hass: HomeAssistant) -> None:
    """Adding a known inverter at a new address updates the entry it already has."""
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        unique_id=SERIAL,
        data={const.CONF_HOST: "192.168.1.10", const.CONF_PORT: 502},
    )
    entry.add_to_hass(hass)

    result = await _start(hass, "192.168.1.20")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[const.CONF_HOST] == "192.168.1.20"


async def _change_options(
    hass: HomeAssistant, entry: MockConfigEntry, host: str, port: int = 502
):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {const.CONF_HOST: host, const.CONF_PORT: port}
    )


def _configured_entry(hass: HomeAssistant, **data: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        unique_id=SERIAL,
        data={const.CONF_HOST: "192.168.1.10", const.CONF_PORT: 502, **data},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_keep_the_address_without_testing_it_again(
    hass: HomeAssistant, device_settings
) -> None:
    entry = _configured_entry(hass)

    result = await _change_options(hass, entry, "192.168.1.10")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {const.CONF_SCAN_INTERVAL: 10}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    device_settings.assert_not_awaited()
    assert entry.data[const.CONF_SCAN_INTERVAL] == 10


@pytest.mark.usefixtures("enable_custom_integrations", "device_settings")
async def test_changing_the_settings_keeps_the_energy_corrections(
    hass: HomeAssistant,
) -> None:
    offsets = {"grid_export_total": 1041.23}
    entry = _configured_entry(hass, **{const.CONF_ENERGY_OFFSETS: offsets})

    result = await _change_options(hass, entry, "192.168.1.10")
    await hass.config_entries.options.async_configure(
        result["flow_id"], {const.CONF_SCAN_INTERVAL: 10}
    )

    assert entry.data[const.CONF_ENERGY_OFFSETS] == offsets


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_energy_totals_wait_for_the_inverter_to_report_them(
    hass: HomeAssistant,
) -> None:
    entry = _configured_entry(hass)

    result = await _change_options(hass, entry, "192.168.1.10")

    assert result["step_id"] == "parameters"
    assert config_flow.ENERGY_TOTALS not in result["data_schema"].schema


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_refuse_an_address_that_does_not_answer(
    hass: HomeAssistant, device_settings
) -> None:
    entry = _configured_entry(hass)
    device_settings.return_value = None

    result = await _change_options(hass, entry, "192.168.1.99")

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data[const.CONF_HOST] == "192.168.1.10"


@pytest.mark.usefixtures("enable_custom_integrations", "device_settings")
async def test_options_move_the_entry_to_an_address_that_answers(
    hass: HomeAssistant,
) -> None:
    entry = _configured_entry(hass)

    result = await _change_options(hass, entry, "192.168.1.99", 5020)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[const.CONF_HOST] == "192.168.1.99"
    assert entry.data[const.CONF_PORT] == 5020


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_inverter_fills_in_its_model_and_serial(
    hass: HomeAssistant, inverter: FakeInverter
) -> None:
    """Read over Modbus TCP from the fake inverter, with nothing patched."""
    inverter.serve_serial(SERIAL, const.SERIAL_NUMBER.address)
    inverter.registers[const.PRODUCT_NUMBER.address] = 3  # PowerOcean Plus
    inverter.registers[const.PRODUCT_CATEGORY.address] = 1  # three-phase

    result = await _start(hass, HOST, inverter.port)
    with patch(
        "custom_components.ef_powerocean_tcpmodbus.async_setup_entry",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == SERIAL
    assert result["data"][const.CONF_INVERTER_MODEL] == "powerocean_plus"


@pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")
async def test_an_address_nothing_answers_on_is_reported(hass: HomeAssistant) -> None:
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        unused_port = probe.getsockname()[1]

    result = await _start(hass, HOST, unused_port)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


def test_section_descriptions_are_plain_text_in_every_language() -> None:
    """The frontend shows a section's description as plain text, unlike a field's.

    A link or placeholder there shows up raw, so those belong in data_description.
    """
    component = Path(const.__file__).parent
    files = sorted(component.glob("translations/*.json"))
    assert files

    for path in files:
        steps = json.loads(path.read_text(encoding="utf-8"))["options"]["step"]
        for step in steps.values():
            for name, section in step.get("sections", {}).items():
                assert "](" not in section["description"], (path.name, name)
                assert "{" not in section["description"], (path.name, name)


def test_every_portal_total_names_its_portal_term_in_every_language() -> None:
    component = Path(const.__file__).parent
    files = sorted(component.glob("translations/*.json"))
    assert files

    for path in files:
        steps = json.loads(path.read_text(encoding="utf-8"))["options"]["step"]
        section = steps["parameters"]["sections"][config_flow.ENERGY_TOTALS]
        totals = set(section["data"]) - {config_flow.REMOVE_CORRECTIONS}
        assert totals == const.ECOFLOW_PORTAL_TOTALS, path.name
        assert set(section["data_description"]) >= totals, path.name
