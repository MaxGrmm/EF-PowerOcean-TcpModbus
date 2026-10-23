# Changelog

## Unreleased

## [2.9.3] - 2026-10-23

### Added

- Active Fault Codes names each code it knows in a `faults` attribute, in English or German. The names come from the EcoFlow portal; a code without one is listed with `null`.
- Grid Connection Code diagnostic sensor: the grid standard the inverter was commissioned with, as EcoFlow numbers it.
- The diagnostics download has a `raw_registers` section: every word the last poll read, undecoded and without the serial number, which the test suite's fake inverter can serve again.

### Changed

- Active Fault Codes shows the codes in decimal, as the EcoFlow app and portal do, instead of hexadecimal. Automations matching a code such as `0x0209` need `521` instead.
- An optional register is only read together with required ones when that read already covers it, and one that makes polls fail three times in a row is no longer read, so an unconfirmed register cannot take the poll down.

### Fixed

- Support for the new 8 kWh battery modules, alone or mixed with the original ones.

## [2.9.2] - 2026-10-21

### Fixed

- An emulated Battery Reserve no longer takes control once the battery is at the backup reserve set in the EcoFlow app, which the inverter keeps by itself. On the PowerOcean Plus holding it there made the battery charge a little and discharge again in a loop. Automatic and Export solar first now leave it to the inverter, as with a native reserve; a manual command is still held at the reserve, and an app reserve below the Battery Reserve is still guarded.
- Below the backup reserve set in the EcoFlow app, the battery now charges up to it, as the inverter does by itself. An emulated Battery Reserve above the app's used to hold the battery below both all night. Charge to Battery Reserve shows on meanwhile and cannot be turned off, and in Automatic an app reserve at or above the Battery Reserve is left to the inverter to charge to.
- A Charge Limit below the Battery Reserve keeps the battery frozen again when the app's backup reserve is at or above the Battery Reserve.
- The Discharge Battery, Export to Grid and Solar Export Limit numbers no longer change their maximum with the inverter's capacity reading, which moves from poll to poll. Home Assistant warned that they updated their capabilities too often, and a value set while the reading was low was cut to it for good. They now go up to the inverter's rating, and the capacity only caps the power sent.
- A battery a few percent from a guard's limit can use those percent again. Charge taken just above the Battery Reserve was held until the battery reached 5 % above it, and a battery just below the Charge Limit was not refilled. It now moves back to the limit once it has moved 2 % away from it.
- A Charge Limit or Battery Reserve that is on stays on across a restart. Restarting while the battery was within 5% of the limit used to turn the guard off, so the battery charged or discharged where it would otherwise have been held.
- Control Status shows Export limit reached, target unreachable when a command such as Discharge battery would export more than the export limit lets out, instead of Ramping for as long as it runs.
- Switching the Battery Reserve from emulated to native removes the Charge to Battery Reserve switch instead of leaving it unavailable.

## [2.9.1] - 2026-10-17

### Added

- System Alerts sensor naming the active System State 2 bits as documented in EcoFlow's Open Modbus Protocol, or `none`.
- Nothing is read from or written to the inverter, the heartbeat included, until it shows the entry's serial number. A different serial raises a repairs issue.
- Diagnostic sensors with the device's own lifetime energy totals, such as Solar Yield Total (Device), next to the checked totals. They have no state class, so they never feed the Energy dashboard.
- A repairs issue when an energy total has been held for more than 24 hours while the inverter reports another value, offering to take the inverter's value, or to report it with a filled-in bug report and the diagnostics.
- The diagnostics download has an `energy` section: every counter as published and as the device reports it, when each total was last accepted, which totals are held, the daily snapshots and the maximum powers the totals are checked against.

### Changed

- Modbus requests time out after 3 s instead of 20 s, and a whole poll after 12 s.
- Processing errors make the entities unavailable instead of publishing an empty update.
- CI type-checks the integration with mypy in strict mode, lints with a wider set of ruff rules, and pins every GitHub Action to a commit.

### Fixed

- A reconnect no longer holds the Modbus connection during its backoff, which stalled writes and unloading for up to two minutes.
- Self-use Mode and Intelligent Mode no longer both turn on for an undocumented operating status.
- Sensors shown without decimals round their value instead of cutting it off, so 1499.7 W shows as 1500 W.
- A lifetime energy total whose first read is 0 no longer holds the real value back for days. The 0 is shown but not trusted, and the first real value is taken once the next read follows it as a counter would (#169).
- An energy total held at 0 by an earlier version while the inverter reports its real total is corrected once the inverter's total rises in step with its daily counter (#169).
- An energy total restored from a state file written by 2.4.0 is no longer frozen for good when its first poll after loading is held, for example because that poll came back without the energy registers (#169).

## [2.9.0] - 2026-10-10

### Added

- Control sends the command again when the inverter doesn't report following it, as after an inverter restart, and shows Not accepted by inverter if that doesn't help.
- Control steps aside while off-grid or with the battery disconnected, and Control Status shows Off-grid, Battery disconnected or Inverter fault.
- Control Status shows Limited by inverter when the battery goes the other way from the command, or past it, instead of Ramping indefinitely.
- Run control test action: checks which control methods the inverter's firmware follows and writes a report to attach to an issue, followed by the new Control Test sensor. The action returns as soon as the test has started, and a notification says when the report is ready. It can start with Modbus Control on: the selected battery mode pauses, Control Status shows Control test, and the mode resumes afterwards. Cancel control test stops it. The last report is included in the diagnostics.
- `scripts/compare_reports.py` lists what changed between two control test reports, such as before and after a firmware update, and `scripts/control_feature_scan.py --json` writes the same report.
- Charge to Battery Reserve: below the Battery Reserve, the battery charges from the grid up to it at the Charge Battery power, as the app's backup reserve does.
- Each poll reads only the registers an enabled entity, the battery control or the energy counters need, so a disabled sensor no longer costs a read. On the Ocean 2, which reads registers one at a time, this takes them off the poll.
- Registers the inverter refused are tried again once its firmware version changes, rather than only after a restart of Home Assistant or a reload, and the device page shows the new version straight away.
- The inverter's serial number is the config entry's unique id. An entry keyed by host and port takes the serial number the first time it reads it, and adding an inverter that has moved on the network updates its entry's address instead of making a second one.

### Changed

- "Modbus is disabled" is a repairs issue under Settings → Repairs rather than a persistent notification.
- The inverter is a device in its own right rather than a service.

- The Battery Reserve is the one reserve control: native on the PowerOcean Three Phase, where it writes the inverter's own reserve, and emulated by the integration on the other models. Its `implementation` attribute says which, and the integration options can switch it to test a model.
- The Minimum SOC Limit number is removed. The inverter's reserve shows as the App Backup Reserve sensor on models with an emulated reserve.

### Fixed

- The battery command actions no longer log a deprecation warning on Home Assistant 2026.10 for looking up the device's config entry.

## [2.8.2] - 2026-10-07

### Changed

- Import from Grid is capped by the configured maximum grid power instead of the inverter's rating, since the house counts towards it.
- Maximum grid power is no longer pre-filled from the inverter's rating during setup, as it is the grid connection's limit.

## Fixed

- integration shows up properly in the new Modbus menu introduced in home assistant 2026.10.

## [2.8.1] - 2026-10-05

### Changed

- Added the Ocean 2 Plus Single Phase
- Export solar first under the Battery Reserve can charge from a surplus while it waits to switch, as Automatic does, instead of always holding.
- Export solar first keeps its wait between switches through a full battery or a reached Charge Limit.

### Fixed

- Export solar first keeps exporting while it waits to switch back to Export to grid, instead of charging the battery under Automatic, unless the surplus is more than the grid can take.
- Export solar first no longer charges the battery past the Charge Limit when solar hovers around the house load.
- Export to grid and Import from grid respect the Charge Limit and Battery Reserve whichever way they move the battery, and are stopped from the first watt rather than after 200 W.
- Charge battery and Discharge battery at 0 W hold the battery instead of running as Automatic past the guards.
- HACS now installs from the release zip asset, so GitHub tracks download counts. The zip contains the integration files at its root, as HACS expects.

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
