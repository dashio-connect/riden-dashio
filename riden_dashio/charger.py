"""Li-ion charging with an RD60xx: constant current, then constant voltage, then off when the current tapers below the cutoff.

The RD60xx's main output is a plain supply: in CV it holds the voltage for ever and never stops by itself.
The Charger watches it and turns the output off when the charge is done or something is wrong.
While charging, the PSU's own OVP and OCP are set just over the target voltage and charge current, so the PSU
protects the battery even if this program stops. The limits from before are put back when the charge ends.
It's for one cell, or cells in parallel (1S): the target is at most 4.20 V.
"""
from __future__ import annotations

import dataclasses
import enum
import time

from .rd60xx import RD60xx, Status

MAX_CELL_VOLTAGE = 4.20
MIN_CELL_VOLTAGE = 3.0
DONE_SECONDS = 30  # The current must stay under the cutoff this long, so a dip doesn't end the charge
NO_CHARGE_AH = 0.02  # Less than this delivered when the current tapers off: the battery was full, or isn't connected
SAVE_SECONDS = 60
OVP_MARGIN = 0.10  # V over the target
OCP_MARGIN = 0.2  # Times the charge current, and at least 0.1 A


class ChargeError(Exception):
    pass


class State(enum.Enum):
    IDLE = "Idle"
    CC = "CC"
    CV = "CV"
    DONE = "Done"
    STOPPED = "Stopped"


@dataclasses.dataclass
class ChargeSettings:
    capacity_ah: float = 6.8
    voltage: float = 4.20
    current: float = 3.4  # 0.5C
    cutoff: float = 0.34  # C/20
    max_hours: float = 5.0
    max_probe_temp: float | None = None  # °C at the PSU's external probe; None: not checked
    min_start_voltage: float = 2.5

    @classmethod
    def for_capacity(cls, capacity_ah: float, max_current: float) -> "ChargeSettings":
        current = min(round(capacity_ah / 2, 2), max_current)
        return cls(capacity_ah=capacity_ah, current=current, cutoff=round(capacity_ah / 20, 3),
                   max_hours=round(capacity_ah / current * 1.5 + 2, 1))


def duration_text(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60}h{minutes % 60:02d}"


class Charger:
    """Call update() with each Status the device reads. notify(text, level) reports events; level is
    "info", "good", "warn", "bad" (stopped for a fault), or "done". save(dict or None) keeps an active charge across restarts."""

    def __init__(self, psu: RD60xx, settings: ChargeSettings, notify, save=None, clock=time.time, sleep=time.sleep):
        self.psu = psu
        self.settings = settings
        self._notify = notify
        self._save = save or (lambda state: None)
        self._clock = clock
        self._sleep = sleep
        self.state = State.IDLE
        self.message = ""
        self.amp_hours = 0.0
        self.watt_hours = 0.0
        self.started = 0.0
        self._last = 0.0
        self._last_save = 0.0
        self._below_since = None
        self._restore_protection: tuple[float, float] | None = None  # The OVP and OCP from before the charge

    @property
    def active(self) -> bool:
        return self.state in (State.CC, State.CV)

    def start(self):
        """Checks the battery with the output off, then sets the PSU and turns the output on. Raises ChargeError."""
        if self.active:
            return
        s = self.settings
        status = self.psu.status()
        if status.output:
            self.psu.set_output(False)
            self._sleep(1.5)
            status = self.psu.status()
        v = status.v_out
        if v < s.min_start_voltage:
            raise ChargeError(f"No battery seen: {v:.2f} V at the output, under {s.min_start_voltage:g} V")
        if v > MAX_CELL_VOLTAGE + 0.05:
            raise ChargeError(f"{v:.2f} V at the output is over {MAX_CELL_VOLTAGE:.2f} V: not a single Li-ion cell")
        if v >= s.voltage - 0.01:
            raise ChargeError(f"The battery is already at {v:.2f} V, the target is {s.voltage:.2f} V")
        self._restore_protection = self.psu.protection()
        try:
            self.psu.set_protection(*self.charge_protection())
            self.psu.set_current(s.current)
            self.psu.set_voltage(s.voltage)
            self.psu.set_output(True)
            # Turning the output on clears a latched OVP/OCP flag. Check it did, and that the output is on.
            self._sleep(0.5)
            status = self.psu.status()
            if status.protection or not status.output:
                self.psu.set_output(False)
                raise ChargeError(f"The output didn't come on cleanly ({status.protection or 'output off'})")
        except (IOError, ValueError, ChargeError):
            self._put_back_protection()
            raise
        now = self._clock()
        self.state, self.message = State.CC, ""
        self.amp_hours = self.watt_hours = 0.0
        self.started = self._last = now
        self._below_since = None
        self._notify(f"Charge started at {v:.2f} V: {s.current:g} A to {s.voltage:.2f} V, done under {s.cutoff:g} A", "good")
        self._save_state(now)

    def charge_protection(self) -> tuple[float, float]:
        """The OVP and OCP while charging."""
        s = self.settings
        return round(s.voltage + OVP_MARGIN, 2), round(max(s.current * (1 + OCP_MARGIN), s.current + 0.1), 3)

    def apply_settings(self):
        """Sends changed charge settings to the PSU during a charge: the limits are widened first and narrowed last."""
        if not self.active:
            return
        ovp, ocp = self.charge_protection()
        active_ovp, active_ocp = self.psu.protection()
        self.psu.set_protection(max(ovp, active_ovp), max(ocp, active_ocp))
        self.psu.set_voltage(self.settings.voltage)
        self.psu.set_current(self.settings.current)
        self.psu.set_protection(ovp, ocp)

    def stop(self, reason: str, level="warn", output_off=True):
        if self.active:
            self._finish(State.STOPPED, reason, level, output_off)

    def resume(self, saved: dict, status: Status):
        """Carries on a charge that was active when the program stopped, if the PSU is still charging."""
        if not saved:
            return
        if status.output and self._is_target(status.v_set):
            self.state = State.CV if status.mode == "CV" else State.CC
            self.started = float(saved.get("started", self._clock()))
            self.amp_hours = float(saved.get("amp_hours", 0))
            self.watt_hours = float(saved.get("watt_hours", 0))
            if saved.get("restore_ovp"):
                self._restore_protection = (float(saved["restore_ovp"]), float(saved["restore_ocp"]))
            self._last = self._last_save = self._clock()
            self._notify(f"Charge resumed after a restart, {duration_text(self._last - self.started)} in", "info")
        else:
            if saved.get("restore_ovp"):
                self._restore_protection = (float(saved["restore_ovp"]), float(saved["restore_ocp"]))
                self._put_back_protection()
            self._save(None)
            self._notify("The charge from before the restart wasn't resumed: the PSU's output or voltage has changed", "warn")

    def update(self, status: Status):
        if not self.active:
            return
        s = self.settings
        now = self._clock()
        dt = now - self._last
        self._last = now
        if 0 < dt < 60:
            self.amp_hours += status.i_out * dt / 3600
            self.watt_hours += status.p_out * dt / 3600

        if status.protection:
            return self._finish(State.STOPPED, f"{status.protection} tripped at {status.v_out:.2f} V, {status.i_out:.3f} A", "bad")
        if not status.output:
            return self._finish(State.STOPPED, "The output was switched off on the PSU", "warn", output_off=False)
        if not self._is_target(status.v_set):
            return self._finish(State.STOPPED, f"The voltage was changed to {status.v_set:.2f} V on the PSU", "warn", output_off=False)
        if s.max_probe_temp is not None and status.ext_temp >= s.max_probe_temp:
            return self._finish(State.STOPPED, f"The battery probe reached {status.ext_temp} °C", "bad")
        if now - self.started >= s.max_hours * 3600:
            return self._finish(State.STOPPED, f"Not done after {s.max_hours:g} hours", "bad")

        state = State.CV if status.mode == "CV" else State.CC
        if state != self.state:
            self.state = state
            self._notify(f"{'Constant voltage' if state == State.CV else 'Constant current'} at {status.v_out:.2f} V, {status.i_out:.3f} A, "
                         f"{self.amp_hours:.2f} Ah in", "info")

        if self.state == State.CV and status.i_out < s.cutoff:
            self._below_since = self._below_since or now
            if now - self._below_since >= DONE_SECONDS:
                if self.amp_hours < NO_CHARGE_AH:
                    return self._finish(State.STOPPED, "No charge current: the battery is full or not connected", "warn")
                return self._finish(State.DONE, f"Charged: {self.amp_hours:.2f} Ah, {self.watt_hours:.1f} Wh "
                                                f"in {duration_text(now - self.started)}", "done")
        else:
            self._below_since = None

        if now - self._last_save >= SAVE_SECONDS:
            self._save_state(now)

    def summary(self, status: Status | None) -> str:
        if self.active and status:
            return (f"{self.state.value} {status.v_out:.2f} V {status.i_out:.2f} A  "
                    f"{self.amp_hours:.2f} Ah  {duration_text(self._clock() - self.started)}")
        return self.message or "Not charging"

    def _is_target(self, v_set: float) -> bool:
        return abs(v_set - self.settings.voltage) <= 0.6 / self.psu.model.volt_scale

    def _finish(self, state: State, message: str, level: str, output_off=True):
        if output_off:
            try:
                self.psu.set_output(False)
            except IOError as e:
                message += f". The output couldn't be switched off: {e}"
                level = "bad"
        problem = self._put_back_protection()
        if problem:
            message += f". {problem}"
        self.state, self.message = state, message
        self._save(None)
        self._notify(message, level)

    def _put_back_protection(self) -> str:
        """Puts back the OVP and OCP from before the charge. Returns what went wrong, if anything."""
        if self._restore_protection is None:
            return ""
        try:
            self.psu.set_protection(*self._restore_protection)
        except (IOError, ValueError) as e:
            return f"The OVP and OCP couldn't be put back to {self._restore_protection[0]:g} V, {self._restore_protection[1]:g} A: {e}"
        finally:
            self._restore_protection = None
        return ""

    def _save_state(self, now):
        self._last_save = now
        state = {"started": self.started, "amp_hours": round(self.amp_hours, 4), "watt_hours": round(self.watt_hours, 3)}
        if self._restore_protection:
            state["restore_ovp"], state["restore_ocp"] = self._restore_protection
        self._save(state)
