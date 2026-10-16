"""Выход «драйверов» двойника (sensor_model.py) без ROS: скан как у urg_node2, глубина как у realsense2_camera."""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np

TWIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TWIN))

import sensor_model as sm  # noqa: E402


class ScanTest(unittest.TestCase):
    def test_no_return_and_self_hit_are_nan(self):
        out = sm.urg_ranges([0.05, 0.099, 0.15, 1.2, float("inf"), 5.7, float("nan"), 5.6], 5.6, sm.SELF_HIT_M)
        self.assertTrue(all(math.isnan(v) for v in out[:2] + out[4:7]))
        self.assertEqual(out[2:4] + out[7:], [0.15, 1.2, 5.6])

    def test_threshold_covers_camera_body_with_noise(self):
        # дальний угол корпуса D435i — 0,099 м от оси лидара; шум gpu_lidar σ 0,01 м: порог ≥ 0,099 + 5σ
        self.assertGreaterEqual(sm.SELF_HIT_M, 0.099 + 5 * 0.01 - 1e-9)


class DepthTest(unittest.TestCase):
    def test_sigma_grows_with_square(self):
        s1, s2 = sm.depth_sigma(np.array([1.0, 2.0]), 424, sm.DEPTH_HFOV, sm.SUBPIXEL, sm.BASELINE_M)
        self.assertAlmostEqual(s2 / s1, 4.0)
        f = 212 / math.tan(sm.DEPTH_HFOV / 2)
        self.assertAlmostEqual(s1, 0.08 / (f * 0.05))

    def test_mm_and_no_data(self):
        z = np.array([[1.0, 0.05, np.inf], [np.nan, 10.5, 2.5]], dtype=np.float32)
        out = sm.depth_mm(z, 0.105, 10.0, sm.DEPTH_HFOV, 0.0, sm.BASELINE_M, np.random.default_rng(0))
        self.assertEqual(out.dtype, np.uint16)
        self.assertEqual(out.tolist(), [[1000, 0, 0], [0, 0, 2500]])

    def test_noise_is_seeded_and_keeps_no_data(self):
        z = np.full((4, 424), 2.0)
        z[0, 0] = np.nan
        a = sm.depth_mm(z, 0.105, 10.0, sm.DEPTH_HFOV, sm.SUBPIXEL, sm.BASELINE_M, np.random.default_rng(1))
        b = sm.depth_mm(z, 0.105, 10.0, sm.DEPTH_HFOV, sm.SUBPIXEL, sm.BASELINE_M, np.random.default_rng(1))
        self.assertTrue(np.array_equal(a, b))
        self.assertEqual(a[0, 0], 0)
        self.assertGreater(np.std(a[1:].astype(float)), 1.0)


if __name__ == "__main__":
    unittest.main()
