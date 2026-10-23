"""The repairs flow that takes the inverter's value for long-held energy totals."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from string import Formatter
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.util.yaml import parse_yaml

from custom_components.ef_powerocean_tcpmodbus import const, models, repairs
from custom_components.ef_powerocean_tcpmodbus.repairs import (
    HeldTotalsRepairFlow,
    async_create_fix_flow,
)

BUG_FORM = Path(__file__).parents[1] / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml"
HELD = "- sensor.grid_export_total: 1548.73 kWh shown, 507.50 kWh on the inverter"


@pytest.fixture(autouse=True)
def device_and_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dr, "async_get", lambda _: None)
    monkeypatch.setattr(
        dr,
        "async_entries_for_config_entry",
        lambda *_: [SimpleNamespace(id="device")],
    )
    monkeypatch.setattr(
        repairs,
        "async_get_integration",
        AsyncMock(return_value=SimpleNamespace(version="2.9.1")),
    )


def _flow(coordinator: Any) -> HeldTotalsRepairFlow:
    flow = asyncio.run(
        async_create_fix_flow(None, "issue", {"entry_id": "entry"})  # type: ignore[arg-type]
    )
    assert isinstance(flow, HeldTotalsRepairFlow)
    entry = SimpleNamespace(domain=const.DOMAIN, runtime_data=coordinator)
    flow.hass = SimpleNamespace(  # type: ignore[assignment]
        config_entries=SimpleNamespace(async_get_entry=lambda _: entry)
    )
    flow.flow_id, flow.handler = "flow", const.DOMAIN
    return flow


def _held_coordinator() -> SimpleNamespace:
    return SimpleNamespace(
        long_held_totals={"grid_export_total": object()},
        describe_held_totals=lambda: HELD,
        async_take_device_totals=AsyncMock(return_value={"grid_export_total": 507.5}),
        inverter_model=models.InverterModel.POWEROCEAN_PLUS,
        identity=models.DeviceIdentity(firmware_version="V3.0.20.54"),
    )


def test_the_flow_shows_what_it_will_take_before_taking_it() -> None:
    coordinator = _held_coordinator()
    result = asyncio.run(_flow(coordinator).async_step_init())

    assert result["type"] == "form"
    assert result["step_id"] == "confirm"
    assert (result.get("description_placeholders") or {})["totals"] == HELD
    coordinator.async_take_device_totals.assert_not_called()


def test_the_flow_links_to_the_diagnostics_and_a_filled_in_bug_report() -> None:
    result = asyncio.run(_flow(_held_coordinator()).async_step_init())
    placeholders = result.get("description_placeholders") or {}

    url = urlsplit(placeholders["report_url"])
    fields = {key: values[0] for key, values in parse_qs(url.query).items()}

    assert placeholders["device_page"] == "/config/devices/device/device"
    assert f"{url.scheme}://{url.netloc}{url.path}" == const.BUG_REPORT_URL
    assert fields["template"] == "bug_report.yml"
    assert fields["device_type"] == "PowerOcean Plus"
    assert fields["integration_version"] == "2.9.1"
    assert fields["device_firmware"] == "V3.0.20.54"
    assert fields["ha_version"]
    assert HELD in fields["description"]
    assert "grid_export_total" in fields["title"]


def test_every_model_name_is_an_option_of_the_bug_form() -> None:
    form: Any = parse_yaml(BUG_FORM.read_text(encoding="utf-8"))
    fields = {block.get("id"): block for block in form["body"]}
    options = fields["device_type"]["attributes"]["options"]

    for model in models.InverterModel:
        assert model.traits.display_name in options
    for key in ("ha_version", "integration_version", "device_firmware", "steps"):
        assert key in fields


def test_confirming_takes_the_device_totals() -> None:
    coordinator = _held_coordinator()
    result = asyncio.run(_flow(coordinator).async_step_confirm({}))

    assert result["type"] == "create_entry"
    coordinator.async_take_device_totals.assert_awaited_once()


def test_the_flow_aborts_when_nothing_is_held_anymore() -> None:
    coordinator = _held_coordinator()
    coordinator.long_held_totals = {}
    result = asyncio.run(_flow(coordinator).async_step_init())

    assert result["type"] == "abort"
    assert result["reason"] == "not_held"


def test_the_issue_id_home_assistant_passes_in_is_not_a_confirmation() -> None:
    coordinator = _held_coordinator()
    result = asyncio.run(_flow(coordinator).async_step_init({"issue_id": "issue"}))

    assert result["type"] == "form"
    coordinator.async_take_device_totals.assert_not_called()


def test_every_language_uses_exactly_the_placeholders_the_flow_provides() -> None:
    component = Path(const.__file__).parent
    files = sorted(component.glob("translations/*.json"))
    assert files
    result = asyncio.run(_flow(_held_coordinator()).async_step_init())
    provided = set(result.get("description_placeholders") or {})

    for path in files:
        issue = json.loads(path.read_text(encoding="utf-8"))["issues"][
            "energy_totals_held"
        ]
        description = issue["fix_flow"]["step"]["confirm"]["description"]
        used = {name for _, name, _, _ in Formatter().parse(description) if name}
        assert used == provided, path.name
        assert {
            name for _, name, _, _ in Formatter().parse(issue["title"]) if name
        } == {"host"}, path.name
