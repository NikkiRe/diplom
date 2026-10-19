"""Пульт: живой шлюз как процесс ROS 2 (ВМ sim, Jazzy; на роботе — Foxy): `python -m neurobot.ui live` с env-файлом и
каталогом состояния во временном каталоге, тестовый узел вместо графа робота.

Проверяется: READY-путь (HTTP отвечает, /healthz), издатели узла по графу — только /neurobot/stop_now и /neurobot/estop
(плюс /parameter_events rclpy), `neurobot wait-graph ui` их принимает; поток SSE после входа по коду несёт
safety_state, запрос исполнителя (из поля req safety_state), скорость /cmd_vel и точки скана (подписка сырым,
rclpy.serialization) из графа; «СТОП движения» — 5 копий с одним id в /neurobot/stop_now, принят — по росту
counters.stop_now подставного safety_gate (200), не посчитан — 202, устаревший — 409 и в граф не уходит; «Поставить
защёлку» — 3 копии engage в /neurobot/estop, итог — по safety_state подставного safety_gate; подписка подставного
safety_gate reliable + volatile (как у настоящего) получает обе команды с первого раза; ремап в командной строке
пульт не принимает. Нужен rclpy: без него тесты пропускаются. Домен DDS — NEUROBOT_TEST_DOMAIN (по умолчанию 48).
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DOMAIN = os.environ.get("NEUROBOT_TEST_DOMAIN", "48")
ROS_SETUP = os.environ.get("NEUROBOT_TEST_ROS_SETUP", "/opt/ros/jazzy/setup.bash")     # Foxy — tests/foxy_smoke.sh
os.environ["ROS_DOMAIN_ID"] = DOMAIN

try:
    import rclpy
    from geometry_msgs.msg import TransformStamped, Twist
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
    from sensor_msgs.msg import LaserScan
    from std_msgs.msg import String
    from tf2_msgs.msg import TFMessage
except ImportError:
    rclpy = None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(rclpy, "нет rclpy: процессные тесты — в ВМ sim (source /opt/ros/jazzy/setup.bash)")
class GatewayProcessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = os.path.join(cls.tmp.name, "ui")
        cls.port = free_port()
        cls.env_file = os.path.join(cls.tmp.name, "neurobot.env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write("NEUROBOT_COMPONENTS=\"safety ui\"\nNEUROBOT_ROS_SETUP={}\n"
                    "ROS_DOMAIN_ID={}\nNEUROBOT_UI_PORT={}\nNEUROBOT_UI_SOURCE=двойник\nNEUROBOT_UI_DOCTOR_S=0\n"
                    "NEUROBOT_UI_STATE_DIR={}\nNEUROBOT_WAIT_S=20\n".format(ROS_SETUP, DOMAIN, cls.port, cls.state))
        for k in ("RMW_IMPLEMENTATION", "CYCLONEDDS_URI"):
            if os.environ.get(k):
                with open(cls.env_file, "a", encoding="utf-8") as f:
                    f.write("{}={}\n".format(k, os.environ[k]))
        rclpy.init()
        cls.node = rclpy.create_node("ui_test_graph")
        cls.got = {"stop": [], "estop": []}
        cls.latched = None
        cls.stops, cls.stop_ids, cls.count_stops = 0, set(), True
        cls.node.create_subscription(String, "/neurobot/stop_now", cls.on_stop, 10)
        cls.node.create_subscription(String, "/neurobot/estop", cls.on_estop, 10)
        cls.state_pub = cls.node.create_publisher(String, "/neurobot/safety_state", 10)
        cls.cmd_pub = cls.node.create_publisher(Twist, "/cmd_vel", 10)
        cls.scan_pub = cls.node.create_publisher(LaserScan, "/scan", 10)
        tf_pub = cls.node.create_publisher(TFMessage, "/tf_static", QoSProfile(
            depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL, reliability=ReliabilityPolicy.RELIABLE))
        tr = TransformStamped()
        tr.header.frame_id, tr.child_frame_id = "base_link", "laser_frame"
        tr.transform.rotation.w = 1.0
        tf_pub.publish(TFMessage(transforms=[tr]))
        cls.tf_pub = tf_pub
        cls.node.create_timer(0.2, cls.publish_state)
        cls.node.create_timer(0.1, cls.publish_scan)
        cls.ex = SingleThreadedExecutor()
        cls.ex.add_node(cls.node)
        cls.spinning = True
        cls.spinner = threading.Thread(target=cls.spin, daemon=True)
        cls.spinner.start()
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT), str(ROOT / "neurobot" / "deploy"),
                                                           os.environ.get("PYTHONPATH", "")]),
                   NEUROBOT_ENV=cls.env_file, PYTHONDONTWRITEBYTECODE="1")
        cls.proc = subprocess.Popen([sys.executable, "-m", "neurobot.ui", "live", "--state-dir", cls.state,
                                     "--ros-args", "-r", "/neurobot/stop_now:=/cmd_vel"],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        cls.env = env
        end = time.monotonic() + 30
        while time.monotonic() < end:
            try:
                if cls.get("/healthz")[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)
        else:
            cls.proc.kill()
            raise AssertionError("шлюз не поднялся: " + cls.proc.stdout.read())

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.spinning = False
        cls.spinner.join(2)
        cls.node.destroy_node()
        rclpy.shutdown()
        cls.tmp.cleanup()

    @classmethod
    def spin(cls):
        while cls.spinning:
            cls.ex.spin_once(timeout_sec=0.05)

    @classmethod
    def on_stop(cls, msg):
        cls.got["stop"].append(msg.data)
        sid = json.loads(msg.data).get("id")
        if cls.count_stops and sid not in cls.stop_ids:      # копии с одним id — один стоп, как у safety_gate
            cls.stop_ids.add(sid)
            cls.stops += 1

    @classmethod
    def publish_scan(cls):
        m = LaserScan()
        m.header.frame_id = "laser_frame"
        m.angle_min, m.angle_increment, m.range_min, m.range_max = -1.0, 0.01, 0.02, 5.6
        m.ranges = [2.0] * 200
        cls.scan_pub.publish(m)

    @classmethod
    def on_estop(cls, msg):
        cls.got["estop"].append(msg.data)
        doc = json.loads(msg.data)
        if doc.get("cmd") == "engage" and cls.latched is None:
            cls.latched = time.time()

    @classmethod
    def publish_state(cls):
        latched = cls.latched is not None
        doc = {"boot": "test", "state": "estop" if latched else "slow", "counters": {"stop_now": cls.stops},
               "reasons": ["estop: защёлка (оператор)"] if latched else ["lidar_blind: перед в слепом секторе"],
               "warnings": [], "min_clearance_m": 0.8, "front_m": 1.1, "req": [0.2, 0.0], "out": [0.0, 0.0],
               "limits": {"v_max": 0.15, "v_rev": 0.05, "w_max": 0.8}, "cut": False,
               "estop": {"engaged": latched, "since": cls.latched, "reasons": ["estop: оператор"] if latched else []}}
        cls.state_pub.publish(String(data=json.dumps(doc)))
        tw = Twist()
        tw.linear.x = 0.12
        cls.cmd_pub.publish(tw)

    @classmethod
    def get(cls, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        c.request("GET", path, headers=dict({"Host": "127.0.0.1:%d" % cls.port}, **(headers or {})))
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body, r

    def login(self):
        code = "proc-%d" % time.time_ns()
        with open(os.path.join(self.state, "codes", hashlib.sha256(code.encode()).hexdigest()), "w") as f:
            json.dump({"exp": time.time() + 60}, f)
        status, _, r = self.get("/login?code=" + code)
        self.assertEqual(status, 303)
        cookie = r.getheader("Set-Cookie").split(";", 1)[0]
        meta = json.loads(self.get("/api/meta", {"Cookie": cookie})[1])
        return cookie, meta["csrf"]

    def post(self, path, cookie, csrf, issued_at=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps({"issued_at": time.time() if issued_at is None else issued_at}).encode()
        c.request("POST", path, body=body, headers={
            "Host": "127.0.0.1:%d" % self.port, "Origin": "http://127.0.0.1:%d" % self.port, "Cookie": cookie,
            "X-Neurobot-CSRF": csrf, "Content-Type": "application/json"})
        r = c.getresponse()
        doc = json.loads(r.read())
        c.close()
        return r.status, doc

    def node_publishers(self):
        end = time.monotonic() + 10
        pubs = set()
        while time.monotonic() < end:
            if "ui_gateway" in self.node.get_node_names():
                pubs |= {t for t, _ in self.node.get_publisher_names_and_types_by_node("ui_gateway", "/")}
                if set(("/neurobot/stop_now", "/neurobot/estop")) <= pubs:
                    time.sleep(1.0)
                    pubs |= {t for t, _ in self.node.get_publisher_names_and_types_by_node("ui_gateway", "/")}
                    return pubs
            time.sleep(0.2)
        return pubs

    def test_1_publishers_only_from_list_and_wait_graph(self):
        pubs = self.node_publishers()
        self.assertEqual(pubs - {"/parameter_events"}, {"/neurobot/stop_now", "/neurobot/estop"})
        self.assertNotIn("/cmd_vel", pubs)                    # ремап из командной строки не действует
        self.assertEqual(self.node.get_publishers_info_by_topic("/cmd_vel")[0].node_name, "ui_test_graph")
        r = subprocess.run([sys.executable, "-m", "neurobot_deploy", "--env", self.env_file, "wait-graph", "ui"],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("пишет только в /neurobot/estop, /neurobot/stop_now", r.stderr)

    def test_2_stream_carries_graph(self):
        cookie, _ = self.login()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(("GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nCookie: %s\r\n\r\n" % (self.port, cookie))
                  .encode())
        f = s.makefile("rb")
        kinds, cmds, end = set(), {}, time.monotonic() + 10
        hello = scan = None
        while time.monotonic() < end and not ({"safety", "cmd", "hb", "scan"} <= kinds and len(cmds) == 2):
            line = f.readline().decode("utf-8").strip()
            if line.startswith("data: "):
                m = json.loads(line[6:])
                kinds.add(m["k"])
                if m["k"] == "hello":
                    hello = m
                if m["k"] == "cmd":
                    cmds[m["topic"]] = m["v"]
                if m["k"] == "scan":
                    scan = m
        s.close()
        self.assertEqual((hello["source"], hello["mode"], hello["node"]), ("двойник", "live", "ui_gateway"))
        self.assertTrue({"safety", "cmd", "hb", "scan"} <= kinds, kinds)
        self.assertEqual(cmds, {"out": 0.12, "req": 0.2})      # req — из safety_state, своей подписки нет
        self.assertAlmostEqual(min(r for r in scan["r"] if r is not None), 2.0, places=1)   # скан сырым и разобран

    def test_3_stop_and_engage(self):
        cookie, csrf = self.login()
        t = time.monotonic()
        status, res = self.post("/api/stop", cookie, csrf)
        self.assertEqual((status, res["ok"]), (200, True), res)              # safety_gate посчитал стоп
        end = time.monotonic() + 3
        while len(self.got["stop"]) < 5 and time.monotonic() < end:
            time.sleep(0.02)
        took = time.monotonic() - t
        docs = [json.loads(d) for d in self.got["stop"]]
        self.assertEqual(len(docs), 5)                         # первый стоп — сразу: издатель создан при старте
        self.assertEqual({d["id"] for d in docs}, {res["id"]})
        self.assertEqual(docs[0]["by"], "ui:operator")
        self.assertLess(took, 1.0)
        status, res = self.post("/api/estop", cookie, csrf)
        self.assertEqual((status, res["ok"], res["state"]), (200, True, "estop"), res)
        docs = [json.loads(d) for d in self.got["estop"]]
        self.assertEqual([d["cmd"] for d in docs], ["engage"] * 3)
        self.assertEqual({d["id"] for d in docs}, {res["id"]})
        audit = open(os.path.join(self.state, "audit.jsonl"), encoding="utf-8").read()
        self.assertIn('"action": "stop"', audit)
        self.assertIn('"action": "estop_engage"', audit)

    def test_4_unconfirmed_and_late_stop(self):
        cookie, csrf = self.login()
        type(self).count_stops = False             # тему слушают, но safety_gate стоп не считает
        try:
            status, res = self.post("/api/stop", cookie, csrf)
        finally:
            type(self).count_stops = True
        self.assertEqual((status, res["ok"], res["sent"]), (202, False, True), res)
        n = len(self.got["stop"])
        status, res = self.post("/api/stop", cookie, csrf, issued_at=time.time() - 5)
        self.assertEqual((status, res["sent"]), (409, False), res)
        time.sleep(0.5)
        self.assertEqual(len(self.got["stop"]), n)                            # запоздавший в граф не ушёл


if __name__ == "__main__":
    unittest.main()
