"""Alerts by verbosity: each rule on its own, the repairs issues, and the log."""

from __future__ import annotations

import asyncio
import logging
import zlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.ef_powerocean_tcpmodbus import const, repairs, watchdog
from custom_components.ef_powerocean_tcpmodbus.models import AlertVerbosity
from custom_components.ef_powerocean_tcpmodbus.powerocean import const as device_const
from custom_components.ef_powerocean_tcpmodbus.powerocean.models import ControlStatus
from custom_components.ef_powerocean_tcpmodbus.powerocean.reading_checks import Finding

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
CONTROL = device_const.CONTROL_COMMAND_REGISTER


def _view(seconds: float = 0, **changes: Any) -> watchdog.PollView:
    view = watchdog.PollView(
        at=T0 + timedelta(seconds=seconds),
        ok=True,
        connected=True,
        data={
            "house_power": 500.0,
            "solar_power": 300.0,
            "grid_power": 250.0,
            "battery_power": 50.0,
        },
        findings=(),
        distrusted=frozenset(),
        control_enabled=False,
        control_testing=False,
        control_status=ControlStatus.NO_MODBUS_CONTROL,
        writes={},
        reconnects=0,
        held_totals=frozenset(),
        scan_interval=timedelta(seconds=5),
    )
    return replace(view, **changes)


def _run(rule: watchdog.Rule, views: list[watchdog.PollView]) -> list[str | None]:
    return [rule.check(view) for view in views]


# ── A healthy system raises nothing ───────────────────────────────────────────


def test_a_healthy_hour_raises_nothing() -> None:
    rules = [rule() for rule in watchdog.RULES]
    for poll in range(720):
        view = _view(
            5 * poll,
            data={
                "house_power": 500.0 + poll % 7,
                "solar_power": 300.0,
                "grid_power": 250.0 + poll % 7,
                "battery_power": 50.0,
                "solar_total_raw": 100.0 + poll / 1000,
            },
            # Battery saver toggled once by the user.
            writes={CONTROL: 1 if poll > 100 else 0},
        )
        assert [rule.check(view) for rule in rules] == [None] * len(rules)


# ── Verbosity ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("verbosity", "rules"),
    (
        (
            AlertVerbosity.READINGS,
            {
                "reading_findings",
                "counters",
                "control_not_accepted",
                "unavailable_entities",
            },
        ),
        (
            AlertVerbosity.DETAILED,
            {
                "reading_findings",
                "counters",
                "control_not_accepted",
                "unavailable_entities",
                "control_writes_while_off",
                "frequent_reconnects",
                "slow_polls",
                "energy_balance",
            },
        ),
    ),
)
def test_each_level_raises_what_the_one_before_does_and_more(
    verbosity: AlertVerbosity, rules: set[str]
) -> None:
    dog = watchdog.Watchdog(
        watchdog.Notifier(cast(Any, SimpleNamespace()), "entry"), verbosity
    )
    assert {rule.name for rule in dog._rules} == rules


def test_every_rule_has_a_level_above_essential() -> None:
    """Essential is what the user must act on, raised where it arises."""
    assert all(rule.level is not AlertVerbosity.ESSENTIAL for rule in watchdog.RULES)
    assert [level.rank for level in AlertVerbosity] == [0, 1, 2, 3]


def test_a_finding_with_its_own_issue_is_not_raised_twice() -> None:
    stalled = Finding(
        ControlStatus.INCONSISTENT_READINGS,
        "telemetry_stalled",
        T0,
        remedy="telemetry_stalled",
    )
    assert watchdog.ReadingFindings().check(_view(findings=(stalled,))) is None


# ── Each rule ─────────────────────────────────────────────────────────────────


def test_control_writes_every_poll_with_control_off_are_raised() -> None:
    """The #227 loop: a control word every 25 s with control off."""
    views = [_view(25 * poll, writes={CONTROL: poll}) for poll in range(6)]

    results = _run(watchdog.ControlWritesWhileOff(), views)

    assert results[:4] == [None] * 4
    assert results[4] is not None
    assert "while Modbus Control is off" in results[4]


def test_control_writes_with_control_on_are_expected() -> None:
    views = [
        _view(5 * poll, writes={CONTROL: poll}, control_enabled=True)
        for poll in range(20)
    ]
    assert not any(_run(watchdog.ControlWritesWhileOff(), views))


def test_frequent_reconnects_are_raised() -> None:
    views = [_view(60 * poll, reconnects=poll) for poll in range(8)]

    results = _run(watchdog.FrequentReconnects(), views)

    assert results[5] is None
    assert results[6] is not None


def test_polls_far_apart_are_raised_once_they_keep_coming_late() -> None:
    views = [_view(25 * poll) for poll in range(7)]

    results = _run(watchdog.SlowPolls(), views)

    assert results[:5] == [None] * 5
    assert results[5] is not None
    assert "25 s apart" in results[5]


def test_retries_during_an_outage_are_not_late_polls() -> None:
    views = [_view(30 * poll, ok=False) for poll in range(10)]
    assert not any(_run(watchdog.SlowPolls(), views))


def test_only_readings_count_as_unexpectedly_unavailable() -> None:
    """A setpoint is unavailable on purpose while Modbus Control is off."""
    entries = [
        SimpleNamespace(
            domain=domain,
            unique_id=f"entry_{key}",
            entity_id=f"{domain}.{key}",
            disabled_by=None,
        )
        for domain, key in (("sensor", "pv2_voltage"), ("number", "charge_power"))
    ]
    states = SimpleNamespace(get=lambda _: SimpleNamespace(state=STATE_UNAVAILABLE))

    keys = watchdog.unavailable_keys(
        cast(Any, SimpleNamespace(states=states)),
        cast(Any, SimpleNamespace(entry_id="entry")),
        entries,
    )

    assert keys == {"pv2_voltage"}


def test_a_finding_is_raised_when_it_starts() -> None:
    finding = Finding(
        ControlStatus.INCONSISTENT_READINGS,
        "telemetry_stalled",
        T0,
        keys=frozenset({"voltage_l1"}),
    )
    views = [_view(), _view(5, findings=(finding,)), _view(10, findings=(finding,))]

    results = _run(watchdog.ReadingFindings(), views)

    assert results[0] is None
    assert results[1] is not None
    assert "telemetry_stalled" in results[1]
    assert "voltage_l1" in results[1]
    assert results[2] is None


def test_power_that_does_not_add_up_is_raised_after_a_minute() -> None:
    # The battery's sign flipped: 300 + 250 + 50 is 600, not 500 - 50.
    data = {
        "house_power": 2000.0,
        "solar_power": 300.0,
        "grid_power": 250.0,
        "battery_power": 50.0,
    }
    views = [_view(5 * poll, data=data) for poll in range(12)]

    results = _run(watchdog.EnergyBalance(), views)

    assert results[:11] == [None] * 11
    assert results[11] is not None


def test_power_that_does_not_add_up_while_readings_are_distrusted_is_not_raised() -> (
    None
):
    data = {"house_power": 2000.0, "solar_power": 0.0, "grid_power": 0.0}
    views = [
        _view(5 * poll, data={**data, "battery_power": 0.0}, distrusted={"house_power"})
        for poll in range(20)
    ]
    assert not any(_run(watchdog.EnergyBalance(), views))


def test_a_counter_that_goes_back_or_is_held_is_raised() -> None:
    views = [
        _view(0, data={"solar_total_raw": 100.0}),
        _view(5, data={"solar_total_raw": 99.0}),
        _view(10, data={"solar_total_raw": 99.5}, held_totals=frozenset({"x"})),
        _view(15, data={"solar_total_raw": 99.6}, held_totals=frozenset({"x"})),
    ]

    results = _run(watchdog.CountersBack(), views)

    assert results[0] is None
    assert results[1] is not None
    assert "solar_total went from 100.0 to 99.0" in results[1]
    assert results[2] == "held: x"
    assert results[3] is None


def test_control_not_accepted_is_raised_when_it_turns() -> None:
    views = [
        _view(0),
        _view(5, control_status=ControlStatus.NOT_ACCEPTED),
        _view(10, control_status=ControlStatus.NOT_ACCEPTED),
    ]
    assert _run(watchdog.ControlNotAccepted(), views)[1:] == [
        "Control Status turned to Not accepted.",
        None,
    ]


def test_entities_unavailable_after_good_polls_are_raised() -> None:
    views = [
        _view(5 * poll, unavailable=frozenset({"pv2_voltage"})) for poll in range(3)
    ]

    results = _run(watchdog.UnexpectedlyUnavailable(), views)

    assert results[:2] == [None, None]
    assert results[2] == "Unavailable: pv2_voltage."


def test_entities_a_check_distrusts_are_expected_to_be_unavailable() -> None:
    views = [
        _view(
            5 * poll,
            unavailable=frozenset({"battery_soc"}),
            distrusted=frozenset({"battery_soc"}),
        )
        for poll in range(5)
    ]
    assert not any(_run(watchdog.UnexpectedlyUnavailable(), views))


# ── Repairs issues and ignoring them ─────────────────────────────────────────


@pytest.fixture
def issues(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    created, deleted = Mock(), Mock()
    monkeypatch.setattr(ir, "async_create_issue", created)
    monkeypatch.setattr(ir, "async_delete_issue", deleted)
    monkeypatch.setattr(dt_util, "as_local", lambda value: value)
    monkeypatch.setattr(dt_util, "now", lambda *_: T0)
    return SimpleNamespace(created=created, deleted=deleted)


class _Store:
    def __init__(self, stored: dict[str, Any] | None = None) -> None:
        self.stored = stored
        self.saved: dict[str, Any] | None = None

    async def async_load(self) -> dict[str, Any] | None:
        return self.stored

    def async_delay_save(self, data: Any, _delay: float) -> None:
        self.saved = data()


def _ignores(release: str = "2.9.6", stored: dict[str, Any] | None = None):
    ignores = watchdog.Ignores(cast(Any, _Store(stored)), release)
    asyncio.run(ignores.async_load())
    return ignores


def test_a_recurring_alert_is_one_issue_updated_now_and_then(
    issues: SimpleNamespace,
) -> None:
    notifier = watchdog.Notifier(cast(Any, SimpleNamespace()), "entry")
    for minute in range(25):
        notifier.notify("rule", "Title", "Went wrong", T0 + timedelta(minutes=minute))

    # At minutes 0, 10 and 20, always the same issue, with a way to ignore it.
    assert issues.created.call_count == 3
    ids = {call.args[2] for call in issues.created.call_args_list}
    assert ids == {"alert_rule_entry"}
    kwargs = issues.created.call_args.kwargs
    assert kwargs["is_fixable"] is True
    assert kwargs["translation_key"] == "alert"
    assert kwargs["data"]["count"] == "21"
    assert kwargs["data"]["message"] == "Went wrong"


def test_an_alert_ignored_until_the_next_release_comes_back_with_it(
    issues: SimpleNamespace,
) -> None:
    stored = {"rule": {"release": "2.9.5"}}

    same = watchdog.Notifier(
        cast(Any, SimpleNamespace()), "e", _ignores("2.9.5", stored)
    )
    same.notify("rule", "Title", "Went wrong", T0)
    issues.created.assert_not_called()

    newer = watchdog.Notifier(
        cast(Any, SimpleNamespace()), "e", _ignores("2.9.6", stored)
    )
    newer.notify("rule", "Title", "Went wrong", T0)
    issues.created.assert_called_once()


def test_an_alert_ignored_for_a_day_comes_back_after_it(
    issues: SimpleNamespace,
) -> None:
    ignores = _ignores()
    ignores.async_ignore("rule", ignores.until_time(T0 + timedelta(hours=24)))
    notifier = watchdog.Notifier(cast(Any, SimpleNamespace()), "e", ignores)

    notifier.notify("rule", "Title", "Went wrong", T0 + timedelta(hours=23))
    issues.created.assert_not_called()

    notifier.notify("rule", "Title", "Went wrong", T0 + timedelta(hours=25))
    issues.created.assert_called_once()


def test_an_alert_ignored_this_time_comes_back_once_it_went_away(
    issues: SimpleNamespace,
) -> None:
    notifier = watchdog.Notifier(cast(Any, SimpleNamespace()), "entry")
    dog = watchdog.Watchdog(notifier, AlertVerbosity.READINGS)
    accepted = ControlStatus.NOT_ACCEPTED
    dog.observe(_view(0, control_status=accepted))
    assert issues.created.call_count == 1

    dog.async_ignore_this_time("control_not_accepted")
    issues.deleted.assert_called_once()
    # Gone for now, so it raises nothing while it lasts...
    dog.observe(_view(5, control_status=accepted))
    assert issues.created.call_count == 1

    # ...and comes back once it went away and happens again.
    dog.observe(_view(10))
    dog.observe(_view(15, control_status=accepted))
    assert issues.created.call_count == 2


def test_a_logged_message_ignored_this_time_comes_back_after_a_quiet_spell(
    issues: SimpleNamespace,
) -> None:
    notifier = watchdog.Notifier(cast(Any, SimpleNamespace()), "entry")
    notifier.notify("log_x", "Logged", "Odd", T0)
    notifier.async_ignore_this_time("log_x")

    # Logged again and again, never quiet for long: still ignored.
    for minute in range(1, 30):
        notifier.notify("log_x", "Logged", "Odd", T0 + timedelta(minutes=minute))
    assert issues.created.call_count == 1

    # Quiet for over 10 minutes, then logged again: it is back.
    notifier.notify("log_x", "Logged", "Odd", T0 + timedelta(minutes=45))
    assert issues.created.call_count == 2


def test_an_alert_ignored_permanently_stays_away(issues: SimpleNamespace) -> None:
    ignores = _ignores()
    ignores.async_ignore("rule", ignores.forever())
    notifier = watchdog.Notifier(cast(Any, SimpleNamespace()), "e", ignores)

    notifier.notify("rule", "Title", "Went wrong", T0 + timedelta(days=400))

    issues.created.assert_not_called()


def test_ignoring_takes_the_issue_down_and_is_remembered(
    issues: SimpleNamespace,
) -> None:
    store = _Store()
    ignores = watchdog.Ignores(cast(Any, store), "2.9.6")
    notifier = watchdog.Notifier(cast(Any, SimpleNamespace()), "entry", ignores)
    dog = watchdog.Watchdog(notifier)
    notifier.notify("rule", "Title", "Went wrong", T0)

    dog.async_ignore_until_next_release("rule")
    dog.async_ignore_for_a_day("other")
    dog.async_ignore_permanently("third")

    issues.deleted.assert_any_call(notifier.hass, const.DOMAIN, "alert_rule_entry")
    assert store.saved == {
        "rule": {"release": "2.9.6"},
        "other": {"time": (T0 + watchdog.IGNORE_FOR).isoformat()},
        "third": {"forever": "yes"},
    }


def test_a_stored_ignore_that_cannot_be_read_is_not_an_ignore() -> None:
    ignores = _ignores(stored={"rule": {"time": "not a time"}, "other": "junk"})

    assert not ignores.ignored("rule", T0)
    assert not ignores.ignored("other", T0)


def test_a_rule_that_breaks_is_dropped_and_the_others_go_on(
    issues: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    dog = watchdog.Watchdog(watchdog.Notifier(cast(Any, SimpleNamespace()), "entry"))
    broken = Mock(side_effect=RuntimeError("bad rule"))
    monkeypatch.setattr(dog._rules[0], "check", broken)

    dog.observe(_view(0, control_status=ControlStatus.NOT_ACCEPTED))
    dog.observe(_view(5))

    broken.assert_called_once()
    assert issues.created.call_count == 1


def test_the_fix_flow_ignores_the_alert_it_was_opened_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = Mock()
    monkeypatch.setattr(watchdog, "_RUNNING", {"entry": running})
    data = {"flow": "alert", "entry_id": "entry", "alert": "rule"}

    flow = asyncio.run(repairs.async_create_fix_flow(cast(Any, None), "id", data))
    assert isinstance(flow, repairs.AlertRepairFlow)
    asyncio.run(flow.async_step_ignore_release())
    asyncio.run(flow.async_step_ignore_day())

    running.async_ignore_until_next_release.assert_called_once_with("rule")
    running.async_ignore_for_a_day.assert_called_once_with("rule")


# ── The integration's own log ─────────────────────────────────────────────────


def test_warnings_the_integration_logs_become_alerts(issues: SimpleNamespace) -> None:
    dog = watchdog.Watchdog(watchdog.Notifier(cast(Any, SimpleNamespace()), "entry"))
    loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *args: fn(*args))
    handler = watchdog._Forwarder(cast(Any, SimpleNamespace(loop=loop)), dog)
    logger = logging.getLogger("custom_components.ef_powerocean_tcpmodbus.control")
    logger.addHandler(handler)
    try:
        logger.debug("Routine detail")
        logger.warning("Odd value %s", 42)
        logger.warning("Odd value %s", 43)
        logging.getLogger(watchdog.__name__).warning("Not about itself")
    finally:
        logger.removeHandler(handler)

    # One alert for the kind of message, not one per value.
    assert issues.created.call_count == 1
    assert issues.created.call_args.kwargs["data"]["message"].startswith("Odd value 42")


def test_watching_starts_and_stops_with_the_entry(
    issues: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopping takes down the alerts it raised; they come back if still true."""

    async def integration(*_: Any) -> Any:
        return SimpleNamespace(version="2.9.6")

    monkeypatch.setattr(watchdog, "async_get_integration", integration)
    monkeypatch.setattr(watchdog, "Store", lambda *_: _Store())
    remove_listener = Mock()
    coordinator = SimpleNamespace(async_add_listener=Mock(return_value=remove_listener))
    package = logging.getLogger("custom_components.ef_powerocean_tcpmodbus")
    before = list(package.handlers)

    stop = asyncio.run(
        watchdog.async_start(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(entry_id="entry")),
            cast(Any, coordinator),
            AlertVerbosity.DEBUG,
        )
    )
    assert len(package.handlers) == len(before) + 1
    running = watchdog.running("entry")
    assert running is not None
    running.observe(_view(0, control_status=ControlStatus.NOT_ACCEPTED))
    issues.created.assert_called_once()

    stop()

    issues.deleted.assert_called_once()

    remove_listener.assert_called_once()
    assert package.handlers == before
    assert watchdog.running("entry") is None


def test_the_log_is_only_watched_at_debug(
    issues: SimpleNamespace,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def integration(*_: Any) -> Any:
        return SimpleNamespace(version="2.9.6")

    monkeypatch.setattr(watchdog, "async_get_integration", integration)
    monkeypatch.setattr(watchdog, "Store", lambda *_: _Store())
    coordinator = SimpleNamespace(async_add_listener=Mock(return_value=Mock()))
    package = logging.getLogger("custom_components.ef_powerocean_tcpmodbus")
    before = list(package.handlers)

    stop = asyncio.run(
        watchdog.async_start(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(entry_id="entry")),
            cast(Any, coordinator),
            AlertVerbosity.DETAILED,
        )
    )

    assert package.handlers == before
    stop()


def test_a_log_alert_keeps_its_id_when_its_module_moves() -> None:
    """An alert the user ignored stays ignored after a module moves package."""
    package = "custom_components.ef_powerocean_tcpmodbus"
    template = "The %s reading check could not run"
    old = watchdog.log_alert(f"{package}.reading_checks", template)

    assert watchdog.log_alert(f"{package}.powerocean.reading_checks", template) == old
    assert (
        old == f"log_{zlib.crc32(f'{package}.reading_checks:{template}'.encode()):08x}"
    )
    assert watchdog.log_alert(f"{package}.outage", template) != old


@pytest.mark.parametrize(
    "logger",
    (
        "custom_components.ef_powerocean_tcpmodbus",
        "custom_components.ef_powerocean_tcpmodbus.coordinator",
        "custom_components.ef_powerocean_tcpmodbus.reading_checks",
    ),
)
def test_a_log_alert_has_the_id_it_always_had(logger: str) -> None:
    """The id is the one alerts were stored under before modules moved."""
    template = "Something went wrong: %s"
    before = f"log_{zlib.crc32(f'{logger}:{template}'.encode()):08x}"

    assert watchdog.log_alert(logger, template) == before
