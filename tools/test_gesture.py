import unittest

import gesture


class TapDetectorTests(unittest.TestCase):
    def test_single_tap_is_reported_on_release(self):
        taps = gesture.TapDetector()
        self.assertIsNone(taps.update(True, 10.00))
        self.assertEqual(taps.update(False, 10.05), 'tap')

    def test_two_quick_taps_make_a_double(self):
        taps = gesture.TapDetector()
        taps.update(True, 10.00)
        self.assertEqual(taps.update(False, 10.05), 'tap')
        taps.update(True, 10.20)
        self.assertEqual(taps.update(False, 10.25), 'double')

    def test_second_tap_outside_the_window_is_just_a_tap(self):
        taps = gesture.TapDetector()
        taps.update(True, 10.00)
        taps.update(False, 10.05)
        taps.update(True, 11.00)
        self.assertEqual(taps.update(False, 11.05), 'tap')

    def test_a_hold_is_not_the_first_half_of_a_double(self):
        taps = gesture.TapDetector()
        taps.update(True, 10.00)
        self.assertIsNone(taps.update(False, 10.50))  # longer than TAP_MAX
        taps.update(True, 10.60)
        self.assertEqual(taps.update(False, 10.65), 'tap')

    def test_repeating_the_same_key_state_does_nothing(self):
        taps = gesture.TapDetector()
        self.assertIsNone(taps.update(True, 10.00))
        self.assertIsNone(taps.update(True, 10.10))
        self.assertEqual(taps.update(False, 10.15), 'tap')
        self.assertIsNone(taps.update(False, 10.20))

    def test_reset_forgets_a_waiting_first_tap(self):
        taps = gesture.TapDetector()
        taps.update(True, 10.00)
        taps.update(False, 10.05)
        taps.reset()
        taps.update(True, 10.20)
        self.assertEqual(taps.update(False, 10.25), 'tap')

    def test_three_taps_are_a_double_then_a_tap(self):
        taps = gesture.TapDetector()
        taps.update(True, 10.00)
        taps.update(False, 10.05)
        taps.update(True, 10.10)
        self.assertEqual(taps.update(False, 10.15), 'double')
        taps.update(True, 10.25)
        self.assertEqual(taps.update(False, 10.30), 'tap')

    def test_start_gate_is_longer_than_a_tap(self):
        self.assertGreater(gesture.START_GATE, gesture.TAP_MAX)
        self.assertGreater(gesture.START_GATE, 0)


class DoublePressTests(unittest.TestCase):
    def test_two_presses_report_on_the_second_down(self):
        press = gesture.DoublePress()
        self.assertIsNone(press.update(True, 10.00))
        self.assertIsNone(press.update(False, 10.05))
        self.assertEqual(press.update(True, 10.20), 'double')

    def test_holding_each_press_still_counts(self):
        press = gesture.DoublePress()
        press.update(True, 10.00)
        press.update(False, 10.30)  # longer than TAP_MAX
        self.assertEqual(press.update(True, 10.40), 'double')

    def test_presses_outside_the_window_do_not_count(self):
        press = gesture.DoublePress()
        press.update(True, 10.00)
        press.update(False, 10.10)
        self.assertIsNone(press.update(True, 11.00))
        self.assertIsNone(press.update(False, 11.10))

    def test_release_alone_never_reports(self):
        press = gesture.DoublePress()
        press.update(True, 10.00)
        self.assertIsNone(press.update(False, 10.05))
        self.assertIsNone(press.update(False, 10.10))

    def test_reset_forgets_the_first_press(self):
        press = gesture.DoublePress()
        press.update(True, 10.00)
        press.update(False, 10.05)
        press.reset()
        self.assertIsNone(press.update(True, 10.20))


if __name__ == '__main__':
    unittest.main()
