"""Логика проб двойника (twin_probe.py) без ROS: блоки NaN, частоты, наклон по IMU, точки глубины, остановка,
одометрия против истины, SDF бокса для сервиса create (RTF — tests/test_truth.py)."""
from __future__ import annotations

import math
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

TWIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TWIN))

import twin_config as cfg  # noqa: E402
import twin_probe as tp  # noqa: E402


def quat_axis(axis, angle):
    s = math.sin(angle / 2)
    return [axis[0] * s, axis[1] * s, axis[2] * s, math.cos(angle / 2)]


def quat_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz]


class BlindTest(unittest.TestCase):
    def test_blocks_and_gaps(self):
        flags = [False, True, True, False, True, False, False, True]
        self.assertEqual(tp.nan_blocks(flags), [(1, 2), (4, 4), (7, 7)])
        self.assertEqual(tp.merge_gaps([(1, 2), (4, 4), (7, 7)], 1), [(1, 4), (7, 7)])
        self.assertEqual(tp.nan_blocks([]), [])

    def test_summary_robot_block_with_hole(self):
        frac = [0.0] * cfg.SCAN_N
        for i in range(168, 353):
            frac[i] = 1.0
        frac[213] = 0.0                      # стык gpu_lidar
        frac[460] = 0.95                     # дверной проём: тоже стабильный NaN, но короткий
        b = tp.blind_summary(frac)
        m = b["main"]
        self.assertEqual((m["first"], m["last"], m["holes"]), (168, 352, [213]))
        self.assertEqual((m["robot_first"], m["robot_last"]), (168, 353))
        self.assertEqual(len(b["blocks"]), 3)

    def test_unstable_nan_not_blind(self):
        frac = [0.5] * cfg.SCAN_N
        self.assertNotIn("main", tp.blind_summary(frac))


class RateTest(unittest.TestCase):
    def test_rate(self):
        self.assertAlmostEqual(tp.rate([0.0, 0.1, 0.2, 0.3]), 10.0)
        self.assertIsNone(tp.rate([1.0]))
        self.assertIsNone(tp.rate([1.0, 1.0]))


class TiltTest(unittest.TestCase):
    q_link_opt = quat_mul(quat_axis((0, 0, 1), -math.pi / 2), quat_axis((1, 0, 0), -math.pi / 2))

    def test_accel_flat_and_nose_up(self):
        self.assertAlmostEqual(tp.tilt_accel([0.0, -9.81, 0.0]), 0.0)
        th = math.radians(12)
        # в кадре звена: x = g·sinθ, z = g·cosθ → в оптическом: x_opt = −y = 0, y_opt = −z, z_opt = x
        self.assertAlmostEqual(tp.tilt_accel([0.0, -9.81 * math.cos(th), 9.81 * math.sin(th)]), 12.0, places=6)

    def test_orientation_nose_up_any_yaw(self):
        for yaw in (0.0, 1.0, -2.5):
            q_world_link = quat_mul(quat_axis((0, 0, 1), yaw), quat_axis((0, 1, 0), -math.radians(12)))
            q = quat_mul(q_world_link, self.q_link_opt)
            self.assertAlmostEqual(tp.tilt_orientation(q), 12.0, places=6)
        self.assertAlmostEqual(tp.tilt_orientation(self.q_link_opt), 0.0, places=6)

    def test_static_rotation_matches_urdf(self):
        m = tp.quat_matrix(*self.q_link_opt)
        self.assertTrue(np.allclose(m, tp.R_LINK_OPT))


class DepthTest(unittest.TestCase):
    def test_points_in_base_frame(self):
        k = [100.0, 0, 2.0, 0, 100.0, 1.0, 0, 0, 1]
        img = np.zeros((3, 5), dtype=np.uint16)
        img[1, 2] = 2000                     # центр кадра, 2 м по оптической оси
        img[2, 2] = 1000                     # строка ниже центра, 1 м
        pts = tp.depth_points(img, k, tp.R_LINK_OPT, (0.05, -0.04, 0.665))
        self.assertEqual(len(pts), 2)
        self.assertTrue(np.allclose(pts[0], [2.05, -0.04, 0.665]))
        self.assertTrue(np.allclose(pts[1], [1.05, -0.04, 0.665 - 0.01]))

    def test_profile_bins(self):
        pts = np.array([[1.31, 0.0, 0.15], [1.33, 0.1, 0.10], [1.35, 0.5, 0.15], [2.55, 0.0, 0.0], [-1.0, 0, 0]])
        prof = tp.depth_profile(pts)
        self.assertEqual([b[0] for b in prof], [1.3, 2.5])
        self.assertEqual(prof[0][1:], [2, 0.125, 0.15])


class StopTest(unittest.TestCase):
    def test_stop_times(self):
        out = [(0.9, 0.2), (1.2, 0.2), (1.52, 0.18), (1.6, 0.1), (1.72, 0.0)]
        truth = [(1.0, 0.2), (1.7, 0.05), (1.8, 0.005)]
        st = tp.stop_times(1.0, out, truth, 0.2)
        self.assertEqual(st, {"brake_s": 0.52, "out_zero_s": 0.72, "truth_stop_s": 0.8})

    def test_hold_never_stops(self):
        st = tp.stop_times(1.0, [(1.5, 0.3), (3.0, 0.3)], [(2.0, 0.3)], 0.3)
        self.assertEqual(st, {"brake_s": None, "out_zero_s": None, "truth_stop_s": None})


class OdomTest(unittest.TestCase):
    def test_relative_pose(self):
        x, y, yaw = tp.relative((1.0, 1.0, math.pi / 2), (1.0, 2.0, math.pi))
        self.assertAlmostEqual(x, 1.0)
        self.assertAlmostEqual(y, 0.0)
        self.assertAlmostEqual(yaw, math.pi / 2)

    def test_error_in_start_frame(self):
        # истина стартует в (−1,5; 0) мира, одометрия — в (0; 0): ошибки нет, хотя кадры разные
        truth = [(t, -1.5 + 0.1 * t, 0.0, 0.0) for t in range(5)]
        odom = [(t, 0.1 * t, 0.0, 0.0) for t in range(5)]
        e = tp.odom_error(odom, truth)
        self.assertAlmostEqual(e["pos_err_end_m"], 0.0)
        self.assertAlmostEqual(e["path_m"], 0.4)
        slip = [(t, 0.15 * t, 0.0, 0.0) for t in range(5)]
        self.assertAlmostEqual(tp.odom_error(slip, truth)["pos_err_end_m"], 0.2)
        self.assertEqual(tp.odom_error([], truth), {})


class BoxSdfTest(unittest.TestCase):
    def test_box_sdf(self):
        model = ET.fromstring(tp.box_sdf("cable", cfg.TRUTH_CABLE[2])).find("model")
        self.assertEqual(model.get("name"), "cable")
        self.assertEqual(model.findtext("static"), "true")
        size = [float(v) for v in model.findtext("link/collision/geometry/box/size").split()]
        self.assertEqual(size, list(cfg.TRUTH_CABLE[2]))
        self.assertEqual(model.findtext("link/visual/geometry/box/size"), model.findtext(
            "link/collision/geometry/box/size"))


if __name__ == "__main__":
    unittest.main()
