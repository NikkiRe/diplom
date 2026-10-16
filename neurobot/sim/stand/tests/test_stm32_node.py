"""stm32_sim как узел ROS: разбор параметров, явный ноль при старте, вход TwistStamped, публикация только при
изменении, задержка, сторож, неконечные команды. Нужен ROS 2 (ВМ sim); свой домен DDS и свои темы — поднятый стенд
тест не задевает. На Mac тесты пропускаются."""
from __future__ import annotations

import math
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import rclpy
    from geometry_msgs.msg import Twist, TwistStamped
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.parameter import Parameter

    import stm32_sim
except ImportError:
    rclpy = None

DOMAIN = 88


@unittest.skipUnless(rclpy, "нет ROS 2 (запускать в ВМ sim)")
class Stm32SimNodeTest(unittest.TestCase):
    def setUp(self):
        self.ctx = rclpy.Context()
        rclpy.init(context=self.ctx, domain_id=DOMAIN)
        self.ns = "/stm32_test_%d" % os.getpid()
        self.exe = SingleThreadedExecutor(context=self.ctx)
        self.harness = rclpy.create_node("stm32_test", context=self.ctx)
        self.exe.add_node(self.harness)
        self.out = []
        self.harness.create_subscription(Twist, self.ns + "/out", lambda m: self.out.append(
            (m.linear.x, m.angular.z)), 10)
        self.node = None

    def tearDown(self):
        self.exe.shutdown()
        for n in (self.node, self.harness):
            if n is not None:
                n.destroy_node()
        rclpy.shutdown(context=self.ctx)

    def make(self, **params):
        params = dict(dict(input_topic=self.ns + "/in", output_topic=self.ns + "/out"), **params)
        return stm32_sim.Stm32Sim(context=self.ctx, parameter_overrides=[Parameter(k, value=v)
                                                                         for k, v in params.items()])

    def start(self, stamped=False, **params):
        self.node = self.make(**params)
        self.exe.add_node(self.node)
        pub = self.harness.create_publisher(TwistStamped if stamped else Twist, self.ns + "/in", 10)
        self.spin_until(lambda: pub.get_subscription_count() and self.harness.count_publishers(self.ns + "/out"))
        self.spin_for(0.3)      # обнаружение DDS в обе стороны
        self.assertEqual(self.out, [(0.0, 0.0)])       # явный ноль при старте
        return pub

    def spin_for(self, seconds):
        t_end = time.monotonic() + seconds
        while time.monotonic() < t_end:
            self.exe.spin_once(timeout_sec=0.01)

    def spin_until(self, cond, timeout=5.0):
        t_end = time.monotonic() + timeout
        while not cond():
            self.assertLess(time.monotonic(), t_end, "не дождались")
            self.exe.spin_once(timeout_sec=0.01)

    @staticmethod
    def twist(x, stamped=False):
        msg = TwistStamped() if stamped else Twist()
        (msg.twist if stamped else msg).linear.x = x
        return msg

    def test_bad_parameters_fail_start(self):
        for bad in ({"cmd_vel_type": "Foo"}, {"timeout_ms": 0.5}, {"timeout_ms": True}, {"delay_ms": -1},
                    {"max_lin": "0.1"}, {"rate_hz": 0.0}):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.make(**bad)

    def test_publish_on_change_only(self):
        pub = self.start(delay_ms=0)
        for _ in range(5):
            pub.publish(self.twist(0.2))
            self.spin_for(0.05)
        self.spin_for(0.2)
        self.assertEqual(self.out, [(0.0, 0.0), (0.2, 0.0)])

    def test_stamped_input_with_delay(self):
        pub = self.start(stamped=True, cmd_vel_type="TwistStamped", delay_ms=40)
        t0 = time.monotonic()
        pub.publish(self.twist(0.3, stamped=True))
        self.spin_until(lambda: len(self.out) == 2)
        self.assertGreaterEqual(time.monotonic() - t0, 0.04)
        self.assertEqual(self.out, [(0.0, 0.0), (0.3, 0.0)])

    def test_watchdog_and_non_finite(self):
        pub = self.start(timeout_ms=150, delay_ms=0)
        pub.publish(self.twist(0.2))
        self.spin_until(lambda: len(self.out) == 2)
        pub.publish(self.twist(math.nan))               # отброшена и сторожа не взводит
        self.spin_until(lambda: len(self.out) == 3, timeout=1.0)
        self.assertEqual(self.out, [(0.0, 0.0), (0.2, 0.0), (0.0, 0.0)])
        self.assertEqual((self.node._model.timeouts, self.node._model.rejected), (1, 1))


if __name__ == "__main__":
    unittest.main()
