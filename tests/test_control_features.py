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


def test_the_select_offers_every_mode() -> None:
    assert const.BATTERY_MODE_SELECT.key == "battery_mode"
    assert const.CONTROL_STATUS_SENSOR.options == tuple(
        str(status) for status in ControlStatus
    )


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
    """The device slews at roughly 1.5 kW/min, so a gap alone proves nothing."""
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
