# Sensors

Every entity the integration creates, by group, and how the energy totals are
checked and corrected. For setup and battery control, see the
[README](../README.md).

<!-- toc -->

## Contents

- [Power (real-time)](#power-real-time)
- [Battery](#battery)
- [Solar](#solar)
- [AC Grid](#ac-grid)
- [Status](#status)
- [Faults (Diagnostic)](#faults-diagnostic)
- [Inverter Limits (Diagnostic)](#inverter-limits-diagnostic)
- [Energy – Today](#energy--today)
- [Energy – Lifetime](#energy--lifetime)

<!-- tocstop -->

## Power (real-time)

| Sensor        | Unit | Description                                 |
| ------------- | ---- | ------------------------------------------- |
| House Power   | W    | Current house consumption                   |
| Grid Power    | W    | Grid exchange (negative = export)           |
| Solar Power   | W    | Total PV generation (sum of active strings) |
| Battery Power | W    | Battery charge/discharge power              |

## Battery

| Sensor                            | Unit | Description                                                      |
| --------------------------------- | ---- | ---------------------------------------------------------------- |
| Battery SOC                       | %    | System state of charge                                           |
| Battery 1–12 SOC                  | %    | Per-module state of charge (diagnostic)                          |
| Battery Module Count              | –    | Modules reported online by the device (diagnostic)               |
| Battery Remaining Energy          | kWh  | Estimated: Battery Nominal Capacity × SOC                        |
| Battery Voltage                   | V    | Pack voltage                                                     |
| Battery Current                   | A    | Positive = charging, negative = discharging                      |
| Battery Temperature               | °C   | Mean module temperature                                          |
| Battery Nominal Capacity          | Wh   | Pack capacity; the last one kept while the device reads 0        |
| Available Battery Charge Power    | W    | Charge power limit reported from the EcoFlow app (diagnostic)    |
| Available Battery Discharge Power | W    | Discharge power limit reported from the EcoFlow app (diagnostic) |
| App Backup Reserve                | %    | Backup reserve set in the EcoFlow app (emulated reserve only)    |
| Effective Battery Reserve         | %    | Reserve the battery is held at now; `source` says by what        |

> ⚠️ _Available Battery Charge/Discharge Power_ reflect limits configured in the
> EcoFlow app, but **battery control over Modbus ignores those limits**. For example,
> a 500 W app limit will not stop a Modbus charge command from running at the
> configured battery-control ceiling.

## Solar

| Sensor                  | Unit | Description                                     |
| ----------------------- | ---- | ----------------------------------------------- |
| PV String 1/2/3 Power   | W    | Per-string power (current × own string voltage) |
| PV String 1/2/3 Current | A    | MPPT string current                             |
| PV String 1/2/3 Voltage | V    | Per-string DC voltage                           |

## AC Grid

| Sensor                     | Unit | Description                                                          |
| -------------------------- | ---- | -------------------------------------------------------------------- |
| Inverter Voltage L1/L2/L3  | V    | Per-phase voltage at the inverter's AC side                          |
| Inverter Current L1/L2/L3  | A    | Per-phase current at the inverter's AC side                          |
| Inverter AC Power          | W    | Power through the inverter's AC side (positive = drawn) (diagnostic) |
| Inverter Reactive Power    | var  | Reactive power at the inverter's AC side (diagnostic)                |
| Grid Frequency             | Hz   | Frequency the inverter measures                                      |
| Grid-side Voltage L1/L2/L3 | V    | Per-phase voltage at the grid connection (diagnostic)                |
| Grid-side Current L1/L2/L3 | A    | Per-phase current at the grid connection (diagnostic)                |
| Inverter Temperature       | °C   | Inverter temperature                                                 |
| Grid Connection Code       | –    | Grid standard code set by the installer (diagnostic)                 |

The _Grid Connection Code_ is the grid standard the inverter was commissioned with, as a
raw number. EcoFlow doesn't document which standard each number stands for and (in my testing)
the mapping used by other EcoFlow products doesn't fully match the PowerOcean series, so ask
your installer if you need to know.

### Inverter side and grid side

The _Inverter_ sensors and _Grid Frequency_ are measured by the inverter itself.
On-grid, Inverter Voltage and Grid Frequency follow the grid's.

On models with a built-in smart meter, like the PowerOcean Plus, the meter sits at the
GRID terminals. Inverter Voltage, Inverter Current, Inverter AC Power and Inverter
Reactive Power then show the grid connection.

_Inverter Reactive Power_ comes from a register that is not in EcoFlow's V1.0
protocol, only in their register schema. It is confirmed on a PowerOcean Plus. On a
model that refuses it, the read is dropped and the sensor stays unknown. Its sign is
not known yet.

The _Grid-side_ sensors need an external EcoFlow Smart Meter. Without one they read 0,
so the integration disables them, and enables them again once a meter reports.

## Status

| Sensor            | Values                                            | Description                                   |
| ----------------- | ------------------------------------------------- | --------------------------------------------- |
| Grid Mode         | Grid-connected / Islanded                         | On-grid or off-grid operation                 |
| Operating Mode    | Standby / Self-consumption / AI / Battery heating | Working mode reported by the inverter         |
| Self-powered Mode | Active / Inactive                                 | Self-consumption mode                         |
| Intelligent Mode  | Active / Inactive                                 | AI mode                                       |
| Battery Heating   | Running / Not running                             | The inverter is heating the batteries         |
| System Fault      |                                                   | Device reports an abnormal system state       |
| System Powered On |                                                   | Device is powered on (diagnostic)             |
| Modbus Control    |                                                   | The device is accepting our commands          |
| Control Status    |                                                   | What the selected battery mode is achieving   |
| Control Test      | Idle / Running / Done / Aborted                   | The last run of the control test (diagnostic) |
| Reading Check     | Problem / OK                                      | A check shows readings wrong (diagnostic)     |

_Battery heating_ is not in EcoFlow's protocol. A PowerOcean Three Phase reports it
in the System Status while it heats its batteries in the cold, around 0 °C, and
lists fault 256, Battery temperature too low, for each pack meanwhile. The heater
takes its power through the inverter, not from the battery cells, and it blocks
charging while it runs. _Battery Power_ and the battery charged energy count the
heater's power as charging.

## Faults (Diagnostic)

| Sensor             | Description                                               |
| ------------------ | --------------------------------------------------------- |
| Active Fault Count | Number of faults the device is currently reporting (0–20) |
| Active Fault Codes | Comma-separated fault codes, in decimal                   |

The fault codes are shown in decimal, as the EcoFlow app and web portal show them.
The `faults` attribute of Active Fault Codes lists each code with its name in Home
Assistant's language, or `null` for a code nobody has named yet. EcoFlow's protocol
does not include its fault definitions, so the names are collected from the event
history in the EcoFlow portal. To add one, see
[CONTRIBUTING.md](../CONTRIBUTING.md#fault-names).

## Inverter Limits (Diagnostic)

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
| Coordinator Status                 | –    | Integration polling state; Readings stalled during a stall      |

The inverter keeps its own copy of settings such as the export cap for Modbus
mode. It takes the app's values the first time Modbus mode is turned on, and
after that the two copies are no longer kept in step.

## Energy – Today

| Sensor                   | Unit | Description                        |
| ------------------------ | ---- | ---------------------------------- |
| House Consumption Today  | kWh  | Calculated from energy balance     |
| Solar Yield Today        | kWh  | Total solar energy generated today |
| Grid Import Today        | kWh  | Energy imported from grid today    |
| Grid Export Today        | kWh  | Energy exported to grid today      |
| Battery Charged Today    | kWh  | Energy charged today               |
| Battery Discharged Today | kWh  | Energy discharged today            |

### Energy - Today (Diagnostic)

Daily energy values are calculated from the corresponding lifetime counters because device-reported daily values have been shown to not reliably reset. The original device values remain available through these diagnostic sensors.

| Sensor                            | Entity key                 | Unit |
| --------------------------------- | -------------------------- | ---- |
| Solar Yield Today (Device)        | `solar_today_raw`          | kWh  |
| Grid Import Today (Device)        | `grid_import_today_raw`    | kWh  |
| Grid Export Today (Device)        | `grid_export_today_raw`    | kWh  |
| Battery Charged Today (Device)    | `bat_charged_today_raw`    | kWh  |
| Battery Discharged Today (Device) | `bat_discharged_today_raw` | kWh  |

## Energy – Lifetime

| Sensor                   | Unit | Description                           |
| ------------------------ | ---- | ------------------------------------- |
| House Consumption Total  | kWh  | Calculated from energy balance        |
| Solar Yield Total        | kWh  | Lifetime solar generation             |
| Grid Import Total        | kWh  | Lifetime grid import                  |
| Grid Export Total        | kWh  | Lifetime grid export                  |
| Battery Charged Total    | kWh  | Lifetime energy charged               |
| Battery Discharged Total | kWh  | Lifetime energy discharged            |
| Battery Energy Loss      | kWh  | Charged minus discharged (diagnostic) |

### Energy - Lifetime (Diagnostic)

The lifetime totals above are checked before they are published: a value that drops, or rises faster than the configured maximum power allows, is held. These diagnostic sensors show what the device itself reports, so a held total can be told apart from a device that reports the same. They have no state class, so they never feed the Energy dashboard or long-term statistics.

| Sensor                            | Entity key                  | Unit |
| --------------------------------- | --------------------------- | ---- |
| Solar Yield Total (Device)        | `solar_total_raw`           | kWh  |
| Grid Import Total (Device)        | `grid_import_total_raw`     | kWh  |
| Grid Export Total (Device)        | `grid_export_total_raw`     | kWh  |
| Battery Charged Total (Device)    | `bat_charged_total_raw`     | kWh  |
| Battery Discharged Total (Device) | `bat_discharged_total_raw`  | kWh  |
| Inverter AC Input Total (Device)  | `inverter_ac_in_total_raw`  | kWh  |
| Inverter AC Output Total (Device) | `inverter_ac_out_total_raw` | kWh  |

If a total has been held for more than 24 hours while the device reports a value above 0, a repairs issue lists it with both values. It offers to keep Home Assistant's total, or to switch to the inverter's. If the values look wrong instead, it links to a filled-in bug report and to the device page for the diagnostics download. This happens when the inverter resets a counter or a wrong value was accepted earlier. A total held at 0 while the device's total rises in step with its own daily counter is corrected automatically.

### Grid energy calculated from Grid Power

Some inverters report 0 for grid import and export while Grid Power reads right (#218), which also makes House Consumption wrong. If all four grid energy registers have read 0 for more than 24 hours while at least 1 kWh went through the grid by Grid Power, a repairs issue offers to turn on **Calculation of grid energy**. It is never turned on by itself, since a counter has been seen reading a bogus 0 for hours.

With it on, Grid Import Total and Grid Export Total are integrated from Grid Power on every poll (positive is import, negative export), and the today values follow from them as for any total. The totals continue from the values last shown, so the Energy dashboard records no jump. It is an estimate: power changes between polls are not seen, and nothing is counted across a gap of more than 2 minutes between readings, such as an outage or a restart, or while Grid Power stands still because the inverter stopped updating its readings. The (Device) sensors keep showing what the inverter reports, and the diagnostics download has the integration's state under `energy.grid_energy`. Turning it off again holds the totals at their last value while the inverter reports 0.

### Correcting a lifetime total

Some inverters reset a lifetime counter, while EcoFlow's portal keeps counting. A total can be corrected so it continues as before, by adding an offset to what the device reports:

- **Keep Home Assistant's totals** in the repairs issue keeps each held total at its value in Home Assistant and works the offset out from it, so the Energy dashboard continues without a jump.
- **Correct energy totals**, a collapsed section in **Configure**, sets a total to a value you enter, such as the one under historical data in [EcoFlow's portal](https://user-portal.ecoflow.com/user/eu/en/main/home/dashboard/historicalData). Leave the totals that are right as they are. Decimals can be typed with a comma or a point. Each field names the matching total in the portal, such as "From solar" for Solar Yield Total. The inverter's AC input and output totals are not in the portal, so they are never corrected: a held AC total takes the inverter's value after 24 hours instead of raising the repairs issue. To remove a correction, enter the value of the total's (Device) sensor. While any correction is set, the section also has a **Reset all totals to the inverter's values** toggle that removes them all.

Today's values are not changed by a correction, and the (Device) sensors keep showing what the inverter reports. A value lower than the total shown asks for confirmation first: Home Assistant records a drop as negative energy, or, if it is more than 10%, as a new meter that adds the whole value to the Energy dashboard. Developer tools → Statistics → Adjust sum corrects that history. House Consumption Total is calculated from the corrected totals, so it moves with a correction: up in one step, or held until it has caught up if the correction lowers it. The offsets are in the diagnostics download under `energy_offsets`.

### Energy in the diagnostics download

The diagnostics download has an `energy` section with every counter as published, as the device reports it (totals and today), the time each total was last accepted, which totals are held, the daily snapshots and the maximum powers the totals are checked against.
Its `raw_registers` section holds every word the last poll read, undecoded, by the
address each read started at, so a report shows exactly what the inverter sent.
The serial number is never included.
