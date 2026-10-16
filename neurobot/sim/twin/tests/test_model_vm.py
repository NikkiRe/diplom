"""(только ВМ, нужны ROS 2 Jazzy и gz) Модель и launch двойника: xacro собирается, sdformat сливает звенья так, как
ждёт датчик касаний корпуса, визуальные формы пересекает плоскость скана только корпус D435i; мост и параметры
контроллеров из launch; поиск сирот gz; звено cmd_relay в своём домене DDS: перевод команд и начальный ноль до первого
/odom или первой команды."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

TWIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TWIN))
sys.path.insert(0, str(TWIN.parent / "stand"))

import twin_config as cfg  # noqa: E402

HAVE_ROS = shutil.which("xacro") is not None and shutil.which("gz") is not None


def xacro(*args) -> str:
    return subprocess.run(["xacro", str(TWIN / "urdf" / "neurochip.urdf.xacro"), *args], check=True,
                          capture_output=True, text=True).stdout


def load_launch():
    spec = importlib.util.spec_from_file_location("twin_launch", TWIN / "launch" / "twin.launch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(HAVE_ROS, "нужны xacro и gz (ВМ sim)")
class ModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.urdf_path = Path(cls.tmp.name) / "robot.urdf"
        cls.urdf_path.write_text(xacro("controllers:=/dev/null"))
        cls.sdf = ET.fromstring(subprocess.run(["gz", "sdf", "-p", str(cls.urdf_path)], check=True,
                                               capture_output=True, text=True).stdout)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_kinematic_urdf_has_no_gazebo(self):
        kin = xacro("sim:=false")
        self.assertNotIn("<gazebo", kin)
        self.assertIn("<ros2_control", kin)

    def test_contact_sensor_names_match_lumped_collisions(self):
        """Датчик касаний — на всём корпусе, кроме опор: их касания truth.py считает по геометрии."""
        link = self.sdf.find("model/link[@name='base_link']")
        lumped = {c.get("name") for c in link.findall("collision")}
        wanted = {c.text for c in link.findall("sensor[@name='body_contact']/contact/collision")}
        self.assertEqual(wanted, {n for n in lumped if "caster" not in n})
        self.assertEqual(len(lumped - wanted), 2)

    def test_sensors_and_topics(self):
        sensors = {s.get("name"): s for s in self.sdf.iter("sensor")}
        self.assertEqual(sorted(sensors), ["body_contact", "d435_color", "d435_depth", "d435i_imu", "urg_04lx"])
        self.assertEqual(sensors["urg_04lx"].findtext("topic"), "twin/scan")
        self.assertEqual(sensors["d435_depth"].findtext("topic"), "twin/depth/image")
        self.assertEqual(sensors["body_contact"].findtext("contact/topic"), "twin/contacts")

    def test_only_camera_crosses_scan_plane(self):
        link = self.sdf.find("model/link[@name='base_link']")
        crossing = []
        for v in link.findall("visual"):
            z = float(v.findtext("pose").split()[2])
            g = v.find("geometry")[0]
            half = {"box": lambda: float(g.findtext("size").split()[2]) / 2,
                    "cylinder": lambda: float(g.findtext("length")) / 2,
                    "sphere": lambda: float(g.findtext("radius"))}[g.tag]()
            if z - half < 0.67 < z + half:
                crossing.append(v.get("name"))
        self.assertEqual(len(crossing), 1)
        self.assertIn("camera_link", crossing[0])


@unittest.skipUnless(HAVE_ROS, "нужен launch ROS 2 (ВМ sim)")
class LaunchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.launch = load_launch()

    def test_bridge_uses_robot_topic_names(self):
        entries = self.launch.bridge_config(cfg.WORLD_NAME)
        topics = {e["ros_topic_name"]: e for e in entries if "ros_topic_name" in e}
        for name in ("/clock", "/camera/imu", "/camera/color/image_raw", "/camera/depth/camera_info",
                     "/ground_truth/pose", "/twin/contacts", cfg.SCAN_RAW, cfg.DEPTH_RAW, cfg.MODELS_TOPIC):
            self.assertIn(name, topics)
        self.assertEqual(topics[cfg.MODELS_TOPIC]["gz_type_name"], "gz.msgs.LogicalCameraImage")
        self.assertTrue(all(e["direction"] == "GZ_TO_ROS" for e in topics.values()))
        self.assertTrue(topics["/camera/color/image_raw"].get("lazy"))
        services = {e["service_name"] for e in entries if "service_name" in e}
        self.assertIn("/world/default/set_pose", services)

    def test_controllers_timeout(self):
        src = TWIN / "config" / "controllers.yaml"
        self.assertEqual(self.launch.controllers_config(src, 0.0)["diff_cont"]["ros__parameters"]["cmd_vel_timeout"],
                         0.0)
        with self.assertRaises(RuntimeError):
            self.launch.controllers_config(src, -0.1)
        with self.assertRaises(RuntimeError):
            self.launch.controllers_config(src, float("nan"))

    def test_orphan_gz_only_from_twin_root(self):
        from stand_procs import Proc
        procs = [Proc(10, 10, ("gz sim -r -s /home/u/twin_runs/a/world.sdf",)),
                 Proc(20, 20, ("gz sim -r -s /home/u/robot_sim/b/world.sdf",)),
                 Proc(30, 30, ("gz sim -r -s /home/u/twin_runs/c/world.sdf",)),
                 Proc(40, 40, ("python3", "/home/u/twin_runs/x.py"))]
        self.assertEqual([p.pid for p in self.launch.orphan_gz(procs, "/home/u/twin_runs", 30)], [10])


RELAY_CHILD = r"""
import json, sys, time, rclpy
from geometry_msgs.msg import Twist, TwistStamped
from nav_msgs.msg import Odometry
sys.path.insert(0, %r)
from twin_config import DIFF_CMD, MUX_OUT
rclpy.init(); n = rclpy.create_node("relay_test"); got = []
n.create_subscription(TwistStamped, DIFF_CMD, lambda m: got.append((time.monotonic(), m)), 10)
cmd, odom = n.create_publisher(Twist, MUX_OUT, 10), n.create_publisher(Odometry, "/odom", 10)

def spin(s):
    t = time.monotonic() + s
    while time.monotonic() < t:
        rclpy.spin_once(n, timeout_sec=0.05)

def brief(m):
    return [m.header.stamp.sec, m.header.stamp.nanosec, m.header.frame_id, m.twist.linear.x, m.twist.angular.z]

t = time.monotonic()
while len(got) < 2 and time.monotonic() - t < 15:
    spin(0.1)
out = {"first": [brief(m) for _, m in got[:2]], "gap_s": round(got[1][0] - got[0][0], 2) if len(got) > 1 else None}
if sys.argv[1] == "odom":
    for _ in range(3):
        odom.publish(Odometry()); spin(0.1)
else:
    m = Twist(); m.linear.x = 0.2; m.angular.z = -0.3
    while not [g for _, g in got if g.twist.linear.x == 0.2] and time.monotonic() - t < 30:
        cmd.publish(m); spin(0.2)
spin(0.3)
mark = len(got)
spin(1.5)
out["last"] = [brief(m) for _, m in got[mark - 1:mark]]
out["after"] = [brief(m) for _, m in got[mark:]]
print(json.dumps(out))
""" % str(TWIN)


@unittest.skipUnless(HAVE_ROS, "нужен rclpy (ВМ sim)")
class CmdRelayTest(unittest.TestCase):
    def relay_run(self, how: str) -> dict:
        """cmd_relay и тестовый узел в своём домене DDS; how — чем прекратить начальный ноль: odom | cmd."""
        env = dict(os.environ, ROS_DOMAIN_ID=str(90 + os.getpid() % 10))
        relay = subprocess.Popen([sys.executable, str(TWIN / "cmd_relay.py")], env=env)
        try:
            out = subprocess.run([sys.executable, "-c", RELAY_CHILD, how], env=env, capture_output=True, text=True,
                                 timeout=90)
        finally:
            relay.terminate()
            relay.wait(10)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_initial_zero_until_odom(self):
        r = self.relay_run("odom")
        self.assertEqual(r["first"], [[0, 0, "base_link", 0.0, 0.0]] * 2)
        self.assertAlmostEqual(r["gap_s"], 0.5, delta=0.2)            # повтор, пока контроллер не ответил /odom
        self.assertEqual(r["after"], [])

    def test_twist_to_zero_stamped_and_no_zero_after_command(self):
        r = self.relay_run("cmd")
        self.assertEqual(r["first"][0], [0, 0, "base_link", 0.0, 0.0])
        self.assertEqual(r["last"], [[0, 0, "base_link", 0.2, -0.3]])
        self.assertEqual(r["after"], [])


if __name__ == "__main__":
    unittest.main()
