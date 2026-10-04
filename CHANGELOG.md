# Changelog

## Unreleased

## [2.8.0] - 2026-10-04

### Added

- Import from grid Battery Mode, the counterpart of Export to grid. It holds the grid at a draw with a power of its own, so the battery charges with what the house leaves and backs off when a heavy load starts, where Charge battery keeps charging and the draw grows.
- Export solar first Battery Mode, which exports solar up to a limit before charging the battery.
- The System State 2 word (40532) as the `system_state_2_hex` attribute of Active Fault Count and in the diagnostics.

### Fixed

- Fixed the wrong word order for Ocean 2 Single Phase.
- Battery guards on models whose guards do not track setpoints (PowerOcean Single Phase and Ocean 2 Single Phase) now hand control back directly to native Automatic mode during solar deficits or surpluses rather than attempting Modbus setpoint tracking. A 20 W deadband with hysteresis, a 30-second settling window, and a 30-second dwell time prevent rapid toggling between Hold and Automatic.
- Reconciles grid-side and solar-side natural battery power estimates using the smaller surplus on models whose guards do not track setpoints, ensuring false surpluses or DC-DC settling transients do not delay handback or cause grid import.

## [2.7.0] - 2026-09-30

### Added

- Set battery command action for automations and battery planners. It sets the Battery Mode, its power and the Charge Limit in one step. With a timeout, the mode returns to Automatic unless the command is sent again in time, leaving the Charge Limit as it is.
- On Home Assistant 2026.9 and later, the inverter is reached over the connection Home Assistant's Modbus integration shares, so another integration using the same host and port no longer competes with this one. Older versions keep their own connection as before.

### Fixed

- Changing the Charge Limit or Battery Reserve no longer takes control back from the inverter while the guard stays on. An automation that raises the reserve a percent at a time, as a battery planner does, used to make the integration take over for a minute after every change. Writing the value a limit already has now does nothing at all.

## [2.6.0] - 2026-09-29

### Fixed

- Grid Feed-in Mode 2, a limit by percentage of the rated power, was shown as Unlimited. It is now shown as Limited (percent).
- Discharge Battery and Export to Grid can no longer be set above the inverter's maximum DC-to-AC power (40546). Charging is not limited by the rectifier power, because solar on the DC side charges the battery alongside it.
- Three-phase Ocean 2 energy counters are no longer a thousand times too high. The Ocean 2 reports its lifetime and daily counters in Wh where the PowerOcean models use kWh, so a solar today of 11.84 kWh was read as 11840 kWh. The counters are now converted for this model; every other model is read as before.

### Added

- Grid Feed-in switch that stops the export to the grid and puts the inverter's own settings back afterwards. It remembers the configured cap (40538), never the effective one (40609), which the safety rules can derate, and it is unavailable while the inverter limits the export by percentage. A model that refuses to read 40538 has nothing to restore, so the switch stays unavailable there.
- Maximum feed-in Power (Configured) and (Effective), 40538 and 40609 read side by side, and Maximum feed-in Power (Percent), 40573. Maximum feed-in Power keeps following the effective cap, or the configured one on the PowerOcean Plus. The PowerOcean Plus gets neither the Effective nor the Percent sensor, since it reads 0 in both.
- Registers not every model has confirmed can be marked optional. A model that refuses reads over addresses it does not implement, such as the Ocean 2, reads them apart from the others and stops polling any it refuses as invalid, so a missing one cannot break the rest of the poll.
- More registers as diagnostic sensors, so they can be checked against each model:
  - Active Control Method, Modbus Control (Device) and BMS Connected, from bits 7-12 of the System Status. The raw status word is the `system_modes_hex` attribute of Active Control Method.
  - Grid-side voltage and current per phase, Inverter AC Power and Circuit Breaker Capacity. The existing Grid Voltage and Grid Current sensors are renamed Inverter Voltage and Inverter Current, since those registers measure the inverter's own phases; their entity ids are unchanged.
  - Inverter AC Input and Output energy counters.

### Changed

- Modbus Control is now a switch in the device's Configuration section instead of an option in the setup and settings dialog, so it can be turned on and off without reloading the integration. An existing setting carries over.
- The inverter model selected in the setup and settings dialog now decides how the device is read. The model the device reports only pre-fills the setup form; it no longer overrides the selection for word order and energy units.
- Serial number and firmware version are read again after every reconnect, not only at startup. A firmware update reboots the inverter, and a failed first connect used to leave them unknown until Home Assistant restarted. New Modbus Protocol Version and Modbus Device Address diagnostic sensors show what the inverter reports for them.

## [2.5.2] - 2026-09-28

### Fixed

- Three-phase Ocean 2 support. The model is now recognised from its product number, its 32-bit registers are read high word first, and its reads stop at every address it does not implement instead of being refused as a whole.
- The grid no longer powers the house while the Charge Limit is on and the battery could. When the house uses more than the solar for a while, the inverter now runs by itself until the power flow turns. Small power corrections also wait until the battery has reached the last one.

### Changed

- Control Status now shows Ramping or Unreachable when the inverter is not doing what a guard asks for, instead of always showing Charge limit reached or Reserve reached. Automations that check for those states should read the new `guard` attribute instead.
- Instead of using hardcoded defaults when setting up the integration, we query the integration and pre-fill the values for battery count, solar power etc. The user can adjust these manually during and after, but the pre-filled values should be good for the vast majority of users.
- Rework tests to use [pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component) and support all versions back to Home Assistant 2025.12.0

### Added

- Ocean 2 Single Phase as a selectable model, detected as product number 4 with the single-phase category.

## [2.5.1] - 2026-09-21

### Added

- Control Status now reports when a full battery does not need to be held.

### Fixed

- House Consumption Total retains its full precision with 2 decimals, instead of being rounded to whole kWh.
- Modbus control heartbeat now runs independently of polling and retries temporary busy responses, preventing avoidable handover back to the EcoFlow app.
- Hold Battery no longer curtails solar production when the battery is full, and selecting Automatic now clears the previous power setpoint.
- Charge Limit and Battery Reserve guards now remain stable in Automatic mode, including on systems where solar power must be calculated.
- Lots of improvement to the robustness of the modbus controls.

## [2.5.0] - 2026-09-16

### Added

- Modbus Control setting, off by default, that lets the integration command the inverter. While it is enabled the EcoFlow app cannot control the system, and turning it off hands control back after about 60 seconds.
- Battery Mode control: automatic, hold battery, charge battery, discharge battery and export to grid, each with its own power setting.
- Control Status sensor reporting whether the commanded mode is active, still ramping, or unreachable because the battery is full or empty.
- Charge Limit and Battery Reserve guards, which apply in every mode including automatic. Both are off by default.
- Battery Saver Mode switch.
- Modbus Control binary sensor showing whether the inverter is currently following the integration.
- System Power Setpoint, Inverter Power Setpoint and Battery Power Setpoint sensors.

### Changed

- Battery mode and its power sit under Controls, the two guards under Configuration.
- Moved to Diagnostic: System Modes, Battery Charge Power Limit, Battery Discharge Power Limit and Battery Net Energy. System Fault is no longer diagnostic.
- Writable controls dropped the "Control" suffix from their names.

### Fixed

- 32-bit register writes were sent low word first and silently ignored by the inverter. They are now sent high word first.

### Removed

- Device LED brightness sensor, which duplicated the LED Brightness control.
- Battery Saver Mode binary sensor. The switch now carries the inverter's own state as an attribute instead.
- Minimum SOC Limit control on PowerOcean Plus, whose firmware accepts the write and then ignores it.

## [2.4.3] - 2026-09-04

### Fixed

- Remove remaining last usage of UnitOfRatio preventing launch on pre 2026.7

## [2.4.2] - 2026-09-04

### Fixed

- Revert the usage of UnitOfRatio since that only was introduced in 2026.7
- Let the configured battery count override, and only log when they don't match
- Rework modbus disabled to use inverter_rated_power+limit_inv_max = 0 for 3 consecutive reads

## [2.4.1] - 2026-09-02

### Added

- Control LED brightness and minimum battery SOC limit

### Changed

- Fix display values for grid mode
- Rework the energy validation and the daily reset

## [2.4.0] - 2026-08-18

### Added

- Add notification if Modbus TCP is not enabled in the EcoFlow Pro app.

## [2.3.0] - 2026-08-13

### Added

- **Inverter Rated Power** sensor (register 40528)
- **Maximum Battery Charge Power** sensor (register 40556)
- **Grid Mode** sensor (Bit 0 register 40530)

### Changed

- Updated Maximum feed-in Power with separate register (40529) for PowerOcean Plus

## [2.2.0] – 2026-08-08

### Added

- Improve code quality and add tests
- Download diagnostic data

### Fixed

- Fix the phantom voltage filter for all inverter models
- Fix CI running twice on PR pushes
- Increase timeout to reduce connection errors

## [2.1.0] – 2026-08-06

### Added

- **Maximum feed-in Power** sensor (register 40609)
- **Battery Module Count** sensor (register 42081)
- **Battery 1 SOC** sensor (register 42082)
- **Battery 2 SOC** sensor (register 42083)
- **Battery 3 SOC** sensor (register 42084)
- **Self-powered Mode** binary sensor (Bit 3 register 40530)
- **Intelligent Mode** binary sensor (Bit 4 register 40530)
- **Battery Saver Mode** binary sensor (Bit 5 register 40530)
- **Device LED brightness** sensor (register 40541)
- Added new reconnect behavior

### Changed

- Switch the pymodbus client to AsyncModbusTcpClient to reduce the configurable polling interval to 2 seconds
- Detection of unauthorized spikes in the energy sensors

### Fixed

- Reconnect Bugfix

## [2.0.0] – 2026-03-31

### Added

- **House Power** sensor (register 40519) – previously incorrectly listed as cloud-only
- **Grid Power** sensor (register 40521) – previously incorrectly listed as cloud-only
- **Solar Power** sensor – calculated from active PV strings (more reliable than register 40523)
- **Per-string PV Power** sensors (W) for strings 1/2/3, calculated from current × PV voltage
- **PV Voltage Global** sensor (register 40598)
- **Serial Number** and **Operation Mode** diagnostic sensors
- **Battery Nominal Capacity** sensor
- **Min SOC Limit**, **Battery Temp Warning Max/Min** diagnostic sensors
- **Inverter power limit** sensors (nominal + current)
- **Max Battery Discharge Power** and **Max Charge Power** sensors (calculated from module count)
- **House Consumption Today/Total** energy sensors (calculated from energy balance)
- **Solar Yield Today/Total** energy sensors
- **Configurable battery capacity** – workaround for unreliable register 40528
- **Configurable PV string count** (1–3) – unused strings are ignored
- **Phantom current filter** – string currents below 0.05 A are treated as 0
- **Configurable poll interval** (5–60 seconds) via UI
- **Options Flow** – all settings editable after setup via Configure button
- **Debug logging** toggle in HA UI via `manifest.json` loggers field
- **German and English translations** for all config/options flow fields
- **Heartbeat check** at the start of each poll cycle – detects inverter unavailability immediately
- **Automatic reconnect** after inverter restart or network interruption – stale TCP connections are detected and cleanly closed, with reconnect on the next poll

### Changed

- Switched from individual register reads to **block reads** (5 requests per poll cycle instead of ~25)
- Inverter Temperature register corrected to 40592 (was incorrectly mapped to 40600)
- `inverter_ac_power` (40530) now read as direct INT16 Watts (division by 100 removed)
- Power limit register offsets corrected (40546/40548/40550/40552)
- Registers 40550/40552 replaced by calculated values (were unreliable)
- `const.py` cleaned up – individual REG\_\* constants removed, block addressing used in coordinator
- `sensor.py` uses `UnitOfApparentPower.VOLT_AMPERE` instead of hardcoded `"VA"`

### Fixed

- Grid power and solar power returning 0 due to incorrect register mapping
- Battery remaining energy returning double the correct value (wrong scale factor)
- Phantom voltage on unconfigured PV string 3

### Removed

- Unused `ConfigEntryNotReady` import from `__init__.py`
- Unused REG\_\* constants from `const.py`
- `pv1_today` / `pv2_today` individual string energy sensors (not available via Modbus)

---

## [1.0.5] – 2026-03-23

- Previous release (see GitHub releases for details)
