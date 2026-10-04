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
- Optional **Battery Controls**: charge, discharge, hold, export, import or export solar first, with state-of-charge guards and a grid feed-in switch
- Full battery monitoring: SOC, voltage, current, power, temperature, remaining energy
- Per-module state of charge for up to 12 battery modules
- Per-string PV power, current and voltage (1–3 strings)
- Per-phase AC measurements: voltage, current, frequency
- Energy counters: daily and lifetime for grid, solar, battery charge/discharge, house consumption
- Operating mode, grid mode and system status as dedicated entities
- Fault reporting: active fault count and raw fault codes
- Serial number and firmware version read from the device. The model it reports pre-fills the setup form; the model you select is the one the integration uses
- Reconfigurable after setup via **Settings → Configure** (no re-install needed)
- Debug logging toggle directly in the HA UI
- German and English translations

---

## Supported Devices

| Device                     | Read         | Control      |
| -------------------------- | ------------ | ------------ |
| EcoFlow PowerOcean Plus    | ✅ Confirmed | ✅ Confirmed |
| EcoFlow PowerOcean 3-phase | ✅ Confirmed | ✅ Confirmed |
| EcoFlow PowerOcean 1-phase | ✅ Confirmed | ❓ Untested  |
| EcoFlow PowerOcean DC Fit  | ❓ Untested  | ❓ Untested  |
| EcoFlow Ocean 2 3-phase    | ✅ Confirmed | ❓ Untested  |
| EcoFlow Ocean 2 1-phase    | ✅ Confirmed | ❓ Untested  |

Running an untested model? [scripts/register_scan.py](scripts/register_scan.py) produces a read-only report comparing your inverter against the register map this integration expects. Attaching its output to an issue is what makes a device supportable, see [Register Scan](CONTRIBUTING.md#register-scan).

---

## Supported Home Assistant Versions

Home Assistant **2025.12.0** is the earliest supported version. HACS blocks installing it on older releases, and CI tests every change against both 2025.12.0 and a recent release.

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
| Poll Interval (seconds)    | 5                      | How often values are fetched                                                                                                                                     |

To change settings after setup: **Settings → Devices & Services → EF-PowerOcean-TcpModbus → Configure**

---

## Battery Control

Off by default. Turning on **Modbus Control** (in the device's Configuration section)
lets the integration control the inverter and **locks the EcoFlow app out** while it is
on. Turning it off hands control back to the app within about 60 seconds.

Choose what the battery does with the **Battery Mode** select:

| Mode               | Goal                                        | How                                                                                                         |
| ------------------ | ------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Automatic          | Use as much of your own solar as possible   | The inverter's normal self-consumption, as the app runs it.                                                 |
| Hold battery       | Keep the battery's charge for later         | The battery neither charges nor discharges; surplus solar is exported.                                      |
| Charge battery     | Fill the battery                            | Charges at Battery Charge Power, from the grid when solar is not enough.                                    |
| Discharge battery  | Empty the battery                           | Discharges at Battery Discharge Power; what the house does not use is exported.                             |
| Export to grid     | Sell power                                  | Exports Grid Export Power, using the battery when solar falls short and charging it with any surplus above. |
| Import from grid   | Charge from the grid without overloading it | Draws Grid Import Power; the battery takes what the house leaves and backs off as the house takes more.     |
| Export solar first | Export solar, and store only what is left   | Exports surplus up to the Solar Export Limit and stores only the rest; otherwise runs as Automatic.         |

Each mode's power can be set ahead of time; only the selected mode's is sent. The
Solar Export Limit goes up to your export limit, or the inverter's maximum without
one, and keeps 100 W under an export limit it is set at.

### Guards

Two guards bind whatever mode is selected. They only ever stop the battery, never turn
it around:

| Guard           | The battery is not…                   | Off at |
| --------------- | ------------------------------------- | ------ |
| Charge Limit    | charged above this state of charge    | 100 %  |
| Battery Reserve | discharged below this state of charge | 0 %    |

A guard holds the battery wherever the selected mode would move it the forbidden way.
In Automatic the battery can still power the house under the Charge Limit, and still
charge from solar under the Battery Reserve. A guard lets go once the state of charge
has moved 5% back. Leave Min SOC and schedules in the EcoFlow app off, since the
inverter follows them whenever it runs by itself.

<details>
<summary>How the guards keep control</summary>

The inverter has no setting for a charge limit, so while a guard is on the integration
runs self-consumption itself, except in the direction the guard forbids. When the
battery can only move the allowed way for a while, such as the house clearly drawing
more than the solar under the Charge Limit, the inverter does that faster by itself, so
the integration lets it and takes over again once the power flow turns. On single-phase
models it does so at once, with a 20 W deadband and 30 s settling. A load that keeps
switching, like an oven, makes it wait longer each time.

The guards are soft limits: the battery can move the wrong way for a few seconds before
it is caught. While held near zero, the battery wanders a few hundred watts either way
as clouds pass, because EcoFlow does not expose the power limit the app's schedules use.
See [Battery power limits](EcoFlow_PowerOcean_Modbus.md#battery-power-limits).

</details>

### Control Status

| Control Status             | Meaning                                                         |
| -------------------------- | --------------------------------------------------------------- |
| No Modbus control          | Modbus Control is off, or control authority was lost            |
| Handing back to the app    | Modbus Control was switched off; the app takes over within 60 s |
| Automatic                  | The inverter runs its normal self-consumption                   |
| Active                     | The selected mode is doing what it should                       |
| Ramping                    | The inverter has not reached the target for a few polls         |
| Charge limit reached       | The Charge Limit is stopping the battery from charging          |
| Reserve reached            | The Battery Reserve is stopping the battery from discharging    |
| Below solar export limit   | Export solar first: the whole surplus is being exported         |
| Unreachable: battery full  | The target needs the battery to charge, but it is full          |
| Unreachable: battery empty | The target needs the battery to discharge, but it is empty      |

When the inverter misses a target a guard set, the status shows Ramping or Unreachable
instead; the guard stays available in the `guard` attribute for automations.

On the device page, **Controls** hold what you ask for (Battery Mode and each mode's
power), **Configuration** the standing settings (Modbus Control, the guards, LED
Brightness, Battery Saver Mode, Grid Feed-in), and **Control Status** what the inverter
is actually doing.

### Commanding the battery from automations

The **Set battery command** action (`ef_powerocean_tcpmodbus.set_battery_command`) sets
the Battery Mode, its power and the Charge Limit in one step, for automations and
battery planners.

```yaml
action: ef_powerocean_tcpmodbus.set_battery_command
data:
  device_id: <your inverter's device id>
  mode: charge_battery
  power: 3000
  charge_limit_soc: 80
  expire_in: 900
```

- It changes the same entities you would change by hand.
- `power` is required for Charge battery, Discharge battery, Export to grid and Import
  from grid, optional for Export solar first (it sets the Solar Export Limit), and not
  allowed otherwise. It must be above 0 W and is capped at the inverter's maximum.
- `charge_limit_soc` is optional and applied together with the mode.
- With `expire_in` (60 to 86400 s) the mode returns to Automatic unless the command is
  sent again in time; repeating it only moves the expiry. Choosing a mode by hand
  cancels it, a restart starts in Automatic, and the Charge Limit stays. Control Status shows when it runs out as
  `expires_at`, and the `ef_powerocean_tcpmodbus_command_expired` event fires when it
  does.
- Only `mode: automatic` is accepted while Modbus Control is off.

Run one controller at a time: the action does not stop two of them overriding each
other.

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
> EcoFlow app, but **battery control over Modbus ignores those limits**, as it does
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

| Sensor                             | Unit | Description                                                     |
| ---------------------------------- | ---- | --------------------------------------------------------------- |
| Inverter Rated Power               | W    | Nameplate system power                                          |
| Maximum Inverter Power (DC to AC)  | W    | Nameplate inverter (discharge direction) capacity               |
| Maximum Rectifier Power (AC to DC) | W    | Nameplate rectifier (charge direction) capacity                 |
| Maximum feed-in Power              | W    | The export cap in force, which bounds Export to Grid            |
| Maximum feed-in Power (Configured) | W    | Export cap as configured (40538)                                |
| Maximum feed-in Power (Effective)  | W    | Export cap after the safety rules (40609), not on the PO Plus   |
| Maximum feed-in Power (Percent)    | %    | Export cap as a share of the rated power, not on the PO Plus    |
| Grid Feed-in Mode                  | –    | Whether the export is limited in watts, in percent or unlimited |
| System Modes                       | –    | Raw system status                                               |
| Coordinator Status                 | –    | Integration polling state                                       |

The inverter keeps its own copy of settings such as the export cap for Modbus
mode. It takes the app's values the first time Modbus mode is turned on, and
after that the two copies are no longer kept in step.

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

<img width="431" height="431" alt="Controls" src="https://github.com/user-attachments/assets/1c0f9217-cba7-47f4-8bc6-9a3632c1bdcd" />
<img width="431" height="443" alt="Configuration" src="https://github.com/user-attachments/assets/320ce431-1d6b-4f3f-965c-912f9e4be31a" />
<img width="431" height="1539" alt="Sensor_Entities" src="https://github.com/user-attachments/assets/dca4b3c8-a56d-4d8d-ad13-6499f2cfd63c" />
<img width="431" height="2075" alt="Diagnostics" src="https://github.com/user-attachments/assets/4380c0bb-a5f9-4a7d-8225-af3c5f2d3ab2" />

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
- **Tested firmware:** 3.0.19.19 + 3.0.20.54(PO+)
- **Tested pymodbus version:** 3.6.9, 3.11.x and 3.13.x

The register map lives in [`const.py`](custom_components/ef_powerocean_tcpmodbus/const.py) as absolute Modbus addresses. For address numbering, word order, decoding and known gaps, see [EcoFlow_PowerOcean_Modbus.md](EcoFlow_PowerOcean_Modbus.md).

---

## Contributing

Contributions use personal forks and pull requests. See
[CONTRIBUTING.md](CONTRIBUTING.md) for setup, testing, safety requirements, and the
review checklist.

---

## Credits

Special thanks to all contributors for the massive amount of time and effort that helped this project grow so fast!

<p>
  <a href="https://github.com/windmark">
    <img src="https://github.com/windmark.png" width="50" height="50" alt="windmark"/><br/>
    windmark
  </a>
</p>
<p>
  <a href="https://github.com/fuchsi585">
    <img src="https://github.com/fuchsi585.png" width="50" height="50" alt="fuchsi585"/><br/>
    fuchsi585
  </a>
</p>
<p>
  Kater Carlo
</p>

---

## Disclaimer

This integration was developed through community reverse engineering.
EcoFlow does not officially support or document this Modbus interface.
Use at your own risk. Not affiliated with EcoFlow Technology Co., Ltd.

---

## License

MIT License – free to use, modify and distribute with attribution.
