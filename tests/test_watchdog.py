"""Developer alerts: each rule on its own, the notifications, and the log."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from homeassistant.components import persistent_notification
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.util import dt as dt_util

from custom_components.ef_powerocean_tcpmodbus import const, watchdog
from custom_components.ef_powerocean_tcpmodbus.models import ControlStatus
from custom_components.ef_powerocean_tcpmodbus.reading_checks import Finding

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
CONTROL = const.CONTROL_COMMAND_REGISTER


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


# ── Notifications ─────────────────────────────────────────────────────────────


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch) -> Mock:
    create = Mock()
    monkeypatch.setattr(persistent_notification, "async_create", create)
    monkeypatch.setattr(dt_util, "as_local", lambda value: value)
    monkeypatch.setattr(dt_util, "now", lambda *_: T0)
    return create


def test_a_recurring_alert_is_one_notification_updated_now_and_then(
    created: Mock,
) -> None:
    notifier = watchdog.Notifier(cast(Any, SimpleNamespace()), "entry")
    for minute in range(25):
        notifier.notify("rule", "Title", "Went wrong", T0 + timedelta(minutes=minute))

    # At minutes 0, 10 and 20, always the same notification.
    assert created.call_count == 3
    ids = {call.kwargs["notification_id"] for call in created.call_args_list}
    assert ids == {f"{const.DOMAIN}_entry_developer_rule"}
    assert "Seen 21 times" in created.call_args.args[1]


def test_a_rule_that_breaks_is_dropped_and_the_others_go_on(
    created: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    dog = watchdog.Watchdog(watchdog.Notifier(cast(Any, SimpleNamespace()), "entry"))
    broken = Mock(side_effect=RuntimeError("bad rule"))
    monkeypatch.setattr(dog._rules[0], "check", broken)

    dog.observe(_view(0, control_status=ControlStatus.NOT_ACCEPTED))
    dog.observe(_view(5))

    broken.assert_called_once()
    assert created.call_count == 1


# ── The integration's own log ─────────────────────────────────────────────────


def test_warnings_the_integration_logs_become_alerts(created: Mock) -> None:
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
    assert created.call_count == 1
    assert created.call_args.args[1].startswith("Odd value 42")


def test_watching_starts_and_stops_with_the_entry(created: Mock) -> None:  # noqa: ARG001
    remove_listener = Mock()
    coordinator = SimpleNamespace(async_add_listener=Mock(return_value=remove_listener))
    package = logging.getLogger("custom_components.ef_powerocean_tcpmodbus")
    before = list(package.handlers)

    stop = watchdog.async_start(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(entry_id="entry")),
        cast(Any, coordinator),
    )
    assert len(package.handlers) == len(before) + 1

    stop()

    remove_listener.assert_called_once()
    assert package.handlers == before
