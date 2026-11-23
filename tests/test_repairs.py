"""The repairs flow that takes the inverter's value for long-held energy totals."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from string import Formatter
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.util.yaml import parse_yaml

from custom_components.ef_powerocean_tcpmodbus import const, repairs
from custom_components.ef_powerocean_tcpmodbus.health import watchdog
from custom_components.ef_powerocean_tcpmodbus.powerocean import models as device_models
from custom_components.ef_powerocean_tcpmodbus.repairs import (
    AlertRepairFlow,
    GridEnergyRepairFlow,
    HeldTotalsRepairFlow,
    async_create_fix_flow,
)

BUG_FORM = Path(__file__).parents[1] / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml"
HELD = (
    "- sensor.grid_export_total: 1548.73 kWh in Home Assistant, "
    "507.50 kWh on the inverter"
)


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


def _flow(coordinator: Any, issue_id: str = "issue") -> Any:
    flow = asyncio.run(
        async_create_fix_flow(None, issue_id, {"entry_id": "entry"})  # type: ignore[arg-type]
    )
    entry = SimpleNamespace(domain=const.DOMAIN, runtime_data=coordinator)
    flow.hass = SimpleNamespace(  # type: ignore[assignment]
        config_entries=SimpleNamespace(async_get_entry=lambda _: entry)
    )
    flow.flow_id, flow.handler = "flow", const.DOMAIN
    return flow


def test_the_held_totals_issue_gets_the_held_totals_flow() -> None:
    assert isinstance(_flow(_held_coordinator()), HeldTotalsRepairFlow)


def _held_coordinator() -> SimpleNamespace:
    return SimpleNamespace(
        long_held_totals={"grid_export_total": object()},
        describe_held_totals=lambda: HELD,
        async_take_device_totals=AsyncMock(return_value={"grid_export_total": 507.5}),
        async_keep_shown_totals=Mock(),
        inverter_model=device_models.InverterModel.POWEROCEAN_PLUS,
        identity=device_models.DeviceIdentity(firmware_version="V3.0.20.54"),
    )


def test_the_flow_shows_the_held_totals_before_changing_anything() -> None:
    coordinator = _held_coordinator()
    result = asyncio.run(_flow(coordinator).async_step_init())

    assert result["type"] == "menu"
    assert result.get("menu_options") == ["keep_shown", "take_device"]
    assert (result.get("description_placeholders") or {})["totals"] == HELD
    coordinator.async_take_device_totals.assert_not_called()
    coordinator.async_keep_shown_totals.assert_not_called()


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

    for model in device_models.InverterModel:
        assert model.traits.display_name in options
    for key in ("ha_version", "integration_version", "device_firmware", "steps"):
        assert key in fields


def test_taking_the_inverters_value_takes_the_device_totals() -> None:
    coordinator = _held_coordinator()
    result = asyncio.run(_flow(coordinator).async_step_take_device())

    assert result["type"] == "create_entry"
    coordinator.async_take_device_totals.assert_awaited_once()
    coordinator.async_keep_shown_totals.assert_not_called()


def test_continuing_from_the_value_shown_keeps_the_totals() -> None:
    coordinator = _held_coordinator()
    result = asyncio.run(_flow(coordinator).async_step_keep_shown())

    assert result["type"] == "create_entry"
    coordinator.async_keep_shown_totals.assert_called_once()
    coordinator.async_take_device_totals.assert_not_called()


@pytest.mark.parametrize("step", ("keep_shown", "take_device"))
def test_a_choice_made_after_the_totals_were_released_changes_nothing(
    step: str,
) -> None:
    coordinator = _held_coordinator()
    flow = _flow(coordinator)
    coordinator.long_held_totals = {}

    result = asyncio.run(getattr(flow, f"async_step_{step}")())

    assert result["type"] == "abort"
    coordinator.async_keep_shown_totals.assert_not_called()
    coordinator.async_take_device_totals.assert_not_called()


def test_the_flow_aborts_when_nothing_is_held_anymore() -> None:
    coordinator = _held_coordinator()
    coordinator.long_held_totals = {}
    result = asyncio.run(_flow(coordinator).async_step_init())

    assert result["type"] == "abort"
    assert result["reason"] == "not_held"


def test_the_issue_id_home_assistant_passes_in_is_not_a_confirmation() -> None:
    coordinator = _held_coordinator()
    result = asyncio.run(_flow(coordinator).async_step_init({"issue_id": "issue"}))

    assert result["type"] == "menu"
    coordinator.async_take_device_totals.assert_not_called()
    coordinator.async_keep_shown_totals.assert_not_called()


def test_every_language_uses_exactly_the_placeholders_the_flow_provides() -> None:
    component = Path(const.__file__).parent
    files = sorted(component.glob("translations/*.json"))
    assert files
    result = asyncio.run(_flow(_held_coordinator()).async_step_init())
    provided = set(result.get("description_placeholders") or {})
    choices = set(cast(list[str], result.get("menu_options")))

    for path in files:
        issue = json.loads(path.read_text(encoding="utf-8"))["issues"][
            "energy_totals_held"
        ]
        step = issue["fix_flow"]["step"]["init"]
        used = {
            name for _, name, _, _ in Formatter().parse(step["description"]) if name
        }
        assert used == provided, path.name
        assert set(step["menu_options"]) == choices, path.name
        assert {
            name for _, name, _, _ in Formatter().parse(issue["title"]) if name
        } == {"host"}, path.name


# ── Alerts ──────────────────────────────────────────────────────────

ALERT = {
    "flow": "alert",
    "entry_id": "entry",
    "alert": "slow_polls",
    "title": "Polls run late",
    "message": "5 polls in a row came 25 s apart.",
    "count": "5",
    "first": "2026-11-19 12:00:00",
    "last": "2026-11-19 12:02:00",
}


def _alert_flow(monkeypatch: pytest.MonkeyPatch, running: Any) -> AlertRepairFlow:
    monkeypatch.setattr(watchdog, "_RUNNING", {"entry": running} if running else {})
    flow = asyncio.run(async_create_fix_flow(None, "issue", dict(ALERT)))  # type: ignore[arg-type]
    assert isinstance(flow, AlertRepairFlow)
    flow.hass = SimpleNamespace()  # type: ignore[assignment]
    flow.flow_id, flow.handler = "flow", const.DOMAIN
    return flow


def test_an_alert_shows_what_was_seen_and_how_to_ignore_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = asyncio.run(_alert_flow(monkeypatch, Mock()).async_step_init())

    assert result["type"] == "menu"
    assert result.get("menu_options") == [
        "ignore_once",
        "ignore_day",
        "ignore_release",
        "ignore_forever",
    ]
    placeholders = result.get("description_placeholders") or {}
    assert placeholders["message"] == ALERT["message"]
    assert placeholders["count"] == "5"


@pytest.mark.parametrize(
    ("step", "ignore"),
    (
        ("async_step_ignore_once", "async_ignore_this_time"),
        ("async_step_ignore_release", "async_ignore_until_next_release"),
        ("async_step_ignore_day", "async_ignore_for_a_day"),
        ("async_step_ignore_forever", "async_ignore_permanently"),
    ),
)
def test_an_alert_is_ignored_as_chosen(
    monkeypatch: pytest.MonkeyPatch, step: str, ignore: str
) -> None:
    running = Mock()

    result = asyncio.run(getattr(_alert_flow(monkeypatch, running), step)())

    assert result["type"] == "create_entry"
    getattr(running, ignore).assert_called_once_with("slow_polls")


@pytest.mark.parametrize(
    "step",
    (
        "async_step_init",
        "async_step_ignore_once",
        "async_step_ignore_release",
        "async_step_ignore_day",
        "async_step_ignore_forever",
    ),
)
def test_an_alert_flow_ends_once_alerts_are_off(
    monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    result = asyncio.run(getattr(_alert_flow(monkeypatch, None), step)())

    assert result["type"] == "abort"
    assert result.get("reason") == "not_watching"


def test_every_language_explains_an_alert_with_what_the_flow_provides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = asyncio.run(_alert_flow(monkeypatch, Mock()).async_step_init())
    provided = set(result.get("description_placeholders") or {})
    choices = set(cast(list[str], result.get("menu_options")))

    for path in sorted(Path(const.__file__).parent.glob("translations/*.json")):
        issue = json.loads(path.read_text(encoding="utf-8"))["issues"]["alert"]
        step = issue["fix_flow"]["step"]["init"]
        used = {
            name for _, name, _, _ in Formatter().parse(step["description"]) if name
        }
        assert used <= provided, path.name
        assert set(step["menu_options"]) == choices, path.name


# ── Grid energy not counted ───────────────────────────────────────────────────

GRID_ISSUE = "grid_energy_not_counted_entry"


def _not_counting_coordinator() -> SimpleNamespace:
    return SimpleNamespace(
        grid_energy_not_counted=True,
        async_enable_grid_energy_calculation=Mock(),
    )


def test_the_grid_energy_issue_gets_its_own_flow() -> None:
    assert isinstance(
        _flow(_not_counting_coordinator(), GRID_ISSUE), GridEnergyRepairFlow
    )


def test_the_grid_energy_flow_explains_before_turning_anything_on() -> None:
    coordinator = _not_counting_coordinator()
    flow = _flow(coordinator, GRID_ISSUE)

    result = asyncio.run(flow.async_step_init({"issue_id": GRID_ISSUE}))

    assert result["type"] == "form"
    assert result["step_id"] == "confirm"
    coordinator.async_enable_grid_energy_calculation.assert_not_called()


def test_confirming_turns_the_grid_energy_calculation_on() -> None:
    coordinator = _not_counting_coordinator()

    result = asyncio.run(_flow(coordinator, GRID_ISSUE).async_step_confirm({}))

    assert result["type"] == "create_entry"
    coordinator.async_enable_grid_energy_calculation.assert_called_once()


def test_confirming_after_the_registers_count_again_changes_nothing() -> None:
    coordinator = _not_counting_coordinator()
    flow = _flow(coordinator, GRID_ISSUE)
    coordinator.grid_energy_not_counted = False

    result = asyncio.run(flow.async_step_confirm({}))

    assert result["type"] == "abort"
    assert result["reason"] == "not_needed"
    coordinator.async_enable_grid_energy_calculation.assert_not_called()


def test_every_language_explains_the_grid_energy_issue_with_its_placeholders() -> None:
    component = Path(const.__file__).parent
    result = asyncio.run(
        _flow(_not_counting_coordinator(), GRID_ISSUE).async_step_init()
    )
    provided = set(result.get("description_placeholders") or {})

    for path in sorted(component.glob("translations/*.json")):
        issue = json.loads(path.read_text(encoding="utf-8"))["issues"][
            "grid_energy_not_counted"
        ]
        step = issue["fix_flow"]["step"]["confirm"]
        used = {
            name for _, name, _, _ in Formatter().parse(step["description"]) if name
        }
        assert used == provided, path.name
        assert "not_needed" in issue["fix_flow"]["abort"], path.name
        assert {
            name for _, name, _, _ in Formatter().parse(issue["title"]) if name
        } == {"host"}, path.name
