"""An inverter that stops answering is reported, with the likely cause."""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ef_powerocean_tcpmodbus import const, outage
from custom_components.ef_powerocean_tcpmodbus.outage import Cause
from tests.fake_inverter import HOST, FakeInverter


def _closed_port() -> int:
    """Return a port nothing listens on."""
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


# ── The cause ─────────────────────────────────────────────────────────────────


async def test_a_port_that_takes_connections_is_a_silent_inverter(
    inverter: FakeInverter,
) -> None:
    assert await outage.async_classify(HOST, inverter.port) is Cause.MODBUS_SILENT
    assert inverter.requests == 0


@pytest.mark.usefixtures("socket_enabled")
async def test_a_closed_port_is_an_unreachable_inverter() -> None:
    assert await outage.async_classify(HOST, _closed_port()) is Cause.UNREACHABLE


async def test_a_host_name_that_cannot_be_encoded_is_unreachable() -> None:
    """Such as "a..b", whose encoding fails with a ValueError, not an OSError.

    Stubbed, because the test harness refuses the name lookup before that.
    """
    with patch.object(
        outage.asyncio,
        "open_connection",
        AsyncMock(side_effect=UnicodeError("label empty or too long")),
    ):
        assert await outage.async_classify("a..b", 502) is Cause.UNREACHABLE


async def test_a_connection_held_without_answers_needs_no_probe() -> None:
    client = SimpleNamespace(connected=True)

    with patch.object(outage, "async_classify", side_effect=AssertionError):
        cause = await outage.async_cause(client, HOST, 502)  # type: ignore[arg-type]

    assert cause is Cause.MODBUS_SILENT


async def test_without_a_held_connection_the_probe_decides() -> None:
    client = SimpleNamespace(connected=False)

    with patch.object(
        outage, "async_classify", AsyncMock(return_value=Cause.UNREACHABLE)
    ) as probe:
        cause = await outage.async_cause(client, HOST, 502)  # type: ignore[arg-type]

    assert cause is Cause.UNREACHABLE
    probe.assert_awaited_once_with(HOST, 502)


# ── The outage and its issue ──────────────────────────────────────────────────

START = datetime(2026, 11, 17, 8, 42, tzinfo=UTC)


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=const.DOMAIN, data={const.CONF_HOST: HOST, const.CONF_PORT: 502}
    )
    entry.add_to_hass(hass)
    return entry


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(const.DOMAIN, outage.issue_id(entry))


async def _fail(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    after_s: float,
    cause: Cause = Cause.MODBUS_SILENT,
    host: str = HOST,
) -> AsyncMock:
    """Fail a poll *after_s* into the outage; return the stand-in for the probe."""
    client = SimpleNamespace(connected=False)
    probe = AsyncMock(return_value=cause)
    with (
        patch.object(
            dt_util, "utcnow", return_value=START + timedelta(seconds=after_s)
        ),
        patch.object(outage, "async_cause", probe),
    ):
        await outage.async_failed(
            hass,
            entry,
            client,  # type: ignore[arg-type]
            host,
            502,
        )
    return probe


async def test_an_outage_as_long_as_a_firmware_reboot_raises_no_issue(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)

    await _fail(hass, entry, 0)
    await _fail(hass, entry, outage.NOT_ANSWERING_AFTER_S - 1)

    assert _issue(hass, entry) is None


async def test_a_longer_outage_raises_the_issue_for_its_cause(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)

    await _fail(hass, entry, 0)
    await _fail(hass, entry, outage.NOT_ANSWERING_AFTER_S)

    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.translation_key == "not_answering_modbus_silent"
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["host"] == HOST
    # When it started, not when it was noticed.
    assert issue.translation_placeholders["since"] == dt_util.as_local(START).strftime(
        "%Y-%m-%d %H:%M"
    )


async def test_the_issue_follows_a_change_of_cause(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    await _fail(hass, entry, 0)
    await _fail(hass, entry, outage.NOT_ANSWERING_AFTER_S)

    await _fail(
        hass,
        entry,
        outage.NOT_ANSWERING_AFTER_S + outage.RECHECK_CAUSE_AFTER_S,
        Cause.UNREACHABLE,
    )

    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.translation_key == "not_answering_unreachable"


async def test_the_cause_is_looked_at_again_only_after_a_while(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    await _fail(hass, entry, 0)
    await _fail(hass, entry, outage.NOT_ANSWERING_AFTER_S)

    probe = await _fail(
        hass,
        entry,
        outage.NOT_ANSWERING_AFTER_S + outage.RECHECK_CAUSE_AFTER_S - 1,
        Cause.UNREACHABLE,
    )

    probe.assert_not_awaited()
    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.translation_key == "not_answering_modbus_silent"


async def test_an_answer_ends_the_outage_and_its_issue(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    await _fail(hass, entry, 0)
    await _fail(hass, entry, outage.NOT_ANSWERING_AFTER_S)

    outage.async_answered(hass, entry)

    assert _issue(hass, entry) is None
    # The next failure starts a new outage, which has to last as long again.
    await _fail(hass, entry, outage.NOT_ANSWERING_AFTER_S + 60)
    assert _issue(hass, entry) is None


async def test_a_new_address_starts_a_new_outage(hass: HomeAssistant) -> None:
    """After Reconfigure, the issue would otherwise name the old host."""
    entry = _entry(hass)
    await _fail(hass, entry, 0)
    await _fail(hass, entry, outage.NOT_ANSWERING_AFTER_S)

    await _fail(hass, entry, outage.NOT_ANSWERING_AFTER_S + 60, host="127.0.0.2")

    assert _issue(hass, entry) is None
    assert outage._outages[entry.entry_id].host == "127.0.0.2"


@pytest.mark.parametrize("cause", tuple(Cause))
def test_every_language_explains_every_cause(cause: Cause) -> None:
    files = sorted((Path(const.__file__).parent / "translations").glob("*.json"))
    assert files

    for path in files:
        issues = json.loads(path.read_text(encoding="utf-8"))["issues"]
        issue = issues[f"not_answering_{cause}"]
        assert "{host}" in issue["title"], path.name
        assert "{since}" in issue["description"], path.name
