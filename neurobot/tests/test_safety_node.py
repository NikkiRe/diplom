"""safety_gate и исполнитель как процессы ROS 2: явный ноль на старте и на выходе (сигнал, исключение), READY и
WATCHDOG, блокировка старта, датчики и пределы, защёлка (тема, файл, рестарт; снятие — только файлом каталога
release_dir), стоп агента (ноль сразу, без защёлки), копия состояния для агента, чужой писатель /cmd_vel, второй
издатель /scan (в том числе с пустыми сканами под именем драйвера), ослепший лидар (lidar_fault), параметры, цепочка
исполнитель → safety_gate (task_end при защёлке; новый процесс исполнителя не продолжает манёвр жеста, идущий с его
старта).

Нужен rclpy (ВМ sim, Jazzy; на роботе — Foxy): без него тесты пропускаются. Тестовый узел подаёт датчики, как у
двойника: скан URG с блоком NaN RS3, одометрию (повторяет /cmd_vel), IMU, глубину 424×240 (ровный пол,
трассировщик test_safety_parts), /tf_static; выход twist_mux повторяет /cmd_vel. Домен DDS — NEUROBOT_TEST_DOMAIN
(по умолчанию 47). Времена — ориентир, не результат.
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
os.environ["ROS_DOMAIN_ID"] = os.environ.get("NEUROBOT_TEST_DOMAIN", "47")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_safety_parts import CAM, CAMERA, OPTICAL_Q, render  # noqa: E402

SPOOF = ROOT / "neurobot" / "sim" / "suites" / "scan_spoof.py"

try:
    import rclpy
    from geometry_msgs.msg import TransformStamped, Twist
    from nav_msgs.msg import Odometry
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
    from sensor_msgs.msg import CameraInfo, Image, Imu, LaserScan
    from std_msgs.msg import String
    from tf2_msgs.msg import TFMessage
except ImportError:
    rclpy = None


CRASH_WHILE_MOVING = """
import sys
from neurobot.safety import gate, node

tick = gate.Gate.tick
moving = [0]

def crashing(self, now):
    d = tick(self, now)
    moving[0] += d.v != 0.0
    if moving[0] == 10:
        raise RuntimeError("проверка: исключение в решении")
    return d

gate.Gate.tick = crashing
sys.exit(node.main())
"""


def scan_msg() -> "LaserScan":
    msg = LaserScan()
    msg.header.frame_id = "laser_frame"
    msg.angle_min, msg.angle_max = -2.0923497677, 2.0923497677
    msg.angle_increment = (msg.angle_max - msg.angle_min) / 682
    msg.range_min, msg.range_max = 0.02, 5.6
    ranges = [3.0] * 683
    ranges[168:354] = [math.nan] * 186
    msg.ranges = ranges
    return msg


def tf_static() -> "TFMessage":
    out = []
    for child, xyz, q in (("laser_frame", (-0.015, 0.0, 0.67), (0.0, 0.0, 0.0, 1.0)),
                          ("camera_depth_optical_frame", tuple(CAM), OPTICAL_Q),
                          ("camera_imu_optical_frame", tuple(CAM), OPTICAL_Q)):
        t = TransformStamped()
        t.header.frame_id, t.child_frame_id = "base_link", child
        (t.transform.translation.x, t.transform.translation.y, t.transform.translation.z) = map(float, xyz)
        (t.transform.rotation.x, t.transform.rotation.y, t.transform.rotation.z, t.transform.rotation.w) = map(float, q)
        out.append(t)
    return TFMessage(transforms=out)


def depth_msgs():
    info = CameraInfo()
    info.header.frame_id = "camera_depth_optical_frame"
    info.width, info.height = CAMERA.width, CAMERA.height
    info.k = [CAMERA.fx, 0.0, CAMERA.cx, 0.0, CAMERA.fy, CAMERA.cy, 0.0, 0.0, 1.0]
    img = Image()
    img.header.frame_id = info.header.frame_id
    img.width, img.height, img.encoding, img.step = CAMERA.width, CAMERA.height, "16UC1", CAMERA.width * 2
    img.data = render().tobytes()
    return info, img


class Harness:
    """Процесс (safety_gate или исполнитель) + тестовый узел с датчиками в своём потоке."""

    def __init__(self, args=(), module="neurobot.safety", code=None, state_dir=None, sensors=True, req=None):
        self.dir = tempfile.TemporaryDirectory()
        self.state_dir = state_dir or self.dir.name
        # каталог снятия защёлки: в проде — root; тест от своего пользователя (release_uid)
        self.release_dir = os.path.join(self.state_dir, "release")
        os.makedirs(self.release_dir, mode=0o755, exist_ok=True)
        if module == "neurobot.safety" or code is not None:
            # у каждого Harness свой издатель /scan, и в тестах с двумя процессами их два: стоп по второму издателю —
            # только в своём тесте (scan_dup_policy block явно)
            args = ("-p", "release_dir:=" + self.release_dir, "-p", "release_uid:=%d" % os.getuid(),
                    "-p", "scan_dup_policy:=warn") + tuple(args)
        self.notify_path = os.path.join(self.dir.name, "notify.sock")
        self.notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.notify.bind(self.notify_path)
        self.notify.settimeout(0.05)
        self.node = rclpy.create_node("nb_test_safety_%d_%d" % (os.getpid(), id(self) % 10000))
        self.lock = threading.Lock()
        self.cmd, self.state, self.events, self.motion = [], [], [], []
        self.sensors, self.req, self.last = sensors, req, (0.0, 0.0)
        n = self.node
        n.create_subscription(Twist, "/cmd_vel", self._on_cmd, 10)
        for topic, where in (("/neurobot/safety_state", self.state), ("/neurobot/events", self.events),
                             ("/neurobot/motion_state", self.motion)):
            n.create_subscription(String, topic, lambda m, where=where: self._log(where, json.loads(m.data)), 10)
        latched = QoSProfile(depth=10, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                             reliability=ReliabilityPolicy.RELIABLE)
        self.req_pub = n.create_publisher(Twist, "/neurobot/cmd_vel_req", 10)
        self.gesture_pub = n.create_publisher(Twist, "/cmd_vel_gesture", 10)
        self.estop_pub = n.create_publisher(String, "/neurobot/estop", latched)  # как CLI neurobot estop
        self.estop_plain = n.create_publisher(String, "/neurobot/estop", 10)    # как любой узел (QoS по умолчанию)
        self.stop_pub = n.create_publisher(String, "/neurobot/stop_now", 10)   # стоп агента (MCP через rosbridge)
        self.mux_pub = n.create_publisher(Twist, "/diff_cont/cmd_vel_unstamped", 10)
        sd = qos_profile_sensor_data
        self.pubs = {name: n.create_publisher(cls, name, sd) for name, cls in (
            ("/scan", LaserScan), ("/odom", Odometry), ("/imu/data", Imu), ("/camera/imu", Imu),
            ("/camera/depth/camera_info", CameraInfo), ("/camera/depth/image_rect_raw", Image))}
        n.create_publisher(TFMessage, "/tf_static", latched).publish(tf_static())
        self.info, self.image = depth_msgs()
        self.scan = scan_msg()
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(n)
        self.stop = threading.Event()
        self.threads = [threading.Thread(target=self.executor.spin, daemon=True),
                        threading.Thread(target=self._feed, daemon=True)]
        for t in self.threads:
            t.start()
        env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT), os.environ.get("PYTHONPATH", ""))),
                   PYTHONDONTWRITEBYTECODE="1", NOTIFY_SOCKET=self.notify_path, WATCHDOG_USEC="3000000",
                   STATE_DIRECTORY=self.state_dir)
        self.log_path = os.path.join(self.dir.name, "stderr.log")
        entry = ["-m", module] if code is None else ["-c", code]
        with open(self.log_path, "wb") as log:
            self.proc = subprocess.Popen([sys.executable, *entry, "--ros-args", *args],
                                         env=env, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=log)

    def _log(self, where: list, item) -> None:
        with self.lock:
            where.append((time.monotonic(), item))

    def _on_cmd(self, msg) -> None:
        self.last = (msg.linear.x, msg.angular.z)
        self.mux_pub.publish(msg)  # twist_mux: наш вход единственный — выход повторяет его
        self._log(self.cmd, self.last)

    def _feed(self) -> None:
        """Датчики: IMU 100 Гц, одометрия 50 Гц (повторяет /cmd_vel), скан и глубина 10 Гц, запрос 20 Гц."""
        k, x, yaw = 0, 0.0, 0.0
        while not self.stop.is_set():
            if self.sensors:
                stamp = self.node.get_clock().now().to_msg()
                imu = Imu()
                imu.header.frame_id, imu.header.stamp = "camera_imu_optical_frame", stamp
                imu.orientation.x, imu.orientation.y, imu.orientation.z, imu.orientation.w = OPTICAL_Q
                imu.linear_acceleration.y = -9.81  # кадр IMU: y вниз
                self.pubs["/imu/data"].publish(imu)
                self.pubs["/camera/imu"].publish(imu)
                if k % 2 == 0:
                    v, w = self.last
                    x, yaw = x + v * 0.02 * math.cos(yaw), yaw + w * 0.02
                    odom = Odometry()
                    odom.pose.pose.position.x = x
                    odom.pose.pose.orientation.z, odom.pose.pose.orientation.w = math.sin(yaw / 2), math.cos(yaw / 2)
                    odom.twist.twist.linear.x, odom.twist.twist.angular.z = v, w
                    self.pubs["/odom"].publish(odom)
                if k % 10 == 0:
                    self.scan.header.stamp = stamp
                    self.pubs["/scan"].publish(self.scan)
                    self.pubs["/camera/depth/camera_info"].publish(self.info)
                    self.pubs["/camera/depth/image_rect_raw"].publish(self.image)
            if self.req is not None and k % 5 == 0:
                msg = Twist()
                msg.linear.x, msg.angular.z = map(float, self.req)
                self.req_pub.publish(msg)
            k += 1
            time.sleep(0.01)

    def estop(self, cmd: str, by: str = "human", t=None) -> None:
        """Человек — как CLI (transient_local), остальные — QoS по умолчанию (volatile)."""
        doc = {"cmd": cmd, "by": by, "reason": "проверка", "id": "%s-%f" % (cmd, time.time()),
               "t": time.time() if t is None else t}
        (self.estop_pub if by == "human" else self.estop_plain).publish(String(data=json.dumps(doc)))

    def stop_now(self, id_: str) -> None:
        """Стоп агента, как robot_stop патча ros_mcp_server."""
        doc = {"id": id_, "by": "agent", "reason": "проверка", "t": time.time()}
        self.stop_pub.publish(String(data=json.dumps(doc)))

    def release_file(self, state: dict) -> None:
        """Снятие, как sudo neurobot estop release: release.json с защёлкой, которую видел человек."""
        latch = state["estop"]
        doc = {"id": "rel-%f" % time.time(), "t": time.time(), "since": latch["since"], "reasons": latch["reasons"],
               "reason": "проверка"}
        tmp = os.path.join(self.release_dir, ".release.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(self.release_dir, "release.json"))

    def notifications(self, seconds: float) -> list:
        out, end = [], time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                out.append(self.notify.recv(64).decode())
            except socket.timeout:
                pass
        return out

    def wait(self, where: list, pred, timeout: float, since: float = 0.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                for t, item in where:
                    if t >= since and pred(item):
                        return t, item
            time.sleep(0.01)
        return None

    def stderr(self) -> str:
        with open(self.log_path, "rb") as f:
            return f.read().decode(errors="replace")

    def close(self) -> None:
        self.stop.set()
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(10)
        self.executor.shutdown()
        self.node.destroy_node()
        self.notify.close()
        self.dir.cleanup()


@unittest.skipIf(rclpy is None, "нужен rclpy (ВМ sim: source /opt/ros/jazzy/setup.bash)")
class SafetyProcess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rclpy.init()

    @classmethod
    def tearDownClass(cls):
        rclpy.shutdown()

    def start(self, *args, **kw) -> Harness:
        h = Harness(args, **kw)
        self.addCleanup(h.close)
        self.assertIn("READY=1", h.notifications(30.0), h.stderr()[-2000:] if h.proc.poll() is not None else "")
        return h

    def armed(self, h: Harness) -> None:
        got = h.wait(h.state, lambda s: s["state"] in ("ok", "slow"), 15.0)
        self.assertIsNotNone(got, h.stderr()[-2000:])

    def test_zero_first_and_start_interlock(self):
        h = self.start(sensors=False, req=(0.1, 0.3))
        first = h.wait(h.cmd, lambda c: True, 3.0)
        self.assertEqual(first[1], (0.0, 0.0))
        time.sleep(1.5)
        with h.lock:
            self.assertTrue(all(c == (0.0, 0.0) for _, c in h.cmd))
            last = h.state[-1][1]
        self.assertEqual(last["state"], "blocked")
        self.assertEqual(last["limits"], {"v_max": 0.0, "v_rev": 0.0, "w_max": 0.0})
        for code in ("stale_scan", "stale_odom", "stale_imu", "stale_depth"):
            self.assertTrue(any(r.startswith(code) for r in last["reasons"]), code)
        self.assertIn("WATCHDOG=1", h.notifications(1.0))

    def test_drives_with_sensors_capped_and_state_rate(self):
        h = self.start(req=(0.0, 0.0))
        self.armed(h)
        h.req = (0.3, 0.5)
        time.sleep(2.0)
        t = time.monotonic()
        time.sleep(2.0)
        with h.lock:
            cmds = [c for ts, c in h.cmd if ts >= t]
            states = [s for ts, s in h.state if ts >= t]
        self.assertTrue(cmds)
        self.assertTrue(all(abs(v - 0.15) < 1e-6 and abs(w - 0.5) < 1e-6 for v, w in cmds), cmds[-3:])
        self.assertGreaterEqual(len(cmds) / 2.0, 15)  # в движении ≈20 Гц
        self.assertGreaterEqual(len(states) / 2.0, 4)  # 5 Гц
        self.assertEqual(states[-1]["state"], "slow")
        self.assertTrue(states[-1]["reasons"][0].startswith("lidar_blind"))
        # блок RS3; расширение на 2 луча с каждой стороны уже снято: эти лучи видят дальше корпуса 20 сканов подряд
        self.assertEqual(states[-1]["lidar_mask_deg"], [[-60.8, 4.2]])

    def test_estop_topic_file_policy_and_restart(self):
        h = self.start(req=(0.0, 0.0))
        self.armed(h)
        h.req = (0.1, 0.0)
        self.assertIsNotNone(h.wait(h.cmd, lambda c: c[0] > 0.05, 3.0))
        t = time.monotonic()
        h.estop("engage", by="agent")
        zero = h.wait(h.cmd, lambda c: c == (0.0, 0.0), 1.0, t)
        self.assertIsNotNone(zero)
        self.assertLess(zero[0] - t, 0.3)
        self.assertIsNotNone(h.wait(h.state, lambda s: s["state"] == "estop", 1.0, t))
        with open(os.path.join(h.state_dir, "estop.json"), encoding="utf-8") as f:
            self.assertTrue(json.load(f)["engaged"])
        h.estop("release", by="agent")  # агенту снять нельзя
        h.estop("release")  # и узлу, подписавшемуся «human»: по теме не снимает никто
        time.sleep(1.0)
        with h.lock:
            self.assertEqual(h.state[-1][1]["state"], "estop")
            self.assertGreaterEqual(h.state[-1][1]["counters"]["release_denied"], 2)
        self.assertIn("по теме защёлку не снять", h.stderr())

        h.proc.send_signal(signal.SIGTERM)
        self.assertEqual(h.proc.wait(10), 0)
        h2 = self.start(state_dir=h.state_dir, req=(0.1, 0.0))
        got = h2.wait(h2.state, lambda s: True, 5.0)
        self.assertEqual(got[1]["state"], "estop")  # защёлка пережила рестарт
        t = time.monotonic()
        os.chmod(h2.release_dir, 0o777)  # каталог открыт на запись прочим — файлу не верим
        h2.release_file(got[1])
        time.sleep(1.5)
        with h2.lock:
            self.assertEqual(h2.state[-1][1]["state"], "estop")
        self.assertIn("доступен на запись", h2.stderr())
        os.chmod(h2.release_dir, 0o755)
        h2.release_file(got[1])
        self.assertIsNotNone(h2.wait(h2.state, lambda s: s["state"] != "estop", 3.0, t))
        time.sleep(1.0)
        with h2.lock:
            self.assertTrue(all(c == (0.0, 0.0) for ts, c in h2.cmd))  # запрос не обнулялся — не взведён
            self.assertTrue(any(r.startswith("unarmed") for r in h2.state[-1][1]["reasons"]))
        h2.req = (0.0, 0.0)
        time.sleep(0.3)
        h2.req = (0.1, 0.0)
        self.assertIsNotNone(h2.wait(h2.cmd, lambda c: c[0] > 0.05, 3.0))

    def test_agent_stop_zero_at_once_without_latch(self):
        # решение 1а 19.10: стоп агента — ноль сразу, без S-кривой и без защёлки; дальше — с нулевого запроса
        h = self.start(req=(0.0, 0.0))
        self.armed(h)
        h.req = (0.15, 0.3)
        self.assertIsNotNone(h.wait(h.cmd, lambda c: c[0] > 0.14, 3.0))
        t = time.monotonic()
        h.stop_now("s1")
        zero = h.wait(h.cmd, lambda c: c == (0.0, 0.0), 1.0, t)
        self.assertIsNotNone(zero)
        self.assertLess(zero[0] - t, 0.1)  # плавный стоп с 0,15 м/с — ≈0,5 с
        time.sleep(1.0)  # запрос всё ещё 0,15: прежний манёвр сам не продолжается
        with h.lock:
            self.assertTrue(all(c == (0.0, 0.0) for ts, c in h.cmd if ts >= zero[0]))
            last = h.state[-1][1]
        self.assertNotEqual(last["state"], "estop")
        self.assertFalse(last["estop"]["engaged"])
        self.assertTrue(any(r.startswith("unarmed") for r in last["reasons"]))
        self.assertEqual(last["counters"]["stop_now"], 1)
        self.assertFalse(os.path.exists(os.path.join(h.state_dir, "estop.json")))
        h.req = (0.0, 0.0)
        time.sleep(0.3)
        t = time.monotonic()
        h.stop_now("s1")  # копия того же стопа не исполняется
        h.req = (0.1, 0.0)
        self.assertIsNotNone(h.wait(h.cmd, lambda c: c[0] > 0.05, 3.0, t))
        self.assertIn("стоп агента s1 от agent", h.stderr())

    def test_sigterm_while_moving_ends_with_zero(self):
        h = self.start(req=(0.0, 0.0))
        self.armed(h)
        h.req = (0.1, 0.3)
        self.assertIsNotNone(h.wait(h.cmd, lambda c: c[0] > 0.05, 3.0))
        h.proc.send_signal(signal.SIGTERM)  # запросы ещё идут — ноль должен быть последним
        self.assertEqual(h.proc.wait(10), 0)
        time.sleep(0.3)
        with h.lock:
            self.assertEqual(h.cmd[-1][1], (0.0, 0.0))
        self.assertIn("STOPPING=1", h.notifications(0.2))

    def test_exception_while_moving_ends_with_zero(self):
        h = self.start(code=CRASH_WHILE_MOVING, req=(0.0, 0.0))
        self.armed(h)
        h.req = (0.1, 0.0)
        self.assertEqual(h.proc.wait(20), 1)
        time.sleep(0.3)
        with h.lock:
            self.assertTrue(any(c[0] > 0.0 for _, c in h.cmd))
            self.assertEqual(h.cmd[-1][1], (0.0, 0.0))
        self.assertIn("проверка: исключение в решении", h.stderr())

    def test_foreign_writer_latches(self):
        h = self.start(req=(0.0, 0.0))
        self.armed(h)
        rogue = h.node.create_publisher(Twist, "/cmd_vel", 10)
        self.addCleanup(h.node.destroy_publisher, rogue)
        got = h.wait(h.state, lambda s: s["state"] == "estop", 5.0)
        self.assertIsNotNone(got)
        self.assertTrue(any(r.startswith("foreign_writer") and "nb_test_safety" in r for r in got[1]["reasons"]))

    def test_agent_copy_and_second_scan_publisher(self):
        h = self.start("-p", "agent_state_topic:=/neurobot/agent/safety_state", "-p", "scan_dup_policy:=block",
                       "-p", "scan_dup_s:=2.0", req=(0.0, 0.0))
        self.armed(h)
        copy = []
        h.node.create_subscription(String, "/neurobot/agent/safety_state", lambda m: copy.append(json.loads(m.data)),
                                   10)
        time.sleep(1.0)
        self.assertTrue(copy)
        self.assertEqual(set(copy[-1]), set(h.state[-1][1]))  # тот же документ, что /neurobot/safety_state
        # чужой издатель /scan (другое имя, чем у драйвера — издателя скана тестового узла): стоп сразу
        h.req = (0.1, 0.0)
        self.assertIsNotNone(h.wait(h.cmd, lambda c: c[0] > 0.05, 3.0))
        ghost = rclpy.create_node("scan_ghost_%d" % os.getpid())
        ghost.create_publisher(LaserScan, "/scan", qos_profile_sensor_data)
        t = time.monotonic()
        stop = h.wait(h.state, lambda s: any(r.startswith("scan_foreign") for r in s["reasons"]), 3.0, t)
        self.assertIsNotNone(stop)
        self.assertEqual(stop[1]["state"], "blocked")
        self.assertIsNotNone(h.wait(h.cmd, lambda c: c == (0.0, 0.0), 1.0, stop[0]))
        ghost.destroy_node()
        h.req = (0.0, 0.0)
        self.assertIsNotNone(h.wait(h.state, lambda s: s["state"] in ("ok", "slow"), 5.0, time.monotonic()))
        # второй издатель с именем драйвера (рестарт, след убитого в графе DDS): предупреждение, стоп — через scan_dup_s
        h.req = (0.1, 0.0)
        self.assertIsNotNone(h.wait(h.cmd, lambda c: c[0] > 0.05, 3.0))
        twin = rclpy.create_node(h.node.get_name())
        self.addCleanup(twin.destroy_node)
        twin.create_publisher(LaserScan, "/scan", qos_profile_sensor_data)
        t = time.monotonic()
        warn = h.wait(h.state, lambda s: any(w.startswith("scan_publishers") for w in s["warnings"]), 5.0, t)
        self.assertIsNotNone(warn)
        self.assertFalse(any(r.startswith("scan_") for r in warn[1]["reasons"]))
        stop = h.wait(h.state, lambda s: any(r.startswith("scan_publishers") for r in s["reasons"]), 6.0, t)
        self.assertIsNotNone(stop)
        self.assertGreaterEqual(stop[0] - warn[0], 1.0)
        self.assertEqual(stop[1]["state"], "blocked")
        self.assertIsNotNone(h.wait(h.cmd, lambda c: c == (0.0, 0.0), 1.0, stop[0]))

    def test_blind_lidar_and_empty_scans_under_driver_name(self):
        # повторная приёмка 18.10 (S3b): узел с именем драйвера шлёт NaN-сканы между настоящими — стоп сразу, не через
        # scan_dup_s; сам лидар ослеп (весь скан NaN) — lidar_fault, а не stale_scan: сканы идут
        h = self.start("-p", "scan_dup_policy:=block", req=(0.0, 0.0))
        self.armed(h)
        h.req = (0.1, 0.0)
        self.assertIsNotNone(h.wait(h.cmd, lambda c: c[0] > 0.05, 3.0))
        # подмена — отдельный процесс (свой участник DDS), как чужая программа на роботе
        spoof = subprocess.Popen([sys.executable, str(SPOOF), h.node.get_name(), "20"], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.addCleanup(spoof.kill)
        t = time.monotonic()
        stop = h.wait(h.state, lambda s: any(r.startswith("scan_publishers") for r in s["reasons"]), 8.0, t)
        self.assertIsNotNone(stop)
        start = h.wait(h.state, lambda s: any(w.startswith("scan_publishers") for w in s["warnings"]), 8.0, t)
        self.assertLess(stop[0] - start[0], 1.5)  # граф — раз в секунду; scan_dup_s (30 с) не ждём
        self.assertIn("пустые сканы", " ".join(stop[1]["reasons"]))
        self.assertIsNotNone(h.wait(h.cmd, lambda c: c == (0.0, 0.0), 1.0, stop[0]))
        spoof.send_signal(signal.SIGINT)
        spoof.wait(10)
        h.req = (0.0, 0.0)
        self.assertIsNotNone(h.wait(h.state, lambda s: s["state"] in ("ok", "slow"), 15.0, time.monotonic()),
                             (h.state[-1][1]["state"], h.state[-1][1]["reasons"], h.state[-1][1]["warnings"]))
        h.req = (0.1, 0.0)
        self.assertIsNotNone(h.wait(h.cmd, lambda c: c[0] > 0.05, 3.0, time.monotonic()))
        good = h.scan.ranges
        h.scan.ranges = [math.nan] * len(good)
        t = time.monotonic()
        fault = h.wait(h.state, lambda s: s["state"] == "blocked" and s["reasons"], 2.0, t)
        self.assertIsNotNone(fault)
        self.assertEqual([r.split(":")[0] for r in fault[1]["reasons"]], ["lidar_fault"])
        self.assertIsNotNone(h.wait(h.cmd, lambda c: c == (0.0, 0.0), 1.0, fault[0]))
        h.scan.ranges = good
        h.req = (0.0, 0.0)
        self.assertIsNotNone(h.wait(h.state, lambda s: s["state"] in ("ok", "slow"), 5.0, time.monotonic()))

    def test_bad_parameters_exit_2(self):
        for args, word in ((("-p", "max_speed:='0.25'"), "max_speed"), (("-p", "depth_stale:=stop"), "depth_stale"),
                           (("-p", "mask_scans:=2.5"), "mask_scans"), (("-p", "reverse_speed:=0.5"), "reverse_speed")):
            h = Harness(args, sensors=False)
            self.addCleanup(h.close)
            self.assertEqual(h.proc.wait(20), 2, args)
            self.assertIn(word, h.stderr(), args)

    def test_executor_through_gate(self):
        gate = self.start()
        self.armed(gate)
        ex = Harness((), module="neurobot.executor", sensors=False)
        self.addCleanup(ex.close)
        self.assertIn("READY=1", ex.notifications(30.0))
        stop = threading.Event()

        def gestures():
            while not stop.is_set():
                msg = Twist()
                msg.linear.x = 0.1
                gate.gesture_pub.publish(msg)
                time.sleep(0.1)
        feeder = threading.Thread(target=gestures, daemon=True)
        feeder.start()
        self.addCleanup(stop.set)
        self.assertIsNotNone(gate.wait(gate.cmd, lambda c: c[0] > 0.05, 5.0), "жест не дошёл до /cmd_vel")
        t = time.monotonic()
        gate.estop("engage", by="agent")
        self.assertIsNotNone(gate.wait(gate.cmd, lambda c: c == (0.0, 0.0), 1.0, t))
        ev = gate.wait(gate.events, lambda e: e["type"] == "task_end", 3.0, t)
        self.assertIsNotNone(ev)
        self.assertEqual((ev[1]["task"], ev[1]["status"]), ("gesture", "estop"))
        self.assertIsNotNone(gate.wait(gate.motion, lambda m: m.get("safety") == "estop", 3.0, t))
        stop.set()
        feeder.join()

    def test_executor_restart_does_not_resume_maneuver(self):
        # ревью W2: манёвр жеста идёт с самого старта нового процесса исполнителя — он не исполняется до конца
        gate = self.start()
        self.armed(gate)
        v = [0.1]
        stop = threading.Event()

        def gestures():
            while not stop.is_set():
                msg = Twist()
                msg.linear.x = v[0]
                gate.gesture_pub.publish(msg)
                time.sleep(0.1)
        feeder = threading.Thread(target=gestures, daemon=True)
        feeder.start()
        self.addCleanup(stop.set)
        ex = Harness((), module="neurobot.executor", sensors=False)
        self.addCleanup(ex.close)
        self.assertIsNotNone(gate.wait(gate.motion, lambda m: True, 30.0), ex.stderr()[-2000:])
        t = time.monotonic()
        time.sleep(3.0)  # дольше start_quiet_s
        with gate.lock:
            self.assertFalse([c for ts, c in gate.cmd if ts >= t and c[0] > 0.0])
            self.assertTrue([m for ts, m in gate.motion if ts >= t and m["by"] == "none"])
        v[0] = 0.0  # манёвр кончился: демо шлёт нули
        time.sleep(0.5)
        t = time.monotonic()
        v[0] = 0.1  # новый жест
        self.assertIsNotNone(gate.wait(gate.cmd, lambda c: c[0] > 0.05, 5.0, t), "новый жест не дошёл до /cmd_vel")
        stop.set()
        feeder.join()


if __name__ == "__main__":
    unittest.main()
