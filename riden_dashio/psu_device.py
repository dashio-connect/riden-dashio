"""The RD60xx as a DashIO device: its controls, and the loop that polls the PSU."""
from __future__ import annotations

import dataclasses
import logging
import time

import dashio

from .charger import MAX_CELL_VOLTAGE, MIN_CELL_VOLTAGE, Charger, ChargeError, ChargeSettings
from .rd60xx import NUM_PRESETS, Preset, RD60xx, Status

log = logging.getLogger(__name__)

MODES = ["Supply", "Li-ion charge"]
PROVISIONING_MENU = "PROV"  # The parent ID that puts a Menu in the app's provisioning menu
CUSTOM = "Custom"  # The preset selector's first choice: the setpoints don't match a preset
NOTIFY_COLORS = {"info": dashio.Color.WHITE, "good": dashio.Color.GREEN, "warn": dashio.Color.ORANGE, "bad": dashio.Color.RED, "done": dashio.Color.GREEN}


def _pos(x, y, w, h):
    return dashio.ControlPosition(x, y, w, h)


class PsuDevice:
    """Shows an RD60xx in the Dash app, and lets the app set the voltage, current limit and output, or charge a Li-ion battery.

    `max_voltage` and `max_current` cap what the app may set, below the model's own limits.
    `read_only` makes the app a monitor: the setpoints, output button and charge mode can't be changed.
    `save_charge_settings(ChargeSettings)` and `save_charge_state(dict or None)` keep the charge settings, and an active charge,
    across restarts; `charge_state` is the saved active charge, resumed at the first poll.
    `graph_voltage_max` and `graph_current_max` set the graph's axes; by default, the model's full range.
    """

    def __init__(self, psu: RD60xx, device: dashio.Device, max_voltage: float | None = None, max_current: float | None = None,
                 read_only: bool = False, graph_interval: float = 10.0, charge_settings: ChargeSettings | None = None,
                 save_charge_settings=None, save_charge_state=None, charge_state: dict | None = None,
                 graph_voltage_max: float | None = None, graph_current_max: float | None = None):
        self.psu = psu
        self.device = device
        model = psu.model
        self.max_voltage = min(max_voltage or model.max_voltage, model.max_voltage)
        self.max_current = min(max_current or model.max_current, model.max_current)
        self.read_only = read_only
        self.graph_interval = graph_interval
        self.graph_voltage_max = graph_voltage_max or model.max_voltage
        self.graph_current_max = graph_current_max or model.max_current
        self._save_charge_settings = save_charge_settings or (lambda settings: None)
        self._charge_state = charge_state
        self.charger = Charger(psu, charge_settings or ChargeSettings.for_capacity(6.8, self.max_current), self._charge_event,
                               save=save_charge_state)
        self._sent = {}  # The last value sent for each control, so unchanged values aren't sent every poll
        self._samples = []
        self._last_graph = time.monotonic()
        self._last_status: Status | None = None
        self.status: Status | None = None
        self.presets: list[Preset] = []
        self._make_controls()

    def _make_controls(self):
        m = self.psu.model
        self.view = dashio.DeviceView("psu", f"{m.name} PSU", icon=dashio.Icon.LIGHTNING, num_grid_rows=32)
        self.graph_view = dashio.DeviceView("graph", f"{m.name} Graph", num_grid_rows=32)
        self.v_out = dashio.Dial("VOUT", "Voltage", dial_max=m.max_voltage, red_value=self.max_voltage, units="V",
                                 precision=dashio.Precision.FOUR, number_position=dashio.DialNumberPosition.CENTER,
                                 control_position=_pos(0.0, 0.0, 0.333, 0.2))
        self.i_out = dashio.Dial("IOUT", "Current", dial_max=m.max_current, red_value=self.max_current, units="A",
                                 precision=dashio.Precision.FOUR, number_position=dashio.DialNumberPosition.CENTER,
                                 control_position=_pos(0.333, 0.0, 0.333, 0.2))
        self.p_out = dashio.Dial("POUT", "Power", dial_max=m.max_voltage * m.max_current, red_value=m.max_voltage * m.max_current * 0.8,
                                 units="W", precision=dashio.Precision.FOUR, number_position=dashio.DialNumberPosition.CENTER,
                                 control_position=_pos(0.666, 0.0, 0.334, 0.2))
        keyboard = dashio.Keyboard.NONE if self.read_only else dashio.Keyboard.NUM
        self.v_set = dashio.TextBox("VSET", "Set voltage", text_align=dashio.TextAlignment.CENTER, units="V",
                                    keyboard_type=keyboard, control_position=_pos(0.0, 0.21, 0.5, 0.1))
        self.i_set = dashio.TextBox("ISET", "Current limit", text_align=dashio.TextAlignment.CENTER, units="A",
                                    keyboard_type=keyboard, control_position=_pos(0.5, 0.21, 0.5, 0.1))
        self.output = dashio.Button("OUT", "Output", button_enabled=not self.read_only, icon_name=dashio.Icon.ON_OFF,
                                    on_color=dashio.Color.GREEN, off_color=dashio.Color.RED,
                                    control_position=_pos(0.0, 0.32, 0.333, 0.1))
        self.mode = dashio.TextBox("MODE", "Mode", text_align=dashio.TextAlignment.CENTER, keyboard_type=dashio.Keyboard.NONE,
                                   control_position=_pos(0.333, 0.32, 0.333, 0.1))
        self.v_in = dashio.TextBox("VIN", "Input", text_align=dashio.TextAlignment.CENTER, units="V", keyboard_type=dashio.Keyboard.NONE,
                                   control_position=_pos(0.666, 0.32, 0.334, 0.1))
        self.temps = dashio.TextBox("TEMP", "Temp int / ext", text_align=dashio.TextAlignment.CENTER, keyboard_type=dashio.Keyboard.NONE,
                                    control_position=_pos(0.0, 0.43, 0.5, 0.1))
        self.energy = dashio.TextBox("ENERGY", "Energy", text_align=dashio.TextAlignment.CENTER, keyboard_type=dashio.Keyboard.NONE,
                                     control_position=_pos(0.5, 0.43, 0.5, 0.1))

        # Li-ion charging: the mode, its status, and its settings in a menu
        self.charge_mode = dashio.Selector("CHMODE", "Mode", selection=MODES, control_position=_pos(0.0, 0.54, 0.4, 0.1))
        self.charge_status = dashio.TextBox("CHST", "Charge", text_align=dashio.TextAlignment.CENTER, keyboard_type=dashio.Keyboard.NONE,
                                            control_position=_pos(0.4, 0.54, 0.6, 0.1))
        # The charge settings go in the app's provisioning menu, with the device name and Dash login.
        # A Menu there still needs a position, though it isn't shown on a view.
        self.charge_menu = dashio.Menu("CHSET", "Charge settings", text="Charge settings", icon_name=dashio.Icon.COG,
                                       control_position=_pos(0.0, 0.0, 0.2, 0.1))
        self.charge_menu.parent_id = PROVISIONING_MENU
        # The PSU's presets M1 to M9, and the active OVP and OCP (M0's). The choices are filled in from the PSU
        # as it runs, rather than given here, so editing a preset on the PSU doesn't change the layout.
        self.preset = dashio.Selector("PRESET", "Preset", control_position=_pos(0.0, 0.65, 0.5, 0.1))
        self.limits = dashio.TextBox("LIMITS", "Protection", text_align=dashio.TextAlignment.CENTER, keyboard_type=dashio.Keyboard.NONE,
                                     control_position=_pos(0.5, 0.65, 0.5, 0.1))
        self.ch_voltage = dashio.TextBox("CHV", "Charge to (V)", units="V", keyboard_type=keyboard)
        self.ch_current = dashio.TextBox("CHI", "Charge current (A)", units="A", keyboard_type=keyboard)
        self.ch_cutoff = dashio.TextBox("CHCUT", "Done under (A)", units="A", keyboard_type=keyboard)
        self.ch_hours = dashio.TextBox("CHHRS", "Time limit (hours)", units="h", keyboard_type=keyboard)
        self.ch_temp = dashio.TextBox("CHTMP", "Probe limit (°C, 0 off)", units="°C", keyboard_type=keyboard)
        self.charge_controls = [self.ch_voltage, self.ch_current, self.ch_cutoff, self.ch_hours, self.ch_temp]

        self.graph = dashio.TimeGraph("TG", "Output", y_axis_label="V", y_axis_max=self.graph_voltage_max,
                                      y_axis_label_rt="A", y_axis_max_rt=self.graph_current_max, control_position=_pos(0.0, 0.0, 1.0, 1.0))
        self.graph.add_line("V", dashio.TimeGraphLine("Voltage", color=dashio.Color.YELLOW, max_data_points=360))
        self.graph.add_line("I", dashio.TimeGraphLine("Current", color=dashio.Color.AQUA, max_data_points=360, right_axis=True))
        self.event_log = dashio.EventLog("LOG", "Events", control_position=_pos(0.0, 0.76, 1.0, 0.24))
        self.alarm = dashio.Alarm("PROT")  # OVP/OCP, and charges stopped by a fault
        self.charged_alarm = dashio.Alarm("CHARGED")  # A charge finished

        on_view = [self.v_out, self.i_out, self.p_out, self.v_set, self.i_set, self.output, self.mode, self.v_in,
                   self.temps, self.energy, self.charge_mode, self.charge_status, self.preset, self.limits,
                   self.event_log]
        self.device.add_control(self.view)
        for control in on_view:
            self.device.add_control(control)
            self.view.add_control(control)
        self.device.add_control(self.charge_menu)
        self.device.add_control(self.graph_view)
        self.device.add_control(self.graph)
        self.graph_view.add_control(self.graph)
        for control in self.charge_controls:
            self.device.add_control(control)
            self.charge_menu.add_control(control)
        self.device.add_control(self.alarm)
        self.device.add_control(self.charged_alarm)
        self._show_charge_settings()

        if not self.read_only:
            self.v_set.add_receive_message_callback(self._v_set_rx)
            self.i_set.add_receive_message_callback(self._i_set_rx)
            self.output.add_receive_message_callback(self._output_rx)
            self.charge_mode.add_receive_message_callback(self._charge_mode_rx)
            self.preset.add_receive_message_callback(self._preset_rx)
            for control in self.charge_controls:
                control.add_receive_message_callback(self._charge_setting_rx)

    # From the app

    def _setpoint_rx(self, msg, name, units, limit, write, field):
        try:
            if self.charger.active:
                raise ValueError("charging; change the charge settings instead")
            value = float(msg[3])
            if not 0 <= value <= limit:
                raise ValueError(f"must be 0 to {limit:g} {units}")
            write(value)
            self.log_event(f"{name} set to {value:g} {units}")
        except (ValueError, IndexError, IOError) as e:
            self.log_event(f"{name} not set: {e}", dashio.Color.ORANGE)
        self._sent.pop(field, None)  # Send the PSU's setpoint back, changed or not, so the app shows what took
        self.poll()

    def _v_set_rx(self, msg):
        self._setpoint_rx(msg, "Voltage", "V", self.max_voltage, self.psu.set_voltage, "VSET")

    def _i_set_rx(self, msg):
        self._setpoint_rx(msg, "Current limit", "A", self.max_current, self.psu.set_current, "ISET")

    def _output_rx(self, msg):
        try:
            if self.charger.active:
                self.charger.stop("Charge stopped from the app")
            else:
                self.psu.set_output(not (self.status and self.status.output))
        except IOError as e:
            self.log_event(f"Output not switched: {e}", dashio.Color.ORANGE)
        self._sent.pop("OUT", None)
        self.poll()

    def _charge_mode_rx(self, msg):
        try:
            charge = int(msg[3]) == 1
        except (ValueError, IndexError):
            return
        if charge and not self.charger.active:
            try:
                self.charger.start()
            except (ChargeError, ValueError, IOError) as e:
                self.log_event(f"Charge not started: {e}", dashio.Color.ORANGE)
                self.charger.message = f"Not started: {e}"
        elif not charge:
            self.charger.stop("Charge stopped from the app")
        self._sent.pop("CHMODE", None)
        self.poll()

    def _charge_setting_rx(self, msg):
        s = self.charger.settings
        field = msg[2]
        try:
            text = msg[3].strip()
            value = float(text) if text else 0.0
            if field == "CHV":
                if not MIN_CELL_VOLTAGE <= value <= MAX_CELL_VOLTAGE:
                    raise ValueError(f"must be {MIN_CELL_VOLTAGE:g} to {MAX_CELL_VOLTAGE:.2f} V for one Li-ion cell")
                s.voltage = value
                self.charger.apply_settings()
            elif field == "CHI":
                if not s.cutoff < value <= self.max_current:
                    raise ValueError(f"must be over the cutoff ({s.cutoff:g} A) and at most {self.max_current:g} A")
                s.current = value
                self.charger.apply_settings()
            elif field == "CHCUT":
                if not 0.01 <= value < s.current:
                    raise ValueError(f"must be 0.01 A to under the charge current ({s.current:g} A)")
                s.cutoff = value
            elif field == "CHHRS":
                if not 0.5 <= value <= 48:
                    raise ValueError("must be 0.5 to 48 hours")
                s.max_hours = value
            elif field == "CHTMP":
                if value and not 20 <= value <= 80:
                    raise ValueError("must be 20 to 80 °C, or 0 for off")
                s.max_probe_temp = value or None
            self._save_charge_settings(dataclasses.replace(s))
            self.log_event(f"Charge setting {field} set to {text or 'off'}")
        except (ValueError, IndexError, IOError) as e:
            self.log_event(f"Charge setting not changed: {e}", dashio.Color.ORANGE)
        self._show_charge_settings()

    def _preset_rx(self, msg):
        try:
            number = int(msg[3])
        except (ValueError, IndexError):
            return
        if number > 0:  # 0 is "Custom": nothing to recall
            try:
                if self.charger.active:
                    raise ValueError("charging; stop the charge first")
                self.psu.recall_preset(number)
                p = self.presets[number] if number < len(self.presets) else None
                self.log_event(f"Preset M{number} recalled" + (f": {self._preset_text(p)}" if p else ""))
            except (ValueError, IOError) as e:
                self.log_event(f"Preset M{number} not recalled: {e}", dashio.Color.ORANGE)
        self._sent.pop("PRESET", None)
        self.poll()

    # To the app

    def log_event(self, text, color=dashio.Color.WHITE):
        log.info(text)
        self.event_log.add_event_data(dashio.EventData(text, color))

    def _charge_event(self, text, level):
        self.log_event(text, NOTIFY_COLORS[level])
        if level == "bad":
            self.alarm.send(f"{self.psu.model.name} charge stopped", text)
        elif level == "done":
            self.charged_alarm.send("Battery charged", text)

    def _show_charge_settings(self):
        s = self.charger.settings
        self.ch_voltage.text = f"{s.voltage:.2f}"
        self.ch_current.text = f"{s.current:g}"
        self.ch_cutoff.text = f"{s.cutoff:g}"
        self.ch_hours.text = f"{s.max_hours:g}"
        self.ch_temp.text = f"{s.max_probe_temp:g}" if s.max_probe_temp else "0"

    def _send(self, key, value, setter):
        if self._sent.get(key) != value:
            self._sent[key] = value
            setter(value)

    def poll(self):
        """Reads the PSU, runs the charger, and sends what changed. Call it every second or so."""
        try:
            s = self.psu.status()
        except IOError as e:
            log.warning("Can't read the PSU: %s", e)
            for dial in (self.v_out, self.i_out, self.p_out):
                self._send(dial.control_id, None, lambda v, d=dial: setattr(d, "dial_value", v))
            return
        self.status = s
        if self._charge_state is not None:
            self.charger.resume(self._charge_state, s)
            self._charge_state = None
        was_charging = self.charger.active
        self.charger.update(s)
        if self.charger.active != was_charging:
            s = self.status = self.psu.status()  # The charger switched the output off

        vfmt = "%.3f" if self.psu.model.volt_scale >= 1000 else "%.2f"
        ifmt = "%.4f" if self.psu.model.amp_scale >= 10000 else "%.3f"
        self._send("VOUT", s.v_out, lambda v: setattr(self.v_out, "dial_value", v))
        self._send("IOUT", s.i_out, lambda v: setattr(self.i_out, "dial_value", v))
        self._send("POUT", s.p_out, lambda v: setattr(self.p_out, "dial_value", v))
        self._send("VSET", vfmt % s.v_set, lambda v: setattr(self.v_set, "text", v))
        self._send("ISET", ifmt % s.i_set, lambda v: setattr(self.i_set, "text", v))
        self._send("OUT", s.output, self._send_output)
        self._send("MODE", (s.mode, s.protection), self._send_mode)
        self._send("VIN", "%.2f" % s.v_in, lambda v: setattr(self.v_in, "text", v))
        self._send("TEMP", f"{s.int_temp} / {s.ext_temp} °C", lambda v: setattr(self.temps, "text", v))
        self._send("ENERGY", f"{s.amp_hours:.3f} Ah  {s.watt_hours:.3f} Wh", lambda v: setattr(self.energy, "text", v))
        self._show_presets(s)
        self._send("CHMODE", int(self.charger.active), lambda v: setattr(self.charge_mode, "position", v))
        self._send("CHST", self.charger.summary(s), lambda v: setattr(self.charge_status, "text", v))
        self._check_changes(s, charging=was_charging or self.charger.active)
        self._last_status = s
        self._graph(s)

    def _preset_text(self, p: Preset) -> str:
        return f"{p.voltage:g} V {p.current:g} A"

    def _show_presets(self, s: Status):
        try:
            self.presets = self.psu.presets()
        except IOError as e:
            log.warning("Can't read the presets: %s", e)
            return
        active = self.presets[0]
        choices = [CUSTOM] + [f"M{n} {self._preset_text(self.presets[n])}" for n in range(1, NUM_PRESETS)]
        position = 0
        for n in range(1, NUM_PRESETS):
            p = self.presets[n]
            if (p.voltage, p.current, p.ovp, p.ocp) == (s.v_set, s.i_set, active.ovp, active.ocp):
                position = n
                break
        self._send("PRESET", (tuple(choices), position), self._send_preset)
        self._send("LIMITS", f"OVP {active.ovp:.2f} V  OCP {active.ocp:.3f} A", lambda v: setattr(self.limits, "text", v))

    def _send_preset(self, choices_position):
        choices, position = choices_position
        self.preset.selection_list = list(choices)  # A new list: the layout keeps the empty one it was made with
        self.preset.send_selection(position)

    def _send_output(self, on):
        self.output.send_button(dashio.ButtonState.ON if on else dashio.ButtonState.OFF, dashio.Icon.ON_OFF, "ON" if on else "OFF")

    def _send_mode(self, mode_protection):
        mode, protection = mode_protection
        # The colour first: a TextBox sends its colour with the next text, not when the colour is set.
        self.mode.color = dashio.Color.RED if protection else (dashio.Color.ORANGE if mode == "CC" else dashio.Color.GREEN)
        self.mode.text = protection or mode

    def _check_changes(self, s: Status, charging: bool):
        """Logs what changed on the PSU. While charging, the charger reports these itself."""
        last = self._last_status
        if last is None or charging:
            return
        if s.output != last.output:
            self.log_event(f"Output {'on' if s.output else 'off'}", dashio.Color.GREEN if s.output else dashio.Color.RED)
        if s.protection and s.protection != last.protection:
            text = f"{s.protection} tripped at {s.v_out:g} V, {s.i_out:g} A"
            self.log_event(text, dashio.Color.RED)
            self.alarm.send(f"{self.psu.model.name} {s.protection}", text)
        if s.mode != last.mode and s.output:
            self.log_event(f"{'Constant current' if s.mode == 'CC' else 'Constant voltage'}", dashio.Color.ORANGE if s.mode == "CC" else dashio.Color.GREEN)

    def _graph(self, s: Status):
        self._samples.append((s.v_out, s.i_out))
        now = time.monotonic()
        if now - self._last_graph >= self.graph_interval:
            n = len(self._samples)
            self.graph.line_dict["V"].add_data_point(round(sum(v for v, _ in self._samples) / n, 3))
            self.graph.line_dict["I"].add_data_point(round(sum(i for _, i in self._samples) / n, 4))
            self._samples.clear()
            self._last_graph = now
