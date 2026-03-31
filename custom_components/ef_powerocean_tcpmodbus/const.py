"""Constants for EF-PowerOcean-TcpModbus integration."""

DOMAIN = "ef_powerocean_tcpmodbus"
DEFAULT_PORT = 502
DEFAULT_SLAVE = 1
DEFAULT_SCAN_INTERVAL = 10  # seconds
DEFAULT_BATTERY_CAPACITY = 5.0  # kWh – workaround, register 40528 unreliable

CONF_BATTERY_CAPACITY = "battery_capacity"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_PV_STRINGS = "pv_strings"

DEFAULT_PV_STRINGS = 2
PV_CURRENT_THRESHOLD = 0.05  # A – below this value string current is treated as 0 (phantom voltage)

# Used in config_flow connection test only
REG_STATUS = 42081  # UINT16 – 1 = Online
