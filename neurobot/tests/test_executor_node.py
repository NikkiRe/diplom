"""motion_executor v1 как процесс ROS 2: темы, явный ноль на старте и на выходе (сигнал, исключение), sd_notify,
параметры. Выход — /neurobot/cmd_vel_req (вход safety_gate); правила v1 проверяются без safety_gate
(require_safety:=false), работа с ним — test_safety_node.py.

Нужен rclpy (ВМ sim, Jazzy; на роботе — Foxy): без него тесты пропускаются. Узел запускается
`python -m neurobot.executor` в отдельном процессе, тест публикует входы и слушает /cmd_vel. Домен DDS —
NEUROBOT_TEST_DOMAIN (по умолчанию 41; стенд и его тесты берут 30 и 70–99). Времена — ориентир, не результат.
"""
from __future__ import annotations

import json
import math
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ["ROS_DOMAIN_ID"] = os.environ.get("NEUROBOT_TEST_DOMAIN", "41")

try:
    import rclpy
    from geometry_msgs.msg import Twist, TwistStamped
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import LaserScan
    from std_msgs.msg import String
except ImportError:
    rclpy = None


def twist(linear: float, angular: float = 0.0) -> "Twist":
    msg = Twist()
    msg.linear.x, msg.angular.z = float(linear), float(angular)
    return msg


def scan(front: float = 3.0, shadow: bool = False) -> "LaserScan":
    """URG-04LX с геометрией скана робота (683 луча ±2,0923497677 рад); луч прямо вперёд — front, остальные
    3 м; shadow — блок NaN −60,8…+4,2°, как в скане с робота (R7, RS3)."""
    msg = LaserScan()
    msg.angle_min, msg.angle_max = -2.0923497677, 2.0923497677
    msg.angle_increment = (msg.angle_max - msg.angle_min) / 682
    msg.range_min, msg.range_max = 0.02, 5.6
    ranges = [3.0] * 683
    ranges[341] = front
    if shadow:
        ranges[168:354] = [math.nan] * 186
    msg.ranges = ranges
    return msg


# Узел, у которого 10-й тик в движении (≈0,5 с) бросает исключение, — проверка нуля на выходе по ошибке
CRASH_WHILE_MOVING = """
import sys
from neurobot.executor import node

tick = node.MotionExecutor._tick
moving_ticks = 0

def crashing_tick(self):
    global moving_ticks
    tick(self)
    moving_ticks += self._last != (0.0, 0.0)
    if moving_ticks == 10:
        raise RuntimeError("проверка: исключение в тике")

node.MotionExecutor._tick = crashing_tick
sys.exit(node.main())
"""


class Harness:
    """Процесс узла + тестовый узел rclpy в своём потоке; входящие сообщения с монотонным временем.
    code — текст для `python -c` вместо `python -m neurobot.executor`."""

    def __init__(self, args=(), cmd_type=None, topic="/neurobot/cmd_vel_req", code=None):
        self.dir = tempfile.TemporaryDirectory()
        self.notify_path = os.path.join(self.dir.name, "notify.sock")
        self.notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.notify.bind(self.notify_path)
        self.notify.settimeout(0.05)
        self.node = rclpy.create_node("nb_test_executor_%d" % os.getpid())
        self.cmd = []
        self.state = []
        self.lock = threading.Lock()
        self.node.create_subscription(cmd_type or Twist, topic, self._on_cmd, 10)
        self.node.create_subscription(String, "/neurobot/motion_state", self._on_state, 10)
        self.gesture = self.node.create_publisher(Twist, "/cmd_vel_gesture", 10)
        self.agent = self.node.create_publisher(Twist, "/cmd_vel_agent", 10)
        self.scan = self.node.create_publisher(LaserScan, "/scan", qos_profile_sensor_data)
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self.executor.spin, daemon=True)
        self.thread.start()
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT)] + os.environ.get("PYTHONPATH", "").split(os.pathsep)),
                   PYTHONDONTWRITEBYTECODE="1",
                   NOTIFY_SOCKET=self.notify_path, WATCHDOG_USEC="1000000")
        self.log_path = os.path.join(self.dir.name, "stderr.log")
        entry = ["-m", "neurobot.executor"] if code is None else ["-c", code]
        with open(self.log_path, "wb") as log:
            self.proc = subprocess.Popen([sys.executable, *entry, "--ros-args", *args],
                                         env=env, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=log)

    def _on_cmd(self, msg):
        t = msg.twist if hasattr(msg, "twist") else msg
        with self.lock:
            self.cmd.append((time.monotonic(), t.linear.x, t.angular.z, msg))

    def _on_state(self, msg):
        with self.lock:
            self.state.append((time.monotonic(), json.loads(msg.data)))

    def notifications(self, seconds: float) -> list:
        out = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                out.append(self.notify.recv(64).decode())
            except socket.timeout:
                pass
        return out

    def wait_cmd(self, pred, timeout: float, since: float):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                for rec in self.cmd:
                    if rec[0] >= since and pred(rec):
                        return rec
            time.sleep(0.005)
        return None

    def feed(self, seconds: float, gesture=None, agent=None, front=3.0, shadow=False, scan_on=True, hz=10.0):
        """Публиковать входы с частотой hz (как демо и драйвер лидара) seconds секунд."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if scan_on:
                self.scan.publish(scan(front, shadow))
            if gesture is not None:
                self.gesture.publish(twist(*gesture))
            if agent is not None:
                self.agent.publish(twist(*agent))
            time.sleep(1.0 / hz)

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(5)
        self.executor.shutdown()
        self.node.destroy_node()
        self.notify.close()
        self.dir.cleanup()


@unittest.skipIf(rclpy is None, "нужен rclpy (ВМ sim: source /opt/ros/jazzy/setup.bash)")
class ExecutorProcess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rclpy.init()

    @classmethod
    def tearDownClass(cls):
        rclpy.shutdown()

    def start(self, *args, **kw) -> Harness:
        h = Harness(("-p", "require_safety:=false") + args, **kw)
        self.addCleanup(h.close)
        self.assertIn("READY=1", h.notifications(5.0))
        return h

    def test_guard_rules_end_to_end(self):
        h = self.start()
        first = h.wait_cmd(lambda r: True, 3.0, 0.0)
        self.assertIsNotNone(first, "нет /cmd_vel после READY")
        self.assertEqual(first[1:3], (0.0, 0.0))
        h.feed(0.3)  # сначала скан: иначе первый жест мог бы обогнать его и срезаться по stale (порядок DDS)

        t = time.monotonic()
        h.feed(1.0, gesture=(0.5, 0.0))
        rec = h.wait_cmd(lambda r: r[1] == 0.5, 0.1, t)
        self.assertIsNotNone(rec, "жест не дошёл до /cmd_vel")
        with h.lock:
            moving = [r for r in h.cmd if t + 0.3 <= r[0] <= t + 0.9]
        self.assertTrue(moving and all(r[1] == 0.5 for r in moving))
        self.assertGreaterEqual(len(moving) / 0.6, 15)  # в движении ≈20 Гц

        t = time.monotonic()  # жесты замолчали: ноль не позже 0,4 с + тик
        h.feed(1.0)
        rec = h.wait_cmd(lambda r: r[1] == 0.0, 0.5, t)
        self.assertIsNotNone(rec)
        self.assertLess(rec[0] - t, 0.4 + 0.1)

        t = time.monotonic()  # препятствие: вперёд нельзя, поворот можно
        h.feed(1.0, gesture=(0.5, 0.3), front=0.3)
        with h.lock:
            seen = {(r[1], r[2]) for r in h.cmd if r[0] >= t + 0.35}
        self.assertEqual(seen, {(0.0, 0.3)})
        h.feed(0.3, gesture=(0.5, 0.3))  # путь свободен: срез снят, следующий срез — отдельный переход

        t = time.monotonic()  # слепой сектор (RS3): вперёд нельзя, поворот можно
        h.feed(1.0, gesture=(0.5, 0.3), shadow=True)
        with h.lock:
            seen = {(r[1], r[2]) for r in h.cmd if r[0] >= t + 0.35}
            blind = [s for ts, s in h.state if ts >= t + 0.35]
        self.assertEqual(seen, {(0.0, 0.3)})
        self.assertTrue(blind and all(s["front"] == "blind" and s["cut"] for s in blind))
        self.assertGreater(blind[-1]["front_gap_deg"], 30.0)

        t = time.monotonic()  # агент главнее жеста
        h.feed(1.0, gesture=(0.5, 0.0), agent=(0.08, 0.0))
        with h.lock:
            seen = {(r[1], r[2]) for r in h.cmd if r[0] >= t + 0.35}
        self.assertEqual(seen, {(0.08, 0.0)})

        h.feed(0.6, gesture=(0.0, 0.0))  # манёвр демо кончился: подавление агентом снято, агент устарел
        t = time.monotonic()  # /scan пропал: вперёд — пока скану ≤ 0,5 с, затем ноль
        h.feed(1.2, gesture=(0.5, 0.0), scan_on=False)
        moved = h.wait_cmd(lambda r: r[1] == 0.5, 0.1, t)
        cut = h.wait_cmd(lambda r: r[1] == 0.0, 0.1, t + 0.1)
        self.assertIsNotNone(moved)
        self.assertIsNotNone(cut)
        self.assertLess(cut[0] - t, 0.5 + 0.1)

        with h.lock:
            states = [s for _, s in h.state]
        self.assertTrue(any(s["by"] == "agent" for s in states))
        self.assertTrue(any(s["blocked_front"] for s in states))
        last = states[-1]["counters"]
        self.assertEqual(last["cut_forward"], 3)  # препятствие, слепой сектор, пропавший скан
        self.assertGreaterEqual(last["preempted"], 1)
        self.assertGreaterEqual(last["stale"], 1)
        self.assertIn("WATCHDOG=1", h.notifications(0.5))

    def test_sigterm_while_moving_ends_with_zero(self):
        h = self.start()
        h.feed(0.5, gesture=(0.5, 0.0))
        feeder = threading.Thread(target=h.feed, args=(1.0,), kwargs={"gesture": (0.5, 0.0)})
        feeder.start()
        time.sleep(0.3)
        h.proc.send_signal(signal.SIGTERM)  # жесты ещё идут — ноль должен быть последним
        self.assertEqual(h.proc.wait(5), 0)
        feeder.join()
        with h.lock:
            self.assertEqual(h.cmd[-1][1:3], (0.0, 0.0))
            self.assertTrue(any(r[1] == 0.5 for r in h.cmd))
        self.assertIn("STOPPING=1", h.notifications(0.2))

    def test_exception_while_moving_ends_with_zero(self):
        h = self.start(code=CRASH_WHILE_MOVING)
        feeder = threading.Thread(target=h.feed, args=(3.0,), kwargs={"gesture": (0.5, 0.0)})
        feeder.start()
        self.assertEqual(h.proc.wait(10), 1)  # жесты ещё идут — ноль должен быть последним
        feeder.join()
        with h.lock:
            self.assertTrue(any(r[1] == 0.5 for r in h.cmd))
            self.assertEqual(h.cmd[-1][1:3], (0.0, 0.0))
        with open(h.log_path, "rb") as fh:
            self.assertIn("проверка: исключение в тике".encode(), fh.read())

    def test_twist_stamped_output(self):
        h = self.start("-p", "cmd_vel_type:=TwistStamped", "-r", "/neurobot/cmd_vel_req:=/nb_test_cmd_vel_stamped",
                       cmd_type=TwistStamped, topic="/nb_test_cmd_vel_stamped")
        t = time.monotonic()
        h.feed(0.5, gesture=(0.5, 0.0))
        rec = h.wait_cmd(lambda r: r[1] == 0.5, 0.5, t)
        self.assertIsNotNone(rec)
        self.assertEqual(rec[3].header.frame_id, "base_link")

    def test_allow_blind_parameter(self):
        h = self.start("-p", "allow_blind:=true")
        t = time.monotonic()
        h.feed(1.0, gesture=(0.5, 0.0), shadow=True)
        rec = h.wait_cmd(lambda r: r[1] == 0.5, 0.1, t + 0.2)
        self.assertIsNotNone(rec, "под присмотром слепой сектор не должен запрещать движение")

    def test_bad_parameter_exits_with_code_2(self):
        for args, word in ((("-p", "cmd_vel_type:=Twst"), b"cmd_vel_type"), (("-p", "decel:=0.0"), b"decel"),
                           (("-p", "d_stop:=1"), b"d_stop")):  # целое вместо дробного — ошибка типа (Jazzy)
            h = Harness(args)
            self.addCleanup(h.close)
            self.assertEqual(h.proc.wait(10), 2, args)
            with open(h.log_path, "rb") as fh:
                self.assertIn(word, fh.read(), args)


if __name__ == "__main__":
    unittest.main()
