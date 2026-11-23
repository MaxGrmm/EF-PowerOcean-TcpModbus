"""Constants for EF-PowerOcean-TcpModbus integration."""

from __future__ import annotations

from typing import Final

from .powerocean.models import (
    InverterModel,
)

DOMAIN: Final = "ef_powerocean_tcpmodbus"
EVENT_COMMAND_EXPIRED: Final = f"{DOMAIN}_command_expired"
EVENT_CONTROL_TEST_STARTED: Final = f"{DOMAIN}_control_test_started"
EVENT_CONTROL_TEST_FINISHED: Final = f"{DOMAIN}_control_test_finished"
# A reading check failed for long enough to act on, and passes again; see
# reading_checks.py. Control steps aside in between while it is on.
EVENT_READING_CHECK_FAILED: Final = f"{DOMAIN}_reading_check_failed"
EVENT_READING_CHECK_CLEARED: Final = f"{DOMAIN}_reading_check_cleared"
# Read on a reconnect, so it often marks the end of an outage an update caused.
EVENT_FIRMWARE_CHANGED: Final = f"{DOMAIN}_firmware_changed"
ATTR_FROM: Final = "from"
ATTR_TO: Final = "to"
ATTR_STATUS: Final = "status"
ATTR_CHECK: Final = "check"
ATTR_MODE: Final = "mode"
DEFAULT_PORT: Final = 502
DEFAULT_SLAVE: Final = 1
DEFAULT_SCAN_INTERVAL_S: Final = 5
DEFAULT_BATTERY_COUNT: Final = 0

CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_BATTERY_COUNT: Final = "battery_count"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_CALC_SOLAR_POWER: Final = "calc_solar_power"
CONF_CALC_GRID_ENERGY: Final = "calc_grid_energy"
# How much the integration raises in repairs, beyond what the user must act on.
CONF_ALERT_VERBOSITY: Final = "alert_verbosity"
CONF_MODBUS_CONTROL: Final = "modbus_control"
CONF_INVERTER_MODEL: Final = "inverter_model"
# kWh added to what the device reports for a lifetime total, by its key. Set by the
# user for a counter the inverter reset, so the total continues as in EcoFlow's portal.
CONF_ENERGY_OFFSETS: Final = "energy_offsets"

SLEEP_TIME_AFTER_RECONNECT_S: Final = 1

# Inverters on Wi-Fi can answer slowly (#42). A request and its resend hold the
# client lock for up to about two of these, well inside the heartbeat's window.
MODBUS_REQUEST_TIMEOUT_S: Final = 10
# Room for a request that times out and its retry, and well under the sum of all
# request timeouts, which could pass minutes.
MODBUS_POLL_BUDGET_S: Final = 25
STORAGE_VERSION: Final = 1
STATE_SAVE_DELAY_S: Final = 30
# Where a user reports a held total they believe is wrong, with the bug form's
# fields filled in from the device.
BUG_REPORT_URL: Final = "https://github.com/MaxGrmm/EF-PowerOcean-TcpModbus/issues/new"
MODBUS_DISABLED_READ_THRESHOLD: Final = 3


# Home Assistant only introdcued UnitOfRatio in 2026.7, so for backwards compatibility
# we hardcode this
UNIT_OF_RATIO: Final = "%"

DEFAULT_INVERTER_MODEL: Final = InverterModel.POWEROCEAN_THREE_PHASE

# Polls in a row an optional read may time out or lose the link before it is no
# longer polled.
OPTIONAL_STALLS_TO_DROP: Final = 3
# The number that wrote it before the Battery Reserve took over, removed on setup.
RETIRED_MIN_SOC_NUMBER_KEY: Final = "min_soc_limit_control"
# Options: native or emulated Battery Reserve, prefilled with the model's.
CONF_BATTERY_RESERVE: Final = "battery_reserve"
# How many failed reading checks keep a snapshot of the readings for diagnostics.
READING_CHECK_SNAPSHOTS: Final = 5
