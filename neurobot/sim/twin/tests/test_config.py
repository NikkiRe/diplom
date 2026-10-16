"""Согласованность двойника без ROS: xacro, конфиги ros2_control и twist_mux, миры, умолчания twin_config и пороги
сводки описывают одно и то же; корпус D435i по геометрии закрывает лидару сектор скана робота (R7b §3.3)."""
from __future__ import annotations

import math
import re
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

TWIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TWIN))

import twin_summary as sc  # noqa: E402
import twin_config as cfg  # noqa: E402

URDF = (TWIN / "urdf" / "neurochip.urdf.xacro").read_text()
GAZEBO = (TWIN / "urdf" / "neurochip.gazebo.xacro").read_text()


def xacro_props() -> dict:
    """Свойства xacro с вычисленными выражениями ${…} (только арифметика и pi)."""
    env = {"pi": math.pi}
    for name, value in re.findall(r'<xacro:property name="(\w+)" value="([^"]+)"', URDF):
        expr = value[2:-1] if value.startswith("${") else value
        env[name] = eval(expr, {"__builtins__": {}}, env)
    return env


def xacro_args(text: str) -> dict:
    return dict(re.findall(r'<xacro:arg name="(\w+)" default="([^"]*)"', text))


def world(name: str):
    return ET.parse(TWIN / "worlds" / (name + ".sdf")).getroot().find("world")


def model_pose(w, name: str) -> list:
    return [float(v) for v in w.find("model[@name='%s']/pose" % name).text.split()]


class ControllersTest(unittest.TestCase):
    def setUp(self):
        self.p = xacro_props()
        self.c = yaml.safe_load((TWIN / "config" / "controllers.yaml").read_text())

    def test_wheels_match_urdf(self):
        d = self.c["diff_cont"]["ros__parameters"]
        self.assertAlmostEqual(d["wheel_separation"], 2 * self.p["wheel_y"])
        self.assertAlmostEqual(d["wheel_radius"], self.p["wheel_r"])
        self.assertEqual((d["left_wheel_names"], d["right_wheel_names"]), (["left_wheel_joint"], ["right_wheel_joint"]))
        for side in ("left", "right"):
            self.assertIn('<xacro:wheel side="%s"' % side, URDF)

    def test_rates_and_limits(self):
        cm, d = self.c["controller_manager"]["ros__parameters"], self.c["diff_cont"]["ros__parameters"]
        self.assertEqual(cm["update_rate"], cfg.SENSORS["/joint_states"][1])
        self.assertEqual(d["publish_rate"], cfg.SENSORS["/odom"][1])
        self.assertEqual(d["cmd_vel_timeout"], 0.5)
        self.assertLessEqual(d["linear.x.max_deceleration"], 0)          # Jazzy: торможение — отрицательное
        self.assertLessEqual(d["angular.z.max_deceleration"], 0)
        # угловой разгон из колёсного: 2a/b (R7b §5.5)
        self.assertAlmostEqual(d["angular.z.max_acceleration"], 2 * d["linear.x.max_acceleration"] /
                               d["wheel_separation"], places=1)
        self.assertLessEqual(d["linear.x.max_velocity"], self.p["wheel_vel_max"] * self.p["wheel_r"])

    def test_limit_flags_for_foxy(self):
        """В Foxy has_* по умолчанию false, и без них пределы L1 не действуют — флаги заданы явно."""
        d = self.c["diff_cont"]["ros__parameters"]
        for axis in ("linear.x", "angular.z"):
            for flag in ("has_velocity_limits", "has_acceleration_limits"):
                self.assertIs(d["%s.%s" % (axis, flag)], True)

    def test_twist_mux_as_robot(self):
        t = yaml.safe_load((TWIN / "config" / "twist_mux.yaml").read_text())["twist_mux"]["ros__parameters"]
        self.assertFalse(t["use_stamped"])
        got = {"/" + v["topic"]: (v["priority"], v["timeout"]) for v in t["topics"].values()}
        self.assertEqual(got, {k: (p, 0.5) for k, p in cfg.MUX_INPUTS.items()})


class SensorsTest(unittest.TestCase):
    def test_rates_defaults_agree(self):
        launch = dict(cfg.LAUNCH_ARGS)
        args = xacro_args(GAZEBO)
        for arg, topic in (("lidar_hz", "/scan"), ("imu_hz", "/camera/imu"), ("color_hz", "/camera/color/image_raw"),
                           ("depth_hz", "/camera/depth/image_rect_raw"), ("truth_hz", "/ground_truth/pose")):
            self.assertEqual(float(args[arg]), float(launch[arg]))
            self.assertEqual(float(args[arg]), cfg.SENSORS[topic][1])

    def test_lidar_geometry(self):
        lidar = GAZEBO[GAZEBO.index('type="gpu_lidar"'):]
        self.assertIn("<samples>%d</samples>" % cfg.SCAN_N, lidar)
        self.assertIn("<min_angle>-%s</min_angle>" % cfg.SCAN_ANGLE, lidar)
        self.assertIn("<min>%g</min><max>%g</max>" % cfg.SCAN_RANGE, lidar)
        self.assertIn("<gz_frame_id>%s</gz_frame_id>" % cfg.SCAN_FRAME, lidar)
        self.assertAlmostEqual(cfg.scan_angle(cfg.SCAN_N - 1), math.degrees(cfg.SCAN_ANGLE))
        self.assertAlmostEqual(math.radians(cfg.scan_angle(1) - cfg.scan_angle(0)), 2 * math.pi / 1024, places=9)

    def test_camera_body_shadows_robot_sector(self):
        """Лучи в плоскости скана, которые упираются в корпус D435i, — тот же блок, что в скане робота (±2 луча)."""
        p = xacro_props()
        self.assertLess(p["cam_z"] - p["cam_size_z"] / 2, p["urg_z"])
        self.assertGreater(p["cam_z"] + p["cam_size_z"] / 2, p["urg_z"])
        back = p["cam_x"] - p["cam_size_x"] / 2 - p["mast_x"]
        y0, y1 = p["cam_y"] - p["cam_size_y"] / 2, p["cam_y"] + p["cam_size_y"] / 2
        hit = [i for i in range(cfg.SCAN_N)
               if y0 <= back * math.tan(math.radians(cfg.scan_angle(i))) <= y1 and abs(cfg.scan_angle(i)) < 90]
        robot0 = round((cfg.ROBOT_BLIND_DEG[0] - cfg.scan_angle(0)) / (cfg.scan_angle(1) - cfg.scan_angle(0)))
        self.assertLessEqual(abs(hit[0] - robot0), sc.BLIND_EDGE_RAYS)
        self.assertLessEqual(abs(hit[-1] - (robot0 + cfg.ROBOT_BLIND_N - 1)), sc.BLIND_EDGE_RAYS)
        far = max(math.hypot(back + p["cam_size_x"], y) for y in (y0, y1))
        self.assertLess(far, 0.15)                       # всё, что ближе self_hit_m, — свой корпус

    def test_mass_and_center(self):
        p = xacro_props()
        total = p["m_base"] + 2 * p["m_wheel"] + 2 * p["m_caster"] + p["m_mast"] + p["m_head"] + p["m_urg"] + \
            p["m_cam"]
        self.assertAlmostEqual(total, 3.96, places=2)    # R7b §2.3: 4 ± 1 кг


class WorldsTest(unittest.TestCase):
    def test_worlds_parse_and_spawn(self):
        for name, (rel, spawn) in cfg.WORLDS.items():
            path, got = cfg.world(name)
            self.assertEqual((Path(path), got), (TWIN / rel, spawn))
            self.assertEqual(world(name).get("name"), cfg.WORLD_NAME)
        with self.assertRaises(RuntimeError):
            cfg.world("moon")

    def test_lab_geometry_for_summary(self):
        w = world("lab")
        x, y, z = model_pose(w, "low_box")[:3]
        size = [float(v) for v in w.find("model[@name='low_box']/link/collision/geometry/box/size").text.split()]
        self.assertAlmostEqual(x - size[0] / 2 - cfg.WORLDS["lab"][1][0], 1.35)
        self.assertTrue(sc.LOW_BOX_X[0] <= 1.35 <= sc.LOW_BOX_X[1])
        self.assertTrue(sc.LOW_BOX_TOP[0] <= z + size[2] / 2 <= sc.LOW_BOX_TOP[1])
        self.assertLess(z + size[2] / 2, xacro_props()["urg_z"])

    def test_step_geometry_for_summary(self):
        w = world("step")
        x, _, z = model_pose(w, "step_platform")[:3]
        size = [float(v) for v in w.find("model[@name='step_platform']/link/collision/geometry/box/size").text.split()]
        top, edge = z + size[2] / 2, x + size[0] / 2
        spawn = cfg.WORLDS["step"][1]
        self.assertTrue(sc.STEP_Z[0] <= top <= sc.STEP_Z[1])
        self.assertGreater(spawn[2], top)
        ahead = edge - spawn[0]
        self.assertTrue(sc.STEP_EDGE_X[0] <= ahead <= sc.STEP_EDGE_X[1])
        cam = xacro_props()["cam_z"]
        lower_seen = ahead * (cam + top) / cam               # луч через край на нижний пол
        self.assertTrue(sc.STEP_CLIFF_X[0] <= lower_seen <= sc.STEP_CLIFF_X[1])

    def test_truth_probe_geometry_for_summary(self):
        """Ожидаемые зазоры пробы truth — из геометрии мира lab и модели: палуба — нога человека, ролик — «кабель»."""
        w, p = world("lab"), xacro_props()
        person, home, front = cfg.TRUTH_PERSON
        self.assertEqual(tuple(model_pose(w, person)[:2]), home)
        legs = [c for c in w.findall("model[@name='%s']/link/collision" % person) if c.get("name").startswith("leg")]
        leg_y = abs(float(legs[0].findtext("pose").split()[1]))
        leg_r = float(legs[0].findtext("geometry/cylinder/radius"))
        sx, sy = cfg.WORLDS["lab"][1][:2]
        self.assertAlmostEqual(math.hypot(front[0] - sx, front[1] + leg_y - sy) - p["deck_r"] - leg_r,
                               sc.TRUTH_PERSON_CLEAR_M, places=3)
        _, (cx, cy), size = cfg.TRUTH_CABLE
        self.assertAlmostEqual(cx - size[0] / 2 - (sx + p["caster_x"] + p["caster_r"]), sc.TRUTH_CABLE_CLEAR_M,
                               places=3)
        self.assertLess(size[2], p["deck_low_z"])                    # ниже палуб: касается только ролик
        self.assertLess(abs(cy) + size[1] / 2, p["wheel_y"] - p["wheel_r"])      # колёса проходят мимо

    def test_ramp_geometry(self):
        x, _, z, _, pitch, _ = model_pose(world("ramp"), "ramp")
        thick = float(world("ramp").find("model[@name='ramp']/link/collision/geometry/box/size").text.split()[2])
        self.assertAlmostEqual(-math.degrees(pitch), cfg.RAMP_DEG, places=2)
        top_x, top_z = x - thick / 2 * math.sin(-pitch), z + thick / 2 * math.cos(pitch)
        self.assertAlmostEqual(top_x - top_z / math.tan(-pitch), cfg.RAMP_START_X, places=2)
        self.assertLess(cfg.WORLDS["ramp"][1][0] + 0.195, cfg.RAMP_START_X)      # спавн на полу перед пандусом

    def test_xml_files_parse(self):
        for path in list((TWIN / "worlds").glob("*.sdf")) + list((TWIN / "urdf").glob("*.xacro")):
            ET.parse(path)


if __name__ == "__main__":
    unittest.main()
