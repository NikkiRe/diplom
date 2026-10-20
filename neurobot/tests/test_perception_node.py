"""predict_live v2 (--mode legacy) как процесс ROS 2 на подставных модулях стенда + страж: дымовой прогон.

Цепочка: подставной pyrealsense2 (10 к/с) → FakeYolo по таймлайну → настоящая SNN лаборатории (torch CPU,
weights_only=True) → DriveController → /cmd_vel_gesture (ремап) → motion_executor → /neurobot/cmd_vel_req. Тест сам
публикует /scan (URG-04LX с геометрией скана робота: 683 луча ±2,0923497677 рад). Проверяется: READY после
первого кадра, стоящая рука не даёт жеста; махание с предобработкой лаборатории — «не распознано» и покой (G1:
у демо здесь был бы turn180), с --polyline — класс 2 и поворот через стража, а шаг вперёд к препятствию в
0,45 м (дальше порога демо 0,25 м, ближе тормозной дальности стража при 0,5 м/с) страж срезает, и perception
отменяет остаток манёвра (S3, RS2); --ignore-cls — класс без действия; отказ камеры лечится без рестарта
процесса, а WATCHDOG не прерывается и на ступени hardware_reset (дольше WatchdogSec); тёмная сцена — не отказ
(признак dark, без лесенки); жест, оборванный застывшей камерой, отбрасывается; на SIGTERM последний Twist —
ноль; ошибки моделей и аргументов — коды выхода 3 и 2; без стража и safety_gate — предупреждение в журнале.
--camera ros: кадры публикует сам тест в /camera/color/image_raw (rgb8, 10 к/с, метка времени кадра для FakeYolo —
как у подставного pyrealsense2), /neurobot/safety_state — тоже тест (подставной safety_gate, 5 Гц): жест по кадрам
из темы, аварийная защёлка посреди манёвра — ноль сразу и после снятия манёвр не доезжает; издатель молчит, застыл
(тот же header.stamp), отдаёт чёрные кадры — recovering или dark без выхода и рестарта.

Нужны rclpy, torch, snntorch, cv2 и neurobot/sim/gestures (ВМ sim); иначе тесты пропускаются. Времена —
ориентир (стенд, подставной детектор), не результат. В общей ВМ под нагрузкой других процессов (load average
11–17 на 6 vCPU) старт с импортом torch занимал 28–31 с, распознавание SNN — 0,5–1,2 с вместо сотых [замер
15.10]: старт ждём до START_S, а верхние границы задержки проверяются, только пока ВМ не перегружена.
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PERCEPTION = ROOT / "neurobot" / "perception"
SHIM = ROOT / "neurobot" / "sim" / "gestures" / "shim"
SNN_WEIGHTS = ROOT / "third_party" / "neurochip" / "demo" / "demo-ros2_feature" / "models" / "snn_emnist_small.pth"
os.environ["ROS_DOMAIN_ID"] = os.environ.get("NEUROBOT_TEST_DOMAIN", "42")

try:
    import cv2  # noqa: F401
    import numpy as np
    import rclpy
    import snntorch  # noqa: F401
    import torch  # noqa: F401
    from geometry_msgs.msg import Twist
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import Image, LaserScan
    from std_msgs.msg import String

    from neurobot.sim.gestures.clock import encode_marker, mono
    MISSING = None if (SHIM / "pyrealsense2").is_dir() and SNN_WEIGHTS.is_file() else "нет подставных модулей стенда"
except ImportError as e:
    MISSING = "нет %s" % e.name

START_S = 60.0  # старт процесса до READY или до кода выхода

# Таймлайн FakeYolo, секунды от якоря (первый вызов детектора — прогрев до открытия камеры). Кадры идут сразу
# после открытия: hardware_reset при старте — только если конвейер не запустился.
STILL = (4.0, 6.0)   # рука стоит в кадре 2 с и уходит — не жест
WAVE = (8.0, 9.6)    # горизонтальное махание — класс 2 по зондированию R1


def overloaded() -> bool:
    """Нагрузка ВМ выше числа ядер: время распознавания не показательно."""
    return os.getloadavg()[0] > (os.cpu_count() or 1)


def timeline_records() -> list:
    recs = [{"t": 0.0, "bbox": None, "conf": 0.0, "seg": None}]

    def box(t, cx, cy, seg):
        recs.append({"t": round(t, 3), "bbox": [cx - 40, cy - 40, cx + 40, cy + 40], "conf": 0.9, "seg": seg})

    for i in range(int((STILL[1] - STILL[0]) * 10)):
        box(STILL[0] + i / 10, 320 + (3 if i % 2 else -3), 240, 1)
    recs.append({"t": STILL[1], "bbox": None, "conf": 0.0, "seg": None})
    for i in range(int((WAVE[1] - WAVE[0]) * 10)):
        box(WAVE[0] + i / 10, 320 + 150 * math.sin(2 * math.pi * i / 8), 240, 2)
    recs.append({"t": WAVE[1], "bbox": None, "conf": 0.0, "seg": None})
    return recs


def scan_msg(front: float) -> "LaserScan":
    """URG-04LX робота: стена 3 м, лучи ±10° вокруг оси — front."""
    msg = LaserScan()
    msg.angle_min, msg.angle_max = -2.0923497677, 2.0923497677
    msg.angle_increment = (msg.angle_max - msg.angle_min) / 682
    msg.range_min, msg.range_max = 0.02, 5.6
    ranges = [3.0] * 683
    for i in range(341 - 28, 341 + 29):
        ranges[i] = front
    msg.ranges = ranges
    return msg


class Recorder:
    def __init__(self):
        self.node = rclpy.create_node("nb_test_perception_%d" % os.getpid())
        self.lock = threading.Lock()
        self.msgs = {"gesture_cmd": [], "cmd": [], "gesture": [], "health": [], "state": []}
        sub = self.node.create_subscription
        sub(Twist, "/cmd_vel_gesture", lambda m: self._add("gesture_cmd", (m.linear.x, m.angular.z)), 10)
        # выход стража: с 16.10 он пишет желаемую скорость в /neurobot/cmd_vel_req (писатель /cmd_vel — safety_gate)
        sub(Twist, "/neurobot/cmd_vel_req", lambda m: self._add("cmd", (m.linear.x, m.angular.z)), 10)
        sub(String, "/neurobot/gesture", lambda m: self._add("gesture", json.loads(m.data)), 10)
        sub(String, "/neurobot/perception_health", lambda m: self._add("health", json.loads(m.data)), 10)
        sub(String, "/neurobot/motion_state", lambda m: self._add("state", json.loads(m.data)), 10)
        self.front = 3.0
        self.scan = self.node.create_publisher(LaserScan, "/scan", qos_profile_sensor_data)
        self.node.create_timer(0.1, lambda: self.scan.publish(scan_msg(self.front)))
        self.image_mode = None  # узел камеры: None — нет; ok | pause | freeze | black
        self.last_image = None
        self.image_pub = self.node.create_publisher(Image, "/camera/color/image_raw", qos_profile_sensor_data)
        self.node.create_timer(0.1, self._image)
        self.safety = None  # safety_gate: None — нет; иначе state для /neurobot/safety_state
        self.safety_pub = self.node.create_publisher(String, "/neurobot/safety_state", 10)
        self.node.create_timer(0.2, self._safety)
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self.executor.spin, daemon=True)
        self.thread.start()

    def _image(self):
        mode = self.image_mode
        if mode in (None, "pause"):
            return
        if mode == "freeze" and self.last_image is not None:
            self.image_pub.publish(self.last_image)  # застывший издатель: тот же кадр, тот же header.stamp
            return
        img = np.full((480, 640, 3), 0 if mode == "black" else 64, np.uint8)
        if mode != "black":
            encode_marker(img, mono())
        msg = Image()
        msg.header.stamp = self.node.get_clock().now().to_msg()
        msg.header.frame_id = "camera_color_optical_frame"
        msg.height, msg.width, msg.encoding, msg.step = 480, 640, "rgb8", 640 * 3
        msg.data = img.tobytes()
        self.last_image = msg
        self.image_pub.publish(msg)

    def _safety(self):
        if self.safety is not None:
            reasons = ["estop: тест"] if self.safety == "estop" else []
            self.safety_pub.publish(String(data=json.dumps({"state": self.safety, "reasons": reasons,
                                                            "min_clearance_m": None, "limits": {}})))

    def _add(self, key, value):
        with self.lock:
            self.msgs[key].append((time.time(), value))

    def get(self, key) -> list:
        with self.lock:
            return list(self.msgs[key])

    def wait(self, key, pred, timeout: float):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for rec in self.get(key):
                if pred(rec):
                    return rec
            time.sleep(0.02)
        return None

    def close(self):
        self.executor.shutdown()
        self.node.destroy_node()


@unittest.skipIf(MISSING, MISSING or "")
class PerceptionProcess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rclpy.init()

    @classmethod
    def tearDownClass(cls):
        rclpy.shutdown()

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        d = self.dir.name
        self.timeline = os.path.join(d, "timeline.jsonl")
        with open(self.timeline, "w") as fh:
            fh.write(json.dumps({"header": {"version": 1, "duration_s": 30.0, "det_fps": 10}}) + "\n")
            for rec in timeline_records():
                fh.write(json.dumps(rec) + "\n")
        self.control = os.path.join(d, "control.jsonl")
        open(self.control, "w").close()
        self.notify_path = os.path.join(d, "notify.sock")
        self.notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.notify.bind(self.notify_path)
        self.notify.settimeout(0.05)
        self.addCleanup(self.notify.close)
        pythonpath = [str(ROOT), str(SHIM)] + os.environ.get("PYTHONPATH", "").split(os.pathsep)
        self.env = dict(os.environ, PYTHONPATH=os.pathsep.join(pythonpath), PYTHONDONTWRITEBYTECODE="1",
                        NEUROBOT_GESTURES_TIMELINE=self.timeline,
                        NEUROBOT_GESTURES_ANCHOR=os.path.join(d, "anchor.json"),
                        NEUROBOT_FAKE_RS_FPS="10", NEUROBOT_FAKE_RS_CONTROL=self.control)

    def spawn(self, argv, name, notify=False) -> subprocess.Popen:
        env = dict(self.env)
        if notify:
            env.update(NOTIFY_SOCKET=self.notify_path, WATCHDOG_USEC="5000000")
        log = open(os.path.join(self.dir.name, name + ".log"), "wb")
        self.addCleanup(log.close)
        proc = subprocess.Popen([sys.executable] + argv, env=env, cwd=str(ROOT), stdout=log, stderr=log)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait(5)))
        return proc

    def perception(self, *args, notify=False) -> subprocess.Popen:
        return self.spawn([str(PERCEPTION / "predict_live.py"), "--yolo", "fake", "--snn-weights",
                           str(SNN_WEIGHTS)] + list(args) + ["--ros-args", "-r", "/cmd_vel:=/cmd_vel_gesture"],
                          "perception", notify)

    def timed_notifications(self, seconds: float) -> list:
        """[(монотонное время приёма, строка sd_notify)] за seconds секунд."""
        out = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                msg = self.notify.recv(64).decode()
            except socket.timeout:
                continue
            out.append((time.monotonic(), msg))
        return out

    def notifications(self, seconds: float) -> list:
        return [s for _, s in self.timed_notifications(seconds)]

    def wait_ready(self) -> None:
        end = time.monotonic() + START_S
        while time.monotonic() < end:
            try:
                if self.notify.recv(64).decode() == "READY=1":
                    return
            except socket.timeout:
                continue
        self.fail("нет READY=1 за %.0f с\n%s" % (START_S, self.log_text("perception")))

    def log_text(self, name: str) -> str:
        with open(os.path.join(self.dir.name, name + ".log"), encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def wave_gesture(self, rec: Recorder) -> dict:
        """Дождаться READY и единственного жеста (стоящая рука жеста не даёт); проверить поля и END_GAP."""
        self.wait_ready()
        gesture = rec.wait("gesture", lambda r: True, 20.0)
        self.assertIsNotNone(gesture, self.log_text("perception"))
        g = gesture[1]
        self.assertEqual(len(rec.get("gesture")), 1, "стоящая рука не должна давать жеста")
        self.assertEqual(set(g), {"boot", "seq", "cls", "name", "conf", "spikes", "margin", "side", "n_boxes",
                                  "end", "cut", "t_end", "t_rec"})
        self.assertGreaterEqual(g["n_boxes"], 10)
        # уверенный жест — решение спустя --early-gap 0,3 с (H10), иначе — END_GAP 0,5 с; рука в середине кадра
        gap = {"early": 0.3, "gap": 0.5}[g["end"]]
        self.assertEqual(g["cut"], 0.0)
        self.assertGreaterEqual(g["t_rec"] - g["t_end"], gap)
        if not overloaded():
            self.assertLess(g["t_rec"] - g["t_end"], gap + 0.3)  # + кадр 0,1 с + SNN [ориентир]
        return g

    def test_lab_preprocessing_wave_is_unknown_and_robot_stays(self):
        rec = Recorder()
        self.addCleanup(rec.close)
        self.perception(notify=True)
        g = self.wave_gesture(rec)
        self.assertEqual((g["cls"], g["name"], g["spikes"], g["end"]), (-1, "unknown", 0, "gap"))  # «немая» SNN, R1
        time.sleep(2.0)
        self.assertEqual({v for _, v in rec.get("gesture_cmd")}, {(0.0, 0.0)})  # демо дало бы turn180 (G1)
        # стража и safety_gate здесь нет — срез вперёд и защёлку perception не видит и говорит об этом
        self.assertIn("аварийная защёлка не видны", self.log_text("perception"))

    def test_ignored_class_does_not_move(self):
        rec = Recorder()
        self.addCleanup(rec.close)
        self.perception("--polyline", "--ignore-cls", "2", notify=True)
        g = self.wave_gesture(rec)
        self.assertEqual((g["cls"], g["name"]), (2, "none"))
        time.sleep(2.0)
        self.assertEqual({v for _, v in rec.get("gesture_cmd")}, {(0.0, 0.0)})

    def test_wave_drives_through_guard_and_survives_camera_stall(self):
        rec = Recorder()
        self.addCleanup(rec.close)
        rec.front = 0.45
        # safety_gate здесь нет: страж — по правилам v1 (с 16.10 без safety_gate задачи по умолчанию не исполняются)
        guard = self.spawn(["-m", "neurobot.executor", "--ros-args", "-p", "require_safety:=false"], "executor")
        # срез здесь — дело стража: своя отсечка perception по возрасту скана (её проверяет test_perception_drive)
        # отодвинута, чтобы задержки планирования в нагруженной ВМ не отменили шаг вперёд раньше стража
        proc = self.perception("--polyline", "--scan-timeout", "2.0", notify=True)
        g = self.wave_gesture(rec)
        self.assertEqual((g["cls"], g["name"]), (2, "go_" + g["side"]))
        moved = rec.wait("cmd", lambda r: r[0] >= g["t_end"] and r[1] != (0.0, 0.0), 5.0)
        self.assertIsNotNone(moved, self.log_text("executor"))
        if not overloaded():
            self.assertLess(moved[0] - g["t_end"], 1.0)  # [ориентир]

        # go_*: поворот 3 с, затем шаг вперёд к препятствию в 0,45 м — страж срезает, манёвр отменяется (S3)
        cut = rec.wait("state", lambda r: r[1]["cut"] and r[1]["by"] == "gesture", 6.0)
        self.assertIsNotNone(cut, self.log_text("executor") + self.log_text("perception"))
        self.assertEqual(cut[1]["front"], "obstacle")
        end = rec.wait("gesture_cmd", lambda r: r[0] > cut[0] and r[1] == (0.0, 0.0), 1.0)
        self.assertIsNotNone(end, self.log_text("perception"))
        self.assertLess(end[0] - moved[0], 4.0)  # весь манёвр go_* — 8 с
        self.assertIn("манёвр отменяется", self.log_text("perception"))
        self.assertFalse([v for _, v in rec.get("cmd") if v[0] > 0])  # вперёд робот не ехал
        rec.front = 3.0

        with open(self.control, "a") as fh:  # отказ камеры: кадры не идут до рестарта конвейера
            fh.write(json.dumps({"kind": "stall"}) + "\n")
        healed = rec.wait("health", lambda r: r[1]["recoveries"] >= 1 and r[1]["state"] != "recovering", 10.0)
        self.assertIsNotNone(healed, self.log_text("perception"))
        self.assertIsNone(proc.poll(), "процесс не должен перезапускаться")
        states = {h["state"] for _, h in rec.get("health")}
        self.assertTrue({"ok", "paused", "recovering"} <= states, states)  # paused — детекция стоит в манёвре (L6)
        self.assertTrue(any(8.0 <= h["fps"] <= 11.0 for _, h in rec.get("health")))
        self.assertIn("WATCHDOG=1", self.notifications(1.5))

        recoveries = rec.get("health")[-1][1]["recoveries"]
        with open(self.control, "a") as fh:  # темно: живая камера отдаёт чёрные кадры — не отказ, лесенки нет
            fh.write(json.dumps({"kind": "black", "duration_s": 2.0}) + "\n")
        dark = rec.wait("health", lambda r: r[1]["dark"], 5.0)
        self.assertIsNotNone(dark, self.log_text("perception"))
        self.assertEqual((dark[1]["state"], dark[1]["recoveries"]), ("ok", recoveries))
        light = rec.wait("health", lambda r: r[0] > dark[0] and not r[1]["dark"], 5.0)
        self.assertIsNotNone(light, self.log_text("perception"))
        self.assertEqual(light[1]["recoveries"], recoveries)
        self.assertIn("кадр тёмный", self.log_text("perception"))

        t_term = time.time()
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(15), 0, self.log_text("perception"))
        time.sleep(0.8)
        last = [v for t, v in rec.get("gesture_cmd") if t >= t_term - 1.0]
        self.assertTrue(last and last[-1] == (0.0, 0.0), last[-5:])
        cmd = rec.get("cmd")
        self.assertEqual(cmd[-1][1], (0.0, 0.0))  # страж: вход жестов замолчал — явный ноль

        guard.send_signal(signal.SIGTERM)
        self.assertEqual(guard.wait(5), 0)

    def test_camera_topic_estop_and_publisher_faults(self):
        rec = Recorder()
        self.addCleanup(rec.close)
        rec.image_mode, rec.safety = "ok", "ok"
        guard = self.spawn(["-m", "neurobot.executor"], "executor")
        proc = self.perception("--camera", "ros", "--polyline", "--scan-timeout", "2.0", notify=True)
        g = self.wave_gesture(rec)
        self.assertEqual((g["cls"], g["name"]), (2, "go_" + g["side"]))
        moved = rec.wait("gesture_cmd", lambda r: r[0] >= g["t_end"] and r[1] != (0.0, 0.0), 5.0)
        self.assertIsNotNone(moved, self.log_text("perception"))

        # защёлка посреди поворота go_* (поворот 3 с, вперёд 2 с, поворот 3 с): ноль сразу, после снятия манёвр
        # не доезжает — робот стоит до нового жеста
        time.sleep(0.5)
        t_engage = time.time()
        rec.safety = "estop"
        zero = rec.wait("gesture_cmd", lambda r: r[0] > t_engage and r[1] == (0.0, 0.0), 2.0)
        self.assertIsNotNone(zero, self.log_text("perception"))
        if not overloaded():
            self.assertLess(zero[0] - t_engage, 0.2 + 0.2)  # период safety_state + доставка [ориентир]
        paused = rec.wait("health", lambda r: r[0] > zero[0] and r[1]["state"] == "paused", 3.0)
        self.assertIsNotNone(paused)  # детекция стоит, пока защёлка
        time.sleep(1.0)
        rec.safety = "ok"
        time.sleep(8.0)  # без защёлки go_* шёл бы ещё ≈6 с
        self.assertEqual({v for t, v in rec.get("gesture_cmd") if t > zero[0]}, {(0.0, 0.0)})
        log = self.log_text("perception")
        self.assertIn("аварийная остановка (estop: тест)", log)
        self.assertIn("аварийная остановка снята", log)
        self.assertNotIn("аварийная защёлка не видны", log)  # состояния стража и safety_gate приходят

        # издатель камеры молчит, застыл (тот же stamp), отдаёт чёрные кадры: устройство не наше — без выхода
        for mode, seconds in (("pause", 4.0), ("freeze", 4.0)):
            before = rec.get("health")[-1][1]["recoveries"]
            t_fault = time.time()
            rec.image_mode = mode
            stalled = rec.wait("health", lambda r: r[0] > t_fault and r[1]["state"] == "recovering", seconds + 2)
            self.assertIsNotNone(stalled, mode + "\n" + self.log_text("perception"))
            time.sleep(max(0.0, seconds - (time.time() - t_fault)))
            rec.image_mode = "ok"
            healed = rec.wait("health", lambda r: r[1]["recoveries"] > before and r[1]["state"] == "ok", 5.0)
            self.assertIsNotNone(healed, mode + "\n" + self.log_text("perception"))
            self.assertIsNone(proc.poll(), "процесс не должен выходить: камерой владеет другой узел")
        self.assertIn("ждём издателя", self.log_text("perception"))
        self.assertNotIn("hardware_reset", self.log_text("perception"))
        recoveries = rec.get("health")[-1][1]["recoveries"]
        rec.image_mode = "black"
        dark = rec.wait("health", lambda r: r[1]["dark"], 5.0)
        self.assertIsNotNone(dark, self.log_text("perception"))
        self.assertEqual((dark[1]["state"], dark[1]["recoveries"]), ("ok", recoveries))
        rec.image_mode = "ok"
        self.assertIn("WATCHDOG=1", self.notifications(1.5))

        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(15), 0, self.log_text("perception"))
        guard.send_signal(signal.SIGTERM)
        self.assertEqual(guard.wait(5), 0)

    def test_watchdog_kept_through_recovery_ladder(self):
        # «Зависшее устройство» лечится только hardware_reset: кадров нет ≈5,5 с (ступени по 1 с + пауза 3 с) —
        # дольше WatchdogSec 5 с. Сторож systemd не должен убить процесс посреди лесенки (R1): WATCHDOG идёт,
        # пока цикл «зрения» жив, а не только при новых кадрах.
        rec = Recorder()
        self.addCleanup(rec.close)
        proc = self.perception("--stall-step-s", "1", "--reset-pause-s", "3", notify=True)
        self.wait_ready()
        with open(self.control, "a") as fh:
            fh.write(json.dumps({"kind": "gone"}) + "\n")
        t_fault = time.monotonic()
        pings = [t for t, s in self.timed_notifications(12.0) if s == "WATCHDOG=1"]
        healed = rec.wait("health", lambda r: r[1]["recoveries"] >= 1 and r[1]["state"] != "recovering", 1.0)
        self.assertIsNotNone(healed, self.log_text("perception"))
        self.assertIn("— hardware_reset и пауза", self.log_text("perception"))  # ступень 2, а не сброс при старте
        gaps = [b - a for a, b in zip([t_fault] + pings, pings + [time.monotonic()])]
        self.assertLess(max(gaps), 4.0, gaps)  # при WATCHDOG «только по кадрам» был бы разрыв ≥ 5,5 с
        self.assertIsNone(proc.poll())

    def test_gesture_cut_by_camera_fault_is_dropped_not_late(self):
        # камера застывает сразу после махания на 3 с (тот же кадр с тем же номером): END_GAP без кадров не
        # подтвердить — эпизод отбрасывается; иначе жест распознался бы, когда кадры вернутся, и робот поехал бы
        # через секунды после жеста
        faults = os.path.join(self.dir.name, "faults.json")
        with open(faults, "w") as fh:
            json.dump([{"at_s": WAVE[1], "kind": "freeze", "duration_s": 3.0}], fh)
        self.env["NEUROBOT_FAKE_RS_FAULTS"] = faults
        rec = Recorder()
        self.addCleanup(rec.close)
        proc = self.perception("--polyline", notify=True)
        self.wait_ready()
        dropped = None
        end = time.monotonic() + 20.0
        while dropped is None and time.monotonic() < end:
            time.sleep(0.2)
            if "эпизод жеста отброшен" in self.log_text("perception"):
                dropped = time.monotonic()
        self.assertIsNotNone(dropped, self.log_text("perception"))
        time.sleep(4.0)  # кадры вернулись — жест не всплывает
        self.assertEqual(rec.get("gesture"), [])
        self.assertEqual({v for _, v in rec.get("gesture_cmd")}, {(0.0, 0.0)})
        self.assertIsNone(proc.poll())

    def test_bad_snn_weights_exit_3(self):
        proc = self.perception("--snn-weights", os.path.join(self.dir.name, "missing.pth"))
        self.assertEqual(proc.wait(START_S), 3)
        self.assertIn("SNN: не удалось загрузить", self.log_text("perception"))

    def test_bad_args_exit_2(self):
        proc = self.perception("--snn-votes", "0")
        self.assertEqual(proc.wait(START_S), 2)


if __name__ == "__main__":
    unittest.main()
