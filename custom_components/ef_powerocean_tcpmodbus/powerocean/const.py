"""Constants of the PowerOcean and its Modbus protocol."""

from __future__ import annotations

from typing import Final

from .models import (
    BatteryModule,
    ControlFeature,
    ControlFeatureDef,
    ControlMode,
)

DEFAULT_MAX_POWER: Final = 30000
# The power limits control works within, keyed as the options that set them.
CONF_MAX_SOLAR_POWER: Final = "solar_power_max"
CONF_MAX_GRID_POWER: Final = "grid_power_max"
CONF_MAX_BATTERY_CHARGED_POWER: Final = "battery_charged_power_max"
CONF_MAX_BATTERY_DISCHARGED_POWER: Final = "battery_discharged_power_max"

MAX_BATTERY_COUNT: Final = 12
# At most 12 of the 8 kWh modules, with some margin.
MAX_BATTERY_CAPACITY_WH: Final = 100_000

# Old and new 5 kWh modules. They are similar enough to not affect the behaviour.
BATTERY_5KWH: Final = BatteryModule(5000, charge_w=2500, discharge_w=4080)
BATTERY_8KWH: Final = BatteryModule(8030, charge_w=4100, discharge_w=6400)
BATTERY_MODULES: Final = (BATTERY_5KWH, BATTERY_8KWH)
# Modules can be mixed in any way and the capacity does not show the mix, so take
# the highest rate of any module to never set a limit too low.
BATTERY_CHARGE_W_PER_WH: Final = max(
    module.charge_w / module.reported_wh for module in BATTERY_MODULES
)
BATTERY_DISCHARGE_W_PER_WH: Final = max(
    module.discharge_w / module.reported_wh for module in BATTERY_MODULES
)

MAX_FAULT_EVENTS: Final = 20

# The inverter runs on its app settings and merely stores the commands it is sent,
# until this register is written; each write buys it a minute of obeying them. Every
# constant below is seconds measured against that one deadline.
HEARTBEAT_REGISTER: Final = 40608
HEARTBEAT_VALUE: Final = 1
# The inverter's deadline: it stops obeying once the last accepted write is older.
HEARTBEAT_WINDOW_S: Final = 60
# How often to write, so a missed write still leaves two more before the deadline.
HEARTBEAT_INTERVAL_S: Final = 20
# Offsets the write from the poll tick, so the two rarely land on the same second.
HEARTBEAT_JITTER_S: Final = 2
# A write this recent still holds the deadline off, so a command rides on it rather
# than sending one of its own.
HEARTBEAT_REUSE_S: Final = 10
# How long one write may spend waiting between its retries. Below
# HEARTBEAT_INTERVAL_S, so the retries are over before the next write is due. A
# write that times out takes longer, but only while the inverter is not answering,
# when control is lost anyway.
HEARTBEAT_RETRY_TOTAL_S: Final = 15
# The shortest gap between two writes. Only reached after a failed one, where the
# deadline has kept running and waiting a full interval would risk passing it.
HEARTBEAT_MIN_GAP_S: Final = 5
# How long to leave the register alone once the inverter has called the request
# invalid rather than ill-timed, which is what firmware without it answers.
HEARTBEAT_UNSUPPORTED_RETRY_S: Final = 900

# 40534, write-only. Bit 0 forces the system off-grid and bit 1 shuts it down, so a
# command touching either is refused before it reaches the wire. Bit 3 is the
# battery saver switch and bits 4-7 select the control method; the setpoint registers
# only take effect while their control method is selected here.
CONTROL_COMMAND_REGISTER: Final = 40534
CONTROL_COMMAND_UNSAFE_BITS: Final = 0b11
CONTROL_COMMAND_BATTERY_SAVER_BIT: Final = 3
CONTROL_COMMAND_METHOD_SHIFT: Final = 4
CONTROL_COMMAND_METHOD_MASK: Final = 0xF

# The export cap as configured (40538) and as in force (40609). feed_in_power_max
# is the one the export ceiling follows: the effective cap where the model reports
# it, the configured one elsewhere. Only the configured cap is ever written.
FEED_IN_POWER_MAX_SETTING_KEY: Final = "feed_in_power_max_setting"
FEED_IN_POWER_MAX_EFFECTIVE_KEY: Final = "feed_in_power_max_effective"
FEED_IN_POWER_MAX_KEY: Final = "feed_in_power_max"

# 40546 and 40548, the most the inverter converts from DC to AC and from AC to DC.
# The keys predate these names and stay as they are to keep the entities' ids.
INVERTER_CAPACITY_KEY: Final = "limit_inv_power"
RECTIFIER_CAPACITY_KEY: Final = "limit_inv_max"

# How far under the device's export cap Export Solar First pins the meter. The inverter
# holds the meter there by itself, and keeping a little under the cap stops its own
# overshoot from being curtailed. A Solar Export Limit set lower is used as it is.
SOLAR_EXPORT_CAP_MARGIN_W: Final = 100.0

ENERGY_RESOLUTION_KWH: Final = 0.01
# A lifetime total held this long while the inverter reports another value raises
# a repairs issue offering to take the inverter's value. Longer than any glitch on
# record, a bogus 0 that lasted hours.
HELD_TOTAL_ISSUE_AFTER_S: Final = 24 * 3600
# The grid energy registers, which some inverters leave at 0 while Grid Power reads
# right (#218). Grid energy can then be integrated from Grid Power instead.
GRID_ENERGY_REGISTER_KEYS: Final = (
    "grid_import_total",
    "grid_import_today",
    "grid_export_total",
    "grid_export_today",
)
# Grid Power is not integrated across a longer gap between two readings, so an
# outage adds no made-up energy.
GRID_ENERGY_MAX_GAP_S: Final = 120
# The grid energy registers reading 0 this long, while this much energy flowed
# through the grid, raises a repairs issue offering to integrate Grid Power. As long
# as a held total, since a bogus 0 has lasted hours.
GRID_ENERGY_ISSUE_AFTER_S: Final = HELD_TOTAL_ISSUE_AFTER_S
GRID_ENERGY_ISSUE_MIN_KWH: Final = 1.0
# EcoFlow's portal page with the lifetime energy totals, in one of the languages
# below. The first is used for any other language.
ECOFLOW_PORTAL_LANGUAGES: Final = ("en", "de")
ECOFLOW_PORTAL_HISTORY_URL: Final = "https://user-portal.ecoflow.com/user/eu/{language}/main/home/dashboard/historicalData"
# The lifetime totals EcoFlow's portal shows, so the ones a user can correct to it.
# The inverter's AC input and output totals are not among them.
ECOFLOW_PORTAL_TOTALS: Final = frozenset(
    {
        "solar_total",
        "bat_discharged_total",
        "bat_charged_total",
        "grid_import_total",
        "grid_export_total",
    }
)

FAULT_KEYS: Final = tuple(
    f"fault_{fault_number}" for fault_number in range(1, MAX_FAULT_EVENTS + 1)
)

# Read from an external EcoFlow Smart Meter at the grid connection. Without one the
# device reads 0 in all of them, as on a PowerOcean Plus that relies on its built-in
# meter, whose readings show as the inverter's voltage and current (#240).
EXTERNAL_METER_KEYS: Final = (
    "grid_voltage_l1",
    "grid_voltage_l2",
    "grid_voltage_l3",
    "grid_current_l1",
    "grid_current_l2",
    "grid_current_l3",
)

BATTERY_SOC_KEYS: Final = tuple(
    f"soc_battery_{battery_number}"
    for battery_number in range(1, MAX_BATTERY_COUNT + 1)
)

# Values calculated from polled readings, by the readings they come from. A value
# from a reading the checks distrust is not shown either.
DERIVED_FROM: Final[dict[str, frozenset[str]]] = {
    "bat_remaining": frozenset({"battery_soc"}),
    **{
        f"pv{number}_power": frozenset({f"pv{number}_voltage", f"pv{number}_current"})
        for number in (1, 2, 3)
    },
}

CONTROL_FEATURES: Final[dict[ControlFeature, ControlFeatureDef]] = {
    ControlFeature.AUTOMATIC: ControlFeatureDef(method=ControlMode.DEFAULT),
    ControlFeature.HOLD_BATTERY: ControlFeatureDef(
        method=ControlMode.BATTERY_LIMITS,
        setpoint_key="battery_power_setpoint",
        measure_key="battery_power",
    ),
    ControlFeature.CHARGE_BATTERY: ControlFeatureDef(
        method=ControlMode.BATTERY_LIMITS,
        setpoint_key="battery_power_setpoint",
        sign=1,
        measure_key="battery_power",
        config_limit_key=CONF_MAX_BATTERY_CHARGED_POWER,
        default_power=2000.0,
    ),
    # Charging has no converter capacity to respect: PV on the DC side can charge
    # the battery alongside the rectifier, so its capacity alone is no ceiling.
    ControlFeature.DISCHARGE_BATTERY: ControlFeatureDef(
        method=ControlMode.BATTERY_LIMITS,
        setpoint_key="battery_power_setpoint",
        sign=-1,
        measure_key="battery_power",
        config_limit_key=CONF_MAX_BATTERY_DISCHARGED_POWER,
        capacity_key=INVERTER_CAPACITY_KEY,
        default_power=2000.0,
    ),
    ControlFeature.EXPORT_TO_GRID: ControlFeatureDef(
        method=ControlMode.SYSTEM_FEED,
        setpoint_key="system_power_setpoint",
        sign=-1,
        measure_key="grid_power",
        limit_key=FEED_IN_POWER_MAX_KEY,
        capacity_key=INVERTER_CAPACITY_KEY,
        default_power=3000.0,
    ),
    # The target is the meter reading, so the house counts towards it: bounded by
    # the grid connection rather than by what the inverter can convert.
    ControlFeature.IMPORT_FROM_GRID: ControlFeatureDef(
        method=ControlMode.SYSTEM_FEED,
        setpoint_key="system_power_setpoint",
        sign=1,
        measure_key="grid_power",
        config_limit_key=CONF_MAX_GRID_POWER,
        bounded_by_rating=False,
        default_power=3000.0,
    ),
    # Never commanded as such: it runs Automatic, Hold or Export to Grid, whichever
    # suits the surplus, so the method and setpoint here are those of its hold. Its
    # power, the Solar Export Limit, goes up to the export cap, or to the most the
    # inverter can put out without one, and left alone it reads that.
    ControlFeature.EXPORT_SOLAR_FIRST: ControlFeatureDef(
        method=ControlMode.BATTERY_LIMITS,
        setpoint_key="battery_power_setpoint",
        measure_key="battery_power",
        capacity_key=INVERTER_CAPACITY_KEY,
        default_power=float(DEFAULT_MAX_POWER),
        power_optional=True,
    ),
}
# The inverter's own reserve, the backup reserve the EcoFlow app sets. Written by the
# Battery Reserve on models that act on it, shown as the App Backup Reserve on the rest.
BATTERY_RESERVE_REGISTER_KEY: Final = "min_soc_limit"
DEFAULT_CHARGE_LIMIT_SOC: Final = 100.0
DEFAULT_BATTERY_RESERVE_SOC: Final = 0.0

GUARD_SOC_HYSTERESIS: Final = 5.0
# How far a battery stopped at a guard's limit must move away before it may move
# back towards it, so a state of charge flickering by a percent stays stopped.
GUARD_RETURN_SOC: Final = 2.0
GUARD_POWER_DEADBAND_W: Final = 200.0
# Resolution of a guard's tracked setpoint, so an ordinary load does not rewrite it
# every poll. Always rounded towards zero, which leaves the remainder to the grid.
GUARD_TRACKING_STEP_W: Final = 100.0
# Seconds to wait for the battery to reach a new setpoint before correcting it again.
GUARD_SETTLE_S: Final = 30.0
# Minimum power in the direction a guard allows before it hands control back.
GUARD_HANDBACK_W: Final = 500.0
# On models whose guards do not track a setpoint, the power in the allowed direction
# that hands control back. Control is taken back once that power reaches zero, so
# this is also the width of the hysteresis band.
GUARD_DIRECT_HANDBACK_W: Final = 20.0
# How long that power must last before a guard hands control back.
GUARD_HANDBACK_S: Final = 60.0
# Longest hand-back wait; the wait doubles each time a hand-back ends early.
GUARD_HANDBACK_MAX_S: Final = 900.0
MIN_CONTROL_DWELL_S: Final = 60.0
CONTROL_STATUS_DAMPING_POLLS: Final = 3
# How long the inverter gets to report a command it was sent, and how often it is
# sent again when it does not, before the status says it is not accepted.
FOLLOW_GRACE_S: Final = 15.0
FOLLOW_RESENDS: Final = 2
# 0 means "no limit" to the inverter, not "hold at zero", so hold at the lowest
# power instead.
HOLD_SETPOINT_W: Final = 1.0

# Ceiling for a mode whose limit registers are all missing or read zero.
CONTROL_POWER_FALLBACK_MAX: Final = DEFAULT_MAX_POWER
