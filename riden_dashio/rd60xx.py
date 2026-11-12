"""Modbus RTU driver for the RIDEN RD60xx power supplies (RD6006, RD6006P, RD6012, RD6018, RD6024).

The PSU's USB port is a CH340 serial converter (USB ID 1a86:7523). It speaks Modbus RTU at 115200 baud 8N1,
slave address 1 by default: function 03 reads holding registers, function 06 writes one, and function 16 writes several.
"""
from __future__ import annotations

import dataclasses
import datetime
import logging
import struct
import threading
import time

import serial
import serial.tools.list_ports

log = logging.getLogger(__name__)

CH340_VID = 0x1A86
CH340_PID = 0x7523

# Holding registers
REG_MODEL = 0
REG_SERIAL_HI = 1
REG_FIRMWARE = 3
REG_INT_TEMP_SIGN = 4
REG_V_SET = 8
REG_I_SET = 9
REG_V_OUT = 10
REG_I_OUT = 11
REG_P_OUT_HI = 12
REG_V_IN = 14
REG_PROTECT = 16
REG_CV_CC = 17
REG_OUTPUT = 18
REG_BATTERY_MODE = 32
REG_V_BATTERY = 33
REG_EXT_TEMP_SIGN = 34
REG_AH_HI = 38
REG_WH_HI = 40
REG_CLOCK = 48  # Year, month, day, hour, minute, second: the PSU's clock, in the local time it shows
REG_PRESETS = 80  # M0 to M9, 4 registers each: V, I, OVP, OCP. M0's OVP and OCP are the active protection limits
REG_OVP = 82
REG_OCP = 83
NUM_PRESETS = 10
STATUS_REG_COUNT = 42  # Registers 0 to 41, read in one request

PROTECTION = {0: "", 1: "OVP", 2: "OCP"}


@dataclasses.dataclass(frozen=True)
class Model:
    name: str
    volt_scale: int  # Register counts per volt
    amp_scale: int  # Register counts per amp
    max_voltage: float
    max_current: float


# Model ID (register 0) to model. The RD6012P is left out: its current resolution depends on the range.
MODELS = {
    60062: Model("RD6006", 100, 1000, 60.0, 6.0),
    60065: Model("RD6006P", 1000, 10000, 60.0, 6.0),
    60121: Model("RD6012", 100, 100, 60.0, 12.0),
    60181: Model("RD6018", 100, 100, 60.0, 18.0),
    60241: Model("RD6024", 100, 100, 60.0, 24.0),
}


@dataclasses.dataclass(frozen=True)
class Status:
    v_set: float
    i_set: float
    v_out: float
    i_out: float
    p_out: float
    v_in: float
    output: bool
    mode: str  # "CV" or "CC"
    protection: str  # "", "OVP" or "OCP"
    int_temp: int  # °C
    ext_temp: int  # °C, the external probe
    amp_hours: float
    watt_hours: float
    battery_mode: bool
    v_battery: float


@dataclasses.dataclass(frozen=True)
class Preset:
    voltage: float
    current: float
    ovp: float
    ocp: float


class ModbusError(IOError):
    pass


def crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)


def _signed(sign: int, value: int) -> int:
    return -value if sign else value


def _u32(regs, index: int) -> int:
    return (regs[index] << 16) | regs[index + 1]


class RD60xx:
    """One RD60xx power supply. Safe to use from several threads."""

    def __init__(self, port: str | None = None, address: int = 1, baudrate: int = 115200, timeout: float = 0.5, serial_port=None):
        self.address = address
        self._lock = threading.Lock()
        self._port = None if serial_port is not None else port  # None: a port given ready made, which reopen() leaves alone
        self._baudrate, self._timeout = baudrate, timeout
        self._serial = serial_port if serial_port is not None else serial.Serial(port, baudrate, timeout=timeout)
        regs = self.read_registers(REG_MODEL, 4)
        if regs[0] not in MODELS:
            raise ModbusError(f"Unknown RD60xx model ID {regs[0]}")
        self.model_id = regs[0]
        self.model = MODELS[regs[0]]
        self.serial_number = _u32(regs, REG_SERIAL_HI)
        self.firmware = f"{regs[REG_FIRMWARE] // 100}.{regs[REG_FIRMWARE] % 100:02d}"

    def reopen(self):
        """Opens the serial port again, e.g. after the PSU was switched off and its USB port went away, and checks
        it's the same PSU. Raises IOError (serial.SerialException or ModbusError) if it isn't there yet."""
        if self._port is not None:
            with self._lock:
                try:
                    self._serial.close()
                except (IOError, OSError):
                    pass
                self._serial = serial.Serial(self._port, self._baudrate, timeout=self._timeout)
        regs = self.read_registers(REG_MODEL, 4)
        if regs[0] != self.model_id or _u32(regs, REG_SERIAL_HI) != self.serial_number:
            raise ModbusError(f"A different PSU is on {self._port}: model ID {regs[0]}, serial {_u32(regs, REG_SERIAL_HI)}")

    def close(self):
        self._serial.close()

    def _transact(self, request: bytes, reply_len: int) -> bytes:
        frame = bytes([self.address]) + request
        frame += crc16(frame)
        last_error = None
        with self._lock:
            for _ in range(3):
                self._serial.reset_input_buffer()
                self._serial.write(frame)
                reply = self._serial.read(reply_len)
                if len(reply) >= 5 and reply[1] == request[0] | 0x80 and crc16(reply[:3]) == reply[3:5]:
                    raise ModbusError(f"Modbus exception {reply[2]} for function {request[0]}")
                if len(reply) == reply_len and reply[0] == self.address and crc16(reply[:-2]) == reply[-2:]:
                    return reply
                last_error = f"bad reply {reply.hex() or '(none)'}"
                time.sleep(0.05)
        raise ModbusError(last_error)

    def read_registers(self, address: int, count: int) -> tuple[int, ...]:
        reply = self._transact(struct.pack(">BHH", 3, address, count), 5 + 2 * count)
        if reply[2] != 2 * count:
            raise ModbusError(f"Read of {count} registers returned {reply[2]} bytes")
        return struct.unpack(f">{count}H", reply[3:3 + 2 * count])

    def write_register(self, address: int, value: int):
        request = struct.pack(">BHH", 6, address, value)
        reply = self._transact(request, 8)
        if reply[1:6] != request:
            raise ModbusError(f"Write echo mismatch {reply.hex()}")

    def write_registers(self, address: int, values):
        """Writes consecutive registers in one request, so they change together."""
        values = tuple(values)
        request = struct.pack(f">BHHB{len(values)}H", 16, address, len(values), 2 * len(values), *values)
        reply = self._transact(request, 8)
        if reply[1:6] != request[:5]:
            raise ModbusError(f"Write echo mismatch {reply.hex()}")

    def clock(self) -> datetime.datetime:
        """The PSU's clock. ValueError if it isn't a valid time."""
        return datetime.datetime(*self.read_registers(REG_CLOCK, 6))

    def set_clock(self, when: datetime.datetime):
        """Sets the PSU's clock. It has no time zone: give it the local time it should show."""
        self.write_registers(REG_CLOCK, (when.year, when.month, when.day, when.hour, when.minute, when.second))

    def status(self) -> Status:
        r = self.read_registers(0, STATUS_REG_COUNT)
        v, a = self.model.volt_scale, self.model.amp_scale
        return Status(
            v_set=r[REG_V_SET] / v,
            i_set=r[REG_I_SET] / a,
            v_out=r[REG_V_OUT] / v,
            i_out=r[REG_I_OUT] / a,
            p_out=_u32(r, REG_P_OUT_HI) / 100,
            v_in=r[REG_V_IN] / 100,
            output=bool(r[REG_OUTPUT]),
            mode="CC" if r[REG_CV_CC] else "CV",
            protection=PROTECTION.get(r[REG_PROTECT], f"P{r[REG_PROTECT]}"),
            int_temp=_signed(r[REG_INT_TEMP_SIGN], r[REG_INT_TEMP_SIGN + 1]),
            ext_temp=_signed(r[REG_EXT_TEMP_SIGN], r[REG_EXT_TEMP_SIGN + 1]),
            amp_hours=_u32(r, REG_AH_HI) / 1000,
            watt_hours=_u32(r, REG_WH_HI) / 1000,
            battery_mode=bool(r[REG_BATTERY_MODE]),
            v_battery=r[REG_V_BATTERY] / 100,
        )

    def set_voltage(self, volts: float):
        if not 0 <= volts <= self.model.max_voltage:
            raise ValueError(f"{volts} V is outside 0 to {self.model.max_voltage} V")
        self.write_register(REG_V_SET, round(volts * self.model.volt_scale))

    def set_current(self, amps: float):
        if not 0 <= amps <= self.model.max_current:
            raise ValueError(f"{amps} A is outside 0 to {self.model.max_current} A")
        self.write_register(REG_I_SET, round(amps * self.model.amp_scale))

    def set_output(self, on: bool):
        self.write_register(REG_OUTPUT, 1 if on else 0)

    def presets(self) -> list[Preset]:
        """M0 to M9. M0 holds the active OVP and OCP."""
        r = self.read_registers(REG_PRESETS, 4 * NUM_PRESETS)
        v, a = self.model.volt_scale, self.model.amp_scale
        return [Preset(r[i] / v, r[i + 1] / a, r[i + 2] / v, r[i + 3] / a) for i in range(0, 4 * NUM_PRESETS, 4)]

    def protection(self) -> tuple[float, float]:
        """The active OVP and OCP."""
        p = self.presets()[0]
        return p.ovp, p.ocp

    def set_protection(self, ovp: float, ocp: float):
        """Sets the active OVP and OCP (M0's)."""
        max_ovp, max_ocp = self.model.max_voltage + 2, self.model.max_current + 0.2  # 62 V and 6.2 A on the RD6006
        if not 0 < ovp <= max_ovp:
            raise ValueError(f"OVP {ovp} V is outside 0 to {max_ovp:g} V")
        if not 0 < ocp <= max_ocp:
            raise ValueError(f"OCP {ocp} A is outside 0 to {max_ocp:g} A")
        self.write_register(REG_OVP, round(ovp * self.model.volt_scale))
        self.write_register(REG_OCP, round(ocp * self.model.amp_scale))

    def recall_preset(self, number: int):
        """Sets the voltage, current limit, OVP and OCP from preset M1 to M9.

        The limits are widened first and narrowed last, so the change doesn't trip OVP or OCP part way through."""
        if not 1 <= number < NUM_PRESETS:
            raise ValueError(f"No preset M{number}")
        presets = self.presets()
        new, active = presets[number], presets[0]
        self.set_protection(max(new.ovp, active.ovp), max(new.ocp, active.ocp))
        self.set_voltage(new.voltage)
        self.set_current(new.current)
        self.set_protection(new.ovp, new.ocp)


def find_port() -> str:
    """The first CH340 serial port with an RD60xx on it."""
    for port in serial.tools.list_ports.comports():
        if port.vid == CH340_VID and port.pid == CH340_PID:
            try:
                RD60xx(port.device).close()
                return port.device
            except (ModbusError, serial.SerialException) as e:
                log.debug("%s isn't an RD60xx: %s", port.device, e)
    raise ModbusError("No RD60xx found on a CH340 serial port")
