# EF-PowerOcean-TcpModbus

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
![GitHub release](https://img.shields.io/github/release/MaxGrmm/EF-PowerOcean-TcpModbus.svg)

**Local Modbus TCP integration for the EcoFlow PowerOcean Plus home battery system.**

> ⚠️ This integration communicates directly with your device over your local network via Modbus TCP. No cloud connection required for the supported sensors.

---

## Features

- **Local polling** – no EcoFlow cloud account needed
- **10-second refresh rate** (configurable)
- Full battery monitoring: SOC, voltage, current, power, temperature, remaining energy
- Per-phase AC measurements: voltage, current, frequency
- PV string currents for up to 3 strings
- Energy counters: daily and lifetime import/export/charge/discharge
- UI setup via Home Assistant config flow

---

## Supported Devices

| Device | Status |
|--------|--------|
| EcoFlow PowerOcean Plus | ✅ Confirmed |
| EcoFlow PowerOcean DC | ❓ Untested – feedback welcome |
| EcoFlow PowerOcean Connect | ❓ Untested – feedback welcome |

---

## Installation

### Via HACS (recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** → **⋮** → **Custom repositories**
3. Add `https://github.com/MaxGrmm/EF-PowerOcean-TcpModbus` as category **Integration**
4. Click **Install**
5. Restart Home Assistant

### Manual

1. Download the latest release
2. Copy the `custom_components/ef_powerocean_tcpmodbus` folder to your HA `config/custom_components/` directory
3. Restart Home Assistant

---

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **EF-PowerOcean-TcpModbus**
3. Enter the IP address of your PowerOcean Plus
4. Port defaults to `502` (standard Modbus TCP)

---

## Available Sensors

### Battery
| Sensor | Unit | Description |
|--------|------|-------------|
| Battery SOC | % | State of charge |
| Battery Voltage | V | DC bus voltage |
| Battery Current | A | Positive = charging, Negative = discharging |
| Battery Power | W | Calculated from V × I |
| Battery Temperature | °C | Ambient temperature |
| Battery Remaining Energy | kWh | Current stored energy |

### AC Grid
| Sensor | Unit | Description |
|--------|------|-------------|
| Grid Voltage L1/L2/L3 | V | Per-phase voltage |
| Grid Current L1/L2/L3 | A | Per-phase current |
| Grid Frequency | Hz | Grid frequency |
| Grid Apparent Power | VA | Total apparent power |
| Inverter AC Power | W | Calculated sum of U×I per phase |

### Solar
| Sensor | Unit | Description |
|--------|------|-------------|
| PV String 1/2/3 Current | A | MPPT string current |

### Inverter
| Sensor | Unit | Description |
|--------|------|-------------|
| Inverter Temperature | °C | Inverter temperature |

### Energy – Today
| Sensor | Unit | Description |
|--------|------|-------------|
| Grid Import Today | kWh | Energy imported from grid today |
| Grid Export Today | kWh | Energy exported to grid today |
| PV String 1/2 Yield Today | kWh | Solar yield today |
| Battery Charged Today | kWh | Energy charged today |
| Battery Discharged Today | kWh | Energy discharged today |

### Energy – Lifetime
| Sensor | Unit | Description |
|--------|------|-------------|
| Grid Import Total | kWh | Lifetime grid import |
| Grid Export Total | kWh | Lifetime grid export |
| PV String 1/2 Total Yield | kWh | Lifetime solar yield |
| Battery Charged Total | kWh | Lifetime charged |
| Battery Discharged Total | kWh | Lifetime discharged |
| Battery Net Energy | kWh | Charged minus discharged |
| Total System Energy | kWh | Total system energy |

---

## Known Limitations

The following values are **not available via Modbus TCP** and can only be retrieved through the EcoFlow Cloud API:

- Grid Power / Feed-in power (`pcsMeterPower`)
- Total PV Power (`mpptPv_pwrTotal`)
- House consumption (`housePower`)
- Battery SoH, cell temperatures, battery voltage per module

---

## Technical Details

- **Protocol:** Modbus TCP (port 502)
- **Register type:** Holding Registers (Function Code 3)
- **Float encoding:** 32-bit IEEE 754, little-endian word order (word-swapped)
- **Tested firmware:** 3.0.15.10
- **Tested pymodbus version:** 3.6.9 and 3.11.x

Full register map documentation: [EcoFlow_PowerOcean_Modbus.md](EcoFlow_PowerOcean_Modbus.md)

---

## Contributing

Pull requests are welcome! Especially:
- Testing on other EcoFlow devices
- Identifying missing registers (grid power, PV total power)
- Home Assistant Energy Dashboard configuration examples

Please open an issue before submitting large changes.

---

## Disclaimer

This integration was developed through community reverse engineering.
EcoFlow does not officially support or document this Modbus interface.
Use at your own risk. Not affiliated with EcoFlow Technology Co., Ltd.

---

## License

MIT License – free to use, modify and distribute with attribution.
