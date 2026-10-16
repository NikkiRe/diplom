"""predict_live v2: DriveController в режиме legacy (L2, L3, S1–S3, R7, R8, аварийная защёлка) с подставным узлом
ROS; на подставном узле — коды выхода цикла «зрения», /neurobot/perception_health, разбор motion_state и
safety_state (срез вперёд, защёлка) и предупреждение «состояний стража нет».

Импортирует сам predict_live.py, поэтому нужны torch, snntorch, cv2, rclpy и подставной pyrealsense2 стенда
(ВМ sim); без них тесты пропускаются. Узел ROS не создаётся: publisher/timer/subscription — заглушки.
"""
from __future__ import annotations

import importlib
import json
import math
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "neurobot" / "perception", ROOT / "neurobot" / "sim" / "gestures" / "shim"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:
    pl = importlib.import_module("predict_live")
    from sensor_msgs.msg import LaserScan
except ImportError:
    pl = None


class FakeParent:
    """То, что DriveController берёт у узла: publisher (записывает Twist), timer и subscription — пустые."""

    def __init__(self):
        self.sent = []
        self.lock = threading.Lock()

    def create_publisher(self, *args):
        return self

    def create_timer(self, *args):
        return None

    def create_subscription(self, *args):
        return None

    def publish(self, msg):
        with self.lock:
            self.sent.append((time.monotonic(), msg.linear.x, msg.angular.z))

    def since(self, t):
        with self.lock:
            return [(x, z) for ts, x, z in self.sent if ts >= t]


def tb3_scan(hit_deg=None, dist=0.2):
    """TurtleBot3: 360 лучей 0…2π (у демо было IndexError на лучах 166–515)."""
    msg = LaserScan()
    msg.angle_min, msg.angle_max, msg.angle_increment = 0.0, 2 * math.pi, 2 * math.pi / 360
    msg.range_min, msg.range_max = 0.12, 3.5
    ranges = [3.0] * 360
    if hit_deg is not None:
        ranges[hit_deg % 360] = dist
    msg.ranges = ranges
    return msg


@unittest.skipIf(pl is None, "нужны torch, rclpy и подставные модули стенда (ВМ sim)")
class DriveControllerTest(unittest.TestCase):
    def setUp(self):
        self.parent = FakeParent()
        self.dc = pl.DriveController(self.parent, pl.build_parser().parse_args([]))
        self.dc.process_lidar(tb3_scan())  # свежий скан: без него вперёд нельзя (S1)

    def run_async(self, name):
        th = threading.Thread(target=self.dc.run, args=(name,))
        th.start()
        self.addCleanup(lambda: (self.dc.cancel.set(), th.join(10)))
        return th

    def test_command_published_at_once(self):
        t = time.monotonic()
        self.dc.move_forward()
        self.assertEqual(self.parent.since(t), [(0.5, 0.0)])  # без ожидания тика 10 Гц (L2)

    def test_obstacle_cancels_forward_maneuver_with_explicit_zero(self):
        th = self.run_async("come")
        time.sleep(0.2)
        t = time.monotonic()
        self.dc.process_lidar(tb3_scan(hit_deg=5))
        th.join(1.0)
        self.assertFalse(th.is_alive(), "манёвр не отменён препятствием (S3)")
        self.assertEqual(self.parent.since(t)[0], (0.0, 0.0))  # явный ноль, а не молчание (S1)
        self.assertTrue(all(x <= 0 for x, _ in self.parent.since(t)))
        self.assertTrue(self.dc.at_work())

    def test_turn_allowed_near_obstacle_forward_step_cancels_rest(self):
        self.dc.process_lidar(tb3_scan(hit_deg=0))
        t = time.monotonic()
        th = self.run_async("turn180")
        time.sleep(0.3)
        self.assertTrue(th.is_alive())
        self.dc.timer_callback()
        self.assertEqual(self.parent.since(t)[-1], (0.0, -1.0))  # поворот на месте разрешён
        self.dc.cancel.set()
        th.join(1.0)
        t = time.monotonic()
        th = self.run_async("come")
        th.join(1.0)
        self.assertFalse(th.is_alive())
        self.assertEqual(set(self.parent.since(t)), {(0.0, 0.0)})  # вперёд к стене не поехал

    def test_guard_cut_cancels_only_forward_step(self):
        th = self.run_async("turn180")
        time.sleep(0.2)
        self.dc.guard_cut("страж")  # срез вперёд на повороте — устаревшее состояние стража, манёвр идёт
        th.join(0.3)
        self.assertTrue(th.is_alive())
        self.dc.cancel.set()
        th.join(1.0)

        self.dc.process_lidar(tb3_scan())
        th = self.run_async("come")
        time.sleep(0.2)
        t = time.monotonic()
        with self.assertLogs("predict_live", "INFO") as logs:
            self.dc.guard_cut("страж")  # страж срезал движение вперёд — остаток манёвра не доезжаем (S3)
        th.join(1.0)
        self.assertFalse(th.is_alive())
        self.assertEqual(self.parent.since(t), [(0.0, 0.0)])
        self.assertIn("страж: вперёд нельзя", logs.output[0])

    def test_estop_stops_any_maneuver_and_it_does_not_resume(self):
        th = self.run_async("think")  # поворот 2 с, вперёд 2 с, поворот 5 с, вперёд 2 с
        time.sleep(0.3)
        self.dc.put_cmd("come")  # непрочитанная команда — тоже не должна пережить защёлку
        t = time.monotonic()
        with self.assertLogs("predict_live", "WARNING") as logs:
            self.dc.estop(True, ("estop: кнопка",))
        self.assertEqual(self.parent.since(t)[0], (0.0, 0.0))  # ноль сразу, а не по тику 10 Гц
        th.join(1.0)
        self.assertFalse(th.is_alive(), "поворот не отменён защёлкой")
        self.assertIn("estop: кнопка", logs.output[0])
        self.assertFalse(self.dc.at_work())  # детекция на паузе, пока защёлка
        self.dc.estop(True)  # повтор состояния (5 Гц) — без новых строк и нулей вне тика
        self.dc.put_cmd("go_left")  # жест, распознанный в момент защёлки, — отброшен
        late = self.run_async("go_right")  # команда, взятая из ящика до защёлки, — не начинается
        late.join(0.5)
        self.assertFalse(late.is_alive(), "манёвр начат в защёлке: после снятия он доехал бы")
        self.dc.move_forward()  # шаг манёвра, начатый до защёлки, — только ноль
        self.dc.timer_callback()
        self.assertEqual(set(self.parent.since(t)), {(0.0, 0.0)})

        t = time.monotonic()
        with self.assertLogs("predict_live", "INFO"):
            self.dc.estop(False)
        self.assertTrue(self.dc.at_work())
        self.assertIsNone(self.dc.cmd_slot.take(0.0))
        for _ in range(5):
            self.dc.timer_callback()
            time.sleep(0.1)
        self.assertEqual(set(self.parent.since(t)), {(0.0, 0.0)})  # после снятия остаток think не доезжает
        th = self.run_async("turn180")  # новый жест после снятия — едет
        time.sleep(0.2)
        self.assertEqual(self.parent.since(t)[-1], (0.0, -1.0))

    def test_estop_drops_unread_command_and_detection_resumes_after_release(self):
        # поток контроллера команду не взял (манёвра не было): кроме защёлки, детекцию после снятия вернуть некому
        self.dc.put_cmd("come")
        self.assertFalse(self.dc.at_work())
        with self.assertLogs("predict_live", "WARNING"):
            self.dc.estop(True)
        self.assertIsNone(self.dc.cmd_slot.take(0.0))
        self.assertFalse(self.dc.at_work())
        with self.assertLogs("predict_live", "INFO"):
            self.dc.estop(False)
        self.assertTrue(self.dc.at_work())

    def test_estop_racing_put_cmd_leaves_slot_empty(self):
        # защёлка приходит из колбэка ROS, пока цикл «зрения» кладёт команду: ящик после неё пуст при любом порядке
        put = self.dc.cmd_slot.put
        racer = threading.Thread(target=self.dc.estop, args=(True,))

        def slow_put(value):
            racer.start()
            time.sleep(0.1)  # защёлка успевает всё, что не ждёт замка контроллера
            return put(value)
        self.dc.cmd_slot.put = slow_put
        with self.assertLogs("predict_live", "WARNING"):
            self.dc.put_cmd("come")
            racer.join(1.0)
        self.assertIsNone(self.dc.cmd_slot.take(0.0), "команда легла в ящик после очистки — после снятия доехала бы")
        with self.assertLogs("predict_live", "INFO"):
            self.dc.estop(False)
        self.assertTrue(self.dc.at_work())

    def test_forward_needs_fresh_scan(self):
        parent = FakeParent()
        # таймаут скана 1 с: запас на планирование потоков в нагруженной ВМ, проверяется сама отсечка
        dc = pl.DriveController(parent, pl.parse_args(["--scan-timeout", "1.0"]))  # скана ещё не было
        t = time.monotonic()
        dc.turn_right()
        self.assertEqual(parent.since(t), [(0.0, -1.0)])  # поворот без скана разрешён, как у стража
        with self.assertLogs("predict_live", "WARNING"):
            dc.run("come")
        self.assertLess(time.monotonic() - t, 1.0)  # отменён сразу, а не через 3 с манёвра
        self.assertEqual(set(parent.since(t)[1:]), {(0.0, 0.0)})

        # сканы пропали посреди манёвра: ноль с тиком таймера после scan_timeout
        t_scan = time.monotonic()
        dc.process_lidar(tb3_scan())
        th = threading.Thread(target=dc.run, args=("come",))
        th.start()
        self.addCleanup(lambda: (dc.cancel.set(), th.join(10)))
        while th.is_alive() and time.monotonic() - t_scan < 3.0:
            dc.timer_callback()
            time.sleep(0.1)
        self.assertFalse(th.is_alive(), "манёвр без свежего скана не отменён")
        sent = [(ts, x) for ts, x, _ in parent.sent if ts >= t_scan]
        self.assertEqual(sent[0][1], 0.5)
        first_zero = next(ts for ts, x in sent if x == 0.0)
        self.assertGreaterEqual(first_zero - t_scan, 1.0)
        self.assertLess(first_zero - t_scan, 1.0 + 0.5)
        self.assertTrue(all(x == 0.0 for ts, x in sent if ts >= first_zero))

    def test_sector_by_angles(self):
        self.dc.process_lidar(tb3_scan(hit_deg=90))  # сбоку — не препятствие
        self.assertFalse(self.dc.is_obstructed.is_set())
        self.dc.process_lidar(tb3_scan(hit_deg=-10))
        self.assertTrue(self.dc.is_obstructed.is_set())
        self.dc.process_lidar(tb3_scan())
        self.assertFalse(self.dc.is_obstructed.is_set())

    def test_broken_scan_is_failsafe_and_logged_once(self):
        with self.assertLogs("predict_live", "ERROR") as logs:
            self.dc.process_lidar(object())
            self.dc.process_lidar(object())
        self.assertTrue(self.dc.is_obstructed.is_set())
        self.assertEqual(len(logs.output), 1)

    def test_put_cmd_latest_wins_and_stops_detection(self):
        self.dc.put_cmd("come")
        with self.assertLogs("predict_live", "WARNING"):
            self.dc.put_cmd("think")
        self.assertFalse(self.dc.at_work())
        self.assertEqual(self.dc.cmd_slot.take(0.0), "think")

    def test_zero_is_never_overtaken_by_stale_step(self):
        # шаг манёвра прочитан до защёлки, а публикуется медленно: ноль защёлки не должен уйти раньше него
        parent = FakeParent()
        publish = parent.publish

        def slow_publish(msg):
            if msg.linear.x or msg.angular.z:
                time.sleep(0.2)
            publish(msg)
        parent.publish = slow_publish
        dc = pl.DriveController(parent, pl.build_parser().parse_args([]))
        dc.process_lidar(tb3_scan())
        th = threading.Thread(target=dc.move_forward)
        th.start()
        time.sleep(0.05)
        with self.assertLogs("predict_live", "WARNING"):
            dc.estop(True)
        th.join(1.0)
        self.assertEqual(parent.since(0.0)[-1], (0.0, 0.0))

    def test_shutdown_sends_zero_and_blocks_further_motion(self):
        th = self.run_async("come")
        time.sleep(0.2)
        t = time.monotonic()
        self.dc.shutdown()
        th.join(1.0)
        self.dc.move_forward()
        self.assertEqual(set(self.parent.since(t)), {(0.0, 0.0)})
        self.dc.run("come")  # после shutdown манёвры не стартуют
        self.assertEqual(set(self.parent.since(t)), {(0.0, 0.0)})


@unittest.skipIf(pl is None, "нужны torch, rclpy и подставные модули стенда (ВМ sim)")
class States(unittest.TestCase):
    """on_motion_state и on_safety_state узла на подставном self с настоящим DriveController: срез вперёд по
    схеме motion_state из PLAN (blocked_front, без cut) и по safety_state blocked, защёлка по safety_state estop."""

    def setUp(self):
        self.parent = FakeParent()
        self.dc = pl.DriveController(self.parent, pl.build_parser().parse_args([]))
        self.dc.process_lidar(tb3_scan())
        node = SimpleNamespace(drive_controller=self.dc, _bad_topics=set(), states_t=0.0, executor_moving=False,
                               executor_state_t=0.0)
        node.parsed = lambda *a: pl.VelocityPublisher.parsed(node, *a)
        self.node = node

    def deliver(self, handler, doc):
        getattr(pl.VelocityPublisher, handler)(self.node, SimpleNamespace(data=json.dumps(doc)))

    def come(self):
        th = threading.Thread(target=self.dc.run, args=("come",))
        th.start()
        self.addCleanup(lambda: (self.dc.cancel.set(), th.join(10)))
        time.sleep(0.2)
        return th

    def test_plan_motion_state_blocked_front_cancels_forward(self):
        th = self.come()
        t = time.monotonic()
        with self.assertLogs("predict_live", "INFO"):
            self.deliver("on_motion_state", {"boot": "b", "moving": False, "task": None, "by": "rule", "reflex": None,
                                             "blocked_front": True, "last_gesture": None})
        th.join(1.0)
        self.assertFalse(th.is_alive(), "S3 не сработал на схеме PLAN без cut")
        self.assertEqual(set(self.parent.since(t)), {(0.0, 0.0)})
        self.assertGreater(self.node.states_t, 0.0)

    def test_safety_blocked_cancels_forward(self):
        th = self.come()
        self.deliver("on_safety_state", {"state": "slow", "reasons": [], "min_clearance_m": 0.6, "limits": {}})
        th.join(0.3)
        self.assertTrue(th.is_alive())  # slow — едем дальше, скорость урезает safety_gate
        with self.assertLogs("predict_live", "INFO") as logs:
            self.deliver("on_safety_state", {"state": "blocked", "reasons": ["низкий предмет"]})
        th.join(1.0)
        self.assertFalse(th.is_alive())
        self.assertIn("safety_gate: вперёд нельзя", logs.output[0])

    def test_safety_estop_latches_until_other_state(self):
        th = self.come()
        with self.assertLogs("predict_live", "WARNING"):
            self.deliver("on_safety_state", {"state": "estop", "reasons": ["estop: оператор"]})
        th.join(1.0)
        self.assertFalse(th.is_alive())
        self.assertTrue(self.dc.halted)
        with self.assertLogs("predict_live", "WARNING"):  # неразобранное состояние защёлку не снимает
            self.deliver("on_safety_state", {"state": "ESTOP"})
            self.deliver("on_safety_state", {"state": "released"})
        self.assertTrue(self.dc.halted)
        with self.assertLogs("predict_live", "INFO"):
            self.deliver("on_safety_state", {"state": "blocked", "reasons": ["датчики не готовы"]})
        self.assertFalse(self.dc.halted)


@unittest.skipIf(pl is None, "нужны torch, rclpy и подставные модули стенда (ВМ sim)")
class ExitCodes(unittest.TestCase):
    """main_loop и publish_health узла на подставном self: коды выхода 4, 5, 6 (на них опирается doctor),
    состояния perception_health и признак dark."""

    def loop_exit(self, error) -> int:
        def step():
            raise error

        node = SimpleNamespace(stop_event=threading.Event(), exit_code=None, opt=SimpleNamespace(max_errors=3),
                               step=step, beat=0.0)
        node.request_exit = lambda code: pl.VelocityPublisher.request_exit(node, code)
        pl.VelocityPublisher.main_loop(node)
        return node.exit_code

    def test_vision_loop_exit_codes(self):
        self.assertEqual((pl.EXIT_CAMERA, pl.EXIT_LOOP, pl.EXIT_BUSY), (4, 5, 6))
        with self.assertLogs("predict_live", "ERROR") as logs:
            self.assertEqual(self.loop_exit(pl.CameraFailed("лесенка исчерпана")), pl.EXIT_CAMERA)
            self.assertEqual(self.loop_exit(pl.CameraBusy("камера занята")), pl.EXIT_BUSY)
            self.assertEqual(self.loop_exit(RuntimeError("CUDA error")), pl.EXIT_LOOP)
        self.assertEqual(sum("ошибка цикла зрения" in m for m in logs.output), 3)  # max_errors подряд

    def health(self, ready=True, alive=True, level=0, dark=False, at_work=True, node=None):
        published, exits, pings = [], [], []
        node = node or SimpleNamespace(states_t=time.monotonic(), _states_silent=False)
        node.__dict__.update(
            camera=SimpleNamespace(frames=10, level=level, recoveries=2, dark=dark, frame_age=lambda: 0.05),
            _health_frames=0, _health_t=time.monotonic() - 1.0, ready=ready, beat=time.monotonic(), live_s=7.0,
            notifier=SimpleNamespace(watchdog=lambda: pings.append(1)),
            controller_thread=SimpleNamespace(is_alive=lambda: alive), request_exit=exits.append,
            at_work=lambda: at_work,
            health_pub=SimpleNamespace(publish=lambda msg: published.append(json.loads(msg.data))))
        pl.VelocityPublisher.publish_health(node)
        return published[0], exits, len(pings)

    def test_health_states(self):
        h, exits, pings = self.health(ready=False)
        self.assertEqual((h["state"], exits, pings), ("starting", [], 0))  # до READY — без WATCHDOG
        h, exits, pings = self.health()
        self.assertEqual(set(h), {"fps", "frame_age_s", "recoveries", "state", "dark"})
        self.assertEqual((h["state"], h["dark"], h["recoveries"], exits, pings), ("ok", False, 2, [], 1))
        self.assertAlmostEqual(h["fps"], 10.0, delta=0.5)
        self.assertEqual(self.health(level=1)[0]["state"], "recovering")
        self.assertEqual(self.health(at_work=False)[0]["state"], "paused")
        h = self.health(dark=True)[0]
        self.assertEqual((h["state"], h["dark"]), ("ok", True))  # темно — не отказ камеры

    def test_dead_controller_thread_exits_5(self):
        with self.assertLogs("predict_live", "ERROR"):
            _, exits, _ = self.health(alive=False)
        self.assertEqual(exits, [pl.EXIT_LOOP])

    def test_silent_guard_and_gate_warned_once_after_ready(self):
        node = SimpleNamespace(states_t=time.monotonic() - pl.STATE_SILENCE_S - 1, _states_silent=False)
        self.health(ready=False, node=node)
        self.assertFalse(node._states_silent)  # до READY — молчим
        with self.assertLogs("predict_live", "WARNING") as logs:
            self.health(node=node)
            self.health(node=node)
            self.assertIn("срез движения вперёд стражем (S3) и аварийная защёлка не видны", logs.output[0])
            self.assertEqual(len(logs.output), 1)
        node.states_t = time.monotonic()
        with self.assertLogs("predict_live", "INFO") as logs:
            self.health(node=node)
        self.assertIn("снова приходит", logs.output[0])


if __name__ == "__main__":
    unittest.main()
