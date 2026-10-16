"""Части safety_gate без rclpy (Python ≥ 3.8): параметры, геометрия и TF, тормозной путь и разгон, лидар (маска
корпуса, коридор, участок неизвестного луча в коридоре, направления без лучей), глубина (кодировки, уменьшенная карта,
покрытие коридора, препятствие, пол, обрыв, память, «точка свободна» для лидара), защёлка, IMU.

Кадры глубины рендерит маленький трассировщик лучей: камера двойника (424×240, 87°, оптический центр на
(0,058; −0,042; 0,665) базы, ось — вперёд), пол, низкий ящик мира lab и край ступеньки мира step.
"""
from __future__ import annotations

import json
import math
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from neurobot.safety.config import ConfigError, Params, make  # noqa: E402
from neurobot.safety.depth import (DROP, OBSTACLE, Camera, Coverage, FreeSpace, Hazards, Memory,  # noqa: E402
                                   Sampler, classify, image_array)
from neurobot.safety.geometry import Pose, StaticTf, edge_distance, quat_matrix, yaw_of  # noqa: E402
from neurobot.safety.latch import Latch, parse  # noqa: E402
from neurobot.safety.lidar import MaskLearner, Rays, ScanMeta, mask_spans, view  # noqa: E402
from neurobot.safety.monitors import Shock, Stall, tilt_deg  # noqa: E402
from neurobot.safety.motion import Ramp, allowed_speed, stop_distance  # noqa: E402

P = Params()


def quat(axis, angle: float):
    """Кватернион (x, y, z, w) поворота на angle вокруг оси."""
    a = np.asarray(axis, float) / np.linalg.norm(axis)
    s = math.sin(angle / 2)
    return (a[0] * s, a[1] * s, a[2] * s, math.cos(angle / 2))


def qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz)


# оптический кадр и кадр IMU realsense (x вправо, y вниз, z вперёд) в базе (x вперёд, y влево, z вверх); у двойника
# camera_imu_optical_frame — rpy (−π/2, 0, −π/2) от camera_link (urdf/neurochip.urdf.xacro)
OPTICAL_Q = qmul(quat((0, 0, 1), -math.pi / 2), quat((1, 0, 0), -math.pi / 2))
OPTICAL = quat_matrix(*OPTICAL_Q)
CAM = np.array([0.058, -0.042, 0.665])
FX = 212.0 / math.tan(math.radians(43.5))
CAMERA = Camera(FX, FX, 211.5, 119.5, 424, 240)


def render(box=None, edge_x=None, drop=0.15) -> np.ndarray:
    """Кадр глубины 16UC1 (мм, 0 — нет данных): пол z = 0 (за edge_x — на drop ниже), бокс (x0, x1, y0, y1, z0, z1)."""
    v, u = np.mgrid[0:CAMERA.height, 0:CAMERA.width]
    d_opt = np.stack(((u - CAMERA.cx) / CAMERA.fx, (v - CAMERA.cy) / CAMERA.fy, np.ones(u.shape)), axis=-1)
    d = d_opt @ OPTICAL.T
    dx, dy, dz = d[..., 0], d[..., 1], d[..., 2]
    t = np.full(u.shape, np.inf)
    with np.errstate(divide="ignore", invalid="ignore"):
        tf = np.where(dz < 0, -CAM[2] / dz, np.inf)
        on_floor = np.ones(u.shape, bool) if edge_x is None else CAM[0] + tf * dx < edge_x
        t = np.where(on_floor, np.minimum(t, tf), t)
        if edge_x is not None:
            tl = np.where(dz < 0, (-drop - CAM[2]) / dz, np.inf)
            t = np.where(CAM[0] + tl * dx >= edge_x, np.minimum(t, tl), t)
            te = np.where(dx > 0, (edge_x - CAM[0]) / dx, np.inf)
            ze = CAM[2] + te * dz
            t = np.where((ze <= 0) & (ze >= -drop), np.minimum(t, te), t)
        if box is not None:
            x0, x1, y0, y1, z0, z1 = box
            lo, hi = np.full(u.shape, -np.inf), np.full(u.shape, np.inf)
            for c, dc, a, b in ((CAM[0], dx, x0, x1), (CAM[1], dy, y0, y1), (CAM[2], dz, z0, z1)):
                t1, t2 = (a - c) / dc, (b - c) / dc
                lo, hi = np.maximum(lo, np.minimum(t1, t2)), np.minimum(hi, np.maximum(t1, t2))
            t = np.where((hi >= lo) & (lo > 0), np.minimum(t, lo), t)
    t = np.where(np.isfinite(t) & (t <= 10.0), t, 0.0)
    return np.round(t * 1000).astype("<u2")


def hazards(image: np.ndarray) -> Hazards:
    """Кадр 16UC1 → опасности и покрытие, как в узле."""
    pose = Pose(OPTICAL, CAM)
    sampler = Sampler(CAMERA, P.depth_cols)
    points, valid = sampler.points(sampler.depths(image, 0.001))
    return classify(pose.apply(points), CAM, P, Coverage(sampler, pose, P)(valid))


NOTHING = np.zeros((0, 2))


class Config(unittest.TestCase):
    def test_defaults_and_conversion(self):
        self.assertEqual(make({}), P)
        self.assertEqual(make({"max_speed": 0.2, "t_lat": 1}).t_lat, 1.0)  # целое годится для дробного
        self.assertEqual(make({"override_topics": ["/a"]}).override_topics, ("/a",))
        self.assertEqual(make({"mask_from_deg": -70, "mask_to_deg": 10.0}).mask_from_deg, -70.0)  # пеленг со знаком

    def test_rejected(self):
        for values, word in (({"max_speed": "0.25"}, "max_speed"), ({"max_speed": True}, "max_speed"),
                             ({"mask_scans": 20.0}, "mask_scans"), ({"t_lat": math.nan}, "t_lat"),
                             ({"margin": -0.1}, "margin"), ({"rate_hz": 0.0}, "rate_hz"),
                             ({"depth_stale": "stop"}, "depth_stale"), ({"cmd_vel_type": "Twst"}, "cmd_vel_type"),
                             ({"override_policy": "off"}, "override_policy"), ({"mask_ratio": 1.5}, "mask_ratio"),
                             ({"obstacle_min_h": 0.8}, "obstacle_min_h"), ({"reverse_speed": 0.5}, "reverse_speed"),
                             ({"override_topics": "/cmd_vel_joy"}, "override_topics"), ({"nope": 1}, "nope"),
                             ({"depth_min_valid": 0.0}, "depth_min_valid"),
                             ({"depth_min_valid": 1.5}, "depth_min_valid"),
                             ({"mask_from_deg": 10.0}, "mask_from_deg"), ({"mask_to_deg": 200.0}, "mask_to_deg")):
            with self.assertRaises(ConfigError) as e:
                make(values)
            self.assertIn(word, str(e.exception), values)


class Geometry(unittest.TestCase):
    def test_static_chain_and_missing(self):
        tf = StaticTf()
        tf.add("base_link", "mast", (-0.015, 0, 0.175), quat((0, 0, 1), 0))
        tf.add("/mast", "laser_frame", (0, 0, 0.495), quat((0, 0, 1), math.pi / 2))
        pose = tf.lookup("base_link", "/laser_frame")
        np.testing.assert_allclose(pose.apply(np.array([[1.0, 0, 0]])), [[-0.015, 1.0, 0.67]], atol=1e-9)
        self.assertIsNone(tf.lookup("base_link", "camera_link"))
        tf.add("a", "b", (0, 0, 0), (0, 0, 0, 1))
        tf.add("b", "a", (0, 0, 0), (0, 0, 0, 1))
        self.assertIsNone(tf.lookup("base_link", "a"))  # цикл
        with self.assertRaises(ValueError):
            quat_matrix(0, 0, 0, 0)

    def test_yaw_and_edge_distance(self):
        self.assertAlmostEqual(yaw_of(*quat((0, 0, 1), 0.7)), 0.7)
        np.testing.assert_allclose(edge_distance(np.array([0.5, 0.5, 0.5]), np.array([0.0, 0.195, 0.3]), 0.195),
                                   [0.305, 0.5, 0.5])


class Motion(unittest.TestCase):
    def test_stop_distance_from_body_edge(self):
        self.assertAlmostEqual(stop_distance(P, 0.25), 0.25 * 0.25 + 0.25 ** 2 / 1.6 + 0.05)  # 0,152 м
        self.assertAlmostEqual(stop_distance(P, 0.0), P.margin)
        self.assertEqual(stop_distance(P, math.nan), math.inf)
        for d in (0.06, 0.1, 0.3, 1.0):
            self.assertAlmostEqual(stop_distance(P, allowed_speed(P, d)), d)
        self.assertEqual(allowed_speed(P, math.inf), math.inf)
        self.assertEqual(allowed_speed(P, P.margin), 0.0)
        self.assertEqual(allowed_speed(P, math.nan), 0.0)

    def test_ramp(self):
        r = Ramp(0.5, 2.0)
        vs = [r.step(0.25, 0.05) for _ in range(20)]
        self.assertAlmostEqual(vs[0], 0.005)        # рывок: ускорение растёт на 0,1 м/с² за тик
        self.assertAlmostEqual(vs[-1], 0.25)
        self.assertEqual(r.step(0.1, 0.05), 0.1)     # торможение — сразу
        self.assertEqual(r.step(-0.05, 0.05), 0.0)   # смена знака — сначала ноль
        self.assertLess(r.step(-0.05, 0.05), 0.0)
        self.assertAlmostEqual(Ramp(1.0).step(0.5, 0.05), 0.05)  # без предела рывка


class Lidar(unittest.TestCase):
    N, AMIN = 683, -2.0923497677

    def meta(self):
        return ScanMeta(self.AMIN, 2 * -self.AMIN / (self.N - 1), self.N, 0.02, 5.6)

    def test_mask_learning_ratio_dilation_and_body_sector(self):
        learner = MaskLearner(P._replace(mask_scans=5))
        body = np.zeros(self.N, bool)
        body[50:300] = True  # сектор корпуса
        flicker = np.zeros(self.N, bool)
        flicker[100:110] = True
        flicker[290:299] = True   # у края сектора: расширение за сектор не выходит
        flicker[400:420] = True   # вне сектора — обстановка
        for k in range(5):
            scan = flicker.copy()
            scan[200] = k < 3  # 3 из 5 — меньше mask_ratio 0,8
            mask = learner.feed(scan, body)
        self.assertEqual(list(np.flatnonzero(mask)), list(range(98, 112)) + list(range(288, 300)))
        self.assertEqual(list(np.flatnonzero(learner.outside)), list(range(400, 420)))
        self.assertIs(learner.feed(np.ones(self.N, bool), body), mask)  # выученная маска обучением не меняется
        self.assertIsNone(learner.feed(np.zeros(10, bool), body[:10]))  # другое число лучей — учится заново

    def test_mask_shrinks_on_hits_past_the_body(self):
        learner = MaskLearner(P._replace(mask_scans=3))
        body = np.ones(self.N, bool)
        blind = np.zeros(self.N, bool)
        blind[100:110] = True
        for _ in range(3):
            learner.feed(blind, body)
        beyond = np.zeros(self.N, bool)
        beyond[98:104] = True
        flicker = beyond.copy()
        flicker[104] = True
        for scan in (beyond, flicker, beyond):  # лучи 98–103 видят дальше корпуса три скана подряд, 104 — один
            freed = learner.watch(scan)
        self.assertEqual(list(np.flatnonzero(freed)), [98, 99, 100, 101, 102, 103])
        self.assertEqual(list(np.flatnonzero(learner.mask)), list(range(104, 112)))

    def test_upside_down_lidar_and_self_hits(self):
        meta = self.meta()
        flipped = Rays(meta, Pose(quat_matrix(*quat((1, 0, 0), math.pi)), np.zeros(3)))
        self.assertAlmostEqual(math.degrees(flipped.bearing[0]), 119.88, places=1)  # angle_min смотрит влево
        rays = Rays(meta, Pose(np.eye(3), np.array([-0.015, 0, 0.67])))
        ranges = np.full(self.N, 3.0)
        ranges[300] = 0.05  # попадание в свой корпус по лучу маски — не препятствие
        mask = np.zeros(self.N, bool)
        mask[295:305] = True
        v = view(ranges, rays, mask, P)
        self.assertGreater(v.front_m, 2.0)
        self.assertFalse(v.touch)
        self.assertTrue(v.blind)
        self.assertEqual(mask_spans(mask, rays), [[-16.2, -13.0]])
        self.assertTrue(view(ranges, rays, np.zeros(self.N, bool), P).touch)  # вне маски — касание

    def test_unknown_ray_segment_in_corridor_beyond_body(self):
        # решение 16.10: участок неизвестного луча — от выхода из круга R + запас до выхода из полосы |y| ≤ R + запас
        # (у оси базы — (R + запас)/|sin θ|) или до переднего края этого круга, сдвинутого на d_stop(max_speed)
        rays = Rays(self.meta(), Pose(np.eye(3), np.array([-0.015, 0, 0.67])))
        start, end = rays.corridor(P)
        w, reach = P.robot_radius + P.lateral_margin, stop_distance(P, P.max_speed)

        def at(deg):
            k = int(np.argmin(np.abs(np.degrees(rays.dir_bearing) - deg)))
            return float(start[k]), float(end[k])
        s0, e0 = at(0.0)
        self.assertAlmostEqual(s0, w + 0.015, delta=1e-3)     # лидар в 1,5 см за осью базы
        self.assertAlmostEqual(e0 - s0, reach, delta=1e-3)    # прямо по курсу — весь тормозной путь
        s80, e80 = at(80.0)
        self.assertAlmostEqual(e80, w / math.sin(math.radians(80.0)), delta=2e-3)  # выход из полосы
        self.assertLess(e80 - s80, 0.002)                     # у плеча — в коридоре только внутри круга
        lengths = [at(d)[1] - at(d)[0] for d in (0, 20, 40, 55, 60, 65, 75, 85)]
        self.assertEqual(lengths, sorted(lengths, reverse=True))
        self.assertGreater(at(59.0)[1] - at(59.0)[0], P.max_gap_m)
        self.assertLess(at(61.0)[1] - at(61.0)[0], P.max_gap_m)
        s89, e89 = at(89.9)
        self.assertLessEqual(e89 - s89, 0.0)                   # выходит из круга сбоку-сзади: участка нет

    def test_directions_without_rays_are_filled(self):
        rays = Rays(self.meta()._replace(n=400), Pose(np.eye(3), np.zeros(3)))  # −120…+20°
        filled = rays.dir_ray < 0
        self.assertGreater(float(np.min(np.degrees(rays.dir_bearing[filled]))), 20.0)
        self.assertGreater(float(np.max(np.degrees(rays.dir_bearing[filled]))), 89.0)
        np.testing.assert_allclose(np.diff(rays.dir_bearing), abs(self.meta().angle_increment), rtol=0.2)
        self.assertFalse((Rays(self.meta(), Pose(np.eye(3), np.zeros(3))).dir_ray < 0).any())


class Depth(unittest.TestCase):
    def test_encodings(self):
        a, s = image_array("16UC1", struct.pack(">4H", 1, 2, 3, 4), 2, 2, 4, True)
        self.assertEqual((a.tolist(), s), ([[1, 2], [3, 4]], 0.001))
        a, s = image_array("32FC1", struct.pack("<3f", 1.5, 2.5, 9.0), 1, 2, 12, False)  # step с хвостом
        self.assertEqual((a.tolist(), s), ([[1.5, 2.5]], 1.0))
        with self.assertRaises(ValueError):
            image_array("rgb8", b"\0" * 12, 2, 2, 6, False)
        with self.assertRaises(ValueError):
            image_array("16UC1", b"\0" * 6, 2, 2, 4, False)

    def test_sampler_size(self):
        s = Sampler(CAMERA, 80)
        self.assertEqual((len(s.cols), len(s.rows)), (85, 48))  # 424×240 с шагом 5

    def test_coverage_of_the_corridor(self):
        # ревью W2: кадр без данных — покрытие 0, а не «пусто, всё свободно»; провал половины коридора — тоже
        sampler = Sampler(CAMERA, P.depth_cols)
        cover = Coverage(sampler, Pose(OPTICAL, CAM), P)
        self.assertEqual(len(cover.bands), 5)  # 0,49 м поперёк курса по ≈0,1 м
        self.assertTrue(all(len(b) >= 20 for b in cover.bands))
        floor = render()
        right = floor.copy()
        right[:, 212:] = 0          # правая половина кадра (оптическая ось x — вправо) без данных
        left_band = floor.copy()
        left_band[:, :40] = 0       # полоса без данных у левого края, как у D435 (стереобаза)
        close = floor.copy()
        close[120:, 150:280] = 0    # пятно перед роботом: чёрный предмет или ближе min-z
        for image, low in ((floor, False), (np.zeros_like(floor), True), (right, True), (left_band, False),
                           (close, True), (render(box=(1.35, 1.65, -0.15, 0.15, 0.0, 0.15)), False),
                           (render(edge_x=1.5), False)):
            h = hazards(image)
            self.assertEqual(h.coverage < P.depth_min_valid, low, h.coverage)
        self.assertEqual(hazards(floor).coverage, 1.0)
        self.assertEqual(hazards(np.zeros_like(floor)).coverage, 0.0)
        up = Pose(quat_matrix(*quat((0, 1, 0), -0.8)) @ OPTICAL, CAM)  # камера смотрит вверх: пола коридора не видно
        self.assertEqual(Coverage(sampler, up, P)(np.ones(len(sampler.dirs), bool)), 0.0)

    def test_flat_floor_is_floor_only(self):
        h = hazards(render())
        self.assertEqual((len(h.obstacle), len(h.drop)), (0, 0))
        near = np.min(np.hypot(*(h.floor - CAM[:2]).T))
        self.assertAlmostEqual(near, CAM[2] / math.tan(math.radians(28.3)), delta=0.1)  # пол виден с ≈1,2 м

    def test_low_box_of_lab_world(self):
        h = hazards(render(box=(1.35, 1.65, -0.15, 0.15, 0.0, 0.15)))
        self.assertGreater(len(h.obstacle), 10)
        self.assertAlmostEqual(float(np.min(h.obstacle[:, 0])), 1.35, delta=0.03)
        self.assertEqual(len(h.drop), 0)
        near = render(box=(0.6, 0.85, -0.15, 0.15, 0.0, 0.15))
        self.assertEqual(len(hazards(near).obstacle), 0)  # ближе ≈0,93 м от камеры верх ящика не виден

    def test_table_top_at_urg_height(self):
        h = hazards(render(box=(0.6, 1.6, -0.3, 0.3, 0.68, 0.71)))  # столешница мира lab: лидар под ней
        self.assertAlmostEqual(float(np.min(h.obstacle[:, 0])), 0.6, delta=0.03)
        self.assertEqual(len(hazards(render(box=(0.6, 1.6, -0.3, 0.3, 0.80, 0.83))).obstacle), 0)  # выше робота

    def test_step_edge_is_drop_at_the_edge(self):
        h = hazards(render(edge_x=1.5))
        self.assertGreater(len(h.drop), 10)
        self.assertAlmostEqual(float(np.min(h.drop[:, 0])), 1.5, delta=0.05)  # там, где пол должен был быть
        self.assertLess(float(np.max(h.floor[:, 0])), 1.5)

    def test_memory_keeps_unseen_and_clears_seen_floor(self):
        mem = Memory(P)
        mem.update((0.0, 0.0, 0.0), hazards(render(box=(1.35, 1.65, -0.15, 0.15, 0.0, 0.15), edge_x=2.2)))
        kinds = set(mem.cells.values())
        self.assertEqual(kinds, {OBSTACLE, DROP})
        pts, drop = mem.in_base((0.5, 0.0, 0.0))  # проехали 0,5 м: ящик в 0,85 м, его уже не видно
        self.assertAlmostEqual(float(np.min(pts[~drop, 0])), 0.85, delta=0.06)
        mem.update((0.5, 0.0, 0.0), Hazards(NOTHING, NOTHING, NOTHING, 1.0))
        self.assertIn(OBSTACLE, mem.cells.values())  # пустой кадр память не стирает
        mem.update((0.0, 0.0, 0.0), hazards(render()))  # ящик убрали: виден пол
        self.assertNotIn(OBSTACLE, mem.cells.values())
        mem.update((5.0, 0.0, 0.0), Hazards(NOTHING, NOTHING, NOTHING, 1.0))
        self.assertEqual(mem.cells, {})  # дальше memory_m — забыто

    def test_free_space_for_lidar_unknown(self):
        # точка плоскости скана свободна, если камера смотрит сквозь неё и видит поверхность дальше FREE_TOL_M
        sampler = Sampler(CAMERA, P.depth_cols)
        pose = Pose(OPTICAL, CAM)

        def free(image, points):
            return FreeSpace(sampler, pose, sampler.depths(image, 0.001))(np.array(points, float)).tolist()
        wall_far = render(box=(2.0, 2.1, -2.0, 2.0, 0.0, 1.0))
        wall_near = render(box=(0.37, 0.47, -2.0, 2.0, 0.0, 1.0))
        ahead = [[0.35, 0.0, 0.67], [0.35, -0.1, 0.67]]
        self.assertEqual(free(wall_far, ahead), [True, True])
        self.assertEqual(free(wall_near, ahead), [False, False])       # стена в 2 см за точкой — ближе допуска
        self.assertEqual(free(np.zeros_like(wall_far), ahead), [False, False])  # нет данных — не свободно
        side = [[0.2, 0.25, 0.67], [-0.3, 0.0, 0.67]]                  # вне поля 87° и за камерой
        self.assertEqual(free(wall_far, side), [False, False])

    def test_turned_robot_memory_in_base(self):
        mem = Memory(P)
        mem.update((1.0, 1.0, math.pi / 2), Hazards(np.array([[0.5, 0.0]] * 3), NOTHING, NOTHING, 1.0))
        pts, _ = mem.in_base((1.0, 1.0, math.pi / 2))
        np.testing.assert_allclose(pts, [[0.525, -0.025]], atol=0.03)


class Estop(unittest.TestCase):
    def test_parse(self):
        cmd = parse(json.dumps({"cmd": "release", "by": "human", "reason": "ок", "id": "x", "t": 5}))
        self.assertEqual(cmd, ("release", "human", "ок", "x", 5.0))
        self.assertIsNone(parse('{"cmd": "engage", "t": true}').t)
        self.assertEqual(parse('{"cmd": "engage"}').by, "?")
        for bad in ("{", '{"cmd": "off"}', "[]", '{"by": "human"}'):
            with self.assertRaises(ValueError):
                parse(bad)

    def test_file_format_matches_deploy_fake(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "estop.json")
            latch = Latch(path)
            self.assertFalse(latch.engage("estop", "a") and latch.engage("estop", "b"))
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            self.assertEqual((doc["engaged"], doc["reasons"]), (True, ["estop: a"]))
            self.assertFalse(os.path.exists(path + ".tmp"))
            latch.release()
            self.assertFalse(Latch(path).engaged)


class Imu(unittest.TestCase):
    def test_tilt_from_imu_orientation(self):
        base_in_imu = quat_matrix(*OPTICAL_Q).T
        for pitch in (0.0, 5.0, 12.0, -12.0):
            world_base = qmul(quat((0, 0, 1), 0.8), quat((0, 1, 0), math.radians(pitch)))  # рысканье не влияет
            self.assertAlmostEqual(tilt_deg(qmul(world_base, OPTICAL_Q), base_in_imu), abs(pitch), places=6)
        ned = qmul(quat((1, 0, 0), math.pi), OPTICAL_Q)  # мир NED (z вниз)
        self.assertAlmostEqual(tilt_deg(ned, base_in_imu), 0.0, places=6)

    def test_shock(self):
        s = Shock()
        self.assertEqual(s.feed(0.0, (0, 0, 9.81)), 0.0)
        self.assertLess(s.feed(0.005, (0.02, 0, 9.8)), 0.1)
        self.assertGreater(s.feed(0.01, (7.0, 0, 9.81)), 6.0)

    def test_stall_needs_delivery_and_time(self):
        st = Stall(P)
        self.assertFalse(st.feed(0.0, 0.1, 0.0, 0.0, 0.0, True))
        self.assertFalse(st.feed(0.9, 0.1, 0.0, 0.0, 0.0, True))
        self.assertTrue(st.feed(1.0, 0.1, 0.0, 0.0, 0.0, True))
        self.assertFalse(st.feed(1.1, 0.1, 0.0, 0.0, 0.0, False))
        self.assertFalse(st.feed(1.2, 0.01, 0.0, 0.0, 0.0, True))  # команда ниже stall_cmd — не движение


if __name__ == "__main__":
    unittest.main()
