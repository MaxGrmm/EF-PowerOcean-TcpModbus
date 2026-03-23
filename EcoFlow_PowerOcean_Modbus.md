# EcoFlow PowerOcean Plus – Modbus TCP Interface

> Community-discovered Modbus TCP register map and Python monitor for the EcoFlow PowerOcean Plus home battery system.

---

## Table of Contents

- [Overview](#overview)
- [Requirements](#requirements)
- [Connection](#connection)
- [Register Map](#register-map)
  - [Device Info](#device-info)
  - [Status & Battery SOC](#status--battery-soc)
  - [Live Measurements](#live-measurements)
  - [Energy Counters – Today](#energy-counters--today)
  - [Energy Counters – Lifetime](#energy-counters--lifetime)
  - [Configuration Registers](#configuration-registers)
- [Data Types & Decoding](#data-types--decoding)
- [Python Monitor Script](#python-monitor-script)
- [Known Limitations](#known-limitations)
- [Tested Hardware](#tested-hardware)
- [Contributing](#contributing)
- [Disclaimer](#disclaimer)
- [License](#license)

---

## Overview

EcoFlow does **not publish an official Modbus register map** for the PowerOcean Plus.  
This documentation was created by scanning the device and cross-referencing values with the official EcoFlow Home Assistant integration (Cloud API).

All registers were discovered empirically on firmware version unknown, hardware serial `R371ZDH4ZGAW0028`.

---

## Requirements

```
Python >= 3.8
pymodbus == 3.6.9
```

Install:

```bash
pip install pymodbus==3.6.9
```

> **Note:** pymodbus 3.12.x has a breaking API change. Version 3.6.9 is recommended for stability.

---

## Connection

| Parameter | Value |
|-----------|-------|
| Protocol  | Modbus TCP |
| Default Port | 502 |
| Default Slave ID | 1 (device responds to all IDs 1–250) |
| Data Encoding | Big-endian IEEE 754 float, **word-swapped** |
| Register Numbering | 1-based (Modbus standard) |

```python
from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient("192.168.x.x", port=502, timeout=3)
client.connect()
```

---

## Register Map

### Device Info

| Register | Type | Value | Description |
|----------|------|-------|-------------|
| 40001 | UINT16 | 1 | Device type identifier |
| 40002 | UINT16 | 1 | Unknown |
| 40003 | UINT16 | 3 | Unknown |
| 40004–40011 | ASCII (8×UINT16) | e.g. `R371ZDH4ZGAW0028` | Serial number (2 chars per register, high byte first) |

**Reading the serial number:**

```python
r = client.read_holding_registers(40004, count=8, slave=1)
chars = []
for val in r.registers:
    chars.append(chr((val >> 8) & 0xFF))
    chars.append(chr(val & 0xFF))
serial = ''.join(c for c in chars if 32 <= ord(c) <= 126)
# → 'R371ZDH4ZGAW0028'
```

---

### Status & Battery SOC

| Register | Type | Unit | Scale | Description |
|----------|------|------|-------|-------------|
| 42081 | UINT16 | — | — | System status: `1` = Online, `0` = Offline |
| 42082 | UINT16 | % | ×1 | Battery State of Charge (SOC) |

---

### Live Measurements

All values in this section are **32-bit IEEE 754 floats** stored in **2 consecutive registers**, **word-swapped** (low word first, high word second).

#### Decoding example

```python
import struct

def read_float(client, addr, scale=1):
    r = client.read_holding_registers(addr, count=2, slave=1)
    if r.isError():
        return None
    # Word-swapped: registers[1] is high word, registers[0] is low word
    raw = struct.pack('>HH', r.registers[1], r.registers[0])
    return round(struct.unpack('>f', raw)[0] * scale, 3)
```

#### Solar

| Register | Unit | Scale | Description |
|----------|------|-------|-------------|
| 40574/40575 | W | ×100 | Total PV power |
| 40602/40603 | A | ×1 | PV string 1 current |
| 40604/40605 | A | ×1 | PV string 2 current |
| 40606/40607 | A | ×1 | PV string 3 current |

#### Battery

| Register | Unit | Scale | Description |
|----------|------|-------|-------------|
| 40576/40577 | W | ×1000 | Battery power (positive = charging, negative = discharging) |
| 40578/40579 | °C | ×1 | Battery ambient temperature |

#### AC Grid

| Register | Unit | Scale | Description |
|----------|------|-------|-------------|
| 40580/40581 | V | ×1 | Grid voltage phase L1 |
| 40582/40583 | V | ×1 | Grid voltage phase L2 |
| 40584/40585 | V | ×1 | Grid voltage phase L3 |
| 40586/40587 | A | ×1 | Grid current phase L1 |
| 40588/40589 | A | ×1 | Grid current phase L2 |
| 40590/40591 | A | ×1 | Grid current phase L3 |
| 40592/40593 | Hz | ×1 | Grid frequency (measurement 1) |
| 40594/40595 | Hz | ×1 | Grid frequency (measurement 2) |
| 40596/40597 | W | ×10 | Active power / grid feed-in power (positive = feed-in, negative = consumption) |
| 40598/40599 | VA | ×10 | Apparent power |

#### Inverter

| Register | Unit | Scale | Description |
|----------|------|-------|-------------|
| 40600/40601 | °C | ×1 | Inverter temperature |

---

### Energy Counters – Today

Values reset at midnight.

| Register | Unit | Scale | Description |
|----------|------|-------|-------------|
| 42163/42164 | kWh | ×1 | Grid import today |
| 42179/42180 | kWh | ×1 | Grid export today |
| 42195/42196 | kWh | ×1 | PV string 1 yield today |
| 42211/42212 | kWh | ×1 | PV string 2 yield today |
| 42243/42244 | kWh | ×1 | Battery charged today |
| 42145/42146 | kWh | ×1 | Battery discharged today |

---

### Energy Counters – Lifetime

| Register | Unit | Scale | Description |
|----------|------|-------|-------------|
| 42113/42114 | kWh | ×1 | Battery net energy (charged minus discharged) |
| 42161/42162 | Wh | ×1 | Grid import lifetime |
| 42177/42178 | Wh | ×1 | Grid export lifetime |
| 42193/42194 | kWh | ×1 | PV string 1 total yield |
| 42209/42210 | kWh | ×1 | PV string 2 total yield |
| 42225/42226 | kWh | ×1 | Battery total charged (lifetime) |
| 42227/42228 | kWh | ×1 | Battery remaining energy (current) |
| 42241/42242 | kWh | ×1 | Battery total discharged (lifetime) |
| 42257/42258 | kWh | ×1 | Total system energy |

---

### Configuration Registers

These registers appear to hold device configuration. Their exact meaning is not fully confirmed.  
**Do not write to these registers unless you know what you are doing.**

| Register | Value (observed) | Notes |
|----------|-----------------|-------|
| 40527 | 100 | Unknown – possibly max SOC limit (%) |
| 40528 | 15000 | Unknown – possibly power limit (W×10 = 1500 W?) |
| 40536 | 11 | Unknown |
| 40537 | 1 | Unknown |
| 40538 | 15000 | Unknown |
| 40540 | 32 | Unknown |
| 40541 | 20 | Unknown – possibly min cell temperature |
| 40546 | 15000 | Unknown |
| 40548 | 15000 | Unknown |
| 40552 | 5000 | Unknown |
| 40615 | 10000 | Unknown |
| 40616 | 10000 | Unknown |
| 40617 | 6000 | Unknown |
| 40618 | 6000 | Unknown |
| 40625 | 800 | Unknown |
| 40626 | 800 | Unknown |
| 40627 | 10000 | Unknown |
| 40628 | 10000 | Unknown |

---

## Data Types & Decoding

### UINT16 (single register)

```python
def read_uint16(client, addr):
    r = client.read_holding_registers(addr, count=1, slave=1)
    return r.registers[0] if not r.isError() else None
```

### IEEE 754 Float – word-swapped (2 registers)

The PowerOcean Plus stores all floating point values as **32-bit IEEE 754**, but with the **two 16-bit words in reverse order** (little-endian word order, big-endian byte order within each word).

```python
import struct

def read_float(client, addr, scale=1):
    r = client.read_holding_registers(addr, count=2, slave=1)
    if r.isError():
        return None
    # registers[0] = LOW word, registers[1] = HIGH word
    raw = struct.pack('>HH', r.registers[1], r.registers[0])
    value = struct.unpack('>f', raw)[0]
    return round(value * scale, 3)
```

### ASCII String (multiple registers)

```python
def read_ascii(client, addr, count):
    r = client.read_holding_registers(addr, count=count, slave=1)
    if r.isError():
        return None
    chars = []
    for val in r.registers:
        hi = (val >> 8) & 0xFF
        lo = val & 0xFF
        if 32 <= hi <= 126:
            chars.append(chr(hi))
        if 32 <= lo <= 126:
            chars.append(chr(lo))
    return ''.join(chars).strip()
```

---

## Python Monitor Script

Full live monitor with 1-second refresh:

```python
from pymodbus.client import ModbusTcpClient
import struct, time, os

TARGET_IP = "192.168.x.x"  # <-- set your device IP here

client = ModbusTcpClient(TARGET_IP, port=502, timeout=3)
client.connect()

def reg(addr):
    r = client.read_holding_registers(addr, count=1, slave=1)
    return r.registers[0] if not r.isError() else None

def fl(addr, scale=1):
    r = client.read_holding_registers(addr, count=2, slave=1)
    if r.isError():
        return None
    raw = struct.pack('>HH', r.registers[1], r.registers[0])
    return round(struct.unpack('>f', raw)[0] * scale, 2)

while True:
    os.system('cls' if os.name == 'nt' else 'clear')

    status     = reg(42081)
    soc        = reg(42082)

    u_l1       = fl(40580)
    u_l2       = fl(40582)
    u_l3       = fl(40584)
    i_l1       = fl(40586)
    i_l2       = fl(40588)
    i_l3       = fl(40590)
    freq       = fl(40592)
    p_watt     = fl(40596, scale=10)
    s_va       = fl(40598, scale=10)

    pv_pwr     = fl(40574, scale=100)
    pv1_amp    = fl(40602)
    pv2_amp    = fl(40604)
    pv3_amp    = fl(40606)

    bat_pwr    = fl(40576, scale=1000)
    bat_temp   = fl(40578)
    bat_remain = fl(42227)
    inv_temp   = fl(40600)

    e_pv1_h    = fl(42195)
    e_pv2_h    = fl(42211)
    e_export_h = fl(42179)
    e_import_h = fl(42163)
    e_bat_ch_h = fl(42243)
    e_bat_ds_h = fl(42145)

    e_pv1_ges  = fl(42193)
    e_pv2_ges  = fl(42209)
    e_bat_ch_g = fl(42225)
    e_bat_ds_g = fl(42241)
    e_bat_net  = fl(42113)
    e_total    = fl(42257)

    netz_pfeil = "-> Feed-in" if p_watt and p_watt > 0 else "<- Consumption"
    bat_pfeil  = "Charging" if bat_pwr and bat_pwr > 50 else (
                 "Discharging" if bat_pwr and bat_pwr < -50 else "Standby")
    soc_bar    = ('|' * (soc // 10)).ljust(10)
    pv_total_h = round((e_pv1_h or 0) + (e_pv2_h or 0), 2)

    print("=" * 50)
    print("  EcoFlow PowerOcean Plus – Live Monitor")
    print("=" * 50)
    print(f"  Status:    {'Online' if status == 1 else 'Offline'}")
    print(f"  Battery:   [{soc_bar}] {soc}% SOC")
    print(f"  Remaining: {bat_remain} kWh")
    print("-" * 50)
    print("  SOLAR")
    print(f"    PV Total:    {pv_pwr:>8.0f} W")
    print(f"    String 1:    {pv1_amp:>8.3f} A")
    print(f"    String 2:    {pv2_amp:>8.3f} A")
    print(f"    String 3:    {pv3_amp:>8.3f} A")
    print("-" * 50)
    print("  BATTERY")
    print(f"    Power:       {bat_pwr:>8.0f} W  ({bat_pfeil})")
    print(f"    Temperature: {bat_temp:>7.1f} C")
    print("-" * 50)
    print("  GRID            VOLTAGE     CURRENT")
    print(f"    L1:           {u_l1:>7.2f} V   {i_l1:>6.3f} A")
    print(f"    L2:           {u_l2:>7.2f} V   {i_l2:>6.3f} A")
    print(f"    L3:           {u_l3:>7.2f} V   {i_l3:>6.3f} A")
    print(f"    Frequency:    {freq:>7.3f} Hz")
    print("-" * 50)
    print("  POWER")
    print(f"    Active:      {p_watt:>8.0f} W  ({netz_pfeil})")
    print(f"    Apparent:    {s_va:>8.0f} VA")
    print("-" * 50)
    print("  TEMPERATURE")
    print(f"    Battery:     {bat_temp:>7.1f} C")
    print(f"    Inverter:    {inv_temp:>7.1f} C")
    print("-" * 50)
    print("  ENERGY TODAY")
    print(f"    PV Total:    {pv_total_h:>7.3f} kWh")
    print(f"    PV String 1: {e_pv1_h:>7.3f} kWh")
    print(f"    PV String 2: {e_pv2_h:>7.3f} kWh")
    print(f"    Grid Export: {e_export_h:>7.3f} kWh")
    print(f"    Grid Import: {e_import_h:>7.3f} kWh")
    print(f"    Bat. Charge: {e_bat_ch_h:>7.3f} kWh")
    print(f"    Bat. Disch.: {e_bat_ds_h:>7.3f} kWh")
    print("-" * 50)
    print("  ENERGY LIFETIME")
    print(f"    PV String 1: {e_pv1_ges:>9.2f} kWh")
    print(f"    PV String 2: {e_pv2_ges:>9.2f} kWh")
    print(f"    Bat. Charged:{e_bat_ch_g:>9.2f} kWh")
    print(f"    Bat. Disch.: {e_bat_ds_g:>9.2f} kWh")
    print(f"    Bat. Net:    {e_bat_net:>9.2f} kWh")
    print(f"    Total:       {e_total:>9.2f} kWh")
    print("=" * 50)
    print("  Press CTRL+C to quit   Refresh: 1s")
    print("=" * 50)

    time.sleep(1)
```

---

## Known Limitations

The following values are **not available via Modbus TCP** and can only be retrieved through the EcoFlow Cloud API:

| Parameter | Cloud API Sensor | Notes |
|-----------|-----------------|-------|
| State of Health | `bpSoh` | e.g. 98% |
| Battery voltage | `bpVol` | e.g. 54 V |
| Max cell temperature | `bpMaxCellTemp` | e.g. 22°C |
| Min cell temperature | `bpMinCellTemp` | e.g. 20°C |
| Battery current (A) | `bpAmp` | e.g. -0.05 A |
| Accumulated charge | `bpAccuChgEnergy` | lifetime Wh |
| Accumulated discharge | `bpAccuDsgEnergy` | lifetime Wh |
| Individual MPPT voltages | `mpptPv1_vol` | e.g. 423 V |
| Individual MPPT power | `mpptPv1_pwr` | e.g. 690 W |
| Per-phase reactive power | `pcsAPhase_reactPwr` | VAr |

> Scanned address range: 40001–44096 and 42081–43500. No additional registers were found beyond those documented above.

---

## Tested Hardware

| Device | Firmware | Result |
|--------|----------|--------|
| EcoFlow PowerOcean Plus | Unknown | Full register map confirmed |

If you have tested on other EcoFlow devices (Power Ocean DC, Power Ocean Connect 5kWh etc.), please open an issue or PR with your findings.

---

## Contributing

Contributions are very welcome! Especially:

- Testing on other firmware versions
- Testing on related devices (PowerOcean DC, PowerOcean Connect)
- Identifying the remaining unknown configuration registers (40527–40628)
- Home Assistant custom integration based on this register map
- Node-RED / ioBroker / openHAB integration examples

Please open an issue before submitting large PRs.

---

## Disclaimer

This register map was discovered through community reverse engineering.  
**EcoFlow does not officially support or document this Modbus interface.**

- Use at your own risk
- Do not write to any registers unless you fully understand the consequences
- This documentation may become inaccurate after firmware updates
- The authors are not responsible for any damage to your system

---

## License

MIT License – free to use, modify, and distribute with attribution.

---

*Discovered and documented by community reverse engineering. Not affiliated with EcoFlow Technology Co., Ltd.*
