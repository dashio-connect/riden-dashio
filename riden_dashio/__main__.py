"""riden_dashio: an RIDEN RD60xx power supply on USB as a DashIO device for the Dash app."""
from __future__ import annotations

import argparse
import configparser
import datetime
import logging
import os
import signal
import threading
import time

import dashio

from .charger import ChargeSettings
from .psu_device import PsuDevice
from .rd60xx import RD60xx, find_port

log = logging.getLogger("riden_dashio")


class Settings:
    def __init__(self, filename: str):
        self.filename = filename
        self.config = configparser.ConfigParser()
        self.config.read_dict({
            "DEVICE": {"device_id": "", "device_name": ""},
            "PSU": {"port": "", "address": "1", "max_voltage": "", "max_current": "", "read_only": "no", "poll_interval": "1", "graph_interval": "10",
                    "graph_voltage_max": "", "graph_current_max": "",  # The graph's axes. Empty: the model's full range
                    "set_clock": "yes"},  # Set the PSU's clock from this computer's at startup, and when the PSU is switched back on
            "DASH": {"username": "", "password": ""},
            # Li-ion charging. Left empty, the current, cutoff and time limit follow the capacity: 0.5C, C/20, and 1.5x the CC time + 2 h.
            "CHARGE": {"capacity_ah": "6.8", "voltage": "4.20", "current": "", "cutoff": "", "max_hours": "", "max_probe_temp": ""},
            "CHARGE_STATE": {},  # A charge in progress, so it carries on after a restart
        })
        self._lock = threading.Lock()
        if not self.config.read(filename):
            self.save()
            log.info("Wrote %s. Add a DashIO username and password to it to use the Dash server.", filename)

    def get(self, section, key):
        return self.config.get(section, key)

    def save(self):
        with self._lock:
            with open(self.filename, "w", encoding="utf-8") as f:
                self.config.write(f)
            os.chmod(self.filename, 0o600)  # It may hold the Dash password

    def charge_settings(self, max_current: float) -> ChargeSettings:
        section = self.config["CHARGE"]
        settings = ChargeSettings.for_capacity(section.getfloat("capacity_ah"), max_current)
        for key in ("voltage", "current", "cutoff", "max_hours", "max_probe_temp"):
            if section.get(key, "").strip():
                setattr(settings, key, section.getfloat(key))
        return settings

    def save_charge_settings(self, settings: ChargeSettings):
        section = self.config["CHARGE"]
        for key in ("voltage", "current", "cutoff", "max_hours"):
            section[key] = f"{getattr(settings, key):g}"
        section["max_probe_temp"] = f"{settings.max_probe_temp:g}" if settings.max_probe_temp else ""
        self.save()

    def charge_state(self) -> dict | None:
        state = dict(self.config["CHARGE_STATE"])
        return state or None

    def save_charge_state(self, state: dict | None):
        self.config.remove_section("CHARGE_STATE")
        self.config.add_section("CHARGE_STATE")
        for key, value in (state or {}).items():
            self.config.set("CHARGE_STATE", key, str(value))
        self.save()


def _stop(signum, frame):
    raise KeyboardInterrupt


def _float_or_none(text):
    return float(text) if text.strip() else None


def main():
    parser = argparse.ArgumentParser(prog="riden_dashio", description="An RIDEN RD60xx power supply on USB as a DashIO device for the Dash app.")
    parser.add_argument("-i", "--ini", default="riden.ini", help="Settings file. Defaults to riden.ini")
    parser.add_argument("-d", "--db", default="riden.db", help="History database for the graph and event log. Defaults to riden.db")
    parser.add_argument("-p", "--port", help="Serial port. Defaults to the ini file's, else the first CH340 port with an RD60xx on it")
    parser.add_argument("--read-only", action="store_true", help="Monitor only: the app can't change the setpoints or output")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    settings = Settings(args.ini)
    port = args.port or settings.get("PSU", "port") or find_port()
    psu = RD60xx(port, address=settings.config.getint("PSU", "address"))
    log.info("%s serial %d, firmware %s, on %s", psu.model.name, psu.serial_number, psu.firmware, port)

    device_id = settings.get("DEVICE", "device_id") or f"rd{psu.serial_number}"
    device_name = settings.get("DEVICE", "device_name") or f"{psu.model.name} {psu.serial_number}"
    if not settings.get("DEVICE", "device_id"):
        settings.config.set("DEVICE", "device_id", device_id)
        settings.config.set("DEVICE", "device_name", device_name)
        settings.save()
    device = dashio.Device("RD60xx", device_id, device_name, store=dashio.DashStore(args.db))

    def name_rx(msg):
        settings.config.set("DEVICE", "device_name", msg[2])
        settings.save()
        return msg[2]

    device.set_name_callback(name_rx)

    # The DashConnection is made without a login too: it doesn't connect until it has one, from the ini or from the app.
    dash = dashio.DashConnection(settings.get("DASH", "username"), settings.get("DASH", "password"))
    connections = [dashio.TCPConnection(), dash]
    for connection in connections:
        connection.add_device(device)

    def dash_login_rx(msg):
        """The Dash server login, from the app's provisioning menu: msg[2] username, msg[3] password."""
        if len(msg) < 4:
            return False
        settings.config.set("DASH", "username", msg[2])
        settings.config.set("DASH", "password", msg[3])
        settings.save()
        dash.set_connection(msg[2], msg[3])
        log.info("Dash server login set from the app, for %s", msg[2] or "(nobody)")
        return True

    device.set_dashio_callback(dash_login_rx)

    max_current = _float_or_none(settings.get("PSU", "max_current"))
    psu_device = PsuDevice(
        psu, device,
        max_voltage=_float_or_none(settings.get("PSU", "max_voltage")),
        max_current=max_current,
        read_only=args.read_only or settings.config.getboolean("PSU", "read_only"),
        graph_interval=settings.config.getfloat("PSU", "graph_interval"),
        charge_settings=settings.charge_settings(min(max_current or psu.model.max_current, psu.model.max_current)),
        save_charge_settings=settings.save_charge_settings,
        save_charge_state=settings.save_charge_state,
        charge_state=settings.charge_state(),
        graph_voltage_max=_float_or_none(settings.get("PSU", "graph_voltage_max")),
        graph_current_max=_float_or_none(settings.get("PSU", "graph_current_max")),
        set_clock_on_return=settings.config.getboolean("PSU", "set_clock"),
    )
    if settings.config.getboolean("PSU", "set_clock"):
        psu_device.set_clock(datetime.datetime.now().replace(microsecond=0))
    # The device reports a hash of its layout, so the app fetches a changed layout without a cfgRev to raise.
    log.info("Dash device %s (%s) ready, layout %s", device_name, device_id, device.layout_id)

    poll_interval = settings.config.getfloat("PSU", "poll_interval")
    signal.signal(signal.SIGTERM, _stop)  # Close cleanly under systemd too
    try:
        while True:
            psu_device.poll()
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        pass
    finally:
        device.close()
        for connection in connections:
            connection.close()
        psu.close()


if __name__ == "__main__":
    main()
