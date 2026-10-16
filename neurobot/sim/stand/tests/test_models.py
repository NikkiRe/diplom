"""Модель базы NeuroChip и миры стенда: числа согласованы между SDF, URDF, профилем и stand_config — без ROS."""
from __future__ import annotations

import re
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

STAND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAND))

import summarize_check  # noqa: E402
from stand_config import BASES  # noqa: E402

PROFILE_ARGS = dict(re.findall(r"(\w+):=([^\s)]+)", (STAND.parents[2] / "scripts/sim/profiles/neurobot.sh").read_text()))
PLANE_Z = 0.67      # плоскость скана URG на базе NeuroChip


def floats(text):
    return [float(v) for v in text.split()]


def z_extent(el):
    """(низ, верх) формы base_link по оси z: поза + половина высоты."""
    z = floats(el.findtext("pose"))[2]
    g = el.find("geometry")[0]
    half = {"box": lambda: floats(g.findtext("size"))[2] / 2, "cylinder": lambda: float(g.findtext("length")) / 2,
            "sphere": lambda: float(g.findtext("radius"))}[g.tag]()
    return z - half, z + half


class NeurochipBaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = BASES["neurochip"]
        cls.text = Path(cls.base.model_sdf).read_text()
        cls.sdf = ET.fromstring(cls.text)
        cls.urdf = ET.parse(cls.base.robot_urdf).getroot()
        cls.link = cls.sdf.find("model/link[@name='base_link']")

    def test_launch_requirements(self):
        self.assertEqual(self.text.count("</model>"), 1)     # сюда robot_sim.launch.py вставляет PosePublisher
        self.assertIsNotNone(self.link.find("sensor[@type='imu']/update_rate"))
        self.assertIn("<odom_publisher_frequency>", self.text)

    def test_lidar_as_profile_and_robot(self):
        lidar = self.link.find("sensor[@type='gpu_lidar']")
        h = lidar.find("ray/scan/horizontal")
        self.assertEqual((h.findtext("samples"), h.findtext("min_angle"), h.findtext("max_angle")),
                         (PROFILE_ARGS["lidar_samples"], PROFILE_ARGS["lidar_min_angle"],
                          PROFILE_ARGS["lidar_max_angle"]))
        self.assertEqual((lidar.findtext("ray/range/min"), lidar.findtext("ray/range/max")),
                         (self.base.range_min, PROFILE_ARGS["lidar_range_max"]))
        self.assertEqual(lidar.findtext("gz_frame_id"), self.base.scan_frame)
        x, y, z = floats(lidar.findtext("pose"))[:3]
        self.assertEqual((x, y, z), (self.base.lidar_x, 0.0, PLANE_Z))
        origin = self.urdf.find("joint[@name='laser_joint']/origin").get("xyz")
        self.assertEqual(floats(origin), [x, y, z])

    def test_visuals_clear_of_scan_plane(self):
        # gpu_lidar считает по визуальным формам: пересечение плоскости — попадания лидара в сам робот.
        for v in self.link.findall("visual"):
            lo, hi = z_extent(v)
            self.assertFalse(lo <= PLANE_Z <= hi, v.get("name"))

    def test_footprint_and_wheels(self):
        decks = self.link.find("collision[@name='decks']/geometry/cylinder")
        self.assertEqual(float(decks.findtext("radius")), self.base.front_m)
        dd = self.sdf.find("model/plugin[@name='gz::sim::systems::DiffDrive']")
        for side in ("left", "right"):
            link = self.sdf.find("model/link[@name='wheel_%s_link']" % side)
            pose = floats(link.findtext("pose"))
            self.assertEqual(float(link.findtext("collision/geometry/cylinder/radius")),
                             float(dd.findtext("wheel_radius")))
            self.assertEqual(2 * abs(pose[1]), float(dd.findtext("wheel_separation")))
            joint = self.urdf.find("joint[@name='wheel_%s_joint']/origin" % side)
            self.assertEqual(floats(joint.get("xyz")), pose[:3])
        bottoms = [z_extent(c)[0] for c in self.link.findall("collision") if c.get("name").startswith("caster")]
        self.assertEqual(bottoms, [0.0, 0.0])              # ролики касаются пола вместе с колёсами


def models(path):
    return {m.get("name"): m for m in ET.parse(STAND / "worlds" / path).getroot().iter("model")}


def top(model, link="link"):
    """Нижняя и верхняя z коллизии link модели мира (без поворотов)."""
    mz = floats(model.findtext("pose"))[2]
    el = model.find("link[@name='%s']" % link)
    lz = floats(el.findtext("pose") or "0 0 0")[2]
    size = floats(el.findtext("collision/geometry/box/size") or "0 0 %s" % el.findtext(
        "collision/geometry/cylinder/length"))
    return mz + lz - size[2] / 2, mz + lz + size[2] / 2


class WorldsTest(unittest.TestCase):
    def test_walls_cross_both_scan_planes(self):
        for world in ("lab_wall.sdf", "lab_low.sdf"):
            for name, m in models(world).items():
                if name.startswith("wall_") or name == "post_back":
                    lo, hi = top(m)
                    self.assertTrue(lo <= 0.17 and hi >= 1.0, (world, name))

    def test_wall_front_and_box_left(self):
        m = models("lab_wall.sdf")
        x = floats(m["wall_front"].findtext("pose"))[0] - floats(m["wall_front"].findtext(
            "link/collision/geometry/box/size"))[0] / 2
        self.assertAlmostEqual(x, summarize_check.WALL_X)
        self.assertLess(top(m["box_left"])[1], PLANE_Z)    # виден только лидару TB3

    def test_negatives_below_or_above_plane(self):
        m = models("lab_low.sdf")
        self.assertNotIn("box_left", m)
        self.assertLess(top(m["neg_box"])[1], PLANE_Z)
        self.assertLess(max(top(m["neg_shelf"], link)[1] for link in ("board", "leg_left", "leg_right")), PLANE_Z)
        lo, hi = top(m["neg_table"], "top")
        self.assertTrue(PLANE_Z < lo < 0.688, lo)          # выше плоскости, ниже верха URG — бьёт голову лидара
        for name, model in (("neg_box", m["neg_box"]), ("neg_shelf", m["neg_shelf"]), ("neg_table", m["neg_table"])):
            pose = floats(model.findtext("pose"))
            link = model.find("link")
            half = floats(link.findtext("collision/geometry/box/size"))[0] / 2
            dx = floats(link.findtext("pose") or "0 0 0")[0]
            self.assertAlmostEqual(pose[0] + dx - half, 0.65, msg=name)   # ближняя грань — как в NEG_CONTACT_X
        self.assertEqual(set(summarize_check.NEG_CONTACT_X), {"neg_box", "neg_shelf", "neg_table"})


if __name__ == "__main__":
    unittest.main()
