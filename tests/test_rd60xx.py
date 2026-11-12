import datetime
import unittest
from unittest import mock

from riden_dashio.rd60xx import RD60xx, ModbusError, crc16
from tests.fake_psu import FakePsu


class TestRD60xx(unittest.TestCase):
    def setUp(self):
        self.fake = FakePsu()
        self.psu = RD60xx(serial_port=self.fake)

    def test_crc16(self):
        # Read registers 0-19 from address 1, as sent to the real PSU
        self.assertEqual(crc16(bytes.fromhex("010300000014")), bytes.fromhex("45c5"))

    def test_identifies_model(self):
        self.assertEqual(self.psu.model.name, "RD6006")
        self.assertEqual(self.psu.serial_number, 39576)
        self.assertEqual(self.psu.firmware, "1.42")

    def test_status(self):
        s = self.psu.status()
        self.assertEqual((s.v_set, s.i_set, s.v_out, s.i_out), (3.8, 0.8, 3.81, 0.453))
        self.assertEqual(s.p_out, 1.72)
        self.assertEqual(s.v_in, 64.73)
        self.assertTrue(s.output)
        self.assertEqual((s.mode, s.protection), ("CV", ""))
        self.assertEqual((s.int_temp, s.ext_temp), (44, 31))
        self.assertEqual((s.amp_hours, s.watt_hours), (2.402, 9.014))

    def test_negative_temperature_and_protection(self):
        self.fake.regs[4:6] = [1, 5]
        self.fake.regs[16:18] = [2, 1]
        s = self.psu.status()
        self.assertEqual(s.int_temp, -5)
        self.assertEqual((s.mode, s.protection), ("CC", "OCP"))

    def test_setters_write_scaled_registers(self):
        self.psu.set_voltage(12.34)
        self.psu.set_current(1.5)
        self.psu.set_output(False)
        self.assertEqual(self.fake.writes, [(8, 1234), (9, 1500), (18, 0)])

    def test_setters_check_limits(self):
        with self.assertRaises(ValueError):
            self.psu.set_voltage(60.01)
        with self.assertRaises(ValueError):
            self.psu.set_current(-1)
        self.assertEqual(self.fake.writes, [])

    def test_rd6006p_resolution(self):
        fake = FakePsu()
        fake.regs[0] = 60065
        fake.regs[8:12] = [5000, 12345, 4999, 10000]
        s = RD60xx(serial_port=fake).status()
        self.assertEqual((s.v_set, s.i_set, s.v_out, s.i_out), (5.0, 1.2345, 4.999, 1.0))

    def test_retries_lost_reply(self):
        self.fake.fail_next = 2
        self.assertEqual(self.psu.status().v_out, 3.81)

    def test_gives_up(self):
        self.fake.fail_next = 3
        with self.assertRaises(ModbusError):
            self.psu.status()

    def test_presets(self):
        presets = self.psu.presets()
        self.assertEqual(len(presets), 10)
        self.assertEqual((presets[1].voltage, presets[1].current, presets[1].ovp, presets[1].ocp), (3.3, 1.0, 62.0, 6.2))
        self.assertEqual(self.psu.protection(), (62.0, 6.2))

    def test_set_protection(self):
        self.psu.set_protection(4.3, 1.25)
        self.assertEqual(self.fake.writes, [(82, 430), (83, 1250)])
        with self.assertRaises(ValueError):
            self.psu.set_protection(70, 1)

    def test_recall_widens_limits_first(self):
        self.fake.regs[82:84] = [500, 500]  # Active OVP 5 V, OCP 0.5 A
        self.fake.regs[92:96] = [1200, 2000, 1300, 2500]  # M3: 12 V, 2 A, OVP 13 V, OCP 2.5 A
        self.psu.recall_preset(3)
        self.assertEqual(self.fake.writes, [(82, 1300), (83, 2500), (8, 1200), (9, 2000), (82, 1300), (83, 2500)])

    def test_recall_narrows_limits_last(self):
        self.fake.regs[84:88] = [330, 1000, 400, 1200]  # M1: 3.3 V, 1 A, OVP 4 V, OCP 1.2 A
        self.psu.recall_preset(1)
        self.assertEqual(self.fake.writes, [(82, 6200), (83, 6200), (8, 330), (9, 1000), (82, 400), (83, 1200)])

    def test_recall_m0_refused(self):
        with self.assertRaises(ValueError):
            self.psu.recall_preset(0)

    def test_set_clock_writes_one_request(self):
        when = datetime.datetime(2026, 11, 11, 23, 59, 58)
        self.psu.set_clock(when)
        self.assertEqual(self.fake.writes, [(48, (2026, 11, 11, 23, 59, 58))],
                         "All six in one write, so the time can't roll over between registers")
        self.assertEqual(self.psu.clock(), when)

    def test_unset_clock_is_an_error(self):
        with self.assertRaises(ValueError):
            self.psu.clock()  # The fake's clock registers are 0

    def test_reopen(self):
        self.psu._port = "/dev/cu.usbserial-test"
        again = FakePsu()
        with mock.patch("serial.Serial", return_value=again) as opened:
            self.psu.reopen()
        opened.assert_called_once_with("/dev/cu.usbserial-test", 115200, timeout=0.5)
        self.psu.set_output(True)
        self.assertEqual(again.writes, [(18, 1)], "The new port is used")

    def test_reopen_finds_a_different_psu(self):
        self.psu._port = "/dev/cu.usbserial-test"
        other = FakePsu()
        other.regs[2] = 12345  # Another serial number
        with mock.patch("serial.Serial", return_value=other), self.assertRaisesRegex(ModbusError, "different PSU"):
            self.psu.reopen()

    def test_unknown_model(self):
        fake = FakePsu()
        fake.regs[0] = 12345
        with self.assertRaises(ModbusError):
            RD60xx(serial_port=fake)


if __name__ == '__main__':
    unittest.main()
