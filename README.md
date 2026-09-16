# EF-PowerOcean-TcpModbus

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/release/MaxGrmm/EF-PowerOcean-TcpModbus.svg)](https://github.com/MaxGrmm/EF-PowerOcean-TcpModbus/releases)

**Local Modbus TCP integration for the EcoFlow PowerOcean home battery system.**

> ⚠️ This integration communicates directly with your device over your local network via Modbus TCP. No cloud connection required.

---

## Features

- **Local polling** – no EcoFlow cloud account needed
- **Configurable poll interval** (2–30 seconds, default 5 s)
- Real-time power flow: house consumption, grid import/export, solar generation, battery
- Optional **Battery Controls**: charge, discharge, export or hold, with state-of-charge guards
- Full battery monitoring: SOC, voltage, current, power, temperature, remaining energy
- Per-module state of charge for up to 12 battery modules
- Per-string PV power, current and voltage (1–3 strings)
- Per-phase AC measurements: voltage, current, frequency
- Energy counters: daily and lifetime for grid, solar, battery charge/discharge, house consumption
- Operating mode, grid mode and system status as dedicated entities
- Fault reporting: active fault count and raw fault codes
- Firmware and product information read from the device, with model mismatch detection where supported
- Reconfigurable after setup via **Settings → Configure** (no re-install needed)
- Debug logging toggle directly in the HA UI
- German and English translations

---

## Supported Devices

| Device                     | Status                         |
| -------------------------- | ------------------------------ |
| EcoFlow PowerOcean Plus    | ✅ Confirmed                   |
| EcoFlow PowerOcean 3-phase | ✅ Confirmed                   |
| EcoFlow PowerOcean 1-phase | ❓ Untested – feedback welcome |
| EcoFlow PowerOcean DC Fit  | ❓ Untested – feedback welcome |
| EcoFlow Ocean2             | ❓ Untested – feedback welcome |

---

## Prerequisites

The ModBus must be enabled by your EcoFlow Partner / Installer, it is disabled by default!

---

## Installation

### Via HACS (recommended)

[![Add to Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=MaxGrmm&repository=EF-PowerOcean-TcpModbus&category=integration)

To add it manually instead:

1. Open HACS in Home Assistant
2. Go to **Integrations** → **⋮** → **Custom repositories**
3. Add `https://github.com/MaxGrmm/EF-PowerOcean-TcpModbus` as category **Integration**
4. Open the repository in HACS and click **Install**
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
| IP Address                 | –                      | Local IP of your PowerOcean inverter                                                                                                                             |
| Port                       | 502                    | Modbus TCP port                                                                                                                                                  |
| Inverter model             | PowerOcean Three Phase |                                                                                                                                                                  |
| Number of Batteries        | 0                      | Number of installed battery modules (0–12); required for safe battery-control power limits                                                                       |
| Maximum solar power        | 12 kW                  | Installed solar power (1–60 kW)                                                                                                                                  |
| Maximum grid power         | 15 kW                  | Expected maximum grid power used to reject implausible readings (1–60 kW)                                                                                        |
| Calculation of solar power | false                  | In some inverters, the modbus register delivers 0W of solar power. This switch allows the solar power to be calculated from the individual powers of the string. |
| Modbus Control             | false                  | Allow this integration to command the battery. See [Battery Control](#battery-control).                                                                          |
| Poll Interval (seconds)    | 5                      | How often values are fetched                                                                                                                                     |

To change settings after setup: **Settings → Devices & Services → EF-PowerOcean-TcpModbus → Configure**

---

## Battery Control

Off by default. Turning **Modbus Control** on makes the integration hold control
authority over the inverter, which **locks the EcoFlow app out control** for
as long as the integration is running and Modbus Control is turned on.

The **Battery Mode** select is the primary control:

| Mode              | What the inverter does                                                         |
| ----------------- | ------------------------------------------------------------------------------ |
| Automatic         | Self-consumption, exactly as the app runs it                                   |
| Hold battery      | Don't charge or discharge the battery. Surplus solar power is exported         |
| Charge battery    | Charges at the set power, importing from the grid if solar power is not enough |
| Discharge battery | Discharges at the set power                                                    |
| Export to grid    | Exports at the set power, uses the battery if solar power is not enough        |

Charge and Export are two views of the same thing: **Charge pins the battery and lets the
grid float, Export pins the grid and lets the battery float.** The setpoint is a target
rather than a cap in both directions, and as long as the battery has room the inverter
reaches it without limiting solar, with whatever is left over going to the battery or is
exported.

Two guards apply in every mode, including Automatic, and only ever restrict:

- **Charge Limit** – state of charge above which the battery is not charged (100 = off)
- **Battery Reserve** – state of charge below which it is not drained (0 = off)

Both guards default to off. Separately, **Modbus Control** defaults to off, so an
untouched install never takes control away from the app.
**Control Status** reports what the selected mode is achieving, including when a guard
is holding it or when the battery has no headroom left to reach the target.

| Control Status             | Meaning                                                        |
| -------------------------- | -------------------------------------------------------------- |
| No Modbus control          | Modbus Control is disabled or control authority was lost       |
| Automatic                  | The inverter is running its normal self-consumption mode       |
| Active                     | The selected target is being maintained                        |
| Ramping                    | The inverter is moving toward the selected target              |
| Charge limit reached       | The Charge Limit guard is preventing further charging          |
| Reserve reached            | The Battery Reserve guard is preventing further discharge      |
| Unreachable: battery full  | The target requires the battery to absorb power, but it cannot |
| Unreachable: battery empty | The target requires the battery to supply power, but it cannot |

On the device page the two are deliberately kept apart:

| Section           | Entities                                                          | Meaning                                                          |
| ----------------- | ----------------------------------------------------------------- | ---------------------------------------------------------------- |
| **Controls**      | Battery Mode, Charge/Discharge/Export Power                       | What you are asking the inverter to do right now                 |
| **Configuration** | Charge Limit, Battery Reserve, LED Brightness, Battery Saver Mode | Standing settings; the two guards bind whatever mode is selected |
| **Sensors**       | Control Status                                                    | What the inverter is actually doing about it                     |

Each mode's power stays editable while another mode is selected, so a command can be
set up before it is needed. Only the selected mode's value is ever sent.

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

| Sensor                            | Unit | Description                                                      |
| --------------------------------- | ---- | ---------------------------------------------------------------- |
| Battery SOC                       | %    | System state of charge                                           |
| Battery 1–12 SOC                  | %    | Per-module state of charge (diagnostic)                          |
| Battery Module Count              | –    | Modules reported online by the device (diagnostic)               |
| Battery Remaining Energy          | kWh  | Estimated: 5 kWh × modules × SOC                                 |
| Battery Voltage                   | V    | Pack voltage                                                     |
| Battery Current                   | A    | Positive = charging, negative = discharging                      |
| Battery Temperature               | °C   | Mean module temperature                                          |
| Battery Nominal Capacity          | Wh   | Nominal pack capacity reported by the device                     |
| Available Battery Charge Power    | W    | Charge power limit reported from the EcoFlow app (diagnostic)    |
| Available Battery Discharge Power | W    | Discharge power limit reported from the EcoFlow app (diagnostic) |
| Min SOC Limit                     | %    | Backup reserve configured in the EcoFlow app                     |

> ⚠️ _Available Battery Charge/Discharge Power_ reflect limits configured in the
> EcoFlow app, but **battery control over Modbus ignores those limits** — as it does
> _Min SOC Limit_. For example, a 500 W app limit will not stop a Modbus charge command
> from running at the configured battery-control ceiling.

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

| Sensor            | Values                          | Description                                 |
| ----------------- | ------------------------------- | ------------------------------------------- |
| Grid Mode         | Grid-connected / Islanded       | On-grid or off-grid operation               |
| Operating Mode    | Standby / Self-consumption / AI | Working mode reported by the inverter       |
| Self-powered Mode | Active / Inactive               | Self-consumption mode                       |
| Intelligent Mode  | Active / Inactive               | AI mode                                     |
| System Fault      |                                 | Device reports an abnormal system state     |
| System Powered On |                                 | Device is powered on (diagnostic)           |
| Modbus Control    |                                 | The device is accepting our commands        |
| Control Status    |                                 | What the selected battery mode is achieving |

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

| Sensor                   | Unit | Description                           |
| ------------------------ | ---- | ------------------------------------- |
| House Consumption Total  | kWh  | Calculated from energy balance        |
| Solar Yield Total        | kWh  | Lifetime solar generation             |
| Grid Import Total        | kWh  | Lifetime grid import                  |
| Grid Export Total        | kWh  | Lifetime grid export                  |
| Battery Charged Total    | kWh  | Lifetime energy charged               |
| Battery Discharged Total | kWh  | Lifetime energy discharged            |
| Battery Energy Loss      | kWh  | Charged minus discharged (diagnostic) |

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
- **Reads:** Holding Registers (Function Code 3); multi-register values are decoded
  low word first (word-swapped)
- **Writes:** Function Code 6 for one register and Function Code 16 for multiple
  registers; multi-register values are encoded high word first
- **Float encoding:** 32-bit IEEE 754
- **Read strategy:** 3 block reads per poll cycle, grouped automatically from the
  register addresses, plus one device-information read when the connection opens
- **Tested firmware:** 3.0.19.19
- **Tested pymodbus version:** 3.6.9, 3.11.x and 3.13.x

The register map lives in [`const.py`](custom_components/ef_powerocean_tcpmodbus/const.py) as absolute Modbus addresses. For address numbering, word order, decoding and known gaps, see [EcoFlow_PowerOcean_Modbus.md](EcoFlow_PowerOcean_Modbus.md).

---

## Contributing

Contributions use personal forks and pull requests. See
[CONTRIBUTING.md](CONTRIBUTING.md) for setup, testing, safety requirements, and the
review checklist.

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
