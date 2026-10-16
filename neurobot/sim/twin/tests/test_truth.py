"""Истина двойника (truth.py) без ROS: формы робота и мира, позы моделей из логической камеры, расстояние в проекции на
пол с учётом высоты, касания, ответ generate_world_sdf, логическая камера в мире, RTF."""
from __future__ import annotations

import math
import sys
import unittest
import xml.etree.ElementTree as ET
from collections import namedtuple
from pathlib import Path

TWIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TWIN))

import truth  # noqa: E402

URDF = """<robot name="r">
  <link name="base_link"/>
  <link name="chassis"><collision name="decks"><origin xyz="0 0 0.1125"/>
    <geometry><cylinder radius="0.195" length="0.125"/></geometry></collision></link>
  <joint name="chassis_joint" type="fixed"><parent link="base_link"/><child link="chassis"/></joint>
  <link name="laser_frame"><collision name="urg"><origin xyz="0 0 -0.017"/>
    <geometry><box size="0.05 0.05 0.07"/></geometry></collision></link>
  <joint name="laser_frame_joint" type="fixed"><parent link="base_link"/><child link="laser_frame"/>
    <origin xyz="-0.015 0 0.67"/></joint>
  <link name="left_wheel"><collision><geometry><sphere radius="0.036"/></geometry></collision></link>
  <joint name="left_wheel_joint" type="continuous"><parent link="base_link"/><child link="left_wheel"/>
    <origin xyz="0 0.19 0.036" rpy="-1.5708 0 0"/></joint>
  <link name="rot"><collision name="b"><origin xyz="0.1 0 0"/><geometry><box size="0.2 0.1 0.1"/></geometry>
    </collision></link>
  <joint name="rot_joint" type="fixed"><parent link="base_link"/><child link="rot"/>
    <origin xyz="0 0 1" rpy="0 0 1.5708"/></joint>
</robot>"""

WORLD = """<sdf version="1.9"><world name="default">
  <model name="ground_plane"><static>true</static><link name="link"><collision name="c"><geometry><plane>
    <normal>0 0 1</normal><size>20 20</size></plane></geometry></collision></link></model>
  <model name="table"><static>true</static><pose>1 0 0 0 0 0</pose><link name="link">
    <collision name="top"><pose>0 0 0.695 0 0 0</pose><geometry><box><size>1.0 0.6 0.03</size></box></geometry>
    </collision></link></model>
  <model name="box"><static>true</static><pose>0 2 0.075 0 0 1.5708</pose><link name="link">
    <collision name="c"><pose>0.1 0 0 0 0 0</pose><geometry><box><size>0.3 0.2 0.15</size></box></geometry>
    </collision></link></model>
</world></sdf>"""


class ShapesTest(unittest.TestCase):
    def test_robot_parts(self):
        parts = {p.name: p for p in truth.robot_parts(URDF)}
        decks, urg, wheel, rot = parts["chassis/decks"], parts["laser_frame/urg"], parts["left_wheel/0"], parts["rot/b"]
        self.assertAlmostEqual(decks.r, 0.195)
        self.assertAlmostEqual(decks.z0, 0.05)
        self.assertAlmostEqual(decks.z1, 0.175)
        self.assertAlmostEqual(urg.cx, -0.015)
        self.assertAlmostEqual(urg.z0, 0.618)
        self.assertAlmostEqual(urg.z1, 0.688)
        self.assertAlmostEqual(wheel.cy, 0.19)
        self.assertAlmostEqual(wheel.r, 0.036)
        self.assertAlmostEqual(rot.cy, 0.1, places=3)     # смещение коллизии повёрнуто вместе со звеном
        self.assertAlmostEqual(rot.yaw, 1.5708)

    def test_model_shapes_skip_terrain_and_compose_pose(self):
        models = truth.model_shapes(WORLD)
        self.assertEqual(sorted(models), ["box", "table"])
        pose, parts = models["box"]
        self.assertEqual(pose, (0.0, 2.0, 0.075, 1.5708))
        self.assertAlmostEqual(parts[0].cx, 0.1)                    # в кадре модели
        obs = {o.name: o for o in truth.placed(models, {n: m[0] for n, m in models.items()})}
        self.assertAlmostEqual(obs["table/top"].z0, 0.68)
        self.assertAlmostEqual(obs["box/c"].cx, 0.0, places=3)
        self.assertAlmostEqual(obs["box/c"].cy, 2.1, places=3)
        self.assertEqual(truth.placed(models, {"table": (5.0, 0.0, 0.0, 0.0)})[0].cx, 5.0)     # без позы — нет в мире
        self.assertEqual(sorted(truth.model_shapes(WORLD, r"^(ground_plane|box)$")), ["table"])

    def test_twin_worlds_parse(self):
        for name in ("lab", "step", "ramp"):
            models = truth.model_shapes((TWIN / "worlds" / (name + ".sdf")).read_text())
            self.assertTrue(truth.placed(models, {n: m[0] for n, m in models.items()}))
            self.assertFalse([n for n in models if n.startswith(("ground_plane", "step_platform", "ramp"))])

    def test_generated_world_reply(self):
        """Ответ `gz service` generate_world_sdf: строка в поле data экранирована, не-ASCII — восьмеричными байтами."""
        sdf = "<sdf version='1.10'>\n  <world name='default'><model name='cable'><pose>-0.95 0 0.02 0 0 0</pose>" \
              "<link name='l'><collision name='c'><geometry><box><size>0.2 0.2 0.04</size></box></geometry>" \
              "</collision></link></model></world>\n</sdf>\n"
        reply = 'data: "%s<!-- \\321\\217 \\"q\\" -->"\n' % sdf.replace("'", "\\'").replace("\n", "\\n")
        text = truth.text_string(reply)
        self.assertTrue(text.endswith('<!-- я "q" -->'))
        pose, parts = truth.model_shapes(text)["cable"]
        self.assertEqual(pose, (-0.95, 0.0, 0.02, 0.0))
        self.assertAlmostEqual(parts[0].z1, 0.02)
        with self.assertRaises(ValueError):
            truth.text_string("Service call timed out")

    def test_with_eye(self):
        lab = (TWIN / "worlds" / "lab.sdf").read_text()
        world = ET.fromstring(truth.with_eye(lab)).find("world")
        eye = world.find("model[@name='%s']" % truth.EYE)
        self.assertEqual(eye.findtext("link/sensor/topic"), "twin/models")
        self.assertEqual(eye.find("link/sensor").get("type"), "logical_camera")
        self.assertEqual(len(eye.findall("plugin")), 1)
        self.assertEqual(world.findall("model")[-1], eye)
        own = lab.replace("</world>", '<plugin filename="gz-sim-logical-camera-system" name="x"/></world>')
        self.assertFalse(ET.fromstring(truth.with_eye(own)).find("world/model[@name='%s']" % truth.EYE)
                         .findall("plugin"))
        self.assertNotIn(truth.EYE, truth.model_shapes(truth.with_eye(lab), "^%s$" % truth.EYE))


class DistanceTest(unittest.TestCase):
    S = truth.Shape

    def test_circle_circle_and_rect(self):
        a = self.S("a", 0, 0, 0, 0.2, 0, 0, 0, 1)
        b = self.S("b", 1, 0, 0, 0.3, 0, 0, 0, 1)
        r = self.S("r", 1, 0, 0, 0, 0.4, 0.4, 0, 1)
        self.assertAlmostEqual(truth.distance_2d(a, b), 0.5)
        self.assertAlmostEqual(truth.distance_2d(a, r), 0.6)
        self.assertEqual(truth.distance_2d(self.S("c", 0.9, 0, 0, 0.2, 0, 0, 0, 1), r), 0.0)

    def test_rotated_rects(self):
        a = self.S("a", 0, 0, 0, 0, 1, 1, 0, 1)
        b = self.S("b", 2, 0, math.pi / 4, 0, 1, 1, 0, 1)
        self.assertAlmostEqual(truth.distance_2d(a, b), 2 - 0.5 - math.sqrt(0.5))
        self.assertEqual(truth.distance_2d(a, self.S("c", 0.8, 0.8, 0.3, 0, 1, 1, 0, 1)), 0.0)

    def test_clearance_respects_height(self):
        parts = truth.robot_parts(URDF)
        models = truth.model_shapes(WORLD)
        table = truth.placed({"table": models["table"]}, {"table": models["table"][0]})[0]
        # под столешницей (0,68–0,71) палуба проходит, URG (0,618–0,688) — нет
        d, nearest, part = truth.clearance(truth.distances(parts, [table], 0.6, 0.0, 0.0, 0.0))
        self.assertEqual((d, nearest, part), (0.0, "table/top", "laser_frame/urg"))
        d, _, part = truth.clearance(truth.distances([p for p in parts if p.name != "laser_frame/urg"], [table],
                                                     0.6, 0.0, 0.0, 0.0))
        self.assertIsNone(d)
        d, _, _ = truth.clearance(truth.distances(parts, [table], 0.0, 0.0, 0.0, 0.0))
        self.assertAlmostEqual(d, 0.5 - (-0.015 + 0.025))       # передняя грань URG — 0,01 м

    def test_support_touch_by_geometry(self):
        """Колесо и ролик (датчика касаний у них нет) касаются препятствия, если зазор ≤ 2 мм; корпус — не по
        геометрии, а по датчику."""
        parts = truth.robot_parts(URDF)
        cable = self.S("cable/c", 0.0, 0.281, 0.0, 0.0, 0.2, 0.1, 0.0, 0.04)    # в 5 мм от левого колеса (до 0,226)
        self.assertEqual(truth.support_touch(truth.distances(parts, [cable], 0.0, 0.0, 0.0, 0.0)), [])
        self.assertEqual(truth.support_touch(truth.distances(parts, [cable], 0.0, 0.004, 0.0, 0.0)), ["cable"])
        far = truth.distances(parts, [cable], 0.0, -0.1, 0.0, 0.0)
        self.assertEqual(truth.support_touch(far), [])
        wall = self.S("wall/c", 0.3, 0.0, 0.0, 0.0, 0.2, 2.0, 0.0, 1.0)         # палуба в 0,005 м — не опора
        self.assertEqual(truth.support_touch(truth.distances(parts, [wall], 0.0, 0.0, 0.0, 0.0), eps=0.01), [])

    def test_place_rotates_offsets(self):
        p = truth.place(self.S("p", 0.1, 0, 0, 0.01, 0, 0, 0, 1), 1.0, 2.0, 0.15, math.pi / 2)
        self.assertAlmostEqual(p.cx, 1.0)
        self.assertAlmostEqual(p.cy, 2.1)
        self.assertAlmostEqual(p.z0, 0.15)


class MiscTest(unittest.TestCase):
    def test_euler(self):
        Q = namedtuple("Q", "x y z w")
        th = 0.3
        r, p, y = truth.euler(Q(0.0, math.sin(-th / 2), 0.0, math.cos(-th / 2)))
        self.assertAlmostEqual(p, -th)
        self.assertAlmostEqual(r, 0.0)
        self.assertAlmostEqual(truth.euler(Q(0, 0, math.sin(0.5), math.cos(0.5)))[2], 1.0)

    def test_contact_model(self):
        self.assertEqual(truth.contact_model("low_box::link::c", "neurochip"), "low_box")
        self.assertIsNone(truth.contact_model("neurochip::base_link::x", "neurochip"))

    def test_world_pose_from_logical_camera(self):
        """Кадр логической камеры twin_eye (замер в ВМ): камера на 15 м смотрит вниз, человек в (1,2; −0,9; 0)."""
        Q = truth.Quat
        cam = Q(0.0, 0.707106771713121, 0.0, 0.707106790659974)
        x, y, z, yaw = truth.world_pose((0.0, 0.0, 15.0), cam, (15.00000003215387, -0.9, 1.199999598076552),
                                        Q(0.0, -0.707106771713121, 0.0, 0.707106790659974))
        self.assertAlmostEqual(x, 1.2, places=6)
        self.assertAlmostEqual(y, -0.9, places=6)
        self.assertAlmostEqual(z, 0.0, places=6)
        self.assertAlmostEqual(yaw, 0.0, places=6)
        turned = truth.q_mul(Q(0.0, -math.sin(math.pi / 4), 0.0, math.cos(math.pi / 4)),
                             Q(0.0, 0.0, math.sin(0.25), math.cos(0.25)))     # модель с рысканием 0,5 рад
        self.assertAlmostEqual(truth.world_pose((0.0, 0.0, 15.0), cam, (15.0, 0.0, 0.0), turned)[3], 0.5)


class RtfTest(unittest.TestCase):
    def test_window(self):
        self.assertAlmostEqual(truth.rtf([(100.0, 5.0), (110.0, 14.0)]), 0.9)
        self.assertIsNone(truth.rtf([(1.0, 1.0)]))
        r = truth.Rtf(window_s=3.0)
        for i in range(41):                                   # 4 с монотонных, сим. время вдвое медленнее
            r.add(10.0 + i * 0.1, 2.0 + i * 0.05)
        self.assertAlmostEqual(r.value(14.0), 0.5, places=6)
        self.assertLess(r.value(15.0), 0.5)                   # сим. время встало — RTF падает
        self.assertIsNone(r.value(20.0))                      # за окно нет данных

    def test_start_and_reset(self):
        r = truth.Rtf(window_s=3.0)
        r.add(0.0, 0.0)
        r.add(1.0, 1.0)
        self.assertIsNone(r.value(1.0))                       # меньше половины окна
        self.assertAlmostEqual(r.value(2.0), 0.5)
        r.add(2.0, 0.1)                                       # мир начат заново
        self.assertEqual(len(r.samples), 1)


if __name__ == "__main__":
    unittest.main()
