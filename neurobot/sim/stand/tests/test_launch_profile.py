"""Правки scripts/sim/launch/robot_sim.launch.py для профиля и launch стенда — на настоящей модели TurtleBot3 и модели
базы NeuroChip. Нужен ROS 2 (ВМ sim): source scripts/sim/vm_env.sh; на Mac тесты пропускаются."""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
STAND = ROOT / "neurobot/sim/stand"
sys.path.insert(0, str(STAND))
from stand_config import BASES, STM32_DEFAULTS  # noqa: E402

try:
    import yaml
    from ament_index_python.packages import get_package_share_directory
    from launch.actions import DeclareLaunchArgument, OpaqueFunction
except ImportError:
    get_package_share_directory = None

PROFILE_ARGS = dict(re.findall(r"(\w+):=([^\s)]+)", (ROOT / "scripts/sim/profiles/neurobot.sh").read_text()))
URG = tuple(PROFILE_ARGS[k] for k in ("lidar_samples", "lidar_min_angle", "lidar_max_angle"))


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def lidar(sdf):
    body = re.search(r'<sensor[^>]*type="gpu_lidar"[^>]*>.*?</sensor>', sdf, re.S).group(0)
    return {t: re.search(r"<%s>([^<]*)</%s>" % (t, t), body).group(1)
            for t in ("samples", "min_angle", "max_angle", "min", "max")}


def strip_lidar(sdf):
    return re.sub(r'<sensor[^>]*type="gpu_lidar"[^>]*>.*?</sensor>', "", sdf, flags=re.S)


@unittest.skipUnless(get_package_share_directory, "нет ROS 2 (запускать в ВМ sim)")
class RobotSimLaunchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rs = _module(ROOT / "scripts/sim/launch/robot_sim.launch.py", "robot_sim_launch")
        tb3 = get_package_share_directory("turtlebot3_gazebo")
        cls.model = (Path(tb3) / "models/turtlebot3_waffle_pi/model.sdf").read_text()
        cls.bridge = os.path.join(tb3, "params", "turtlebot3_waffle_pi_bridge.yaml")

    def test_lidar_geometry_urg_on_tb3(self):
        out = self.rs._set_lidar(self.model, *URG, BASES["tb3"].range_min, PROFILE_ARGS["lidar_range_max"])
        self.assertEqual(lidar(out), dict(zip(("samples", "min_angle", "max_angle"), URG), min="0.06", max="5.6"))
        self.assertEqual(strip_lidar(out), strip_lidar(self.model))   # вне лидара модель не тронута

    def test_neurochip_model_through_launch_edits(self):
        model = Path(BASES["neurochip"].model_sdf).read_text()
        self.rs._set_rate(self.rs._set_rate(model, "imu", 200), "gpu_lidar", 10)    # оба сенсора на месте
        out = self.rs._set_lidar(model, *URG, BASES["neurochip"].range_min, PROFILE_ARGS["lidar_range_max"])
        self.assertEqual(out, model)                  # геометрия модели уже как у профиля

    def test_lidar_defaults_untouched(self):
        self.assertIs(self.rs._set_lidar(self.model, "", "", "", "", ""), self.model)
        out = self.rs._set_lidar(self.model, "", "", "", "", "5.6")
        self.assertEqual(lidar(out), dict(lidar(self.model), max="5.6"))

    def test_lidar_missing_is_error(self):
        no_lidar = self.rs._set_rate(self.model, "gpu_lidar", 0)
        with self.assertRaises(RuntimeError):
            self.rs._set_lidar(no_lidar, "683", "", "", "", "")

    def bridge_entry(self, topic, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.rs._bridge_config(self.bridge, tmp, "off", True, **kw)
            with open(path) as fh:
                entries = yaml.safe_load(fh)
        return next(e for e in entries if e.get("gz_topic_name") == topic)

    def test_cmd_vel_and_scan_default_as_tb3(self):
        e = self.bridge_entry("cmd_vel")
        self.assertEqual((e["ros_topic_name"], e["ros_type_name"]), ("cmd_vel", "geometry_msgs/msg/TwistStamped"))
        self.assertEqual(self.bridge_entry("scan")["ros_topic_name"], "scan")

    def test_cmd_vel_type_and_topic(self):
        e = self.bridge_entry("cmd_vel", cmd_vel_type="Twist", cmd_vel_topic="/stand/base_cmd_vel")
        self.assertEqual((e["ros_topic_name"], e["ros_type_name"], e["direction"]),
                         ("/stand/base_cmd_vel", "geometry_msgs/msg/Twist", "ROS_TO_GZ"))

    def test_scan_topic(self):
        e = self.bridge_entry("scan", scan_topic="/stand/scan_raw")
        self.assertEqual((e["ros_topic_name"], e["direction"]), ("/stand/scan_raw", "GZ_TO_ROS"))

    def test_new_arguments_default_to_tb3(self):
        args = {a.name: a.default_value for a in self.rs.generate_launch_description().entities
                if isinstance(a, DeclareLaunchArgument)}
        for name in ("lidar_samples", "lidar_min_angle", "lidar_max_angle", "lidar_range_min", "lidar_range_max",
                     "cmd_vel_type", "cmd_vel_topic", "scan_topic", "model_sdf", "robot_urdf"):
            self.assertEqual(args[name][0].text, "", name)
        self.assertEqual(args["odom_hz_fix"][0].text, "false")


@unittest.skipUnless(get_package_share_directory, "нет ROS 2 (запускать в ВМ sim)")
class StandLaunchTest(unittest.TestCase):
    def test_arguments_and_opaque_setup(self):
        mod = _module(STAND / "launch/neurobot_stand.launch.py", "neurobot_stand_launch")
        self.assertTrue(os.path.isfile(mod.BASE))
        ents = mod.generate_launch_description().entities
        args = {a.name: a.default_value[0].text for a in ents if isinstance(a, DeclareLaunchArgument)}
        self.assertEqual(args, dict({"base": "tb3", "scan_occluder": "false", "scan_codes": "false"},
                                    **{"stm32_" + n: v for n, v in STM32_DEFAULTS}))
        self.assertEqual(sum(isinstance(a, OpaqueFunction) for a in ents), 1)


if __name__ == "__main__":
    sys.exit(unittest.main())
