# Battery Control

How the guards, the control status and the battery actions work. Turning on Modbus
Control and choosing a Battery Mode are described in the
[README](../README.md#battery-control).

<!-- toc -->

## Contents

- [Guards](#guards)
- [Control Status](#control-status)
- [Commanding the battery from automations](#commanding-the-battery-from-automations)
- [Testing the controls after a firmware update](#testing-the-controls-after-a-firmware-update)

<!-- tocstop -->

## Guards

| Guard           | Stops the battery from       | Off at |
| --------------- | ---------------------------- | ------ |
| Charge Limit    | charging above this level    | 100 %  |
| Battery Reserve | discharging below this level | 0 %    |

The guards apply in every mode and only ever stop the battery, never reverse it. In
Automatic, the battery can still power the house under the Charge Limit, and still
charge from solar under the Battery Reserve. A guard releases once the charge has moved
5 % back, and stays on across a restart until then. Until it does, the battery can
still use that band: charge it took above the reserve can be discharged again down to
the reserve, and a battery a little below the Charge Limit can refill up to it. Once
stopped at a limit, it has to move 2 % away first, so a state of charge flickering by a
percent does not charge and discharge it in turn. Both are off by default.

The Battery Reserve is **native** or **emulated**, shown in its `implementation`
attribute:

- **Native** (PowerOcean Three Phase): it is the backup reserve in the EcoFlow app,
  written to the inverter, which keeps it whatever runs it, even with Home Assistant
  down. A change in the app shows here too, and it can be set with Modbus Control off.
  The integration still holds the battery at it under a manual command, which outranks
  the inverter's own reserve.
- **Emulated** (every other model): the integration keeps it while Modbus Control is
  on. The PowerOcean Plus takes a write to its reserve register but keeps acting on the
  app's value, and the other models are unproven. The app's own reserve still applies
  whenever the integration is not in control, and shows as _App Backup Reserve_. Once
  the battery is at the app's reserve, the inverter stops discharging there by itself,
  so in Automatic the integration leaves it to the inverter as with a native reserve.
  That is whenever the Battery Reserve is at or below the app's.

_Effective Battery Reserve_ shows which of these holds right now. Its `source` is `app`
while the inverter keeps its own reserve, and `battery_reserve` while the integration
keeps the Battery Reserve, which under a manual command can be below the app's.

The integration options are set to the model's; change them only to test a model, and
`scripts/control_feature_scan.py --reserve-probe` shows whether one acts on a written
reserve.

With an emulated reserve, turn on **Charge to Battery Reserve** to also charge from the
grid up to it, as the app's backup reserve does. Below the reserve the battery then
charges at the Charge Battery power, whatever mode is selected, and the reserve holds it
from there. The Charge Limit still applies, and a Charge Battery power of 0 W leaves the
reserve a floor only. A native reserve does this in the inverter, so it has no such
switch.

While the battery is below the backup reserve set in the EcoFlow app, the inverter
charges up to that reserve by itself, so the switch shows on and cannot be turned off
until the battery gets there; its `set_by_app_backup_reserve` attribute says so. In
Automatic, an app reserve at or above the Battery Reserve is left to the inverter to
charge to.

<details>
<summary>How the guards work</summary>

The inverter has no charge limit setting, so while a guard is on, the integration runs
self-consumption itself and blocks the forbidden direction. When the battery only needs
to move the allowed way, it hands control back to the inverter, which reacts faster, and
takes over again when the power flow turns. Single-phase models hand back immediately.
Loads that cycle on and off, like an oven, make it wait longer each time.

The guards are soft limits: the battery may move the wrong way for a few seconds before
it is caught. While held, it drifts a few hundred watts as clouds pass, because EcoFlow
doesn't expose a hard power limit over Modbus. See
[Battery power limits](ecoflow-modbus-protocol-implementation.md#battery-power-limits).

</details>

## Control Status

| Control Status              | Meaning                                                          |
| --------------------------- | ---------------------------------------------------------------- |
| No Modbus control           | Modbus Control is off, or control was lost                       |
| Handing back to the app     | Modbus Control was turned off; the app takes over                |
| Automatic                   | The inverter runs normal self-consumption                        |
| Active                      | The selected mode is working                                     |
| Ramping                     | The inverter hasn't reached the target yet                       |
| Limited by inverter         | The inverter goes the other way or past the target               |
| Charge limit reached        | The Charge Limit is stopping the battery                         |
| Reserve reached             | The Battery Reserve is stopping the battery                      |
| Charging to battery reserve | Charge to Battery Reserve is charging up to it                   |
| Below solar export limit    | Export solar first is exporting the whole surplus                |
| Unreachable: battery full   | The target needs the battery to charge, but it's full            |
| Unreachable: battery empty  | The target needs the battery to discharge, but it's empty        |
| Unreachable: export limit   | The target needs more export than the export limit lets out      |
| Not accepted by inverter    | The inverter doesn't report the command, even after resending it |
| Off-grid                    | Grid outage; control steps aside until the grid is back          |
| Battery disconnected        | The battery dropped off; control steps aside until it's back     |
| Readings inconsistent       | The readings contradict each other; control steps aside          |
| Inverter fault              | The inverter reports a fault or a stop                           |
| Control test                | The control test has the inverter; the mode resumes after it     |

If the inverter misses a guard's target, the status shows Ramping or Unreachable; the
guard is still in the `guard` attribute.

**Limited by inverter** means something in the inverter outranks the command, such as a
firmware limit or its own protection: asked to charge, the battery discharges, or it
discharges more than asked. A command is given 30 seconds to turn the battery around
before it counts.

The inverter reports which control method it follows. Once it has shown that it does,
a mismatch for three polls sends the command again, which recovers from an inverter
restart or Modbus mode being toggled in the installer app. After two resends the status
shows **Not accepted by inverter**. A model that doesn't report these is controlled as
before.

Every poll is also checked for conditions in which the inverter is best left to
itself:

| Check               | Fails when                                                                                | Control Status        |
| ------------------- | ----------------------------------------------------------------------------------------- | --------------------- |
| `off_grid`          | The inverter reports running off-grid                                                     | Off-grid              |
| `grid_voltage`      | The grid-side voltage is gone while the inverter still reports on-grid                    | Off-grid              |
| `bms`               | The inverter reports its battery disconnected, after it reported one                      | Battery disconnected  |
| `telemetry_stalled` | `frozen` and `counter_stalled` fail; without counters, most readings still for 30 minutes | Readings inconsistent |
| `battery_soc`       | The system Battery SOC drops to 0 % from a level no battery empties from                  | Readings inconsistent |
| `frozen`            | Most voltages and the frequency, an AC voltage among them, haven't changed for 5 minutes  | Readings inconsistent |
| `counter_stalled`   | No solar, grid import or battery discharge counter moved for 20 minutes                   | None on its own       |

A check counts once it fails three polls in a row. While one does, control hands the
inverter its own self-consumption and resumes the selected mode once it passes again.
A frame whose readings contradict each other never moves the Charge Limit or Battery
Reserve, so one bad read can't set them off.

The checks run whether or not Modbus Control is on, since a bad reading also misleads
the sensors. A reading a check shows to be wrong, and anything calculated from it,
shows unavailable until it makes sense again: the system Battery SOC for `battery_soc`,
each voltage or frequency that stopped changing for `frozen`, and every measured reading
that stopped changing for `telemetry_stalled`. Energy counters stay shown as the
inverter counts them. `counter_stalled` only counts as evidence for
`telemetry_stalled`, since some models may update a counter late. A house draws about
100 W or more at any hour, from the sun, the grid or the battery, and the counters step
in 0.01 kWh, so one of them moves every few minutes; 20 minutes is about three missed
steps. How long each reading has stood still survives a reload. After a restart longer
than a minute, the readings count from the restart, so the downtime does not count.

The `ef_powerocean_tcpmodbus_reading_check_failed` and
`ef_powerocean_tcpmodbus_reading_check_cleared` events, also shown in the logbook,
carry the `check` and the `status` it stands for; use them to be notified. Each check
is announced on its own, so a stall that follows frozen voltages is announced too, and
each is logged once when it fails and once when it passes again.

The Reading Check diagnostic sensor is on while a check shows readings to be wrong,
also while Modbus Control is off, with the `check`, `since` when and the `distrusted`
readings as attributes. Off-grid and a disconnected battery are the inverter's state,
so they don't turn it on.

Only `telemetry_stalled` is raised in repairs, once it has lasted 30 minutes, since
restarting the inverter is the fix. The other checks reflect the inverter's state, such
as an outage or the battery saver turning the battery off, not something to fix.

While `battery_soc` fails, the **Battery SOC** sensor shows unavailable rather than
0 %, so history, automations and the Energy dashboard don't take it for an empty
battery. Each time a check starts failing, the readings of that poll and the one before
are kept, also across restarts, and included in the diagnostics download. If a check
fails unexpectedly, download diagnostics from the device page whenever you notice and
attach them to a bug report.

## Commanding the battery from automations

The **Set battery command** action sets the Battery Mode, its power and the Charge
Limit in one step:

```yaml
action: ef_powerocean_tcpmodbus.set_battery_command
data:
  device_id: <your inverter's device id>
  mode: charge_battery
  power: 3000
  charge_limit_soc: 80
  expire_in: 900
```

- `power` is required for Charge, Discharge, Export and Import, and optional for Export
  solar first, where it sets the Solar Export Limit. It must be above 0 W and is capped
  at the inverter's maximum.
- `charge_limit_soc` is optional and applied together with the mode.
- With `expire_in` (60 to 86400 s), the mode returns to Automatic unless the command is
  sent again in time. Control Status shows the deadline as `expires_at`, and the
  `ef_powerocean_tcpmodbus_command_expired` event fires when it passes. Choosing a mode
  by hand cancels it.
- Only `mode: automatic` works while Modbus Control is off.

Run one controller at a time; the action doesn't arbitrate between them.

## Testing the controls after a firmware update

EcoFlow firmware updates can change which control methods the inverter follows. The
**Run control test** action checks each method in both directions and writes a report
you can attach to an issue, so a change in behaviour shows up before it surprises an
automation.

1. Run the action, with **Confirm** on:

   ```yaml
   action: ef_powerocean_tcpmodbus.run_control_test
   data:
     device_id: <your inverter's device id>
     confirm: true
     power: 1500
   ```

2. Follow the **Control Test** sensor (diagnostic), or wait for the notification. It
   takes about 10 minutes. The
   battery charges and discharges briefly at the test power and power flows to and
   from the grid. At the end the inverter is handed back to the EcoFlow app for a
   minute, which the report times, and then to Modbus Control if it was on.
3. When it shows **Done**, open a
   [Control test report](https://github.com/MaxGrmm/EF-PowerOcean-TcpModbus/issues/new?template=control_test_report.yml) issue and
   attach the report: either **Download diagnostics** on the device page, or the file
   named in the sensor's `report_file` attribute, under
   `config/ef_powerocean_tcpmodbus/`.

Modbus Control can stay on: a selected battery mode pauses for the test, Control
Status shows **Control test**, and the mode is sent again when the test ends. The
test refuses to start only while another Modbus controller holds the inverter;
nothing is written in that case. While it runs, battery modes, switching Modbus
Control on and Battery Saver are refused.

The action returns as soon as the test has started, so the page can be closed; a
notification says when the report is ready. **Cancel control test** stops a run and
hands control back.

A run is most useful with the battery well above its reserve and below 94 %: a test
the battery cannot take part in is reported as skipped rather than failed. The checks
that need the EcoFlow app open, or that change device settings, are left to
[scripts/control_feature_scan.py](../CONTRIBUTING.md#control-test).
