"""The config flow keys an inverter by its serial number."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import config_flow, const

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


async def test_the_serial_number_is_the_unique_id(
    hass: HomeAssistant, enable_custom_integrations: None, device_settings
) -> None:
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


async def test_falls_back_to_the_address_without_a_serial_number(
    hass: HomeAssistant, enable_custom_integrations: None, device_settings
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


async def test_an_inverter_that_moved_gets_its_new_address(
    hass: HomeAssistant, enable_custom_integrations: None, device_settings
) -> None:
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
