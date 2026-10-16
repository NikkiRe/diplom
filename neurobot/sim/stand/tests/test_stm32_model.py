"""stm32_model: hold, сторож, задержка, неконечные команды, пределы, разбор параметров — без ROS."""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stm32_model import Stm32Model, ms_param, num_param  # noqa: E402


class HoldTest(unittest.TestCase):
    def test_zero_before_first_command(self):
        self.assertEqual(Stm32Model().step(0.0), (0.0, 0.0))

    def test_holds_last_command_forever(self):
        m = Stm32Model()
        m.command(0.5, -1.0, 0.0)
        self.assertEqual(m.step(0.0), (0.5, -1.0))
        self.assertEqual(m.step(3600.0), (0.5, -1.0))
        self.assertEqual(m.timeouts, 0)

    def test_zero_command_stops(self):
        m = Stm32Model()
        m.command(0.5, 0.0, 0.0)
        m.command(0.0, 0.0, 1.0)
        self.assertEqual(m.step(1.0), (0.0, 0.0))


class TimeoutTest(unittest.TestCase):
    def test_fresh_command_kept(self):
        m = Stm32Model(timeout_s=0.3)
        m.command(0.2, 0.0, 10.0)
        self.assertEqual(m.step(10.29), (0.2, 0.0))
        self.assertEqual(m.timeouts, 0)

    def test_silence_gives_zero_once(self):
        m = Stm32Model(timeout_s=0.3)
        m.command(0.2, 0.5, 10.0)
        self.assertEqual(m.step(10.31), (0.0, 0.0))
        self.assertTrue(m.stale)
        m.step(11.0)
        self.assertEqual(m.timeouts, 1)

    def test_new_command_after_timeout_resumes(self):
        m = Stm32Model(timeout_s=0.3)
        m.command(0.2, 0.0, 0.0)
        m.step(1.0)
        m.command(0.1, 0.0, 1.5)
        self.assertEqual(m.step(1.5), (0.1, 0.0))
        self.assertFalse(m.stale)
        m.step(2.0)
        self.assertEqual(m.timeouts, 2)

    def test_no_command_no_timeout(self):
        m = Stm32Model(timeout_s=0.3)
        m.step(100.0)
        self.assertEqual(m.timeouts, 0)

    def test_silence_after_zero_is_not_a_timeout(self):
        m = Stm32Model(timeout_s=0.5)
        m.command(0.2, 0.0, 0.0)
        m.command(0.0, 0.0, 0.1)          # писатель штатно остановился нулём и замолчал
        self.assertEqual(m.step(5.0), (0.0, 0.0))
        self.assertTrue(m.stale)
        self.assertEqual(m.timeouts, 0)


class DelayTest(unittest.TestCase):
    def test_command_reaches_base_after_delay(self):
        m = Stm32Model(delay_s=0.02)
        m.command(0.2, 0.0, 1.0)
        self.assertEqual(m.step(1.019), (0.0, 0.0))
        self.assertEqual(m.step(1.021), (0.2, 0.0))

    def test_commands_applied_in_order(self):
        m = Stm32Model(delay_s=0.02)
        m.command(0.2, 0.0, 1.0)
        m.command(0.0, 0.0, 1.01)
        self.assertEqual(m.step(1.025), (0.2, 0.0))
        self.assertEqual(m.step(1.031), (0.0, 0.0))

    def test_watchdog_counts_from_arrival(self):
        m = Stm32Model(timeout_s=0.3, delay_s=0.02)
        m.command(0.2, 0.0, 0.0)
        m.step(0.02)
        self.assertEqual(m.step(0.315), (0.2, 0.0))     # 0,295 с после прихода на базу
        self.assertEqual(m.step(0.325), (0.0, 0.0))
        self.assertEqual(m.timeouts, 1)


class NonFiniteTest(unittest.TestCase):
    def test_rejected_in_hold_keeps_previous(self):
        m = Stm32Model(max_lin=0.15, max_ang=0.8)
        m.command(0.1, 0.0, 0.0)
        for bad in ((math.nan, 0.0), (0.0, math.inf), (-math.inf, math.nan)):
            self.assertFalse(m.command(*bad, now=0.1))
        self.assertEqual(m.step(0.1), (0.1, 0.0))
        self.assertEqual(m.rejected, 3)

    def test_rejected_does_not_rearm_watchdog(self):
        m = Stm32Model(timeout_s=0.3)
        m.command(0.2, 0.0, 0.0)
        for i in range(1, 10):
            m.command(math.nan, 0.0, i * 0.1)
        self.assertEqual(m.step(0.9), (0.0, 0.0))

    def test_output_bounded_with_acceleration_limit(self):
        m = Stm32Model(max_lin_acc=0.5)
        m.step(0.0)
        m.command(math.nan, 0.0, 0.0)
        for t in (1.0, 10.0, 60.0):
            self.assertEqual(m.step(t), (0.0, 0.0))


class LimitsTest(unittest.TestCase):
    def test_velocity_clamped_both_signs(self):
        m = Stm32Model(max_lin=0.15, max_ang=0.8)
        m.command(0.5, -2.0, 0.0)
        self.assertEqual(m.step(0.0), (0.15, -0.8))
        m.command(-0.5, 2.0, 0.1)
        self.assertEqual(m.step(0.1), (-0.15, 0.8))

    def test_acceleration_ramp(self):
        m = Stm32Model(max_lin_acc=0.5, max_ang_acc=2.0)
        m.step(0.0)
        m.command(0.2, 1.0, 0.0)
        lin, ang = m.step(0.2)
        self.assertAlmostEqual(lin, 0.1)
        self.assertAlmostEqual(ang, 0.4)
        lin, ang = m.step(1.0)
        self.assertEqual((lin, ang), (0.2, 1.0))   # цель не перелетает

    def test_deceleration_limited_after_timeout(self):
        m = Stm32Model(timeout_s=0.3, max_lin_acc=0.5)
        m.step(0.0)
        for i in range(1, 11):                      # команды каждые 0,1 с: разгон 0 → 0,2 за 0,4 с
            m.command(0.2, 0.0, i * 0.1)
            m.step(i * 0.1)
        self.assertAlmostEqual(m.out[0], 0.2)
        self.assertAlmostEqual(m.step(1.31)[0], 0.2 - 0.5 * 0.31)   # сторож сработал: тормозим с пределом
        self.assertEqual(m.step(2.0), (0.0, 0.0))

    def test_bad_limits_rejected(self):
        for kw in ({"timeout_s": -1}, {"delay_s": -0.01}, {"max_lin": math.nan}, {"max_ang_acc": math.inf}):
            with self.assertRaises(ValueError, msg=str(kw)):
                Stm32Model(**kw)


class ParamTest(unittest.TestCase):
    def test_ms_param(self):
        self.assertEqual(ms_param("timeout_ms", 300), 300)
        self.assertEqual(ms_param("timeout_ms", 500.0), 500)
        for bad in (0.5, 0.3, True, False, -5, "300", math.nan):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ms_param("timeout_ms", bad)

    def test_num_param(self):
        self.assertEqual(num_param("max_lin", 1), 1.0)
        self.assertEqual(num_param("max_lin", 0.15), 0.15)
        for bad in (True, "0.1", -0.1, math.inf):
            with self.assertRaises(ValueError, msg=repr(bad)):
                num_param("max_lin", bad)


if __name__ == "__main__":
    unittest.main()
