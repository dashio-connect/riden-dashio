import datetime
import unittest
from unittest import mock

import dashio

from riden_dashio.psu_device import PsuDevice
from riden_dashio.rd60xx import RD60xx
from tests.fake_psu import FakePsu


class TestPsuDevice(unittest.TestCase):
    def setUp(self):
        self.fake = FakePsu()
        self.device = dashio.Device("RD60xx", "rdtest", "Test PSU")
        self.psu_device = PsuDevice(RD60xx(serial_port=self.fake), self.device, max_voltage=24.0)
        self.psu_device.poll()

    def tearDown(self):
        self.device.close()

    def rx(self, control, value):
        control._message_rx_event.fire(["rdtest", "CTRL", control.control_id, value])

    def test_control_ids_fit(self):
        self.assertTrue(self.device.get_cfg_hash())

    def test_poll_shows_status(self):
        p = self.psu_device
        self.assertEqual(p.v_out.dial_value, 3.81)
        self.assertEqual(p.v_set.text, "3.80")
        self.assertEqual(p.i_set.text, "0.800")
        self.assertEqual(p.mode.text, "CV")

    def test_set_voltage(self):
        self.rx(self.psu_device.v_set, "5.5")
        self.assertEqual(self.fake.writes, [(8, 550)])
        self.assertEqual(self.psu_device.v_set.text, "5.50")

    def test_voltage_over_configured_limit_not_written(self):
        self.rx(self.psu_device.v_set, "30")
        self.assertEqual(self.fake.writes, [])
        self.assertEqual(self.psu_device.v_set.text, "3.80")

    def test_bad_number_not_written(self):
        self.rx(self.psu_device.i_set, "abc")
        self.assertEqual(self.fake.writes, [])

    def test_output_button_toggles(self):
        self.rx(self.psu_device.output, "")
        self.assertEqual(self.fake.writes, [(18, 0)])
        self.assertEqual(self.psu_device.output.btn_state, dashio.ButtonState.OFF)

    def test_protection_trip_raises_alarm(self):
        with mock.patch.object(self.psu_device.alarm, "send") as send:
            self.fake.regs[16] = 1
            self.psu_device.poll()
        send.assert_called_once()
        self.assertEqual(self.psu_device.mode.text, "OVP")

    def test_charge_mode(self):
        p = self.psu_device
        self.fake.regs[18] = 0  # Output off, battery at 3.81 V
        self.rx(p.charge_mode, "1")
        self.assertTrue(p.charger.active)
        self.assertEqual(p.charge_mode.position, 1)
        self.assertEqual(self.fake.writes, [(82, 430), (83, 4080), (9, 3400), (8, 420), (18, 1)])

        self.rx(p.v_set, "5")  # The setpoints belong to the charger while it charges
        self.rx(p.preset, "1")
        self.assertEqual(len(self.fake.writes), 5)

        self.rx(p.ch_current, "2")  # A charge setting changes the PSU at once, and its OCP
        self.assertEqual(self.fake.regs[9], 2000)
        self.assertEqual(self.fake.regs[83], 2400)

        self.rx(p.charge_mode, "0")
        self.assertFalse(p.charger.active)
        self.assertEqual(self.fake.regs[18], 0)
        self.assertEqual((self.fake.regs[82], self.fake.regs[83]), (6200, 6200))  # The OVP and OCP put back
        self.assertEqual(p.charge_mode.position, 0)

    def test_charge_voltage_limited_to_one_cell(self):
        self.rx(self.psu_device.ch_voltage, "8.4")
        self.assertEqual(self.psu_device.charger.settings.voltage, 4.2)
        self.assertEqual(self.psu_device.ch_voltage.text, "4.20")

    def test_charge_not_started_without_battery(self):
        self.fake.regs[18] = 0
        self.fake.regs[10] = 0
        self.rx(self.psu_device.charge_mode, "1")
        self.assertFalse(self.psu_device.charger.active)
        self.assertEqual(self.psu_device.charge_mode.position, 0)
        self.assertIn("No battery", self.psu_device.charge_status.text)

    def test_graph_axes(self):
        graph = self.psu_device.graph
        self.assertEqual((graph.y_axis_max, graph.y_axis_max_rt), (60.0, 6.0))
        device = dashio.Device("RD60xx", "rdgr", "Graph")
        try:
            p = PsuDevice(RD60xx(serial_port=self.fake), device, graph_voltage_max=5.0, graph_current_max=4.0)
            self.assertEqual((p.graph.y_axis_max, p.graph.y_axis_max_rt), (5.0, 4.0))
        finally:
            device.close()

    def test_charged_alarm(self):
        p = self.psu_device
        self.fake.regs[18] = 0
        with mock.patch.object(p.charged_alarm, "send") as charged, mock.patch.object(p.alarm, "send") as fault:
            self.rx(p.charge_mode, "1")
            p.charger._finish(p.charger.state.DONE, "Charged: 6.50 Ah", "done")
        charged.assert_called_once_with("Battery charged", "Charged: 6.50 Ah")
        fault.assert_not_called()

    def test_presets_shown(self):
        p = self.psu_device
        self.assertEqual(p.preset.selection_list[:3], ["Custom", "M1 3.3 V 1 A", "M2 5 V 6.1 A"])
        self.assertEqual(p.preset.position, 0)  # 3.8 V 0.8 A isn't a preset
        self.assertEqual(p.limits.text, "OVP 62.00 V  OCP 6.200 A")

    def test_preset_recall(self):
        p = self.psu_device
        self.rx(p.preset, "1")
        self.assertEqual((self.fake.regs[8], self.fake.regs[9]), (330, 1000))
        self.assertEqual(p.preset.position, 1)

    def test_presets_dont_change_layout(self):
        before = self.device.get_cfg_hash()
        self.fake.regs[84] = 500
        self.psu_device.poll()
        self.assertEqual(self.psu_device.preset.selection_list[1], "M1 5 V 1 A")
        self.assertEqual(self.device.get_cfg_hash(), before)

    def test_clock_from_app(self):
        self.assertIn("CLK", self.device._cfg["deviceSetup"].split(","), "The app offers to set the clock")
        utc = 1762819200
        reply = self.device._on_message(f"\trdtest\tCLK\t{utc}\n".encode())
        self.assertEqual(reply, "\trdtest\tCLK\n")
        when = datetime.datetime.fromtimestamp(utc)  # The host's local time
        self.assertEqual(self.fake.writes, [(48, (when.year, when.month, when.day, when.hour, when.minute, when.second))])

    def test_clock_from_app_with_request_id(self):
        self.assertEqual(self.device._on_message(b"\trdtest\tCLK\tR2\t1762819200\n"), "\trdtest\tCLK\n")
        self.assertEqual(self.fake.writes[0][0], 48)

    def test_clock_out_of_range(self):
        self.assertEqual(self.device._on_message(b"\trdtest\tCLK\t99999999999999999\n"), "")
        self.assertEqual(self.fake.writes, [])

    def test_clock_not_set(self):
        self.fake.fail_next = 3  # Every retry
        self.assertEqual(self.device._on_message(b"\trdtest\tCLK\t1762819200\n"), "", "The app isn't told it was set")

    def test_read_only(self):
        device = dashio.Device("RD60xx", "rdro", "RO")
        try:
            p = PsuDevice(RD60xx(serial_port=self.fake), device, read_only=True)
            p.poll()
            self.rx(p.v_set, "5")
            self.rx(p.output, "")
            self.assertEqual(self.fake.writes, [])
        finally:
            device.close()


if __name__ == '__main__':
    unittest.main()
