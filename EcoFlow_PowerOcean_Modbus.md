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
- [Battery power limits](#battery-power-limits)
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

| Parameter      | Value                                                     |
| -------------- | --------------------------------------------------------- |
| Protocol       | Modbus TCP                                                |
| Port           | 502                                                       |
| Unit / slave   | 1 (the device answers on effectively any unit ID)         |
| Functions      | `0x03` read holding registers, `0x06` / `0x10` for writes |
| Addressing     | Direct 4xxxx addresses                                    |
| Address window | 40001–44096, and nothing outside it                       |

The window was measured by probing 0–60000 and bisecting the edges. `0x04` read
input registers answers over exactly the same range, and `0x01` / `0x02` answer
nothing at all, so there is no second address space to search: anything reachable
is reachable through `0x03`.

Best practice is to configure a static IP in your router's admin interface. Otherwise there is a risk that the integration stops working if the inverter gets a new IP.

## Data types and word order

Modbus registers are 16 bits, so anything wider spans consecutive registers.
**Reads and writes disagree about word order**, and the vendor doc contradicts
itself on the point. What the hardware actually does, measured on a PowerOcean
Plus:

| Direction      | Word order          |
| -------------- | ------------------- |
| Read (`0x03`)  | low word first      |
| Write (`0x10`) | **high word first** |

For a 32-bit value read from address _N_:

| Word           | Address | Contents  |
| -------------- | ------- | --------- |
| `registers[0]` | _N_     | low half  |
| `registers[1]` | _N_ + 1 | high half |

A `battery_capacity` of 100000 Wh (`0x000186A0`) therefore arrives as
`registers[0] = 0x86A0` and `registers[1] = 0x0001`.

A write of the same value must send `0x0001` first and `0x86A0` second. Sending it
in read order is not rejected: the device stores the words, interprets them high
word first, and acts on a value 65536 times too large. This is what makes a 32-bit
write look like it succeeded while nothing happens — see
[Writing registers](#writing-registers).

The proof is the device's own read-back. Write `500` to the battery power setpoint
low word first and a few seconds later the register reads `32768000` (= 500 << 16),
because the device re-publishes its internal value in read order. Write it high
word first and the register reads `500`, and the inverter follows the setpoint.

Four layouts occur, modelled as `RegisterType` in `models.py`:

| `RegisterType` | Words | Notes                                                        |
| -------------- | ----- | ------------------------------------------------------------ |
| `UINT16`       | 1     | Percentages, counts, fault codes, enums                      |
| `UINT32`       | 2     | Capacities and power limits. `(high << 16) \| low`           |
| `FLOAT32`      | 2     | IEEE 754, word-swapped. Live power, voltage, current, energy |
| `SERIAL`       | 8     | 16 ASCII bytes, high byte first within each word             |

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
default. Some of them are **signed** 32-bit values, which `RegisterType.INT32` and
`decode_register` both handle.

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
`RegisterDef`, so read and write addresses cannot diverge. 16-bit registers are
written with function code `0x06`, 32-bit ones with `0x10` and the high word first
(`encode_register`).

Read-back proves only that the words arrived, never that the device acted on them.
Immediately after a 32-bit write the register still holds the words as sent; the
firmware swaps them into read order a few seconds later. Both forms count as
confirmation of delivery, and nothing more.

Two registers cannot be verified by reading at all on a PowerOcean Plus:

- `40534` System Control Command is write-only by design and accepts any value,
  including bit patterns the doc reserves. It is not validated.
- `40532` System State 2 reads zero even while `40530` reports the system running,
  although its low seven bits are documented to mirror `40530`. The register is not
  implemented, so the `control_mode` sensor derived from it always says "default".
  Do not use it to decide whether a command was accepted.

The only trustworthy confirmation is behavioural: grid, battery or solar power
moving, the LED changing, or the EcoFlow Pro app reporting that Modbus has control.

Writing to a live inverter can interfere with its internal scheduling. Treat every
writable register as potentially disruptive, and change them one at a time.

## Battery power limits

Unfortunately, we have not found a stable way to set the battery power limits similar
to what the EcoFlow app schedules do. `battery_charge_power_limit` (40556) and
`battery_discharge_power_limit` (40554) are readings, not controls. Every write
to 40556 is refused with `exception code 2` in both word orders, with and without
Modbus control authority.

`battery_charge_power_limit` reports two different quantities depending on what the app is doing:

| App charge schedule | What 40556 holds                           |
| ------------------- | ------------------------------------------ |
| Off                 | BMS instantaneous capability, drifting     |
| Set                 | The configured cap, exactly and unchanging |

There is thus seemingly no modbus register exposed that can control the power the same way
that the app does. Due to this, the integration replicates this behaviour using the `battery_power_setpoint` (40571) and a live monitoring of the situation to balance. It's not perfect,
but it works.

## Disclaimer

Parts of this map are community reverse engineering. Firmware updates may invalidate
any of it.

- Use at your own risk.
- Do not write registers unless you understand the consequences.
- The authors are not responsible for damage to your system.

Not affiliated with EcoFlow Technology Co., Ltd.
