"""Modbus control of the inverter: what to command, and keeping track of what it does.

The coordinator reads and this decides what the inverter should be doing and commands it.
Everything here is driven by one selected feature plus two state-of-charge guards,
and nothing reaches the wire unless the inverter is currently following us.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

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
    GUARD_POWER_DEADBAND_W,
    GUARD_SOC_HYSTERESIS,
    GUARD_TRACKING_STEP_W,
    HEARTBEAT_REGISTER,
    HOLD_SETPOINT_W,
    MIN_CONTROL_DWELL_S,
)
from .heartbeat import Heartbeat
from .modbus import ModbusClient
from .models import (
    BATTERY_FULL_SOC,
    ControlFeature,
    ControlMode,
    ControlStatus,
    InverterModel,
    RegisterDef,
    RegisterType,
    deviation_state,
    encode_register,
)

_LOGGER = logging.getLogger(__name__)


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
        on_update: Callable[[], None],
        on_refresh: Callable[[], Awaitable[None]],
    ) -> None:
        self._modbus_client = modbus_client
        self._registers_by_key = registers_by_key
        self._limits = limits
        self._inverter_model = inverter_model
        self._on_update = on_update
        self._on_refresh = on_refresh

        self._enabled = enabled
        self._heartbeat = Heartbeat(modbus_client, scan_interval_s=scan_interval_s)

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
        # Which guard, if any, is forcing the current command.
        self._blocking_guard: ControlStatus | None = None

        self._commanded_feature = ControlFeature.AUTOMATIC
        self._commanded_power = 0.0
        self._battery_saver = False
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
        """Return whether the user has switched Modbus control on in the config."""
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
    def charge_limit_soc(self) -> float:
        return self._charge_limit_soc

    @property
    def battery_reserve_soc(self) -> float:
        return self._battery_reserve_soc

    @property
    def battery_saver_commanded(self) -> bool:
        """Return whether battery saver mode is being commanded."""
        return self._battery_saver

    def feature_power(self, feature: ControlFeature) -> float:
        """Return the configured power, or zero for a mode that has none."""
        return self._feature_power.get(feature, 0.0)

    def feature_power_max(self, feature: ControlFeature) -> float:
        return self._control_power_ceiling(feature)

    @property
    def status(self) -> ControlStatus:
        """Explain, in one word, what the selected mode is achieving."""
        if not self.in_control:
            return ControlStatus.NO_MODBUS_CONTROL
        if self._blocking_guard is not None:
            return self._blocking_guard
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
            "feature_power": {
                str(feature): power for feature, power in self._feature_power.items()
            },
            "charge_limit_soc": self._charge_limit_soc,
            "battery_reserve_soc": self._battery_reserve_soc,
            "battery_saver": self._battery_saver,
        }

    def load_state(self, stored: dict[str, Any]) -> None:
        """Restore what each mode would command, but never which one was selected."""
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

    def start(self) -> None:
        """Begin holding control authority, if the user switched control on."""
        if self._enabled:
            self._heartbeat.start()

    async def async_stop(self) -> None:
        await self._heartbeat.async_stop()

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
                "Modbus control is off. Enable Modbus Control in the integration "
                "configuration to command the inverter; nothing was written."
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
        await self.async_apply(force=True)

    async def async_set_feature_power(
        self, feature: ControlFeature, watts: float
    ) -> None:
        """Set a mode's power. Editable whether or not that mode is selected."""
        self._feature_power[feature] = self._clamp_power(watts, feature)
        await self.async_apply(force=True)

    async def async_set_charge_limit_soc(self, soc: float) -> None:
        """Set the state of charge above which the battery must not be charged."""
        self._charge_limit_soc = max(0.0, min(100.0, soc))
        # Clear the latch so the new limit starts its hysteresis afresh, but only
        # where async_apply can work it out again below.
        if self._data.get("battery_soc") is not None:
            self._charge_guard = False
        await self.async_apply(force=True)

    async def async_set_battery_reserve_soc(self, soc: float) -> None:
        """Set the state of charge below which the battery must not be drained."""
        self._battery_reserve_soc = max(0.0, min(100.0, soc))
        if self._data.get("battery_soc") is not None:
            self._reserve_guard = False
        await self.async_apply(force=True)

    async def async_set_battery_saver(self, enabled: bool) -> None:
        """Command battery saver mode without disturbing the control intent."""
        previous = self._battery_saver
        self._battery_saver = enabled
        try:
            await self._async_apply_control_command()
        except HomeAssistantError:
            self._battery_saver = previous
            raise

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
        if rated := self._data.get("inverter_rated_power"):
            ceilings.append(float(rated))

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
        """Return what the battery would do if the inverter were left to itself.

        The house balances as solar + grid = house + battery, so the power a battery
        would need to hold the grid at zero reads the same whether the inverter is
        following us or running its own self-consumption. Measuring it on the grid
        side is preferred because it carries the conversion losses the panels do not.
        """
        battery, grid = data.get("battery_power"), data.get("grid_power")
        if battery is not None and grid is not None:
            return float(battery) - float(grid)
        solar, house = data.get("solar_power"), data.get("house_power")
        if solar is not None and house is not None:
            return float(solar) - float(house)
        return None

    def _guard_blocks(self, direction: int) -> ControlStatus | None:
        """Return the guard forbidding movement in direction, if available."""
        if direction >= 0 and self._charge_guard:
            return ControlStatus.CHARGE_LIMIT_REACHED
        if direction <= 0 and self._reserve_guard:
            return ControlStatus.RESERVE_REACHED
        return None

    def _guarded_command(
        self, data: dict[str, Any], blocked: ControlStatus
    ) -> tuple[ControlFeature, float, ControlStatus | None]:
        """Command the inverter to either charge or discharge.

        The battery setpoint is a target and zero means no limit, so no single value
        says "do not charge, but discharge freely". We therefore clamp the natural
        battery power to the allowed direction.
        """
        natural = self._natural_battery_power(data)
        if natural is None:
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

        # Rewrite only once the load has moved a step, and never while the standing
        # setpoint has drifted to the importing side.
        held = self._commanded_power
        slack = watts - held if natural > 0 else held - watts
        if self._commanded_feature is feature and 0.0 <= slack < GUARD_TRACKING_STEP_W:
            watts = held

        if watts <= 0.0:
            return self._hold(data, blocked)
        return feature, self._clamp_power(watts, feature), blocked

    def _hold(
        self, data: dict[str, Any], blocked: ControlStatus | None
    ) -> tuple[ControlFeature, float, ControlStatus | None]:
        """Hold the battery, unless holding it could only limit solar.

        A full battery cannot charge, so a battery limit set against a surplus
        forbids nothing the inverter could do anyway and leaves limiting the
        solar as its only way to balance. Stepping aside is safe because anything
        short of a clear surplus counts as a draw, so the hold is back before the
        house can reach the battery.
        """
        soc = data.get("battery_soc")
        surplus = self._natural_battery_power(data) or 0.0
        if (
            soc is not None
            and float(soc) >= BATTERY_FULL_SOC
            and surplus > GUARD_POWER_DEADBAND_W
        ):
            return ControlFeature.AUTOMATIC, 0.0, blocked
        return ControlFeature.HOLD_BATTERY, HOLD_SETPOINT_W, blocked

    def _desired_command(
        self, data: dict[str, Any]
    ) -> tuple[ControlFeature, float, ControlStatus | None]:
        """Determine what command the inverter should do now."""
        if not self._enabled:
            return ControlFeature.AUTOMATIC, 0.0, None

        definition = CONTROL_FEATURES[self._feature]
        # Holding moves the battery neither way, so no guard has anything to say.
        blocked = (
            None
            if self._feature is ControlFeature.HOLD_BATTERY
            else self._guard_blocks(definition.direction)
        )
        if blocked is not None:
            # Only the inverter's own mode is reproduced. A mode the user chose is
            # restricted to nothing in the forbidden direction, never turned around.
            return (
                self._guarded_command(data, blocked)
                if self._feature is ControlFeature.AUTOMATIC
                else self._hold(data, blocked)
            )

        if self._feature is ControlFeature.AUTOMATIC:
            return ControlFeature.AUTOMATIC, 0.0, None
        if not definition.has_power:
            return self._hold(data, None)
        return (
            self._feature,
            self._clamp_power(self.feature_power(self._feature), self._feature),
            None,
        )

    def _was_last_command_recent(self) -> bool:
        """Return whether the last command is too recent to be worth replacing."""
        if self._last_control_write_time is None:
            return False
        age = (dt.now() - self._last_control_write_time).total_seconds()
        return age < MIN_CONTROL_DWELL_S

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
        self._update_guards(data)

        # A lapsed window hands the inverter back to its app settings, so the command
        # is sent again rather than assumed to have survived.
        if not self.in_control:
            self._control_stale = True

        feature, power, blocked = self._desired_command(data)
        changed = (feature, round(power)) != (
            self._commanded_feature,
            round(self._commanded_power),
        )

        if (
            changed
            and not force
            and blocked is None
            and not self._control_stale
            and self._was_last_command_recent()
        ):
            if notify:
                self._on_update()
            return

        try:
            if changed or self._control_stale:
                await self._async_send_control(feature, power)
            # Committed only once the inverter has been told: recording a command the
            # write never delivered would look settled and never be retried.
            self._commanded_feature = feature
            self._commanded_power = power
            self._blocking_guard = blocked
            if changed:
                self._reset_deviation()
            self._update_deviation(data)
        finally:
            if notify:
                self._on_update()

    def _compose_control_command(self, feature: ControlFeature | None = None) -> int:
        """Build the control word for *feature*, or for the commanded one by default.

        System control command (0x0215)
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
