"""A simulated RD60xx on a fake serial port: holding registers answered over Modbus RTU."""
import struct

from riden_dashio.rd60xx import crc16

# Registers 0-41 as read from a real RD6006 (serial 39576, firmware 1.42), output on into a load.
RD6006_REGS = [60062, 0, 39576, 142, 0, 44, 0, 111, 380, 800, 381, 453, 0, 172, 6473, 0, 0, 0, 1, 0,
               0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 31, 0, 88, 0, 2402,
               0, 9014]
# Registers 80-119 from the same PSU: presets M0 to M9 (V, I, OVP, OCP)
RD6006_PRESETS = [1000, 500, 6200, 6200, 330, 1000, 6200, 6200] + [500, 6100, 6200, 6200] * 8


class FakePsu:
    def __init__(self, regs=None, address=1):
        self.regs = list(regs or RD6006_REGS) + [0] * 100
        self.regs[80:120] = RD6006_PRESETS
        self.address = address
        self.writes = []
        self.fail_next = 0  # Replies to drop, to test retries
        self._mah = self._mwh = 0.0  # The parts of a count not yet in the Ah and Wh registers
        self._reply = b""

    def run(self, seconds):
        """Counts Ah and Wh (registers 38-41, 0.001) for `seconds` at the present output, as the PSU does while it's on."""
        if not self.regs[18]:
            return
        amps, volts = self.regs[11] / 1000, self.regs[10] / 100
        self._mah += amps * seconds / 3.6
        self._mwh += amps * volts * seconds / 3.6
        for reg, part in ((38, "_mah"), (40, "_mwh")):
            whole = int(getattr(self, part))
            setattr(self, part, getattr(self, part) - whole)
            count = ((self.regs[reg] << 16) | self.regs[reg + 1]) + whole
            self.regs[reg], self.regs[reg + 1] = count >> 16, count & 0xFFFF

    # The pyserial methods RD60xx uses

    def reset_input_buffer(self):
        self._reply = b""

    def write(self, frame):
        assert crc16(frame[:-2]) == frame[-2:]
        if self.fail_next:
            self.fail_next -= 1
            return
        addr, func, reg, value = struct.unpack(">BBHH", frame[:6])
        if addr != self.address:
            return
        if func == 3:
            data = struct.pack(f">{value}H", *self.regs[reg:reg + value])
            body = bytes([addr, 3, len(data)]) + data
        elif func == 6:
            self.regs[reg] = value
            if reg == 18 and value:
                self.regs[16] = 0  # Turning the output on clears a latched OVP/OCP, as on the real PSU
            self.writes.append((reg, value))
            body = frame[:6]
        elif func == 16:
            values = struct.unpack(f">{value}H", frame[7:7 + 2 * value])
            self.regs[reg:reg + value] = values
            self.writes.append((reg, values))
            body = frame[:6]
        else:
            body = bytes([addr, func | 0x80, 1])
        self._reply = body + crc16(body)

    def read(self, n):
        reply, self._reply = self._reply[:n], self._reply[n:]
        return reply

    def close(self):
        pass
