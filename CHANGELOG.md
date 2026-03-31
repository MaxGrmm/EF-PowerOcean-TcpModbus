# Changelog

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
- `const.py` cleaned up – individual REG_* constants removed, block addressing used in coordinator
- `sensor.py` uses `UnitOfApparentPower.VOLT_AMPERE` instead of hardcoded `"VA"`

### Fixed
- Grid power and solar power returning 0 due to incorrect register mapping
- Battery remaining energy returning double the correct value (wrong scale factor)
- Phantom voltage on unconfigured PV string 3

### Removed
- Unused `ConfigEntryNotReady` import from `__init__.py`
- Unused REG_* constants from `const.py`
- `pv1_today` / `pv2_today` individual string energy sensors (not available via Modbus)

---

## [1.0.5] – 2026-03-23

- Previous release (see GitHub releases for details)
