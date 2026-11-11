"""A simulated RD60xx on a fake serial port: holding registers answered over Modbus RTU."""
import struct

from rd6006_dashio.rd60xx import crc16

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
        self._reply = b""

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
        else:
            body = bytes([addr, func | 0x80, 1])
        self._reply = body + crc16(body)

    def read(self, n):
        reply, self._reply = self._reply[:n], self._reply[n:]
        return reply

    def close(self):
        pass
