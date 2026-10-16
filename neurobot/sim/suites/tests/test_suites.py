"""Наборы на двойнике (neurobot/sim/suites) без rclpy, Python ≥ 3.8: метрики по истине Gazebo и safety_state, профиль
twin.env и конфиги агентского пути против проверок пакета deploy (те же, что в ExecStartPre на роботе).

Сами сценарии (sensors.py, safety.py) — проверки на живом двойнике в ВМ: их итог — каталог результатов прогона.
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

SUITES = Path(__file__).resolve().parents[1]
ROOT = SUITES.parents[2]
sys.path[:0] = [str(SUITES), str(ROOT / "neurobot" / "deploy")]

import metrics as m  # noqa: E402
from neurobot_deploy import checks, render  # noqa: E402
from neurobot_deploy.config import Config, parse_env  # noqa: E402


def truth(t, v=0.0, x=0.0, y=0.0, yaw=0.0, clearance=1.0, contacts=(), rtf_ok=True, w=0.0):
    return {"t": t, "v": v, "w": w, "x": x, "y": y, "yaw": yaw, "clearance_m": clearance,
            "contacts": list(contacts), "rtf_ok": rtf_ok}


def state(t, st="slow", reasons=("lidar_blind: …",)):
    return {"t": t, "state": st, "reasons": list(reasons)}


class Scan(unittest.TestCase):
    def test_nan_runs_and_camera_block_with_hole(self):
        inc = math.radians(1.0)
        ranges = [1.0] * 10 + [math.nan] * 5 + [2.0] + [None] * 4 + [1.0] * 5 + [math.inf] * 2 + [1.0]
        runs = m.nan_runs(ranges, 0.0, inc)
        self.assertEqual(runs, [[10.0, 14.0, 5], [16.0, 19.0, 4], [25.0, 26.0, 2]])
        self.assertEqual(m.nan_runs(ranges, 0.0, inc, min_len=3), [[10.0, 14.0, 5], [16.0, 19.0, 4]])
        # луч 213 двойника проходит сквозь корпус камеры: серии склеиваются через дыру в один луч
        self.assertEqual(m.block_at(runs, 12.0, 2.5), [10.0, 19.0, 9])
        self.assertEqual(m.block_at(runs, 12.0, 1.0), [10.0, 14.0, 5])
        self.assertIsNone(m.block_at(runs, 22.0, 2.5))

    def test_rate(self):
        self.assertAlmostEqual(m.rate([0.0, 0.1, 0.2, 0.3]), 10.0)
        self.assertIsNone(m.rate([1.0]))
        self.assertIsNone(m.rate([1.0, 1.0]))


class Truth(unittest.TestCase):
    def test_clearance_contacts_and_rtf(self):
        rows = [truth(0.0, clearance=0.5), truth(0.1, clearance=None), truth(0.2, clearance=0.2, rtf_ok=False),
                truth(0.3, clearance=0.3, contacts=["wall_east"])]
        ok = m.ok_truth(rows)
        self.assertEqual(len(ok), 3)
        self.assertEqual(m.min_clearance(ok), 0.3)       # null (форма неизвестна) и rtf ниже порога — не в счёт
        self.assertEqual(m.touched(rows), ["wall_east"])
        self.assertIsNone(m.min_clearance([truth(0.0, clearance=None)]))
        self.assertEqual(len(m.ok_truth(rows, 0.1, 0.25)), 1)

    def test_braking_from_event(self):
        rows = [truth(i * 0.05, v=0.15 if i < 10 else max(0.0, 0.15 - 0.03 * (i - 9)), x=min(i, 14) * 0.0075)
                for i in range(40)]
        b = m.braking(rows, 0.45)
        self.assertEqual(b["v0"], 0.15)
        self.assertAlmostEqual(b["stop_s"], 0.25, places=3)   # |v| < 0,005 с 0,70 с и держится ≥ 0,3 с
        self.assertAlmostEqual(b["coast_m"], 0.0375, delta=0.001)  # округление до мм
        self.assertEqual(m.braking([truth(0.0, v=0.1), truth(0.1, v=0.1)], 0.0)["stop_s"], None)
        self.assertEqual(m.braking([], 0.0), {"v0": None, "stop_s": None, "coast_m": None})

    def test_zero_after_and_stop_reason(self):
        cmds = [{"t": 0.0, "v": 0.0, "w": 0.0}, {"t": 0.1, "v": 0.1, "w": 0.0}, {"t": 0.2, "v": 0.0, "w": 0.0}]
        self.assertEqual(m.zero_after(cmds, 0.0), 0.2)
        self.assertIsNone(m.zero_after(cmds[:2], 0.0))          # ноль до движения — не стоп
        self.assertIsNone(m.zero_after(cmds[:1], 0.0))
        states = [state(0.0), state(0.3, "blocked", ("obstacle: глубина", "lidar_blind: …"))]
        self.assertEqual(m.stop_reason(states, 0.0), ["lidar_blind"])
        self.assertEqual(m.stop_reason(states, 0.1), ["lidar_blind", "obstacle"])  # состояние сразу после нуля
        self.assertEqual(m.stop_reason(states, None), [])

    def test_false_stops(self):
        rows = [truth(i * 0.05, clearance=1.2) for i in range(20)]
        hazard = [state(0.0), state(0.2, "blocked", ("lidar_nodata: …",)), state(0.4)]
        (f,) = m.false_stops(hazard, rows)
        self.assertEqual((f["codes"], f["clearance_m"]), (["lidar_nodata"], 1.2))
        near = [truth(i * 0.05, clearance=0.1) for i in range(20)]
        self.assertEqual(m.false_stops(hazard, near), [])     # рядом препятствие — стоп настоящий
        cliff = [state(0.0), state(0.2, "blocked", ("cliff: край пола",))]
        self.assertEqual(m.false_stops(cliff, rows), [])      # обрыв истина не видит
        stale = [state(0.0), state(0.2, "blocked", ("stale_scan: …",))]
        self.assertEqual(m.false_stops(stale, rows), [])      # отказ датчика — не ложная опасность

    def test_odom_against_truth_in_start_frames(self):
        # одометрия и истина в разных системах (начало одометрии — точка старта, мир — комната): сравнение в позе старта
        odom = [{"t": i * 0.1, "x": i * 0.1, "y": 0.0, "yaw": 0.0} for i in range(11)]
        gt = [{"t": i * 0.1, "x": 2.0, "y": 1.0 + i * 0.1, "yaw": math.pi / 2} for i in range(11)]
        e = m.odom_error(odom, gt)
        self.assertAlmostEqual(e["max_pos_m"], 0.0, places=6)
        self.assertAlmostEqual(e["path_m"], 1.0, places=6)
        gt[-1]["x"] += 0.03
        self.assertAlmostEqual(m.odom_error(odom, gt)["end_pos_m"], 0.03, places=4)
        self.assertEqual(m.odom_error([], gt), {})

    def test_tilt_by_rest_acceleration(self):
        self.assertAlmostEqual(m.tilt_deg(0.0, 9.81, 0.0), 0.0)   # оптический кадр IMU: g вдоль y
        g = 9.81
        a = math.radians(12.0)
        self.assertAlmostEqual(m.tilt_deg(0.0, g * math.cos(a), g * math.sin(a)), 12.0, places=6)
        self.assertTrue(math.isnan(m.tilt_deg(0.0, 0.0, 0.0)))


class Profile(unittest.TestCase):
    """twin.env и конфиги агентского пути проходят те же проверки пакета deploy, что на роботе."""

    def cfg(self, **extra) -> Config:
        values = parse_env((SUITES / "twin.env").read_text(encoding="utf-8"))
        values.update(extra)
        return Config(values, str(SUITES / "twin.env"))

    def test_units_render_with_twin_driver(self):
        cfg = self.cfg()
        self.assertEqual(cfg.instances("DRIVER"), ["twin"])
        units = render.enable_list(cfg)
        self.assertIn("neurobot-driver@twin.service", units)
        for unit in ("neurobot-safety.service", "neurobot-executor.service", "neurobot-perception.service",
                     "neurobot-rosbridge.service", "neurobot-mcp.service", "neurobot-picoclaw.service"):
            self.assertIn(unit, units)

    def test_motion_chain_and_agent_path_checks(self):
        cfg = self.cfg(NEUROBOT_MCP_CONFIG=str(SUITES / "ros_mcp.json"))
        self.assertEqual(checks.check_rosbridge(cfg), [])
        self.assertEqual(checks.check_mcp_config(cfg), [])
        self.assertEqual(checks.check_remap("executor", cfg), [])
        self.assertEqual(checks.check_remap("perception", cfg), [])
        self.assertEqual(checks.camera_mode(cfg), "ros")       # кадры темы, как realsense2_camera на роботе
        self.assertNotIn("ros_mcp_server.py", cfg.get("NEUROBOT_FOREIGN_MCP").split())

    def test_picoclaw_config_for_028(self):
        cfg = self.cfg(PICOCLAW_CONFIG=str(SUITES / "picoclaw.json"), NEUROBOT_PICOCLAW_VERSION="0.2.8")
        self.assertEqual([p.text for p in checks.check_picoclaw_config(cfg) if p.fatal], [])


if __name__ == "__main__":
    unittest.main()
