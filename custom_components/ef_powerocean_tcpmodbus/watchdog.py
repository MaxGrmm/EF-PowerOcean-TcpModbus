"""Alerts beyond what the user must act on, as verbose as the user chose.

At the default, Essential, nothing here runs: the repairs issues the user must
act on are raised where they arise. Each level above adds rules: readings that
cannot be trusted, then the connection's health, then every warning and error
the integration logs. After each poll the rules look at what the poll left
behind, each on its own. Nothing here changes what the integration does, and a
rule that breaks is switched off and logged once.

Each rule raises one repairs issue, updated in place as it recurs, so a problem
that repeats every poll is one issue with a count, not hundreds. Each can be
ignored this time, permanently, until the next release, or for a day.
"""

from __future__ import annotations

import logging
import zlib
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Final, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration
from homeassistant.util import dt

from .const import DOMAIN
from .models import AlertVerbosity
from .powerocean.const import CONTROL_COMMAND_REGISTER
from .powerocean.models import ControlStatus
from .powerocean.reading_checks import Finding

if TYPE_CHECKING:
    from .coordinator import EcoflowCoordinator

_LOGGER = logging.getLogger(__name__)
# Every logger of the integration is a child of this one.
_PACKAGE_LOGGER: Final = __name__.rsplit(".", 1)[0]

# An alert that recurs is updated at most this often, past its first.
UPDATE_EVERY: Final = timedelta(minutes=10)
# How long "ignore for a day" lasts.
IGNORE_FOR: Final = timedelta(hours=24)
STORAGE_VERSION: Final = 1

# The watchdog running for each entry, for the fix flows to reach.
_RUNNING: dict[str, Watchdog] = {}


@dataclass(frozen=True, slots=True)
class PollView:
    """What one poll left behind, as the rules may see it."""

    at: datetime
    ok: bool
    connected: bool
    data: Mapping[str, Any]
    findings: tuple[Finding, ...]
    distrusted: frozenset[str]
    control_enabled: bool
    control_testing: bool
    control_status: ControlStatus | None
    writes: Mapping[int, int]
    reconnects: int
    held_totals: frozenset[str]
    scan_interval: timedelta
    # Keys of this entry's entities that show unavailable.
    unavailable: frozenset[str] = frozenset()


class Rule:
    """One thing that should not happen on a healthy system."""

    name: ClassVar[str]
    title: ClassVar[str]
    # The least verbosity that raises it.
    level: ClassVar[AlertVerbosity]

    def check(self, view: PollView) -> str | None:
        """Return what went wrong in this poll, or None."""
        raise NotImplementedError


class ControlWritesWhileOff(Rule):
    """Control words sent again and again with Modbus Control off (#227).

    A user toggling battery saver writes one now and then; a loop writes one
    every poll.
    """

    name = "control_writes_while_off"
    title = "Control writes while Modbus Control is off"
    level = AlertVerbosity.DETAILED
    LIMIT: ClassVar[int] = 3
    WINDOW: ClassVar[timedelta] = timedelta(minutes=10)

    def __init__(self) -> None:
        self._last: int | None = None
        self._times: deque[datetime] = deque()

    def check(self, view: PollView) -> str | None:
        count = view.writes.get(CONTROL_COMMAND_REGISTER, 0)
        sent = 0 if self._last is None else count - self._last
        self._last = count
        if view.control_enabled or view.control_testing:
            self._times.clear()
            return None
        self._times.extend([view.at] * sent)
        while self._times and view.at - self._times[0] > self.WINDOW:
            self._times.popleft()
        if len(self._times) <= self.LIMIT:
            return None
        return (
            f"{len(self._times)} writes to the control register "
            f"{CONTROL_COMMAND_REGISTER} in the last "
            f"{int(self.WINDOW.total_seconds() // 60)} minutes, "
            "while Modbus Control is off."
        )


class FrequentReconnects(Rule):
    """The connection drops again and again."""

    name = "frequent_reconnects"
    title = "Frequent reconnects"
    level = AlertVerbosity.DETAILED
    LIMIT: ClassVar[int] = 5
    WINDOW: ClassVar[timedelta] = timedelta(hours=1)

    def __init__(self) -> None:
        self._last: int | None = None
        self._times: deque[datetime] = deque()

    def check(self, view: PollView) -> str | None:
        started = 0 if self._last is None else view.reconnects - self._last
        self._last = view.reconnects
        self._times.extend([view.at] * started)
        while self._times and view.at - self._times[0] > self.WINDOW:
            self._times.popleft()
        if len(self._times) <= self.LIMIT:
            return None
        return f"{len(self._times)} reconnects in the last hour."


class SlowPolls(Rule):
    """Polls arrive far apart from what the poll interval asks for."""

    name = "slow_polls"
    title = "Polls run late"
    level = AlertVerbosity.DETAILED
    FACTOR: ClassVar[int] = 3
    IN_A_ROW: ClassVar[int] = 5

    def __init__(self) -> None:
        self._last: datetime | None = None
        self._late = 0

    def check(self, view: PollView) -> str | None:
        gap = None if self._last is None else view.at - self._last
        # While reads fail, Home Assistant retries every 30 s on purpose; the
        # outage has its own repairs issue.
        self._last = view.at if view.ok else None
        if gap is None or not view.ok or gap <= self.FACTOR * view.scan_interval:
            self._late = 0
            return None
        self._late += 1
        if self._late < self.IN_A_ROW:
            return None
        return (
            f"{self._late} polls in a row came {gap.total_seconds():.0f} s apart, "
            f"with a poll interval of {view.scan_interval.total_seconds():.0f} s."
        )


class ReadingFindings(Rule):
    """A reading check found readings to be wrong, or the inverter in a state.

    A finding that raises its own repairs issue, such as stalled readings, is
    not raised twice.
    """

    name = "reading_findings"
    title = "Reading check failed"
    level = AlertVerbosity.READINGS

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def check(self, view: PollView) -> str | None:
        current = {
            finding.check: finding for finding in view.findings if not finding.remedy
        }
        started = [current[check] for check in current.keys() - self._seen]
        self._seen = set(current)
        if not started:
            return None
        return "; ".join(
            f"{finding.check} ({finding.status}) since {finding.since:%Y-%m-%d %H:%M}"
            + (
                f", distrusting {', '.join(sorted(finding.keys))}"
                if finding.keys
                else ""
            )
            for finding in started
        )


class EnergyBalance(Rule):
    """The house power does not add up from solar, grid and battery.

    Grid power is positive importing and battery power positive charging, so the
    house draws solar + grid - battery, less what the inverter loses. A gap well
    past that for a minute is a wrong register or sign.
    """

    name = "energy_balance"
    title = "House power does not add up"
    level = AlertVerbosity.DETAILED
    MARGIN_W: ClassVar[float] = 300.0
    MARGIN_SHARE: ClassVar[float] = 0.2
    IN_A_ROW: ClassVar[int] = 12
    _KEYS: ClassVar[tuple[str, ...]] = (
        "house_power",
        "solar_power",
        "grid_power",
        "battery_power",
    )

    def __init__(self) -> None:
        self._off = 0

    def check(self, view: PollView) -> str | None:
        values = [view.data.get(key) for key in self._KEYS]
        # Readings the checks distrust prove nothing about the mapping.
        if (
            not view.ok
            or view.findings
            or any(value is None for value in values)
            or view.distrusted.intersection(self._KEYS)
        ):
            self._off = 0
            return None
        house, solar, grid, battery = (float(cast(float, value)) for value in values)
        gap = house - (solar + grid - battery)
        if abs(gap) <= max(self.MARGIN_W, self.MARGIN_SHARE * abs(house)):
            self._off = 0
            return None
        self._off += 1
        if self._off < self.IN_A_ROW:
            return None
        return (
            f"House {house:.0f} W, but solar {solar:.0f} + grid {grid:.0f} "
            f"- battery {battery:.0f} = {solar + grid - battery:.0f} W, "
            f"{self._off} polls in a row."
        )


class CountersBack(Rule):
    """A lifetime counter of the inverter went back, or a total is held."""

    name = "counters"
    title = "Energy counter went back or is held"
    level = AlertVerbosity.READINGS

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._held: frozenset[str] = frozenset()

    def check(self, view: PollView) -> str | None:
        problems = []
        for key, value in view.data.items():
            if not key.endswith("_total_raw") or value is None:
                continue
            last = self._last.get(key)
            self._last[key] = float(value)
            if last is not None and float(value) < last:
                problems.append(
                    f"{key.removesuffix('_raw')} went from {last} to {value}"
                )
        if new_held := view.held_totals - self._held:
            problems.append(f"held: {', '.join(sorted(new_held))}")
        self._held = view.held_totals
        return "; ".join(problems) or None


class ControlNotAccepted(Rule):
    """The inverter ignores the commanded control method."""

    name = "control_not_accepted"
    title = "Inverter does not accept control"
    level = AlertVerbosity.READINGS

    def __init__(self) -> None:
        self._was = False

    def check(self, view: PollView) -> str | None:
        now = view.control_status is ControlStatus.NOT_ACCEPTED
        started, self._was = now and not self._was, now
        return "Control Status turned to Not accepted." if started else None


class UnexpectedlyUnavailable(Rule):
    """Entities show unavailable after a good poll, with no check to explain it."""

    name = "unavailable_entities"
    title = "Entities unavailable after a good poll"
    level = AlertVerbosity.READINGS
    IN_A_ROW: ClassVar[int] = 3

    def __init__(self) -> None:
        self._polls = 0

    def check(self, view: PollView) -> str | None:
        unexplained = view.unavailable - view.distrusted
        if not view.ok or not view.connected or not unexplained:
            self._polls = 0
            return None
        self._polls += 1
        if self._polls < self.IN_A_ROW:
            return None
        return f"Unavailable: {', '.join(sorted(unexplained))}."


RULES: Final[tuple[type[Rule], ...]] = (
    ControlWritesWhileOff,
    FrequentReconnects,
    SlowPolls,
    ReadingFindings,
    EnergyBalance,
    CountersBack,
    ControlNotAccepted,
    UnexpectedlyUnavailable,
)


@dataclass(slots=True)
class _Seen:
    count: int
    first: datetime
    last: datetime
    shown: datetime | None = None


class Ignores:
    """Which alerts the user chose to ignore, and until when.

    Kept per entry in a store of its own. An alert ignored until the next
    release comes back once another version of the integration runs; one ignored
    for a day, a day later; one ignored permanently, once the verbosity is set
    back to Essential, which forgets every choice.
    """

    def __init__(self, store: Store[dict[str, Any]], release: str) -> None:
        self._store = store
        self._release = release
        self._until: dict[str, dict[str, str]] = {}

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        self._until = {
            alert: until for alert, until in stored.items() if isinstance(until, dict)
        }

    def ignored(self, alert: str, now: datetime) -> bool:
        until = self._until.get(alert)
        if until is None:
            return False
        if until.get("forever"):
            return True
        if "release" in until:
            return until["release"] == self._release
        try:
            return now < datetime.fromisoformat(until["time"])
        except (KeyError, ValueError):
            return False

    @callback
    def async_ignore(self, alert: str, until: Mapping[str, str]) -> None:
        self._until[alert] = dict(until)
        self._store.async_delay_save(lambda: dict(self._until), 1)

    @staticmethod
    def forever() -> dict[str, str]:
        return {"forever": "yes"}

    def until_next_release(self) -> dict[str, str]:
        return {"release": self._release}

    @staticmethod
    def until_time(time: datetime) -> dict[str, str]:
        return {"time": time.isoformat()}


def issue_id(entry_id: str, alert: str) -> str:
    return f"alert_{alert}_{entry_id}"


@dataclass
class Notifier:
    """One repairs issue per alert, updated in place and not too often.

    A repairs issue, rather than a notification, so its fix flow can ignore it
    this time, permanently, until the next release, or for a day.
    """

    hass: HomeAssistant
    entry_id: str
    ignores: Ignores | None = None
    _seen: dict[str, _Seen] = field(default_factory=dict)
    # Alerts ignored this time: hidden until what raised them goes away and
    # comes back. Not kept across a restart, after which it shows again.
    _this_time: set[str] = field(default_factory=set)

    @callback
    def notify(self, alert: str, title: str, message: str, now: datetime) -> None:
        seen = self._seen.get(alert)
        if seen is None:
            seen = self._seen[alert] = _Seen(0, now, now)
        # A logged message has no poll that passes: quiet for UPDATE_EVERY, it
        # went away.
        if alert in self._this_time and now - seen.last >= UPDATE_EVERY:
            self._this_time.discard(alert)
        seen.count += 1
        seen.last = now
        if alert in self._this_time:
            return
        if self.ignores is not None and self.ignores.ignored(alert, now):
            return
        if seen.shown is not None and now - seen.shown < UPDATE_EVERY:
            return
        seen.shown = now
        first = f"{dt.as_local(seen.first):%Y-%m-%d %H:%M:%S}"
        last = f"{dt.as_local(seen.last):%Y-%m-%d %H:%M:%S}"
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id(self.entry_id, alert),
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="alert",
            translation_placeholders={"title": title},
            data={
                "flow": "alert",
                "entry_id": self.entry_id,
                "alert": alert,
                "title": title,
                "message": message,
                "count": str(seen.count),
                "first": first,
                "last": last,
            },
        )

    @callback
    def async_passed(self, alert: str) -> None:
        """Note that what raised *alert* went away, for an alert ignored this time."""
        self._this_time.discard(alert)

    @callback
    def async_ignore_this_time(self, alert: str) -> None:
        """Hide *alert* until what raised it goes away and comes back."""
        self._this_time.add(alert)
        if (seen := self._seen.get(alert)) is not None:
            seen.shown = None
        ir.async_delete_issue(self.hass, DOMAIN, issue_id(self.entry_id, alert))

    @callback
    def async_forget(self, alert: str) -> None:
        """Take down an alert's issue, until it comes back after an ignore lapses."""
        self._seen.pop(alert, None)
        ir.async_delete_issue(self.hass, DOMAIN, issue_id(self.entry_id, alert))

    @callback
    def async_clear(self) -> None:
        """Take down every alert this entry raised."""
        for alert in list(self._seen):
            self.async_forget(alert)


class Watchdog:
    """Runs every rule after each poll and raises what they find."""

    def __init__(
        self, notifier: Notifier, verbosity: AlertVerbosity = AlertVerbosity.DEBUG
    ) -> None:
        self._notifier = notifier
        self._rules: list[Rule] = [
            rule() for rule in RULES if rule.level.rank <= verbosity.rank
        ]

    @callback
    def async_ignore_this_time(self, alert: str) -> None:
        self._notifier.async_ignore_this_time(alert)

    @callback
    def async_ignore_permanently(self, alert: str) -> None:
        self._ignore(alert, lambda ignores: ignores.forever())

    @callback
    def async_ignore_until_next_release(self, alert: str) -> None:
        self._ignore(alert, lambda ignores: ignores.until_next_release())

    @callback
    def async_ignore_for_a_day(self, alert: str) -> None:
        until = dt.now() + IGNORE_FOR
        self._ignore(alert, lambda ignores: ignores.until_time(until))

    def _ignore(
        self, alert: str, until: Callable[[Ignores], Mapping[str, str]]
    ) -> None:
        if (ignores := self._notifier.ignores) is not None:
            ignores.async_ignore(alert, until(ignores))
        self._notifier.async_forget(alert)

    @callback
    def observe(self, view: PollView) -> None:
        for rule in list(self._rules):
            try:
                message = rule.check(view)
            except Exception:
                self._rules.remove(rule)
                _LOGGER.debug("The %s alert rule broke", rule.name, exc_info=True)
                continue
            if message is None:
                self._notifier.async_passed(rule.name)
            else:
                self._notifier.notify(rule.name, rule.title, message, view.at)

    @callback
    def log_record(self, logger: str, message: str, template: str) -> None:
        """Raise a warning or error the integration logged."""
        # One notification per kind of message, whatever values it carried.
        key = f"log_{zlib.crc32(f'{logger}:{template}'.encode()):08x}"
        self._notifier.notify(
            key, f"Logged by {logger.rsplit('.', 1)[-1]}", message, dt.now()
        )


class _Forwarder(logging.Handler):
    """Hands the integration's warnings and errors to the watchdog, on its loop."""

    def __init__(self, hass: HomeAssistant, watchdog: Watchdog) -> None:
        super().__init__(logging.WARNING)
        self._hass = hass
        self._watchdog = watchdog

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == __name__:
            return
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        if record.exc_info and record.exc_info[1] is not None:
            message += f"\n\n{type(record.exc_info[1]).__name__}: {record.exc_info[1]}"
        self._hass.loop.call_soon_threadsafe(
            self._watchdog.log_record, record.name, message, str(record.msg)
        )


# Only readings: a control is unavailable on purpose while Modbus Control is off.
_READING_DOMAINS: Final = frozenset({"sensor", "binary_sensor"})


def unavailable_keys(
    hass: HomeAssistant, entry: ConfigEntry, entries: Iterable[Any] | None = None
) -> frozenset[str]:
    """Return the keys of this entry's enabled readings that show unavailable."""
    prefix = f"{entry.entry_id}_"
    if entries is None:
        entries = entity_registry.async_entries_for_config_entry(
            entity_registry.async_get(hass), entry.entry_id
        )
    return frozenset(
        registry_entry.unique_id.removeprefix(prefix)
        for registry_entry in entries
        if registry_entry.domain in _READING_DOMAINS
        and not registry_entry.disabled_by
        and (state := hass.states.get(registry_entry.entity_id)) is not None
        and state.state == STATE_UNAVAILABLE
    )


def view_of(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: EcoflowCoordinator
) -> PollView:
    """Take what the last poll left behind."""
    checks = coordinator.control.reading_checks
    client = coordinator.modbus_client
    return PollView(
        at=dt.now(),
        ok=coordinator.last_update_success,
        connected=coordinator.connected,
        data=coordinator.data or {},
        findings=checks.findings,
        distrusted=checks.distrusted,
        control_enabled=coordinator.control.enabled,
        control_testing=coordinator.control.test_running,
        control_status=coordinator.control.status,
        writes=dict(client.writes),
        reconnects=client.reconnects,
        held_totals=frozenset(coordinator.long_held_totals),
        scan_interval=timedelta(seconds=coordinator.scan_interval),
        unavailable=unavailable_keys(hass, entry),
    )


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.alerts")


async def async_forget_ignores(hass: HomeAssistant, entry_id: str) -> None:
    """Forget every alert ignored for *entry_id*, as when set back to Essential."""
    await _store(hass, entry_id).async_remove()


def running(entry_id: str) -> Watchdog | None:
    """Return the watchdog of *entry_id*, if its verbosity is above Essential."""
    return _RUNNING.get(entry_id)


async def async_start(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: EcoflowCoordinator,
    verbosity: AlertVerbosity,
) -> Callable[[], None]:
    """Watch *coordinator* until the returned callback stops it."""
    integration = await async_get_integration(hass, DOMAIN)
    ignores = Ignores(_store(hass, entry.entry_id), str(integration.version))
    await ignores.async_load()
    notifier = Notifier(hass, entry.entry_id, ignores)
    watchdog = Watchdog(notifier, verbosity)
    _RUNNING[entry.entry_id] = watchdog
    handler = _Forwarder(hass, watchdog)
    package = logging.getLogger(_PACKAGE_LOGGER)
    if verbosity is AlertVerbosity.DEBUG:
        package.addHandler(handler)

    @callback
    def _after_poll() -> None:
        watchdog.observe(view_of(hass, entry, coordinator))

    remove_listener = coordinator.async_add_listener(_after_poll)
    _LOGGER.info("Alerts at %s verbosity", verbosity)

    def _stop() -> None:
        remove_listener()
        package.removeHandler(handler)  # a no-op unless it was added
        _RUNNING.pop(entry.entry_id, None)
        notifier.async_clear()

    return _stop
