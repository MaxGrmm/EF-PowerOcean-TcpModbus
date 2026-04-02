# EF-PowerOcean-TcpModbus

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/release/MaxGrmm/EF-PowerOcean-TcpModbus.svg)](https://github.com/MaxGrmm/EF-PowerOcean-TcpModbus/releases)

**Local Modbus TCP integration for the EcoFlow PowerOcean Plus home battery system.**

> ⚠️ This integration communicates directly with your device over your local network via Modbus TCP. No cloud connection required.

---

## Features

- **Local polling** – no EcoFlow cloud account needed
- **Configurable poll interval** (5–60 seconds, default 10 s)
- Real-time power flow: house consumption, grid import/export, solar generation, battery
- Full battery monitoring: SOC, voltage, current, power, temperature, remaining energy
- Per-string PV power and current (configurable 1–3 strings, phantom current filtering)
- Per-phase AC measurements: voltage, current, frequency, apparent power
- Energy counters: daily and lifetime for grid, solar, battery charge/discharge, house consumption
- Configurable battery capacity (workaround for unreliable register value)
- Reconfigurable after setup via **Settings → Configure** (no re-install needed)
- Debug logging toggle directly in the HA UI
- German and English translations

---

## Supported Devices

| Device | Status |
|---|---|
| EcoFlow PowerOcean Plus | ✅ Confirmed |
| EcoFlow PowerOcean |  ✅ Confirmed |
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
3. Fill in the setup form:

| Field | Default | Description |
|---|---|---|
| IP Address | – | Local IP of your PowerOcean Plus |
| Port | 502 | Modbus TCP port |
| Battery Capacity (kWh) | 5.0 | Usable capacity of your battery module |
| Number of PV Strings | 2 | Active strings connected to the inverter (1–3) |
| Poll Interval (seconds) | 10 | How often values are fetched |

To change settings after setup: **Settings → Devices & Services → EF-PowerOcean-TcpModbus → Configure**

---

## Available Sensors

### Power (real-time)

| Sensor | Unit | Description |
|---|---|---|
| House Power | W | Current house consumption |
| Grid Power | W | Grid exchange (negative = export) |
| Solar Power | W | Total PV generation (sum of active strings) |
| Battery Power | W | Battery charge/discharge power |
| Inverter AC Power | W | Total AC output of inverter |

### Battery

| Sensor | Unit | Description |
|---|---|---|
| Battery SOC | % | State of charge |
| Battery Remaining Energy | kWh | Calculated from SOC × configured capacity |
| Battery Voltage | V | DC bus voltage |
| Battery Current | A | Positive = charging, negative = discharging |
| Battery Temperature | °C | Battery temperature |
| Battery Nominal Capacity | kWh | User-configured capacity |

### Solar

| Sensor | Unit | Description |
|---|---|---|
| PV String 1/2/3 Power | W | Per-string power (current × PV voltage) |
| PV String 1/2/3 Current | A | MPPT string current |
| PV Voltage Global | V | Common PV bus voltage |

### AC Grid

| Sensor | Unit | Description |
|---|---|---|
| Grid Voltage L1/L2/L3 | V | Per-phase voltage |
| Grid Current L1/L2/L3 | A | Per-phase current |
| Grid Frequency | Hz | Grid frequency |
| Grid Apparent Power | VA | Total apparent power |
| Inverter Temperature | °C | Inverter temperature |

### Limits (Diagnostic)

| Sensor | Unit | Description |
|---|---|---|
| Inverter Nominal Power Limit | W | Maximum inverter output |
| Inverter Current Max Power | W | Current active power limit |
| Max Battery Discharge Power | W | Calculated: 3300 W × number of modules |
| Max Charge Power | W | Calculated: 2500 W × number of modules |
| Min SOC Limit | % | Configured minimum state of charge |

### Energy – Today

| Sensor | Unit | Description |
|---|---|---|
| House Consumption Today | kWh | Calculated from energy balance |
| Solar Yield Today | kWh | Total solar energy generated today |
| Grid Import Today | kWh | Energy imported from grid today |
| Grid Export Today | kWh | Energy exported to grid today |
| Battery Charged Today | kWh | Energy charged today |
| Battery Discharged Today | kWh | Energy discharged today |

### Energy – Lifetime

| Sensor | Unit | Description |
|---|---|---|
| House Consumption Total | kWh | Calculated from energy balance |
| Solar Yield Total | kWh | Lifetime solar generation |
| Grid Import Total | kWh | Lifetime grid import |
| Grid Export Total | kWh | Lifetime grid export |
| Battery Charged Total | kWh | Lifetime energy charged |
| Battery Discharged Total | kWh | Lifetime energy discharged |
| Battery Net Energy | kWh | Charged minus discharged |

---

## Debug Logging

To enable debug logging without editing `configuration.yaml`:

- Settings → Devices & Services → EF-PowerOcean-TcpModbus → Enable debug logging
---

## Screenshots
<img width="334" height="1202" alt="Screenshot 2026-04-02 132824" src="https://github.com/user-attachments/assets/dc73b934-ad8b-4610-8050-45d445dc318f" />
<img width="326" height="1276" alt="Screenshot 2026-04-02 132833" src="https://github.com/user-attachments/assets/f5908343-ff6f-450b-9b55-8c7a0ad59859" />




---

## Technical Details

- **Protocol:** Modbus TCP (port 502)
- **Register type:** Holding Registers (Function Code 3)
- **Float encoding:** 32-bit IEEE 754, little-endian word order (word-swapped)
- **Read strategy:** Block reads (5 requests per poll cycle)
- **Tested firmware:** 3.0.15.10
- **Tested pymodbus version:** 3.6.9 and 3.11.x

Full register map: [EcoFlow_PowerOcean_Modbus.md](EcoFlow_PowerOcean_Modbus.md)

---

## Contributing

Pull requests are welcome! Especially:

- Testing on other EcoFlow devices (PowerOcean DC, Connect)
- Identifying further Modbus registers
- Home Assistant Energy Dashboard configuration examples

Please open an issue before submitting large changes.

---

## Credits

Special thanks to **Kater Carlo** for his significant contributions to register mapping, sensor definitions and testing – this release would not have happened without him. 🐱

---

## Disclaimer

This integration was developed through community reverse engineering.
EcoFlow does not officially support or document this Modbus interface.
Use at your own risk. Not affiliated with EcoFlow Technology Co., Ltd.

---

## License

MIT License – free to use, modify and distribute with attribution.
