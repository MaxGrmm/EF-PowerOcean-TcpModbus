"""Modbus control of the inverter: what to command, and keeping track of what it does.

The coordinator reads and this decides what the inverter should be doing and commands it.
Everything here is driven by one selected feature plus two state-of-charge guards,
and nothing reaches the wire unless the inverter is currently following us.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, NamedTuple, Protocol

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt

from .const import (
    CONTROL_COMMAND_BATTERY_SAVER_BIT,
    CONTROL_COMMAND_METHOD_MASK,
    CONTROL_COMMAND_METHOD_SHIFT,
    CONTROL_COMMAND_REGISTER,
    CONTROL_COMMAND_UNSAFE_BITS,
    CONTROL_FEATURES,
    CONTROL_POWER_FALLBACK_MAX,
    CONTROL_STATUS_DAMPING_POLLS,
    DEFAULT_BATTERY_RESERVE_SOC,
    DEFAULT_CHARGE_LIMIT_SOC,
    FEED_IN_POWER_MAX_KEY,
    FEED_IN_POWER_MAX_SETTING_KEY,
    GUARD_DIRECT_HANDBACK_W,
    GUARD_HANDBACK_MAX_S,
    GUARD_HANDBACK_S,
    GUARD_HANDBACK_W,
    GUARD_POWER_DEADBAND_W,
    GUARD_SETTLE_S,
    GUARD_SOC_HYSTERESIS,
    GUARD_TRACKING_STEP_W,
    HEARTBEAT_REGISTER,
    HEARTBEAT_WINDOW_S,
    HOLD_SETPOINT_W,
    MIN_CONTROL_DWELL_S,
    SOLAR_EXPORT_CAP_MARGIN_W,
)
from .heartbeat import Heartbeat
from .modbus import ModbusClient
from .models import (
    BATTERY_FULL_SOC,
    POWER_TOLERANCE_FRACTION,
    POWER_TOLERANCE_W,
    ControlFeature,
    ControlMode,
    ControlStatus,
    GridFeedMode,
    InverterModel,
    RegisterDef,
    RegisterType,
    deviation_state,
    encode_register,
)
from .plans import Plan, PlanState, battery_power

_LOGGER = logging.getLogger(__name__)

# The statuses of the two state-of-charge guards, as opposed to a mode's own.
_GUARDS = frozenset({ControlStatus.CHARGE_LIMIT_REACHED, ControlStatus.RESERVE_REACHED})


class NotifyListeners(Protocol):
    """Tells the entities to re-read the manager's state."""

    def __call__(self) -> None: ...


class RequestRefresh(Protocol):
    """Polls the device now, so a write shows without waiting for the next poll."""

    async def __call__(self) -> None: ...


class WriteSetting(Protocol):
    """Writes a register, verifies it and publishes the value, like a number does.

    publish_as replaces the written value in the published data.
    """

    async def __call__(
        self, register: RegisterDef, value: int, *, publish_as: Any = None
    ) -> None: ...


class CommandExpired(Protocol):
    """Announces that a command was not renewed and the mode is back to automatic."""

    def __call__(self, feature: ControlFeature) -> None: ...


def _allows_export(
    mode: GridFeedMode | None, power: float, percent: float | None = None
) -> bool:
    """Return whether these feed settings let the inverter export at all.

    The watt cap only counts in the watt-limited mode and the percentage only in
    the percentage mode; an unknown percentage is taken to allow some export.
    """
    if mode is GridFeedMode.UNLIMITED:
        return True
    if mode is GridFeedMode.LIMITED_PERCENT:
        return percent is None or int(percent) > 0
    return int(power) > 0


def _device_export_limit(data: dict[str, Any]) -> float | None:
    """Return the most the device lets out, infinity if uncapped, or None if unknown.

    The cap is the one in force where the model reports it, since that is where the
    inverter curtails, and it only counts in the mode that applies it.
    """
    mode = data.get("grid_feed_mode")
    if mode is GridFeedMode.UNLIMITED:
        return math.inf
    if mode is GridFeedMode.LIMITED:
        limit = data.get(FEED_IN_POWER_MAX_KEY)
        return None if limit is None else float(limit)
    if mode is GridFeedMode.LIMITED_PERCENT:
        percent = data.get("feed_in_power_max_percent")
        rated = data.get("inverter_rated_power")
        if percent is None or not rated:
            return None
        return float(rated) * float(percent) / 100
    return None


def _configured_feed_cap(data: dict[str, Any]) -> float | None:
    """Return the export cap as configured (40538), which is what a restore puts back.

    Deliberately without a fallback: the effective cap (40609) can sit below it after
    the internal safety rules, and writing that back would lower the configured cap
    for good.
    """
    return data.get(FEED_IN_POWER_MAX_SETTING_KEY)


class HandbackPhase(Enum):
    """Who runs self-consumption while a guard is on."""

    TRACKING = auto()
    PENDING = auto()
    HANDED_BACK = auto()


@dataclass
class GuardHandback:
    """Progress of handing self-consumption back to the inverter under a guard."""

    phase: HandbackPhase = HandbackPhase.TRACKING
    since: datetime | None = None
    wait_s: float = GUARD_HANDBACK_S

    def elapsed_s(self, now: datetime) -> float:
        return 0.0 if self.since is None else (now - self.since).total_seconds()

    def enter(self, phase: HandbackPhase, now: datetime) -> None:
        self.phase = phase
        self.since = now

    def take_back(self, now: datetime) -> None:
        """Resume tracking, waiting longer next time if the hand-back was brief."""
        if self.phase is HandbackPhase.HANDED_BACK:
            self.wait_s = (
                min(2 * self.wait_s, GUARD_HANDBACK_MAX_S)
                if self.elapsed_s(now) < self.wait_s
                else GUARD_HANDBACK_S
            )
        self.enter(HandbackPhase.TRACKING, now)


class Decision(NamedTuple):
    """What to send the inverter, and the status that explains it, if any."""

    feature: ControlFeature
    power: float
    status: ControlStatus | None = None
    # A guard must not lag the battery, and an adapting mode has its own
    # hysteresis, so neither waits out the dwell between commands.
    bypass_dwell: bool = False


class ControlManager:
    """Manages the control of the inverter."""

    def __init__(
        self,
        modbus_client: ModbusClient,
        *,
        registers_by_key: dict[str, RegisterDef],
        limits: dict[str, Any],
        inverter_model: InverterModel,
        enabled: bool,
        scan_interval_s: float,
        on_update: NotifyListeners,
        on_refresh: RequestRefresh,
        write_setting: WriteSetting,
        on_command_expired: CommandExpired,
        heartbeat: Heartbeat | None = None,
    ) -> None:
        self._modbus_client = modbus_client
        self._registers_by_key = registers_by_key
        self._limits = limits
        self._inverter_model = inverter_model
        self._on_update = on_update
        self._on_refresh = on_refresh
        self._write_setting = write_setting
        self._on_command_expired = on_command_expired

        self._enabled = enabled
        # One beating on another clock can be passed in, as for a simulation.
        self._heartbeat = heartbeat or Heartbeat(
            modbus_client, scan_interval_s=scan_interval_s
        )

        # A restart stops the heartbeat, so the inverter has already handed control
        # back to the app by the time we get here: automatic is the truth, not a
        # guess. The parameters are restored from disk, the mode deliberately is not.
        self._feature = ControlFeature.AUTOMATIC
        self._feature_power: dict[ControlFeature, float] = {
            feature: definition.default_power
            for feature, definition in CONTROL_FEATURES.items()
            if definition.has_power
        }
        self._charge_limit_soc = DEFAULT_CHARGE_LIMIT_SOC
        self._battery_reserve_soc = DEFAULT_BATTERY_RESERVE_SOC
        self._charge_guard = False
        self._reserve_guard = False
        # When a command sent with an expiry returns to automatic, unless renewed.
        self._expires_at: datetime | None = None
        # Which guard, if any, is forcing the current command.
        self._command_status: ControlStatus | None = None
        self._handback = GuardHandback()
        self._plan_state = PlanState(ControlFeature.AUTOMATIC)
        # The guard stopping the selected command, kept until it is clearly clear.
        self._guard_holding: ControlStatus | None = None

        self._commanded_feature = ControlFeature.AUTOMATIC
        self._commanded_power = 0.0
        # The setpoint that the latest small correction replaced.
        self._retuned_from: float | None = None
        self._battery_saver = False
        # The export settings to put back, taken from the device itself whenever it
        # allows an export at all.
        self._grid_feed_restore: dict[str, int] | None = None
        # While the switch holds the export off, readings are ours, not the device's.
        self._grid_feed_stopped = False
        self._last_control_write_time: datetime | None = None
        # A restart within the inverter's control window leaves it still following the
        # method it was last told, so the first poll re-asserts rather than assuming
        # control lapsed. Nothing is written at all while the gate is off.
        self._control_stale = enabled
        # How the commanded setpoint is being met, held over brief excursions.
        self._deviation = ControlStatus.ACTIVE
        self._deviation_candidate: ControlStatus | None = None
        self._deviation_polls = 0
        # The last frame read, so a ceiling can be quoted between polls.
        self._data: dict[str, Any] = {}

    @property
    def enabled(self) -> bool:
        """Return whether the user has switched Modbus control on."""
        return self._enabled

    @property
    def heartbeat_supported(self) -> bool | None:
        """Return whether the inverter accepts the heartbeat, or None if untested."""
        return self._heartbeat.supported

    @property
    def last_heartbeat_time(self) -> datetime | None:
        return self._heartbeat.last_success

    @property
    def in_control(self) -> bool:
        """Return whether the inverter is currently accepting our commands."""
        return self._enabled and self._heartbeat.in_control

    @property
    def handing_back(self) -> bool:
        """Return whether the inverter still obeys us after control was turned off."""
        return not self._enabled and self._heartbeat.in_control

    @property
    def hands_back_at(self) -> datetime | None:
        """Return when the inverter returns to the app, while it is handing back."""
        last_beat = self._heartbeat.last_success
        if not self.handing_back or last_beat is None:
            return None
        return last_beat + timedelta(seconds=HEARTBEAT_WINDOW_S)

    @property
    def selected_feature(self) -> ControlFeature:
        """Return the mode the user selected, running or merely waiting."""
        return self._feature

    @property
    def method(self) -> ControlMode:
        """Return the protocol control method currently being commanded."""
        return CONTROL_FEATURES[self._commanded_feature].method

    @property
    def power(self) -> float:
        """Return the power magnitude currently being commanded."""
        return self._commanded_power

    @property
    def command(self) -> int:
        """Return the control command word that the commanded state composes to."""
        return self._compose_control_command()

    @property
    def expires_at(self) -> datetime | None:
        """Return when the running command returns to automatic, if it expires."""
        return self._expires_at

    @property
    def charge_limit_soc(self) -> float:
        return self._charge_limit_soc

    @property
    def battery_reserve_soc(self) -> float:
        return self._battery_reserve_soc

    @property
    def battery_saver_commanded(self) -> bool:
        """Return whether battery saver mode is being commanded."""
        return self._battery_saver

    @property
    def grid_feed_restore(self) -> dict[str, int] | None:
        """Return the settings to restore, or None while the export is not allowed."""
        return self._grid_feed_restore

    @property
    def grid_feed_restore_attributes(self) -> dict[str, Any]:
        """Return the settings to restore, with the mode as its enum."""
        restore = self._grid_feed_restore or {}
        return {
            "restores_feed_mode": GridFeedMode.from_register(restore.get("mode")),
            "restores_feed_in_power_max": restore.get("power"),
        }

    @staticmethod
    def grid_feed_allowed(data: dict[str, Any] | None) -> bool:
        """Return whether a frame shows the inverter allowed to export."""
        data = data or {}
        return _allows_export(
            data.get("grid_feed_mode"),
            _configured_feed_cap(data) or 0,
            data.get("feed_in_power_max_percent"),
        )

    @property
    def grid_feed_supported(self) -> bool:
        """Return whether the inverter is in a mode the switch knows how to restore."""
        mode = self._data.get("grid_feed_mode")
        return mode is None or (isinstance(mode, GridFeedMode) and mode.switchable)

    @property
    def grid_feed_switchable(self) -> bool:
        """Return whether stopping the export could be undone again.

        With nothing but a limited mode and a zero cap to restore the switch would
        be a one-way door: it could only ever turn the export off.
        """
        original = self._grid_feed_restore
        return (
            self.grid_feed_supported
            and original is not None
            and _allows_export(
                GridFeedMode.from_register(original["mode"]), original["power"]
            )
        )

    @property
    def active_guard(self) -> ControlStatus | None:
        """Return the guard that is on, even while the status shows a problem.

        Export Solar First explains its commands the same way a guard does, but it
        is the mode at work rather than a limit, so it is left out.
        """
        guard = self._command_status if self.in_control else None
        return guard if guard in _GUARDS else None

    def feature_power(self, feature: ControlFeature) -> float:
        """Return the configured power, or zero for a mode that has none."""
        return self._feature_power.get(feature, 0.0)

    def feature_power_max(self, feature: ControlFeature) -> float:
        return self._control_power_ceiling(feature)

    @property
    def status(self) -> ControlStatus:
        """Explain, in one word, what the selected mode is achieving."""
        if not self.in_control:
            if self.handing_back:
                return ControlStatus.HANDING_BACK
            return ControlStatus.NO_MODBUS_CONTROL
        if self._command_status is not None:
            if self._deviation is not ControlStatus.ACTIVE:
                return self._deviation
            return self._command_status
        # Only a hold the battery cannot need leaves a selected mode uncommanded.
        if (
            self._feature is not ControlFeature.AUTOMATIC
            and self._commanded_feature is ControlFeature.AUTOMATIC
        ):
            return ControlStatus.HOLD_NOT_NEEDED
        if not CONTROL_FEATURES[self._commanded_feature].commands_power:
            return ControlStatus.AUTOMATIC
        return self._deviation

    def dump_state(self) -> dict[str, Any]:
        """Return what must survive a restart, in a JSON-serializable form."""
        return {
            "modbus_control": self._enabled,
            "feature_power": {
                str(feature): power for feature, power in self._feature_power.items()
            },
            "charge_limit_soc": self._charge_limit_soc,
            "battery_reserve_soc": self._battery_reserve_soc,
            "battery_saver": self._battery_saver,
            "grid_feed_restore": self._grid_feed_restore,
            "grid_feed_stopped": self._grid_feed_stopped,
        }

    def load_state(self, stored: dict[str, Any]) -> None:
        """Restore what each mode would command, but never which one was selected."""
        if (enabled := stored.get("modbus_control")) is not None:
            self._enabled = bool(enabled)
            self._control_stale = self._enabled
        for feature in self._feature_power:
            if (
                power := (stored.get("feature_power") or {}).get(str(feature))
            ) is not None:
                self._feature_power[feature] = float(power)
        if (charge := stored.get("charge_limit_soc")) is not None:
            self._charge_limit_soc = float(charge)
        if (reserve := stored.get("battery_reserve_soc")) is not None:
            self._battery_reserve_soc = float(reserve)
        # A restart does not turn battery saver off on the inverter, so reporting it
        # off would be a lie until the user toggled it twice.
        if (saver := stored.get("battery_saver")) is not None:
            self._battery_saver = bool(saver)
        self._grid_feed_restore = stored.get("grid_feed_restore") or None
        self._grid_feed_stopped = bool(stored.get("grid_feed_stopped"))

    def start(self) -> None:
        """Begin holding control authority, if the user switched control on."""
        if self._enabled:
            self._heartbeat.start()

    async def async_stop(self) -> None:
        await self._heartbeat.async_stop()

    async def async_set_enabled(self, enabled: bool) -> None:
        """Take control from the app, or hand it back.

        Nothing is written when handing back: stopping the heartbeat is enough, and
        the inverter returns to its app settings once its 60 s window runs out.
        """
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if enabled:
            # We cannot know what the inverter follows now, so the next poll re-sends.
            self._control_stale = True
            self._heartbeat.start()
        else:
            await self._heartbeat.async_stop()
            # Start over as if freshly loaded, so no mode comes back by itself.
            self._feature = ControlFeature.AUTOMATIC
            self._commanded_feature = ControlFeature.AUTOMATIC
            self._commanded_power = 0.0
            self._retuned_from = None
            self._command_status = None
            self._handback = GuardHandback()
            self._expires_at = None
            self._control_stale = False
            self._reset_deviation()
        self._on_update()

    def mark_stale(self) -> None:
        """Take stock of the inverter after a connection outage.

        Only an outage that outlasted the inverter's 60 s window handed it back to
        the app; a shorter one left it following the command it already has. The
        verdict is reached here, before the reconnected heartbeat refreshes the
        window, because afterwards the two are indistinguishable.
        """
        if not self._heartbeat.in_control:
            self._control_stale = True
        self._heartbeat.note_reconnect()

    def _require_modbus_control(self) -> None:
        """Refuse a command the inverter would store and ignore."""
        if not self._enabled:
            raise HomeAssistantError(
                "Modbus control is off. Turn on the Modbus Control switch to "
                "command the inverter; nothing was written."
            )

    async def _async_require_control_authority(self) -> None:
        """Confirm the inverter is still following us before the write that follows.

        The inverter stores every write but only acts on it while the heartbeat is
        current, so a command sent without one looks successful and does nothing.
        """
        self._require_modbus_control()

        if not await self._heartbeat.async_ensure_fresh():
            raise HomeAssistantError(
                f"Heartbeat write to register {HEARTBEAT_REGISTER} failed or was "
                "rejected, so the inverter would ignore the command. Nothing written."
            )

    async def async_select_feature(self, feature: ControlFeature) -> None:
        """Select a control feature."""
        if feature is not ControlFeature.AUTOMATIC:
            self._require_modbus_control()

        self._feature = feature
        self._handback = GuardHandback()
        self._expires_at = None
        await self.async_apply(force=True)

    async def async_set_command(
        self,
        feature: ControlFeature,
        *,
        power: float | None = None,
        charge_limit_soc: float | None = None,
        expire_in_s: float | None = None,
    ) -> None:
        """Set a mode, its power and the Charge Limit together, before anything is sent.

        With expire_in_s the mode returns to automatic unless the command is sent
        again in time. Sending the same command again only moves that time.
        """
        if feature is not ControlFeature.AUTOMATIC:
            self._require_modbus_control()

        if power is not None:
            self._feature_power[feature] = self._clamp_power(power, feature)
        if charge_limit_soc is not None:
            self._update_limits(charge_limit_soc, self._battery_reserve_soc)
        if feature is not self._feature:
            self._feature = feature
            self._handback = GuardHandback()
        # Automatic is where an expiry would return to, so it has nothing to expire.
        self._expires_at = (
            None
            if expire_in_s is None or feature is ControlFeature.AUTOMATIC
            else dt.now() + timedelta(seconds=expire_in_s)
        )
        await self.async_apply(force=True)

    async def async_set_feature_power(
        self, feature: ControlFeature, watts: float
    ) -> None:
        """Set a mode's power. Editable whether or not that mode is selected."""
        self._feature_power[feature] = self._clamp_power(watts, feature)
        await self.async_apply(force=True)

    async def async_set_charge_limit_soc(self, soc: float) -> None:
        """Set the state of charge above which the battery must not be charged."""
        if self._update_limits(soc, self._battery_reserve_soc):
            await self.async_apply(force=True)

    async def async_set_battery_reserve_soc(self, soc: float) -> None:
        """Set the state of charge below which the battery must not be drained."""
        if self._update_limits(self._charge_limit_soc, soc):
            await self.async_apply(force=True)

    def _update_limits(
        self, charge_limit_soc: float, battery_reserve_soc: float
    ) -> bool:
        """Store new limits and return whether either one changed.

        A changed limit starts its hysteresis afresh, but only where the last frame
        can work the latch out again. The hand-back survives a change that leaves
        both guards as they were, since who should run the house is then the same.
        """
        charge_limit_soc = max(0.0, min(100.0, charge_limit_soc))
        battery_reserve_soc = max(0.0, min(100.0, battery_reserve_soc))
        if (charge_limit_soc, battery_reserve_soc) == (
            self._charge_limit_soc,
            self._battery_reserve_soc,
        ):
            return False

        latched = (self._charge_guard, self._reserve_guard)
        if self._data.get("battery_soc") is not None:
            if charge_limit_soc != self._charge_limit_soc:
                self._charge_guard = False
            if battery_reserve_soc != self._battery_reserve_soc:
                self._reserve_guard = False
        self._charge_limit_soc = charge_limit_soc
        self._battery_reserve_soc = battery_reserve_soc
        self._update_guards(self._data)
        if (self._charge_guard, self._reserve_guard) != latched:
            self._handback = GuardHandback()
        return True

    async def async_set_battery_saver(self, enabled: bool) -> None:
        """Command battery saver mode without disturbing the control intent."""
        previous = self._battery_saver
        self._battery_saver = enabled
        try:
            await self._async_apply_control_command()
        except HomeAssistantError:
            self._battery_saver = previous
            raise

    async def async_set_grid_feed(self, allow: bool) -> None:
        """Stop the export, or put back the inverter's own last export settings.

        The power cap only applies in limited mode, so the two registers are written
        in the order that never leaves the export briefly uncapped.
        """
        restore = self._grid_feed_restore
        if not self.grid_feed_supported:
            raise HomeAssistantError(
                "The grid feed cannot be switched while the inverter is in a feed-in "
                "mode it cannot restore, such as the percentage limit. Nothing written."
            )
        if not self.grid_feed_switchable:
            raise HomeAssistantError(
                "The grid feed cannot be switched: the inverter has not reported an "
                "export it would allow, so there is nothing to restore."
            )
        # The PowerOcean stores both registers but only acts on them under control.
        await self._async_require_control_authority()

        mode = self._registers_by_key["grid_feed_mode"]
        power = self._registers_by_key[FEED_IN_POWER_MAX_SETTING_KEY]
        mode_value = restore["mode"] if allow else GridFeedMode.LIMITED.register_value
        # Readers expect the enum a poll derives, never the register's raw 0/1.
        mode_state = GridFeedMode.from_register(mode_value)
        if allow:
            await self._write_setting(power, restore["power"])
            await self._write_setting(mode, mode_value, publish_as=mode_state)
            self._grid_feed_stopped = False
        else:
            # Set before writing, so a half-done stop cannot be adopted either.
            self._grid_feed_stopped = True
            await self._write_setting(mode, mode_value, publish_as=mode_state)
            await self._write_setting(power, 0)

    def _track_grid_feed_restore(self, data: dict[str, Any]) -> None:
        """Remember the export settings to put back, while there are any to keep.

        Nothing is adopted while the switch holds the export off, since what the
        device reports then is our own write and adopting it would lose the setting
        the switch has to put back. Any other reading is the inverter's own, so
        raising the cap in the EcoFlow app - an installer lifting an export limit,
        say - is picked up on the next poll.

        The cap kept is the configured one, never the effective one: the two differ
        once the safety rules derate the export, and restoring the effective cap
        would write the derating in for good. A mode the switch cannot restore, such
        as the percentage limit, is not adopted either.
        """
        if self._grid_feed_stopped:
            return
        mode = data.get("grid_feed_mode")
        power = _configured_feed_cap(data)
        if (
            not isinstance(mode, GridFeedMode)
            or not mode.switchable
            or power is None
            or not _allows_export(mode, power)
        ):
            return

        updated = {"mode": mode.register_value, "power": int(power)}
        if updated != self._grid_feed_restore:
            _LOGGER.debug("Grid feed settings to restore are now %s", updated)
        self._grid_feed_restore = updated

    def _control_power_ceiling(self, feature: ControlFeature) -> float:
        """Return the lowest ceiling that applies to *feature*.

        Nothing can exceed the inverter's AC rating whatever the feature asks for,
        and a ceiling the firmware publishes caps it further. The battery modes are
        bounded by the configured module count instead: the inverter's charge and
        discharge limit registers report the limit set in the EcoFlow app, which
        Modbus control ignores, so honouring them would cap the user below what the
        hardware accepts.
        """
        definition = CONTROL_FEATURES[feature]
        ceilings = [float(CONTROL_POWER_FALLBACK_MAX)]

        if definition.limit_key is not None and (
            limit := self._data.get(definition.limit_key)
        ):
            ceilings.append(float(limit))
        # Zero means no battery count was configured, which bounds nothing.
        if definition.config_limit_key is not None and (
            limit := self._limits.get(definition.config_limit_key)
        ):
            ceilings.append(float(limit))
        # Power that has to pass the inverter's DC to AC stage cannot exceed it. Zero
        # or missing means the firmware did not report it, which bounds nothing.
        if definition.capacity_key is not None and (
            capacity := self._data.get(definition.capacity_key)
        ):
            ceilings.append(float(capacity))
        if rated := self._data.get("inverter_rated_power"):
            ceilings.append(float(rated))
        # The export cap only binds in the feed mode that applies it. One too small to
        # keep the margin under bounds nothing here, so a Solar Export Limit set while
        # the export is off is not lowered for good; the command handles that case.
        if feature is ControlFeature.EXPORT_SOLAR_FIRST and (
            limit := _device_export_limit(self._data)
        ):
            if limit > SOLAR_EXPORT_CAP_MARGIN_W:
                ceilings.append(limit)

        return min(ceilings)

    def _clamp_power(self, watts: float, feature: ControlFeature) -> float:
        """Clamp a magnitude to zero and the inverter's own ceiling for *feature*."""
        return max(0.0, min(float(watts), self._control_power_ceiling(feature)))

    def _update_guards(self, data: dict[str, Any]) -> None:
        """Latch both guards, each releasing well clear of where it engaged.

        A ceiling of 100 and a floor of 0 mean the guard is off, so an untouched
        install never takes control away from the app.
        """
        soc = data.get("battery_soc")
        if soc is None:
            return
        soc = float(soc)

        if self._charge_limit_soc >= 100.0:
            self._charge_guard = False
        elif soc >= self._charge_limit_soc:
            self._charge_guard = True
        elif soc <= self._charge_limit_soc - GUARD_SOC_HYSTERESIS:
            self._charge_guard = False

        if self._battery_reserve_soc <= 0.0:
            self._reserve_guard = False
        elif soc <= self._battery_reserve_soc:
            self._reserve_guard = True
        elif soc >= self._battery_reserve_soc + GUARD_SOC_HYSTERESIS:
            self._reserve_guard = False

    def _natural_battery_power(self, data: dict[str, Any]) -> float | None:
        """Estimate the battery power the inverter would reach without us.

        The guard logic hands control back when natural power flows the way the guard
        allows, so this must reconstruct that natural point whether the device is
        following us or running its own self-consumption. Measuring it on the grid
        side is preferred because it carries the conversion losses the panels do not.
        """
        from_grid_side = self._natural_from_grid_side(data)
        if from_grid_side is not None:
            return from_grid_side
        return self._natural_from_solar_side(data)

    def _natural_from_grid_side(self, data: dict[str, Any]) -> float | None:
        """Return the natural battery power read from the battery and the grid."""
        battery, grid = data.get("battery_power"), data.get("grid_power")
        if battery is None or grid is None:
            return None
        return float(battery) - float(grid)

    def _natural_from_solar_side(self, data: dict[str, Any]) -> float | None:
        """Return the natural battery power read from the solar and the house."""
        solar, house = data.get("solar_power"), data.get("house_power")
        if solar is None or house is None:
            return None
        return float(solar) - float(house)

    def _guarded_command(
        self,
        data: dict[str, Any],
        blocked: ControlStatus,
    ) -> Decision:
        """Reproduce Automatic on the battery setpoint, the allowed way only.

        The battery setpoint is a target and zero means no limit, so no single value
        says "do not charge, but discharge freely". We therefore clamp the natural
        battery power to the allowed direction.
        """
        natural = self._natural_battery_power(data)
        if natural is None:
            self._handback.take_back(dt.now())
            return self._hold(data, blocked)

        tracks = self._inverter_model.traits.guard_tracks_setpoints
        if not tracks:
            # The two sides of the balance agree in a frame that balances, which the
            # device does not always publish. Where they disagree, believe the smaller
            # surplus. That hands back sooner under a charge limit, where holding draws
            # from the grid, and later under a reserve, where handing back drains the
            # battery below it.
            from_grid_side = self._natural_from_grid_side(data)
            from_solar_side = self._natural_from_solar_side(data)
            if from_grid_side is not None and from_solar_side is not None:
                natural = min(from_grid_side, from_solar_side)

        if self._advance_handback(natural):
            return Decision(ControlFeature.AUTOMATIC, 0.0, blocked, bypass_dwell=True)

        if not tracks:
            return self._hold(data, blocked)

        if self._charge_guard:
            natural = min(natural, 0.0)
        if self._reserve_guard:
            natural = max(natural, 0.0)

        if natural == 0.0:
            return self._hold(data, blocked)

        # Ensures that we never import from the grid if the battery can cover the demand.
        if natural > 0:
            feature = ControlFeature.CHARGE_BATTERY
            watts = float(math.floor(natural))
        else:
            feature = ControlFeature.DISCHARGE_BATTERY
            watts = float(math.ceil(-natural))

        # Small changes wait for the battery to settle, and tiny ones that only
        # export a little are skipped.
        held = self._commanded_power
        slack = watts - held if natural > 0 else held - watts
        if self._commanded_feature is feature and (
            0.0 <= slack < GUARD_TRACKING_STEP_W
            or (abs(slack) < POWER_TOLERANCE_W and not self._battery_settled(data))
        ):
            watts = held

        if watts <= 0.0:
            return self._hold(data, blocked)
        return Decision(
            feature, self._clamp_power(watts, feature), blocked, bypass_dwell=True
        )

    def _battery_settled(self, data: dict[str, Any]) -> bool:
        """Return whether the battery has reached the current setpoint.

        Some inverters start over on every new setpoint, so correcting before the
        battery gets there can keep it from ever arriving. After GUARD_SETTLE_S we
        correct anyway.
        """
        definition = CONTROL_FEATURES[self._commanded_feature]
        measured = data.get(definition.measure_key) if definition.measure_key else None
        if measured is not None:
            target = self._commanded_power * definition.sign
            off_by = abs(float(measured) - target)
            close_enough = max(
                GUARD_TRACKING_STEP_W, abs(target) * POWER_TOLERANCE_FRACTION
            )
            # A battery that has not moved yet can already look close to a slightly
            # changed setpoint.
            off_from_previous = (
                abs(float(measured) - self._retuned_from * definition.sign)
                if self._retuned_from is not None
                else None
            )
            if off_by <= close_enough and (
                off_from_previous is None or off_by < off_from_previous
            ):
                return True
        return not self._control_written_within(GUARD_SETTLE_S)

    def _advance_handback(self, natural: float) -> bool:
        """Move the hand-back on by one poll and return whether the inverter runs itself.

        Under a charge limit, a house that clearly uses more than the solar can only
        be served by discharging, which the limit allows. The inverter does that by
        itself and faster than we can, so after a while we let it. The same goes for
        a clear surplus above the battery reserve.
        """
        handback = self._handback
        now = dt.now()
        if self._charge_guard and self._reserve_guard:
            handback.take_back(now)
            return False

        allowed = -1.0 if self._charge_guard else 1.0
        wanted = natural * allowed

        if not self._inverter_model.traits.guard_tracks_setpoints:
            if handback.phase is HandbackPhase.HANDED_BACK:
                if (
                    handback.elapsed_s(now) >= GUARD_SETTLE_S
                    and wanted <= -GUARD_DIRECT_HANDBACK_W
                ):
                    handback.take_back(now)
                    return False
                return True

            if self._control_written_within(GUARD_SETTLE_S):
                return False

            if wanted > GUARD_DIRECT_HANDBACK_W:
                handback.enter(HandbackPhase.HANDED_BACK, now)
                return True
            return False

        match handback.phase:
            case HandbackPhase.TRACKING:
                if wanted > GUARD_HANDBACK_W:
                    handback.enter(HandbackPhase.PENDING, now)
                return False
            case HandbackPhase.PENDING:
                if wanted <= GUARD_HANDBACK_W:
                    handback.enter(HandbackPhase.TRACKING, now)
                    return False
                if handback.elapsed_s(now) < handback.wait_s:
                    return False
                handback.enter(HandbackPhase.HANDED_BACK, now)
                return True
            case HandbackPhase.HANDED_BACK:
                if wanted < GUARD_POWER_DEADBAND_W:
                    handback.take_back(now)
                    return False
                return True

    def _hold(self, data: dict[str, Any], blocked: ControlStatus | None) -> Decision:
        """Hold the battery, unless holding it could only limit solar.

        A full battery cannot charge, so a battery limit set against a surplus
        forbids nothing the inverter could do anyway and leaves limiting the
        solar as its only way to balance. Stepping aside is safe because anything
        short of a clear surplus counts as a draw, so the hold is back before the
        house can reach the battery.
        """
        # A guard's hold goes out at once, a hold the mode asks for after the dwell.
        urgent = blocked is not None
        soc = data.get("battery_soc")
        surplus = self._natural_battery_power(data) or 0.0
        if (
            soc is not None
            and float(soc) >= BATTERY_FULL_SOC
            and surplus > GUARD_POWER_DEADBAND_W
        ):
            return Decision(ControlFeature.AUTOMATIC, 0.0, blocked, bypass_dwell=urgent)
        return Decision(
            ControlFeature.HOLD_BATTERY, HOLD_SETPOINT_W, blocked, bypass_dwell=urgent
        )

    def _desired_command(self, data: dict[str, Any]) -> Decision:
        """Decide what to send for the selected mode; plans.py says what each runs."""
        if self._plan_state.mode is not self._feature or not self._enabled:
            self._plan_state = PlanState(self._feature)
            self._guard_holding = None
        if not self._enabled:
            return Decision(ControlFeature.AUTOMATIC, 0.0)

        plan = self._plan_state.plan
        power = self._mode_power(data)
        surplus = self._natural_battery_power(data)
        command = self._adapt(data, surplus, power) if plan.adapts else plan.default
        return self._carry_out(plan, command, surplus, power, data)

    def _adapt(
        self, data: dict[str, Any], surplus: float | None, limit: float | None
    ) -> ControlFeature:
        """Return the command an adapting mode runs for the surplus now."""
        soc = data.get("battery_soc")
        battery_full = soc is not None and float(soc) >= BATTERY_FULL_SOC
        # An adapting mode only decides where a surplus goes. A full battery takes
        # none of it, and under the Charge Limit none may go into the battery, which
        # Automatic under that guard already ensures.
        if battery_full or self._charge_guard or limit is None:
            surplus = None
        return self._plan_state.choose(
            surplus, limit or 0.0, dt.now(), GUARD_POWER_DEADBAND_W
        )

    def _carry_out(
        self,
        plan: Plan,
        command: ControlFeature,
        surplus: float | None,
        power: float | None,
        data: dict[str, Any],
    ) -> Decision:
        """Send *command*, holding the battery where a guard forbids what it does."""
        guard = self._engaged_guard()

        if command is ControlFeature.AUTOMATIC:
            self._guard_holding = None
            if guard is not None:
                # The inverter has no Automatic that only goes one way, so under a
                # guard it is reproduced on the battery setpoint.
                return self._guarded_command(data, guard)
            status = ControlStatus.AUTOMATIC if plan.adapts else None
            return Decision(command, 0.0, status, bypass_dwell=plan.adapts)

        if command is ControlFeature.HOLD_BATTERY:
            self._guard_holding = None
            if not plan.adapts:
                return self._hold(data, None)
            return Decision(
                command,
                HOLD_SETPOINT_W,
                guard or plan.hold_status,
                bypass_dwell=True,
            )

        power = power or 0.0
        if power <= 0.0 and command in (
            ControlFeature.CHARGE_BATTERY,
            ControlFeature.DISCHARGE_BATTERY,
        ):
            # The inverter reads a zero battery setpoint as no limit at all and runs
            # itself, guards or not.
            self._guard_holding = None
            return self._hold(data, None)

        self._guard_holding = self._guard_against(command, surplus, power)
        if self._guard_holding is not None:
            # A mode the user chose is stopped, never turned around.
            return self._hold(data, self._guard_holding)
        status = (guard or ControlStatus.ACTIVE) if plan.adapts else None
        return Decision(command, power, status, bypass_dwell=plan.adapts)

    def _mode_power(self, data: dict[str, Any]) -> float | None:
        """Return the selected mode's power, which an adapting mode uses as its limit.

        None for a mode without one, or for Export Solar First while the device
        exports nothing.
        """
        if self._feature is ControlFeature.EXPORT_SOLAR_FIRST:
            return self._solar_export_target(data)
        if CONTROL_FEATURES[self._feature].has_power:
            return self._clamp_power(self.feature_power(self._feature), self._feature)
        return None

    def _engaged_guard(self) -> ControlStatus | None:
        """Return the guard that is on, the Charge Limit first if both are."""
        if self._charge_guard:
            return ControlStatus.CHARGE_LIMIT_REACHED
        if self._reserve_guard:
            return ControlStatus.RESERVE_REACHED
        return None

    def _guard_against(
        self, command: ControlFeature, surplus: float | None, power: float
    ) -> ControlStatus | None:
        """Return the guard *command* would breach, if any."""
        battery = battery_power(command, surplus, power)
        # Export and Import move the battery with the surplus, so near their turning
        # point a guard would stop and release them every poll. They get a deadband
        # before they are stopped, then stay stopped until they would not breach.
        moves_with_surplus = battery_power(command, None, power) is None
        if battery is None:
            return self._engaged_guard()
        for status, engaged, forbidden_w in (
            (ControlStatus.CHARGE_LIMIT_REACHED, self._charge_guard, battery),
            (ControlStatus.RESERVE_REACHED, self._reserve_guard, -battery),
        ):
            margin = (
                GUARD_POWER_DEADBAND_W
                if moves_with_surplus and self._guard_holding is not status
                else 0.0
            )
            if engaged and forbidden_w > margin:
                return status
        return None

    def _solar_export_target(self, data: dict[str, Any]) -> float | None:
        """Return the export to hold the meter at, or None if there is none.

        The Solar Export Limit, kept just under the device's cap when it is set at
        it. None when the device exports nothing, such as with the Grid Feed-in
        switch off, or when its cap cannot be told.
        """
        device_limit = _device_export_limit(data)
        if device_limit is None or device_limit <= SOLAR_EXPORT_CAP_MARGIN_W:
            return None
        feature = ControlFeature.EXPORT_SOLAR_FIRST
        return min(
            self._clamp_power(self.feature_power(feature), feature),
            device_limit - SOLAR_EXPORT_CAP_MARGIN_W,
        )

    def _control_written_within(self, seconds: float) -> bool:
        """Return whether the last command went out less than *seconds* ago."""
        if self._last_control_write_time is None:
            return False
        age = (dt.now() - self._last_control_write_time).total_seconds()
        return age < seconds

    def _reset_deviation(self) -> None:
        """Forget how the last command was going; a new one starts from nothing."""
        self._deviation = ControlStatus.ACTIVE
        self._deviation_candidate = None
        self._deviation_polls = 0

    def _update_deviation(self, data: dict[str, Any]) -> None:
        """Judge the commanded setpoint, ignoring a miss that passes in a poll or two.

        A load switching on pulls the measurement well outside tolerance until the
        battery takes the step up, which is the system working rather than failing.
        """
        definition = CONTROL_FEATURES[self._commanded_feature]
        if not definition.commands_power:
            self._reset_deviation()
            return

        data = data or {}
        measured = (
            data.get(definition.measure_key)
            if definition.measure_key is not None
            else None
        )
        inverter_floor = float(data.get("min_soc_limit") or 0.0)
        state = deviation_state(
            signed_target=self._commanded_power * definition.sign,
            measured=None if measured is None else float(measured),
            soc=None if (soc := data.get("battery_soc")) is None else float(soc),
            min_soc=max(inverter_floor, self._battery_reserve_soc),
        )

        if state is ControlStatus.ACTIVE:
            self._reset_deviation()
            return
        if state is not self._deviation_candidate:
            self._deviation_candidate = state
            self._deviation_polls = 0
        self._deviation_polls += 1
        if self._deviation_polls >= CONTROL_STATUS_DAMPING_POLLS:
            self._deviation = state

    async def async_poll(self, data: dict[str, Any]) -> None:
        """Run from a poll, where a write failure must not stop the read."""
        self._track_grid_feed_restore(data)
        try:
            await self.async_apply(data, notify=False)
        except HomeAssistantError as err:
            _LOGGER.debug(f"Could not apply {self._feature} this poll: {err!r}")

    async def async_apply(
        self,
        data: dict[str, Any] | None = None,
        *,
        notify: bool = True,
        force: bool = False,
    ) -> None:
        """Send what the mode and guards add up to, if it differs from the last send."""
        if data is not None:
            self._data = data
        data = self._data
        self._expire_command()
        self._update_guards(data)
        if not (self._charge_guard or self._reserve_guard):
            self._handback = GuardHandback()

        # A lapsed window hands the inverter back to its app settings, so the command
        # is sent again rather than assumed to have survived.
        if self._enabled and not self.in_control:
            self._control_stale = True

        decision = self._desired_command(data)
        feature, power = decision.feature, decision.power
        changed = (feature, round(power)) != (
            self._commanded_feature,
            round(self._commanded_power),
        )

        if (
            changed
            and not force
            and not decision.bypass_dwell
            and not self._control_stale
            and self._control_written_within(MIN_CONTROL_DWELL_S)
        ):
            if notify:
                self._on_update()
            return

        try:
            if changed or self._control_stale:
                await self._async_send_control(feature, power)
            # Committed only once the inverter has been told: recording a command the
            # write never delivered would look settled and never be retried.
            retargeted = feature is not self._commanded_feature
            if changed:
                self._retuned_from = None if retargeted else self._commanded_power
            self._commanded_feature = feature
            self._commanded_power = power
            self._command_status = decision.status
            # A guard changes its power nearly every poll, and judging each new value
            # from scratch would hide a battery that never catches up.
            if retargeted or (changed and decision.status is None):
                self._reset_deviation()
            self._update_deviation(data)
        finally:
            if notify:
                self._on_update()

    def _expire_command(self) -> None:
        """Return to automatic once a command's time runs out, leaving the limits."""
        if self._expires_at is None or dt.now() < self._expires_at:
            return
        _LOGGER.debug(
            "The %s command was not renewed in time, returning to automatic",
            self._feature,
        )
        expired = self._feature
        self._feature = ControlFeature.AUTOMATIC
        self._handback = GuardHandback()
        self._expires_at = None
        self._on_command_expired(expired)

    def _compose_control_command(self, feature: ControlFeature | None = None) -> int:
        """Build the control word for *feature*, or for the commanded one by default.

        System control command (40534)
        """
        if feature is None:
            feature = self._commanded_feature
        method = CONTROL_FEATURES[feature].method.command_value
        word = (method & CONTROL_COMMAND_METHOD_MASK) << CONTROL_COMMAND_METHOD_SHIFT
        if self._battery_saver:
            word |= 1 << CONTROL_COMMAND_BATTERY_SAVER_BIT
        return word

    async def _async_send_control(self, feature: ControlFeature, power: float) -> None:
        """Write the setpoint, and the method word only where it is not already set.

        The inverter acts on the setpoint register continuously, so re-selecting a
        method it already holds achieves nothing and only risks a transient. An
        authority lapse is the one case that has to assume it was forgotten.
        """
        if not self._modbus_client.connected:
            raise HomeAssistantError("Modbus client is not connected")

        if CONTROL_FEATURES[feature].commands_power:
            await self._async_require_control_authority()
            await self._async_write_setpoint(feature, power)
        else:
            await self._async_clear_setpoint(self._commanded_feature)

        if CONTROL_FEATURES[feature].method is not self.method or self._control_stale:
            await self._async_write_control_word(self._compose_control_command(feature))
        self._note_control_written()

    async def _async_apply_control_command(self) -> None:
        """Write the composed control word once and refresh so the read-back shows it."""
        value = self._compose_control_command()
        if value & CONTROL_COMMAND_UNSAFE_BITS:
            raise HomeAssistantError(
                f"Refusing control command 0x{value:08X}: it would take the system "
                "off-grid or shut it down."
            )
        if not self._modbus_client.connected:
            raise HomeAssistantError("Modbus client is not connected")

        # Battery saver applies on its own, like the LED brightness does. Only a
        # control method needs the app locked out, so only it takes control.
        if CONTROL_FEATURES[self._commanded_feature].commands_power:
            await self._async_require_control_authority()

        await self._async_write_control_word(value)
        self._note_control_written()
        self._on_update()
        await self._on_refresh()

    async def _async_clear_setpoint(self, feature: ControlFeature) -> None:
        """Release the limit feature left in its register.

        Selecting the default method does not undo a setpoint the inverter still
        holds, and zero is the value that means no limit to it.
        """
        if not CONTROL_FEATURES[feature].commands_power:
            return
        await self._async_require_control_authority()
        await self._async_write_setpoint(feature, 0.0)

    async def _async_write_setpoint(
        self, feature: ControlFeature, watts: float
    ) -> None:
        """Write the register the feature's method acts on, with the feature's sign."""
        definition = CONTROL_FEATURES[feature]
        if definition.setpoint_key is None:
            return
        try:
            register = self._registers_by_key[definition.setpoint_key]
        except KeyError as err:
            raise HomeAssistantError(
                f"No register mapped for setpoint {definition.setpoint_key} on "
                f"{self._inverter_model}"
            ) from err
        value = int(round(watts)) * definition.sign

        try:
            words = encode_register(value, RegisterType.INT32)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

        await self._modbus_client.async_write(
            register.address, words, what=f"setpoint {value} W"
        )

    async def _async_write_control_word(self, value: int) -> None:
        await self._modbus_client.async_write(
            CONTROL_COMMAND_REGISTER,
            encode_register(value, RegisterType.UINT32),
            what=f"control command 0x{value:08X}",
        )

    def _note_control_written(self) -> None:
        """Record a command as delivered, so polling neither repeats nor drops it."""
        self._last_control_write_time = dt.now()
        self._control_stale = False
