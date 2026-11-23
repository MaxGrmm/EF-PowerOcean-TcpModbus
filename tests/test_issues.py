"""The repairs issues an entry raises, and how they go with it."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.ef_powerocean_tcpmodbus.const import DOMAIN
from custom_components.ef_powerocean_tcpmodbus.health import issues

ENTRY = cast(Any, SimpleNamespace(entry_id="entry"))


def test_every_issue_an_entry_raises_has_an_id_that_names_it() -> None:
    assert issues.issue_ids(ENTRY) == [
        "energy_totals_held_entry",
        "grid_energy_not_counted_entry",
        "identity_mismatch_entry",
        "modbus_disabled_entry",
        "telemetry_stalled_entry",
    ]


def test_an_id_tells_its_kind() -> None:
    issue = issues.issue_id(issues.GRID_ENERGY, ENTRY)

    assert issues.is_kind(issues.GRID_ENERGY, issue)
    assert not issues.is_kind(issues.HELD_TOTALS, issue)


def test_unloading_takes_every_issue_down(monkeypatch: pytest.MonkeyPatch) -> None:
    delete = Mock()
    monkeypatch.setattr(ir, "async_delete_issue", delete)

    issues.async_delete_all(cast(Any, None), ENTRY)

    assert sorted(call.args[2] for call in delete.call_args_list) == (
        issues.issue_ids(ENTRY)
    )
    assert {call.args[1] for call in delete.call_args_list} == {DOMAIN}
