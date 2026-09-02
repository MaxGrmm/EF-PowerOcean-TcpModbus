# EcoFlow PowerOcean – Modbus TCP Protocol Notes

Background on how the PowerOcean speaks Modbus TCP, and on the reasoning behind
this integration's register handling.

**This file deliberately does not list registers.** The register map is code:

> [`custom_components/ef_powerocean_tcpmodbus/const.py`](custom_components/ef_powerocean_tcpmodbus/const.py) → `MODBUS_REGISTERS`

## Contents

- [Sources](#sources)
- [Enabling Modbus](#enabling-modbus)
- [Connection](#connection)
- [Data types and word order](#data-types-and-word-order)
- [How the integration reads](#how-the-integration-reads)
- [Not yet mapped](#not-yet-mapped)
- [Reverse-engineered regions](#reverse-engineered-regions)
- [Values only available in the Cloud API](#values-only-available-in-the-cloud-api)
- [Writing registers](#writing-registers)
- [Disclaimer](#disclaimer)

## Sources

1. **Cross-checks against the EcoFlow web portal.** Live values read over Modbus,
   compared against what the portal reported for the same system at the same
   moment. This is what settles any disagreement about what a register holds.
2. **Community reverse engineering.** The original version of this document was
   produced by scanning 40001–44096 on a PowerOcean Plus and matching values
   against the Cloud API. Several regions the integration still depends on come
   from that work.

## Enabling Modbus

Modbus stays off until the inverter is switched into Modbus control mode:

1. Open the **EcoFlow Pro** app.
2. Select the inverter and change its control mode to **Modbus control**.

## Connection

| Parameter | Value |
| | |
| Protocol | Modbus TCP |
| Port | 502 |
| Unit / slave | 1 (the device answers on effectively any unit ID) |
| Functions | `0x03` read holding registers, `0x06` / `0x10` for writes |
| Addressing | Direct 4xxxx addresses |

Best practice is to configure a static IP in your router's admin interface. Otherwise there is a risk that the integration stops working if the inverter gets a new IP.

## Data types and word order

Modbus registers are 16 bits, so anything wider spans consecutive registers.
Multi-word values are stored low word first.

For a 32-bit value at address _N_:

| Word | Address | Contents |
| -- | - | |
| `registers[0]` | _N_ | low half |
| `registers[1]` | _N_ + 1 | high half |

A `battery_capacity` of 100000 Wh (`0x000186A0`) therefore arrives as
`registers[0] = 0x86A0` and `registers[1] = 0x0001`.

Four layouts occur, modelled as `RegisterType` in `const.py`:

| `RegisterType` | Words | Notes |
| -- | -- | |
| `UINT16` | 1 | Percentages, counts, fault codes, enums |
| `UINT32` | 2 | Capacities and power limits. `(high << 16) \| low` |
| `FLOAT32` | 2 | IEEE 754, word-swapped. Live power, voltage, current, energy |
| `SERIAL` | 8 | 16 ASCII bytes, high byte first within each word |

## How the integration reads

Registers are declared individually with absolute addresses and a register type that determines the size.
The read plan is automatically optimized into read blocks.

A new block starts when the gap to the next register exceeds `MAX_REGISTER_GAP`
(48 words) — reading a few dozen unused registers is cheaper than a second round
trip — or when a block would exceed `MAX_REGISTERS_PER_READ` (125, the ceiling for
a single Modbus response).

The main blocks are the following:

| Block       | Start | Words | Contents                               |
| ----------- | ----- | ----- | -------------------------------------- |
| Device info | 40002 | 12    | Product type, serial number, firmware  |
| Live        | 40519 | 89    | Power, limits, voltages, currents, PV  |
| Faults      | 42049 | 45    | Fault count and codes, per-battery SOC |
| Energy      | 42161 | 100   | Lifetime and daily energy counters     |

## Not yet mapped

The configuration region below 40574 holds more registers than the integration
reads. Several are settings rather than measurements, so they are left alone by
default.

Some of them are **signed** 32-bit values. `RegisterType` has no `INT32` member and
`decode_register` has no signed path, so adding both is a prerequisite for reading
them correctly.

## Reverse-engineered regions

Two regions in active use come from community scanning rather than any published
specification. They are confirmed against the portal, but they are the parts most
likely to shift with a firmware update:

- **40574–40607**, battery voltage, current and temperature; per-phase voltage and
  current; inverter temperature; grid frequency; PV string voltages and currents.
- **42161–42260**, lifetime and daily energy counters for grid import/export,
  battery charge/discharge and solar.

## Values only available in the Cloud API

The Cloud API returns far more than Modbus exposes. To see it, log in to
https://user-portal.ecoflow.com and find the `detail?<serialnumber>` request in your
browser's network tab. The extra data falls roughly into these groups:

| Group                     | What it adds                                                            |
| ------------------------- | ----------------------------------------------------------------------- |
| Battery internals         | Cell voltages, per-pack temperatures, state of health, cycle counts     |
| Inverter internals        | DC bus, leakage current, relay states, MPPT temperatures and insulation |
| Arc-fault detection       | AFCI enable state, self-test results, per-channel faults                |
| Per-phase power           | Active, reactive and apparent; Modbus has only voltage and current      |
| Grid safety settings      | Around 180 protection thresholds, ride-through and derating curves      |
| Scheduling and dispatch   | Time-of-use, peak shaving, VPP and AI mode configuration                |
| Detailed error codes      | Per-module error lists; Modbus gives a count plus 20 codes              |
| Monthly and yearly energy | Modbus carries daily and lifetime totals only                           |
| Connectivity and site     | Wi-Fi, Ethernet and 4G status, system name, location, timezone          |

## Writing registers

Writable registers are declared in `WRITABLE_NUMBERS_MAP`. Each entry names the
register it reads from, and the write address is resolved from that same
`RegisterDef`, so read and write addresses cannot diverge. Writes use function code
`0x06` and are read back to confirm the device accepted the value.

Writing to a live inverter can interfere with its internal scheduling. Treat every
writable register as potentially disruptive, and change them one at a time.

## Disclaimer

Parts of this map are community reverse engineering. Firmware updates may invalidate
any of it.

- Use at your own risk.
- Do not write registers unless you understand the consequences.
- The authors are not responsible for damage to your system.

Not affiliated with EcoFlow Technology Co., Ltd.
