"""sd_notify, постоянный исполнитель (H6), сектор лидара, ящик «последняя побеждает», разбор motion_state и
safety_state (perception/node_util.py, executor/sd_notify.py, executor/spinner.py, executor/guard.py).

Копии в node_util (патч лаборатории самодостаточен) проверяются теми же тестами, что и оригиналы стража.
"""
from __future__ import annotations

import json
import math
import os
import random
import socket
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
PERCEPTION = ROOT / "neurobot" / "perception"
for p in (str(ROOT), str(PERCEPTION)):
    if p not in sys.path:
        sys.path.insert(0, p)

import node_util  # noqa: E402
from neurobot.executor import guard, sd_notify, spinner  # noqa: E402

NOTIFIERS = (node_util.Notifier, sd_notify.Notifier)
SPINNERS = (node_util.Spinner, spinner.Spinner)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class NotifierTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "n.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.path)
        self.sock.settimeout(1.0)

    def tearDown(self):
        self.sock.close()
        self.dir.cleanup()

    def received(self) -> list:
        out = []
        self.sock.setblocking(False)
        try:
            while True:
                out.append(self.sock.recv(256).decode())
        except BlockingIOError:
            pass
        return out

    def test_ready_stopping_and_throttled_watchdog(self):
        for cls in NOTIFIERS:
            clock = Clock()
            n = cls({"NOTIFY_SOCKET": self.path, "WATCHDOG_USEC": "3000000"}, clock=clock)
            self.assertEqual(n.watchdog_s, 3.0)
            self.assertTrue(n.ready())
            for t in (0.0, 0.5, 0.7, 0.75, 1.0, 1.6):  # не чаще четверти периода (0,75 с)
                clock.t = t
                n.watchdog()
            self.assertTrue(n.stopping())
            self.assertEqual(self.received(), ["READY=1", "WATCHDOG=1", "WATCHDOG=1", "WATCHDOG=1", "STOPPING=1"],
                             cls.__module__)

    def test_without_systemd_nothing_is_sent(self):
        for cls in NOTIFIERS:
            n = cls({})
            self.assertFalse(n.ready())
            self.assertFalse(n.watchdog())
            n = cls({"NOTIFY_SOCKET": self.path})
            self.assertFalse(n.watchdog())  # нет WATCHDOG_USEC
            n = cls({"NOTIFY_SOCKET": self.path, "WATCHDOG_USEC": "1000000", "WATCHDOG_PID": "1"})
            self.assertFalse(n.watchdog())  # сторож назначен другому процессу
        self.assertEqual(self.received(), [])

    def test_send_error_is_logged_not_raised(self):
        for cls in NOTIFIERS:
            n = cls({"NOTIFY_SOCKET": os.path.join(self.dir.name, "missing.sock")})
            with self.assertLogs(cls.__module__, "WARNING"):
                self.assertFalse(n.ready())

    @unittest.skipUnless(sys.platform.startswith("linux"), "абстрактные сокеты есть только в Linux")
    def test_abstract_socket(self):
        name = "nb-test-%d" % os.getpid()
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.bind("\0" + name)
            for cls in NOTIFIERS:
                self.assertTrue(cls({"NOTIFY_SOCKET": "@" + name}).ready())
                self.assertEqual(s.recv(64), b"READY=1")


class FakeExecutor:
    """SingleThreadedExecutor без rclpy: spin_once ждёт события узла (колбэк или guard condition)."""

    def __init__(self):
        self.node = None
        self.timeouts = []
        self.shutdowns = 0

    def add_node(self, node):
        self.node = node

    def spin_once(self, timeout_sec=None):
        self.timeouts.append(timeout_sec)
        self.node.event.wait()
        self.node.event.clear()
        if self.node.boom is not None:
            raise self.node.boom

    def shutdown(self):
        self.shutdowns += 1


class FakeNode:
    def __init__(self):
        self.event = threading.Event()
        self.boom = None

    def create_guard_condition(self, callback):
        return types.SimpleNamespace(trigger=self.event.set)


class SpinnerTest(unittest.TestCase):
    """H6: один исполнитель на узел, ожидание без таймаута, выход по stop и по упавшему колбэку (оригинал и копия)."""

    def run_spinner(self, cls, script):
        ex = FakeExecutor()
        rclpy = types.SimpleNamespace(ok=lambda: True)
        executors = types.SimpleNamespace(SingleThreadedExecutor=lambda: ex)
        with mock.patch.dict(sys.modules, {"rclpy": rclpy, "rclpy.executors": executors}):
            node, stop = FakeNode(), threading.Event()
            sp = cls(node, stop)
            script(node, stop)
            return sp, ex, stop

    def test_stop_by_signal(self):
        for cls in SPINNERS:
            def script(node, stop):
                for _ in range(3):  # колбэки идут
                    node.event.set()
                threading.Timer(0.05, stop.set).start()  # обработчик сигнала только ставит stop
            sp, ex, stop = self.run_spinner(cls, script)
            sp.wait()
            self.assertTrue(stop.is_set())
            self.assertEqual(set(ex.timeouts), {None}, cls)  # без rcl-таймера на каждое ожидание
            sp.close()
            self.assertEqual(ex.shutdowns, 1, cls)

    def test_callback_error_reaches_main_thread(self):
        for cls in SPINNERS:
            def script(node, stop):
                node.boom = RuntimeError("колбэк упал")
                node.event.set()
            sp, ex, stop = self.run_spinner(cls, script)
            with self.assertRaises(RuntimeError):
                sp.wait()
            self.assertTrue(stop.is_set())
            self.assertEqual(ex.shutdowns, 1, cls)

    def test_copy_matches_original(self):
        import inspect
        for name in ("__init__", "_run", "wait", "close"):
            self.assertEqual(inspect.getsource(getattr(node_util.Spinner, name)),
                             inspect.getsource(getattr(spinner.Spinner, name)), name)


class SectorCopy(unittest.TestCase):
    def test_same_as_guard(self):
        rnd = random.Random(20261014)
        specials = (math.nan, math.inf, -math.inf, 0.007)
        for _ in range(200):
            n = rnd.choice((360, 683, 720))
            angle_min = rnd.uniform(-math.pi, 0)
            inc = rnd.uniform(0.002, 2 * math.pi / n)
            ranges = [rnd.choice(specials) if rnd.random() < 0.1 else rnd.uniform(0, 6) for _ in range(n)]
            start = rnd.randrange(n)
            for i in range(start, min(n, start + rnd.randrange(40))):  # сплошной провал
                ranges[i] = math.nan
            half = math.radians(rnd.uniform(5, 90))
            args = (ranges, angle_min, inc, half, 0.02, 5.6)
            self.assertEqual(node_util.front_scan(*args), guard.front_scan(*args))


class MotionStateTest(unittest.TestCase):
    def test_parse(self):
        parse = node_util.parse_motion_state
        self.assertEqual(parse('{"moving": true, "by": "agent", "cut": false}'), (True, False))
        self.assertEqual(parse('{"moving": false, "by": "gesture", "cut": true}'), (False, True))
        self.assertEqual(parse('{"moving": false, "by": "agent", "cut": true}'), (False, False))  # срезан агент
        self.assertEqual(parse('{"moving": false}'), (False, False))  # без cut и blocked_front
        for bad in ("", "[1]", '"x"', "{}", "{\"moving\": 1", '{"moving": 1}', '{"moving": "false"}',
                    '{"moving": false, "by": "gesture", "cut": "true"}',  # строка "false" истинна — тип проверяется
                    '{"moving": true, "blocked_front": 1}'):
            with self.assertRaises(ValueError, msg=bad):
                parse(bad)

    def test_plan_schema_blocked_front(self):
        # схема PLAN §2.3 (исполнитель v2): поля cut нет, запрет вперёд — blocked_front, от кого бы ни была задача
        parse = node_util.parse_motion_state
        doc = {"boot": "b", "moving": True, "task": "come", "by": "rule", "reflex": None, "blocked_front": True,
               "last_gesture": 3}
        self.assertEqual(parse(json.dumps(doc)), (True, True))
        self.assertEqual(parse(json.dumps(dict(doc, blocked_front=False))), (True, False))
        # страж v1 шлёт оба поля
        self.assertEqual(parse('{"moving": false, "by": "agent", "cut": true, "blocked_front": true}'), (False, True))


class SafetyStateTest(unittest.TestCase):
    def test_parse(self):
        parse = node_util.parse_safety_state
        doc = {"state": "estop", "reasons": ["estop: кнопка"], "min_clearance_m": None, "limits": {"v_max": 0.25}}
        self.assertEqual(parse(json.dumps(doc)), ("estop", ("estop: кнопка",)))
        for state in node_util.SAFETY_STATES:
            self.assertEqual(parse(json.dumps({"state": state})).state, state)
        # причины — только для журнала: формат не держит защёлку
        self.assertEqual(parse('{"state": "blocked", "reasons": [{"id": "cliff"}, 2]}').reasons,
                         ("{'id': 'cliff'}", "2"))
        self.assertEqual(parse('{"state": "blocked", "reasons": "cliff"}').reasons, ())
        for bad in ("", "[1]", "{}", '{"state": "ESTOP"}', '{"state": "fault"}', '{"state": 3}', '{"state": ["estop"]}',
                    '{"reasons": []}'):
            with self.assertRaises(ValueError, msg=bad):
                parse(bad)


class LatestSlotTest(unittest.TestCase):
    def test_latest_wins(self):
        slot = node_util.LatestSlot()
        self.assertFalse(slot.put("come"))
        self.assertTrue(slot.put("turn180"))  # непрочитанная команда вытеснена, а не исключение (R7)
        self.assertEqual(slot.take(0.0), "turn180")
        self.assertIsNone(slot.take(0.01))

    def test_take_wakes_on_put(self):
        slot = node_util.LatestSlot()
        got = []
        t = threading.Thread(target=lambda: got.append(slot.take(5.0)))
        t.start()
        slot.put("think")
        t.join(1.0)
        self.assertEqual(got, ["think"])


if __name__ == "__main__":
    unittest.main()
