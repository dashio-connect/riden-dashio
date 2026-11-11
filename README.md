# riden-dashio

A RIDEN RD60xx bench power supply (RD6006, RD6006P, RD6012, RD6018, RD6024) on USB, as a [DashIO](https://pypi.org/project/dashio/) device for the Dash app.

The app shows:

- the output voltage, current and power, on dials
- the voltage and current limit setpoints, which you can type in
- an output on/off button
- CV/CC mode, OVP/OCP trips, the input voltage, the internal and probe temperatures, and Ah/Wh
- the PSU's presets M1–M9, to recall one, and the active OVP and OCP
- a graph of voltage and current, kept in a local sqlite database
- an event log, and alarms: `PROT` when OVP or OCP trips or a charge stops on a fault, and `CHARGED` when a charge finishes. Alarms reach your phone through the Dash server

## Li-ion charge mode

Through its main output the RD60xx is a plain supply: in CV it holds the voltage for ever and never stops. Set the app's **Mode** to **Li-ion charge** and the program runs the charge:

1. It switches the output off and reads the battery's voltage. It won't start under 2.5 V (no battery), over 4.25 V (not a single cell), or at the target already.
2. It sets the charge current and the target voltage, and turns the output on: constant current, then constant voltage.
3. In CV, once the current stays under the cutoff for 30 seconds, it switches the output off, and logs and alarms "Charged" with the Ah and Wh delivered.

While charging, the PSU's own OVP and OCP are set to 0.10 V over the target and 1.2× the charge current (at least 0.1 A more), so the PSU protects the battery even if the program stops, the computer sleeps or the USB cable comes out. The limits from before are put back when the charge ends.

It stops early, with the output off and an alarm, on OVP/OCP, at the time limit, or when the external probe reaches its limit. A front panel change (output off, or a different voltage) ends the charge mode and leaves the PSU as you set it. A charge in progress carries on after the program restarts.

It's for one Li-ion cell, or cells in parallel (1S): the target is limited to 3.0–4.20 V. The defaults follow the ini's `capacity_ah` (6.8 Ah: 4.20 V, 3.4 A (0.5C), done under 0.34 A (C/20), 5 hours), and can be changed in the app's **Charge settings** menu. While charging, the voltage and current limit boxes are locked; the output button stops the charge.

## Presets

The PSU's memories M1–M9 each hold a voltage, current limit, OVP and OCP. The **Preset** selector lists them, and shows the one that matches the PSU's settings, or Custom. Picking one sets all four. The protection limits are widened first and narrowed last, so the change doesn't trip OVP or OCP part way through. Presets can't be recalled while charging. They're still edited on the PSU's front panel.

M0 holds the active OVP and OCP, shown beside the selector.

## How it talks to the PSU

The PSU's USB port is a CH340 serial converter (USB ID `1a86:7523`). It speaks **Modbus RTU** at 115200 baud 8N1, slave address 1. Function 03 reads the holding registers, and function 06 writes a single register. `riden_dashio/rd60xx.py` is a small driver for it, using only pyserial.

| Register | |
|---|---|
| 0 | Model ID (60062 is the RD6006) |
| 1–2, 3 | Serial number, firmware |
| 4–5, 34–35 | Internal and probe temperatures in °C (sign, value) |
| 8, 9 | Voltage and current setpoints (write) |
| 10, 11, 12–13 | Output voltage, current, power (0.01 W) |
| 14 | Input voltage (0.01 V) |
| 16, 17, 18 | Protection (1 OVP, 2 OCP), CV/CC, output on (write) |
| 38–39, 40–41 | Ah, Wh (0.001) |
| 48–53 | The PSU's clock |
| 80 + 4n | Preset Mn: V, I, OVP, OCP |

The RD6006 counts 0.01 V and 0.001 A. The RD6006P counts 0.001 V and 0.0001 A.

## Running it

```sh
pip install -e .
riden_dashio            # Finds the PSU on the first CH340 port
riden_dashio --read-only
```

The first run writes `riden.ini`. Edit it to:

- set `port`, if it finds the wrong serial port
- set `max_voltage` and `max_current`, to limit what the app may set, below the model's own limits
- set `read_only = yes`
- set `graph_voltage_max` and `graph_current_max`, the tops of the graph's axes, e.g. 5 V and 4 A for charging a cell. Left empty, they're the model's full range
- add a DashIO `username` and `password`, to reach the device through the Dash server as well as over TCP on the local network. Or set them from the app: connect over TCP, open the device's provisioning menu, and enter the Dash login there. The program saves it here

The ini also keeps the layout's `cfg_rev` and hash. cfgRev goes up by itself when the layout changes.

## Tests

```sh
python -m unittest discover -s tests -t .
```

The tests use a simulated PSU (`tests/fake_psu.py`), with registers recorded from a real RD6006. They never open a serial port.

## Licence

MIT. See [LICENSE](LICENSE).
