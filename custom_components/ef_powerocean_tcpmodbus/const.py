"""Constants for EF-PowerOcean-TcpModbus integration."""

DOMAIN = "ef_powerocean_tcpmodbus"
DEFAULT_PORT = 502
DEFAULT_SLAVE = 1
DEFAULT_SCAN_INTERVAL = 10  # seconds

# ── Register addresses ──────────────────────────────────────────────────────
# All addresses are original 1-based Modbus addresses.
# Float decode: SWAPPED word order (registers[1]=high, registers[0]=low).
# Confirmed via live cross-reference with EcoFlow Cloud API.

# Status (UINT16)
REG_STATUS = 42081      # 1 = Online
REG_SOC    = 42082      # Battery State of Charge %

# Live measurements (32-bit float, swapped word order)
REG_BAT_VOLTAGE      = 40574   # × 1    → V    battery voltage
REG_BAT_CURRENT      = 40576   # × 1    → A    battery current (neg=discharge)
REG_BAT_TEMP         = 40578   # × 1    → °C   battery ambient temperature
REG_VOLTAGE_L1       = 40580   # × 1    → V
REG_VOLTAGE_L2       = 40582   # × 1    → V
REG_VOLTAGE_L3       = 40584   # × 1    → V
REG_CURRENT_L1       = 40586   # × 1    → A
REG_CURRENT_L2       = 40588   # × 1    → A
REG_CURRENT_L3       = 40590   # × 1    → A
REG_FREQUENCY        = 40594   # × 1    → Hz
REG_APPARENT_POWER   = 40598   # × 10   → VA
REG_INV_TEMP         = 40600   # × 1    → °C   inverter temperature
REG_PV1_CURRENT      = 40602   # × 1    → A    PV string 1 current
REG_PV2_CURRENT      = 40604   # × 1    → A    PV string 2 current
REG_PV3_CURRENT      = 40606   # × 1    → A    PV string 3 current

# NOT available via Modbus (Cloud API only):
# - Grid Power (pcsMeterPower)
# - PV Total Power (mpptPv_pwrTotal)
# - House Power (housePower)

# Energy today (32-bit float, swapped word order)
REG_GRID_IMPORT_TODAY    = 42163   # kWh
REG_GRID_EXPORT_TODAY    = 42179   # kWh
REG_PV1_TODAY            = 42195   # kWh
REG_PV2_TODAY            = 42211   # kWh
REG_BAT_CHARGE_TODAY     = 42243   # kWh
REG_BAT_DISCHARGE_TODAY  = 42145   # kWh

# Energy lifetime (32-bit float, swapped word order)
REG_BAT_NET_ENERGY       = 42113   # kWh
REG_GRID_IMPORT_TOTAL    = 42161   # Wh → × 0.001 → kWh
REG_GRID_EXPORT_TOTAL    = 42177   # Wh → × 0.001 → kWh
REG_PV1_TOTAL            = 42193   # kWh
REG_PV2_TOTAL            = 42209   # kWh
REG_BAT_CHARGED_TOTAL    = 42225   # kWh
REG_BAT_REMAINING        = 42227   # kWh
REG_BAT_DISCHARGED_TOTAL = 42241   # kWh
REG_TOTAL_ENERGY         = 42257   # kWh
