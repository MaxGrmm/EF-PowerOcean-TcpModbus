# EF-PowerOcean-TcpModbus

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/release/MaxGrmm/EF-PowerOcean-TcpModbus.svg)](https://github.com/MaxGrmm/EF-PowerOcean-TcpModbus/releases)

**Local Modbus TCP integration for the EcoFlow PowerOcean Plus home battery system.**

> ⚠️ This integration communicates directly with your device over your local network via Modbus TCP. No cloud connection required.

---

## Features

- **Local polling** – no EcoFlow cloud account needed
- **Configurable poll interval** (2–30 seconds, default 5 s)
- Real-time power flow: house consumption, grid import/export, solar generation, battery
- Full battery monitoring: SOC, voltage, current, power, temperature, remaining energy
- Per-module state of charge for up to 12 battery modules
- Per-string PV power, current and voltage (1–3 strings)
- Per-phase AC measurements: voltage, current, frequency
- Energy counters: daily and lifetime for grid, solar, battery charge/discharge, house consumption
- Operating mode, grid mode and system status as dedicated entities
- Fault reporting: active fault count and raw fault codes
- Model and firmware version read from the device
- Reconfigurable after setup via **Settings → Configure** (no re-install needed)
- Debug logging toggle directly in the HA UI
- German and English translations

---

## Supported Devices

| Device                     | Status                         |
| -------------------------- | ------------------------------ |
| EcoFlow PowerOcean Plus    | ✅ Confirmed                   |
| EcoFlow PowerOcean         | ✅ Confirmed                   |
| EcoFlow PowerOcean Connect | ❓ Untested – feedback welcome |

---

## Prerequisites

The ModBus must be enabled by your EcoFlow Partner / Installer, it is disabled by default!

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

| Field                      | Default                | Description                                                                                                                                                      |
| -------------------------- | ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| IP Address                 | –                      | Local IP of your PowerOcean Plus                                                                                                                                 |
| Port                       | 502                    | Modbus TCP port                                                                                                                                                  |
| Inverter model             | PowerOcean Three Phase |                                                                                                                                                                  |
| Number of Batteries        | 0                      | Number of installed battery modules (0–12)                                                                                                                       |
| Maximum solar power        | 12kW                   | Installed solar power                                                                                                                                            |
| Maximum grid power         | 15kW                   | Expected maximum grid power to detect unauthorized values                                                                                                        |
| Calculation of solar power | false                  | In some inverters, the modbus register delivers 0W of solar power. This switch allows the solar power to be calculated from the individual powers of the string. |
| Poll Interval (seconds)    | 5                      | How often values are fetched                                                                                                                                     |

To change settings after setup: **Settings → Devices & Services → EF-PowerOcean-TcpModbus → Configure**

---

## Development

Install the development dependencies and enable the formatting hooks:

```shell
python -m pip install -r requirements-development.txt
pre-commit install
```

Run all linting and formatting checks manually with:

```shell
pre-commit run --all-files
```

CI also runs these checks. Formatting fixes are committed automatically to
branches in this repository.

---

## Available Sensors

### Power (real-time)

| Sensor        | Unit | Description                                 |
| ------------- | ---- | ------------------------------------------- |
| House Power   | W    | Current house consumption                   |
| Grid Power    | W    | Grid exchange (negative = export)           |
| Solar Power   | W    | Total PV generation (sum of active strings) |
| Battery Power | W    | Battery charge/discharge power              |

### Battery

| Sensor                            | Unit | Description                                        |
| --------------------------------- | ---- | -------------------------------------------------- |
| Battery SOC                       | %    | System state of charge                             |
| Battery 1–12 SOC                  | %    | Per-module state of charge (diagnostic)            |
| Battery Module Count              | –    | Modules reported online by the device (diagnostic) |
| Battery Remaining Energy          | kWh  | Estimated: 5 kWh × modules × SOC                   |
| Battery Voltage                   | V    | Pack voltage                                       |
| Battery Current                   | A    | Positive = charging, negative = discharging        |
| Battery Temperature               | °C   | Mean module temperature                            |
| Battery Nominal Capacity          | Wh   | Nominal pack capacity reported by the device       |
| Available Battery Charge Power    | W    | Charge headroom right now – drops to 0 W when full |
| Available Battery Discharge Power | W    | Discharge headroom right now                       |
| Min SOC Limit                     | %    | Backup reserve configured in the EcoFlow app       |

> _Available Charge/Discharge Power_ are live headroom values, not static limits. A
> reading of 0W for charging means the battery is full.

### Solar

| Sensor                  | Unit | Description                                     |
| ----------------------- | ---- | ----------------------------------------------- |
| PV String 1/2/3 Power   | W    | Per-string power (current × own string voltage) |
| PV String 1/2/3 Current | A    | MPPT string current                             |
| PV String 1/2/3 Voltage | V    | Per-string DC voltage                           |

### AC Grid

| Sensor                | Unit | Description          |
| --------------------- | ---- | -------------------- |
| Grid Voltage L1/L2/L3 | V    | Per-phase voltage    |
| Grid Current L1/L2/L3 | A    | Per-phase current    |
| Grid Frequency        | Hz   | Grid frequency       |
| Inverter Temperature  | °C   | Inverter temperature |

### Status

| Sensor             | Values                          | Description                             |
| ------------------ | ------------------------------- | --------------------------------------- |
| Grid Mode          | Grid-connected / Islanded       | On-grid or off-grid operation           |
| Operating Mode     | Standby / Self-consumption / AI | Working mode reported by the inverter   |
| Self-powered Mode  | Active / Inactive               | Self-consumption mode                   |
| Intelligent Mode   | Active / Inactive               | AI mode                                 |
| Battery Saver Mode | Enabled / Disabled              | Low-power mode                          |
| System Fault       |                                 | Device reports an abnormal system state |
| System Powered On  |                                 | Device is powered on                    |

### Faults (Diagnostic)

| Sensor             | Description                                               |
| ------------------ | --------------------------------------------------------- |
| Active Fault Count | Number of faults the device is currently reporting (0–20) |
| Active Fault Codes | Comma-separated raw fault codes                           |

The meaning of the fault codes is not known, so we only publish the raw values.

### Inverter Limits (Diagnostic)

| Sensor                             | Unit | Description                                       |
| ---------------------------------- | ---- | ------------------------------------------------- |
| Inverter Rated Power               | W    | Nameplate system power                            |
| Maximum Inverter Power (DC to AC)  | W    | Nameplate inverter (discharge direction) capacity |
| Maximum Rectifier Power (AC to DC) | W    | Nameplate rectifier (charge direction) capacity   |
| Maximum feed-in Power              | W    | Export limit configured in the EcoFlow app        |
| Device LED brightness              | %    | Indicator brightness                              |
| System Modes                       | –    | Raw system status                                 |
| Coordinator Status                 | –    | Integration polling state                         |

### Energy – Today

| Sensor                   | Unit | Description                        |
| ------------------------ | ---- | ---------------------------------- |
| House Consumption Today  | kWh  | Calculated from energy balance     |
| Solar Yield Today        | kWh  | Total solar energy generated today |
| Grid Import Today        | kWh  | Energy imported from grid today    |
| Grid Export Today        | kWh  | Energy exported to grid today      |
| Battery Charged Today    | kWh  | Energy charged today               |
| Battery Discharged Today | kWh  | Energy discharged today            |

#### Energy - Today (Diagnostic)

Daily energy values are calculated from the corresponding lifetime counters because device-reported daily values have been shown to not reliably reset. The original device values remain available through these diagnostic sensors.

| Sensor                            | Entity key                 | Unit |
| --------------------------------- | -------------------------- | ---- |
| Solar Yield Today (Device)        | `solar_today_raw`          | kWh  |
| Grid Import Today (Device)        | `grid_import_today_raw`    | kWh  |
| Grid Export Today (Device)        | `grid_export_today_raw`    | kWh  |
| Battery Charged Today (Device)    | `bat_charged_today_raw`    | kWh  |
| Battery Discharged Today (Device) | `bat_discharged_today_raw` | kWh  |

### Energy – Lifetime

| Sensor                   | Unit | Description                    |
| ------------------------ | ---- | ------------------------------ |
| House Consumption Total  | kWh  | Calculated from energy balance |
| Solar Yield Total        | kWh  | Lifetime solar generation      |
| Grid Import Total        | kWh  | Lifetime grid import           |
| Grid Export Total        | kWh  | Lifetime grid export           |
| Battery Charged Total    | kWh  | Lifetime energy charged        |
| Battery Discharged Total | kWh  | Lifetime energy discharged     |
| Battery Net Energy       | kWh  | Charged minus discharged       |

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
- **Read strategy:** 3 block reads per poll cycle, grouped automatically from the
  register addresses, plus one device-information read when the connection opens
- **Tested firmware:** 3.0.19.19
- **Tested pymodbus version:** 3.6.9, 3.11.x and 3.13.x

The register map lives in [`const.py`](custom_components/ef_powerocean_tcpmodbus/const.py) as absolute Modbus addresses. For address numbering, word order, decoding and known gaps, see [EcoFlow_PowerOcean_Modbus.md](EcoFlow_PowerOcean_Modbus.md).

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
