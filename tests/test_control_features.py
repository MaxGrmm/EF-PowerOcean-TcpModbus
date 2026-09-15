"""The feature table is all that stands between the user and a wrong register."""

from __future__ import annotations

import pytest
from ef_powerocean_tcpmodbus import const
from ef_powerocean_tcpmodbus.models import (
    BATTERY_FULL_SOC,
    ControlFeature,
    ControlMode,
    ControlStatus,
    deviation_state,
)
from ef_powerocean_tcpmodbus.telemetry import TelemetryData, calculate_derived_values


def derived_keys() -> set[str]:
    """Return every key the coordinator adds on top of the raw registers."""
    return set(
        calculate_derived_values(
            TelemetryData.from_mapping({"house_power": 1000.0, "grid_power": -200.0}),
            calculate_solar_power=True,
            startup_voltage=250,
        )
    )


def test_every_feature_is_defined() -> None:
    assert set(const.CONTROL_FEATURES) == set(ControlFeature)


def test_only_automatic_leaves_the_device_to_itself() -> None:
    automatic = const.CONTROL_FEATURES[ControlFeature.AUTOMATIC]
    assert automatic.method is ControlMode.DEFAULT
    assert not automatic.commands_power

    for feature, definition in const.CONTROL_FEATURES.items():
        if feature is ControlFeature.AUTOMATIC:
            continue
        assert definition.commands_power
        assert definition.method is not ControlMode.DEFAULT
        assert definition.sign in (1, -1)


@pytest.mark.parametrize("feature", tuple(ControlFeature))
def test_feature_keys_all_resolve(feature: ControlFeature) -> None:
    """A typo here would silently write to nothing or bound against nothing."""
    definition = const.CONTROL_FEATURES[feature]
    known = set(const.REGISTERS_BY_KEY) | derived_keys()

    for key in (definition.setpoint_key, definition.limit_key, definition.measure_key):
        if key is not None:
            assert key in known, key

    if definition.setpoint_key is not None:
        assert definition.setpoint_key in const.REGISTERS_BY_KEY

    if definition.config_limit_key is not None:
        assert definition.config_limit_key in {
            const.CONF_MAX_BATTERY_CHARGED_POWER,
            const.CONF_MAX_BATTERY_DISCHARGED_POWER,
        }


def test_the_battery_modes_do_not_bound_themselves_by_the_apps_limit() -> None:
    """Modbus control overrides the app's charge limit, so that register is not a cap.

    Measured on a PowerOcean Plus: with the app limited to 500 W, a charge command
    still drew the full 5 kW the modules accept.
    """
    for feature in (ControlFeature.CHARGE_BATTERY, ControlFeature.DISCHARGE_BATTERY):
        definition = const.CONTROL_FEATURES[feature]
        assert definition.limit_key is None
        assert definition.config_limit_key is not None

    # The export limit is a real firmware cap and stays in force.
    assert const.CONTROL_FEATURES[ControlFeature.EXPORT_TO_GRID].limit_key == (
        "feed_in_power_max"
    )


def test_the_signs_follow_the_devices_own_measurements() -> None:
    """Confirmed on hardware: grid export reads negative, battery charging positive.

    Nothing else in the code pins this down, and flipping either sign on the
    assumption that export should be positive inverts the status of every mode.
    """
    export = const.CONTROL_FEATURES[ControlFeature.EXPORT_TO_GRID]
    charge = const.CONTROL_FEATURES[ControlFeature.CHARGE_BATTERY]

    assert (export.measure_key, export.sign) == ("grid_power", -1)
    assert (charge.measure_key, charge.sign) == ("battery_power", 1)

    # Exporting 2 kW against a 2 kW command is the mode working, not a deviation.
    assert (
        deviation_state(
            signed_target=2000.0 * export.sign,
            measured=-2000.0,
            soc=59.0,
            min_soc=0.0,
        )
        is ControlStatus.ACTIVE
    )
    assert (
        deviation_state(
            signed_target=1700.0 * charge.sign,
            measured=1700.0,
            soc=59.0,
            min_soc=0.0,
        )
        is ControlStatus.ACTIVE
    )


def test_opposing_features_share_a_register_but_not_a_sign() -> None:
    """Charge and discharge are one register; the sign is what tells them apart."""
    charge = const.CONTROL_FEATURES[ControlFeature.CHARGE_BATTERY]
    discharge = const.CONTROL_FEATURES[ControlFeature.DISCHARGE_BATTERY]

    assert charge.setpoint_key == discharge.setpoint_key
    assert charge.method is discharge.method
    assert charge.sign == 1
    assert discharge.sign == -1


def test_holding_the_battery_commands_a_method_but_no_power() -> None:
    """Hold is the same method at zero watts, so it needs no parameters."""
    definition = const.CONTROL_FEATURES[ControlFeature.HOLD_BATTERY]

    assert definition.commands_power
    assert not definition.has_power


def test_the_sign_says_which_guard_can_block_a_mode() -> None:
    """Charging is blocked by the charge limit, draining by the battery reserve."""
    features = const.CONTROL_FEATURES

    assert features[ControlFeature.CHARGE_BATTERY].direction == 1
    assert features[ControlFeature.DISCHARGE_BATTERY].direction == -1
    assert features[ControlFeature.EXPORT_TO_GRID].direction == -1
    # Hold moves nothing in either direction, so no guard applies to it.
    assert features[ControlFeature.HOLD_BATTERY].direction == 0


def test_a_setpoint_that_is_being_met_reads_as_active() -> None:
    state = deviation_state(
        signed_target=3000.0, measured=2900.0, soc=50.0, min_soc=10.0
    )

    assert state is ControlStatus.ACTIVE


def test_a_full_battery_explains_a_target_that_needs_absorbing() -> None:
    """Reducing an export means charging, and a full battery cannot."""
    state = deviation_state(
        signed_target=-1000.0, measured=-6400.0, soc=100.0, min_soc=10.0
    )

    assert state is ControlStatus.UNREACHABLE_BATTERY_FULL


def test_an_empty_battery_explains_a_target_that_needs_supplying() -> None:
    state = deviation_state(
        signed_target=-7000.0, measured=-2000.0, soc=10.0, min_soc=10.0
    )

    assert state is ControlStatus.UNREACHABLE_BATTERY_EMPTY


def test_a_wide_miss_with_headroom_left_is_only_ramping() -> None:
    """A gap alone proves nothing while the battery can still close it."""
    state = deviation_state(
        signed_target=5000.0, measured=1000.0, soc=50.0, min_soc=10.0
    )

    assert state is ControlStatus.RAMPING


def test_full_is_judged_below_a_hundred_percent() -> None:
    """SOC is whole percent, so a battery reporting 99 is full enough to explain it."""
    state = deviation_state(
        signed_target=3000.0, measured=0.0, soc=BATTERY_FULL_SOC, min_soc=10.0
    )

    assert state is ControlStatus.UNREACHABLE_BATTERY_FULL
