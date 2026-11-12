import unittest

from riden_dashio.charger import DONE_SECONDS, ChargeError, Charger, ChargeSettings, State
from riden_dashio.rd60xx import REG_CV_CC, REG_I_OUT, REG_OCP, REG_OUTPUT, REG_OVP, REG_PROTECT, REG_V_OUT, REG_V_SET, RD60xx
from tests.fake_psu import FakePsu


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class TestCharger(unittest.TestCase):
    def setUp(self):
        self.fake = FakePsu()
        self.fake.regs[REG_OUTPUT] = 0
        self.fake.regs[REG_V_OUT] = 365  # The battery, seen with the output off
        self.fake.regs[REG_I_OUT] = 0
        self.psu = RD60xx(serial_port=self.fake)
        self.clock = Clock()
        self.events = []
        self.saved = []
        self.charger = Charger(self.psu, ChargeSettings.for_capacity(6.8, 6.0), lambda text, level: self.events.append((level, text)),
                               save=self.saved.append, clock=self.clock, sleep=lambda s: None)

    def run_for(self, seconds, step=1.0):
        end = self.clock.now + seconds
        while self.clock.now < end and self.charger.active:
            self.clock.now += step
            self.fake.run(step)
            self.charger.update(self.psu.status())

    def charging(self, volts, amps, cv=False):
        self.fake.regs[REG_V_OUT] = round(volts * 100)
        self.fake.regs[REG_I_OUT] = round(amps * 1000)
        self.fake.regs[REG_CV_CC] = 0 if cv else 1

    def test_defaults_for_6_8_ah(self):
        s = ChargeSettings.for_capacity(6.8, 6.0)
        self.assertEqual((s.voltage, s.current, s.cutoff, s.max_hours), (4.2, 3.4, 0.34, 5.0))

    def test_start_sets_psu(self):
        self.charger.start()
        # OVP 4.30 V and OCP 4.08 A (1.2 x 3.4 A), then 3.4 A, 4.20 V, output on
        self.assertEqual(self.fake.writes, [(82, 430), (83, 4080), (9, 3400), (8, 420), (18, 1)])
        self.assertEqual(self.charger.state, State.CC)
        self.assertEqual(self.saved[-1]["amp_hours"], 0)
        self.assertEqual((self.saved[-1]["restore_ovp"], self.saved[-1]["restore_ocp"]), (62.0, 6.2))

    def test_start_after_ovp_trip(self):
        self.fake.regs[REG_PROTECT] = 1  # Latched from an earlier trip
        self.charger.start()
        self.run_for(2)
        self.assertTrue(self.charger.active)

    def test_start_refused_if_output_doesnt_come_on(self):
        real_set_output = self.psu.set_output
        self.psu.set_output = lambda on: real_set_output(False)  # The PSU ignores output on
        with self.assertRaisesRegex(ChargeError, "output off"):
            self.charger.start()
        self.assertFalse(self.charger.active)
        self.assertEqual((self.fake.regs[REG_OVP], self.fake.regs[REG_OCP]), (6200, 6200))

    def test_small_charge_current_gets_ocp_margin(self):
        self.charger.settings.current = 0.3
        self.assertEqual(self.charger.charge_protection(), (4.3, 0.4))

    def test_protection_put_back_when_done(self):
        self.charger.start()
        self.charging(3.9, 3.4)
        self.run_for(600)
        self.charging(4.2, 0.1, cv=True)
        self.run_for(DONE_SECONDS + 5)
        self.assertEqual(self.charger.state, State.DONE)
        self.assertEqual((self.fake.regs[REG_OVP], self.fake.regs[REG_OCP]), (6200, 6200))

    def test_protection_put_back_when_stopped(self):
        self.charger.start()
        self.charger.stop("test")
        self.assertEqual((self.fake.regs[REG_OVP], self.fake.regs[REG_OCP]), (6200, 6200))
        self.assertEqual(self.fake.regs[REG_OUTPUT], 0)

    def test_apply_settings_during_charge(self):
        self.charger.start()
        self.charger.settings.current = 1.0
        self.charger.apply_settings()
        self.assertEqual(self.fake.regs[9], 1000)
        self.assertEqual((self.fake.regs[REG_OVP], self.fake.regs[REG_OCP]), (430, 1200))

    def test_start_switches_output_off_to_measure(self):
        self.fake.regs[REG_OUTPUT] = 1
        self.charger.start()
        self.assertEqual(self.fake.writes[0], (18, 0))

    def test_no_battery(self):
        self.fake.regs[REG_V_OUT] = 0
        with self.assertRaisesRegex(ChargeError, "No battery"):
            self.charger.start()
        self.assertEqual(self.fake.writes, [])

    def test_not_one_cell(self):
        self.fake.regs[REG_V_OUT] = 820
        with self.assertRaisesRegex(ChargeError, "single"):
            self.charger.start()

    def test_already_charged(self):
        self.fake.regs[REG_V_OUT] = 420
        with self.assertRaisesRegex(ChargeError, "already"):
            self.charger.start()

    def test_full_charge(self):
        self.charger.start()
        self.charging(3.9, 3.4)
        self.run_for(3600)
        self.charging(4.2, 1.0, cv=True)
        self.run_for(600)
        self.assertEqual(self.charger.state, State.CV)
        self.charging(4.2, 0.30, cv=True)
        self.run_for(DONE_SECONDS - 2)
        self.assertTrue(self.charger.active)
        self.run_for(10)
        self.assertEqual(self.charger.state, State.DONE)
        self.assertEqual(self.fake.regs[REG_OUTPUT], 0)
        self.assertEqual(self.events[-1][0], "done")
        self.assertAlmostEqual(self.charger.amp_hours, 3.4 + 1.0 / 6 + 0.3 * DONE_SECONDS / 3600, places=2)
        self.assertIsNone(self.saved[-1])

    def test_current_dip_doesnt_end_charge(self):
        self.charger.start()
        self.charging(4.2, 1.0, cv=True)
        self.run_for(60)
        self.charging(4.2, 0.2, cv=True)
        self.run_for(DONE_SECONDS // 2)
        self.charging(4.2, 0.9, cv=True)
        self.run_for(DONE_SECONDS)
        self.assertTrue(self.charger.active)

    def test_no_current_from_the_start(self):
        self.charger.start()
        self.charging(4.2, 0.0, cv=True)
        self.run_for(DONE_SECONDS + 5)
        self.assertEqual(self.charger.state, State.STOPPED)
        self.assertIn("not connected", self.charger.message)

    def test_time_limit(self):
        self.charger.start()
        self.charging(3.9, 3.4)
        self.run_for(6 * 3600, step=10)
        self.assertEqual(self.charger.state, State.STOPPED)
        self.assertEqual(self.events[-1][0], "bad")
        self.assertEqual(self.fake.regs[REG_OUTPUT], 0)

    def test_probe_temperature(self):
        self.charger.settings.max_probe_temp = 45
        self.charger.start()
        self.charging(3.9, 3.4)
        self.fake.regs[35] = 45
        self.run_for(2)
        self.assertEqual(self.charger.state, State.STOPPED)
        self.assertIn("45 °C", self.charger.message)

    def test_protection_trip(self):
        self.charger.start()
        self.fake.regs[REG_PROTECT] = 2
        self.run_for(2)
        self.assertIn("OCP", self.charger.message)
        self.assertEqual(self.fake.regs[REG_OUTPUT], 0)

    def test_front_panel_changes_end_charge(self):
        self.charger.start()
        self.fake.regs[REG_V_SET] = 500
        self.run_for(2)
        self.assertEqual(self.charger.state, State.STOPPED)
        self.assertEqual(self.fake.regs[REG_OUTPUT], 1)  # Left as the user set it

    def test_energy_is_the_charges_not_the_psus(self):
        self.assertAlmostEqual(self.psu.status().amp_hours, 2.402)  # Left on the PSU's counters from before
        self.charger.start()
        self.charging(3.9, 3.4)
        self.run_for(3600)
        self.assertAlmostEqual(self.charger.amp_hours, 3.4, places=2)
        self.assertAlmostEqual(self.charger.watt_hours, 3.9 * 3.4, places=1)

    def test_energy_kept_after_the_charge_until_the_output_goes_on(self):
        self.charger.start()
        self.charging(4.2, 1.0, cv=True)
        self.run_for(360)
        self.charger.stop("Stopped from the app")
        self.charger.update(self.psu.status())
        self.assertEqual(self.charger.state, State.STOPPED)
        self.assertAlmostEqual(self.charger.amp_hours, 0.1, places=2)
        self.fake.regs[REG_OUTPUT] = 1
        self.charger.update(self.psu.status())
        self.assertEqual(self.charger.state, State.IDLE)

    def test_resume(self):
        self.charger.start()
        self.charging(3.9, 3.4)
        self.run_for(120)
        saved = self.saved[-1]
        self.fake.run(60)  # Still charging while the program restarts
        charger = Charger(self.psu, self.charger.settings, lambda text, level: None, clock=self.clock)
        charger.resume(saved, self.psu.status())
        self.assertEqual(charger.state, State.CC)
        charger.update(self.psu.status())
        self.assertAlmostEqual(charger.amp_hours, 3.4 * 180 / 3600, delta=0.0015, msg="The PSU counted on while the program was down")

    def test_resume_from_a_save_without_the_counters(self):
        self.charger.start()
        self.charging(3.9, 3.4)
        saved = {"started": self.charger.started, "amp_hours": 0.11, "watt_hours": 0.4}
        charger = Charger(self.psu, self.charger.settings, lambda text, level: None, clock=self.clock)
        charger.resume(saved, self.psu.status())
        charger.update(self.psu.status())
        self.assertAlmostEqual(charger.amp_hours, 0.11)
        self.fake.run(3600)
        charger.update(self.psu.status())
        self.assertAlmostEqual(charger.amp_hours, 3.51, places=3)

    def test_resume_refused_puts_protection_back(self):
        self.fake.regs[REG_OVP] = 430
        charger = Charger(self.psu, self.charger.settings, lambda text, level: None, clock=self.clock)
        charger.resume({"started": "1", "restore_ovp": "62.0", "restore_ocp": "6.2"}, self.psu.status())
        self.assertFalse(charger.active)
        self.assertEqual((self.fake.regs[REG_OVP], self.fake.regs[REG_OCP]), (6200, 6200))

    def test_resume_refused_when_output_off(self):
        self.fake.regs[REG_V_SET] = 420
        saved = []
        charger = Charger(self.psu, self.charger.settings, lambda text, level: None, save=saved.append, clock=self.clock)
        charger.resume({"started": "1"}, self.psu.status())
        self.assertFalse(charger.active)
        self.assertEqual(saved, [None])


if __name__ == '__main__':
    unittest.main()
