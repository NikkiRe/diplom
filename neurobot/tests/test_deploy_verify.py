"""Замеры neurobot/deploy/verify (k6.py, recovery.py, agent_truth.py): разбор записей без rclpy — задержки стопа через
MCP и по скану, квантили, ближайшая дальность в секторе, выбор узла в юните, случаи из env-файла, кто перезапустил,
честность ответа толчка против сдвига. Сами замеры (rclpy, systemd, Gazebo) — на двойнике, артефакты
experiments/results/neurobot_20261018_deploy_fix. Python 3.8+."""
from __future__ import annotations

import math
import sys
import types
import unittest
from pathlib import Path

VERIFY = Path(__file__).resolve().parents[1] / "deploy" / "verify"
sys.path.insert(0, str(VERIFY))

import agent_truth  # noqa: E402
import k6  # noqa: E402
import recovery  # noqa: E402
import stand  # noqa: E402
from neurobot_deploy.config import Config  # noqa: E402


class StandTest(unittest.TestCase):
    def test_quantiles_skip_none(self):
        self.assertEqual(stand.quantiles([]), {"n": 0, "p50": None, "p95": None, "max": None})
        q = stand.quantiles([0.3, None, 0.1, 0.2])
        self.assertEqual((q["n"], q["p50"], q["max"]), (3, 0.2, 0.3))
        self.assertAlmostEqual(q["p95"], 0.29)

    def test_first_and_zero(self):
        rows = [(1.0, 0.1, 0.0), (2.0, 0.0, 0.0), (3.0, 0.0, 0.2)]
        self.assertEqual(stand.first(rows, 0.0, stand.zero), 2.0)
        self.assertEqual(stand.first(rows, 2.5, stand.zero), None)
        self.assertEqual(stand.first(rows, 2.5, lambda r: r[2] > 0), 3.0)

    def test_wrap(self):
        self.assertAlmostEqual(stand.wrap(3 * math.pi / 2), -math.pi / 2)
        self.assertAlmostEqual(stand.wrap(-0.1), -0.1)

    def test_nearest_in_sector_uses_valid_ranges_only(self):
        ranges = [0.2, 1.0, 1.0, 1.0, 1.0, 0.25, 0.01, float("nan"), float("inf")]  # лучи −40…+40° через 10°
        msg = types.SimpleNamespace(angle_min=math.radians(-40), angle_increment=math.radians(10), ranges=ranges,
                                    range_min=0.02, range_max=4.0)
        self.assertEqual(stand.nearest(msg, 5.0, 25.0), 0.25)  # 0,01 — ниже range_min, NaN и inf — не дальность
        self.assertEqual(stand.nearest(msg, 25.0, 40.0), math.inf)
        self.assertEqual(stand.nearest(msg, -45.0, -35.0), 0.2)


class K6Test(unittest.TestCase):
    def test_mcp_run_marks_from_call(self):
        agent = [(9.0, 0.0, 0.0), (10.08, 0.0, 0.0)]
        req = [(10.0, 0.15, 0.0), (10.05, 0.15, 0.0), (10.11, 0.0, 0.0)]
        cmd = [(10.0, 0.15, 0.0), (10.12, 0.15, 0.0), (10.15, 0.12, 0.0), (10.6, 0.0, 0.0)]
        r = k6.mcp_run(10.0, 10.3, agent, req, cmd, 0.15)
        self.assertEqual(r, {"v_ref": 0.15, "call_s": 0.3, "agent_zero_s": 0.08, "req_zero_s": 0.11,
                             "brake_s": 0.15, "zero_s": 0.6})
        self.assertEqual(k6.mcp_run(10.0, None, [], [], cmd[:2], 0.15)["zero_s"], None)

    def test_scan_run_latency_may_be_negative(self):
        scans = [(5.0, 1.2), (5.1, 0.24), (5.2, 0.23)]
        cmd = [(5.0, 0.08, 0.0), (5.12, 0.08, 0.0), (5.15, 0.0, 0.0)]
        self.assertEqual(k6.scan_run(5.05, scans, cmd, 0.35),
                         {"seen_s": 0.05, "zero_after_set_s": 0.1, "zero_after_scan_s": 0.05})
        early = [(5.0, 0.08, 0.0), (5.08, 0.0, 0.0)]  # глубина остановила раньше скана лидара
        self.assertAlmostEqual(k6.scan_run(5.05, scans, early, 0.35)["zero_after_scan_s"], -0.02)
        self.assertEqual(k6.scan_run(5.05, scans[:1], cmd, 0.35)["zero_after_scan_s"], None)

    def test_max_gap_self_check(self):
        self.assertEqual(k6.max_gap([0.0, 0.02, 0.5, 0.52, 3.0], 0.0, 1.0), 0.48)
        self.assertIsNone(k6.max_gap([0.0, 2.0], 0.5, 1.0))

    def test_summary_counts_within_limit(self):
        rows = [{"x": 0.05}, {"x": 0.2}, {"x": None}, {}]
        s = k6.summary(rows, "x", 0.1)
        self.assertEqual((s["n"], s["within"], s["limit_s"]), (2, 1, 0.1))

    def test_args(self):
        a = k6.parse(["--out", "f.json", "--sector", "5,30", "--runs", "3"])
        self.assertEqual((a.sector, a.runs, a.obstacle, a.drive_topic), ((5.0, 30.0), 3, "twin", "/cmd_vel_gesture"))


class RecoveryTest(unittest.TestCase):
    def test_victim_is_node_not_launch(self):
        cmds = {10: "ros2 launch supervised.launch.py", 12: "python3 rosapi_node", 11: "python3 rosbridge_websocket"}
        self.assertEqual(recovery.victim([12, 10, 11], 10, "", cmds.get), 11)
        self.assertEqual(recovery.victim([12, 10, 11], 10, "rosapi", cmds.get), 12)
        self.assertIsNone(recovery.victim([10], 10, "", cmds.get))
        self.assertIsNone(recovery.victim([10, 11], 10, "urg_node2", cmds.get))
        gz = {20: "ros2 launch", 21: "/bin/sh -c ruby gz sim -r -s world.sdf", 22: "gz sim -r -s world.sdf"}
        self.assertEqual(recovery.victim([20, 21, 22], 20, "^gz sim", gz.get), 22)  # сервер, а не обёртка sh

    def test_cases_from_env(self):
        c = Config({"NEUROBOT_DRIVERS": "lidar camera", "NEUROBOT_COMPONENTS": "safety rosbridge"}, "/e/n.env")
        self.assertEqual(recovery.cases(c, []), [
            ("driver@lidar", "neurobot-driver@lidar.service", ""),
            ("driver@camera", "neurobot-driver@camera.service", ""),
            ("rosbridge", "neurobot-rosbridge.service", "rosbridge_websocket")])
        self.assertEqual(recovery.cases(c, ["driver@camera:sensor_model"]),
                         [("driver@camera", "neurobot-driver@camera.service", "sensor_model")])
        with self.assertRaises(SystemExit):
            recovery.cases(c, ["driver@base"])

    def test_who_restarted(self):
        journal = ("x\nneurobot-watchdog: driver@lidar: sensor_stale: restart neurobot-driver@lidar.service: темы\n"
                   "[ERROR] [supervised]: процесс sensor_model.py-1 завершился с кодом -9 — гашу launch\nStarted\n")
        got = recovery.who(journal)
        self.assertEqual(len(got), 2)
        self.assertIn("restart neurobot-driver@lidar.service", got[0])
        self.assertIn("гашу launch", got[1])


class AgentTruthTest(unittest.TestCase):
    def test_path_and_verdict(self):
        self.assertAlmostEqual(agent_truth.path_len([(0, 0.0, 0.0, 0), (1, 0.3, 0.4, 0), (2, 0.3, 0.4, 0)]), 0.5)
        self.assertEqual(agent_truth.path_len([]), 0.0)
        self.assertEqual(agent_truth.verdict({"ok": True}, True), "truth")
        self.assertEqual(agent_truth.verdict({"ok": True}, False), "lie_ok")  # «проехал», а робот стоит
        self.assertEqual(agent_truth.verdict({"ok": False, "status": "blocked"}, False), "truth")
        self.assertEqual(agent_truth.verdict({"ok": False, "status": "unconfirmed"}, True), "lie_refused")
        self.assertEqual(agent_truth.verdict("ошибка: обрыв", False), "truth")

    def test_table(self):
        rows = [{"condition": "estop", "ok": False, "status": "estop", "moved": False},
                {"condition": "estop", "ok": False, "status": "estop", "moved": False},
                {"condition": "free", "ok": True, "status": None, "moved": True}]
        self.assertEqual(agent_truth.table(rows), {"estop": {"estop/still": 2}, "free": {"ok/moved": 1}})


if __name__ == "__main__":
    unittest.main()
