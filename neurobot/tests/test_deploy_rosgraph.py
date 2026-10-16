"""neurobot_deploy.rosgraph без ROS: снимок графа DDS, пока имена узлов не пришли (_NODE_NAME_UNKNOWN_), ожидание
узла компонента (wait-graph), сводка /scan для doctor (доля негодных лучей); CLI wait-graph (с меткой для precheck),
wait-topic и zero. Узел ROS подставной. Только stdlib, Python 3.8+."""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))

from neurobot_deploy import __main__ as cli  # noqa: E402
from neurobot_deploy import checks, doctor, report, rosgraph  # noqa: E402
from neurobot_deploy.config import Config  # noqa: E402
from neurobot_deploy.sd import Entry  # noqa: E402

U = rosgraph.UNKNOWN


class FakeNode:
    """Граф, который собирается со временем: каждый опрос издателей отдаёт следующий снимок (последний — дальше)."""

    def __init__(self, pubs):
        self.pubs, self.calls = pubs, 0

    def get_publishers_info_by_topic(self, topic):
        snap = self.pubs[min(self.calls, len(self.pubs) - 1)]
        self.calls += 1
        return [types.SimpleNamespace(node_name=n) for n in snap]

    def get_subscriptions_info_by_topic(self, topic):
        return [types.SimpleNamespace(node_name="twist_mux")]


class SettleTest(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self.orig = rosgraph.time
        clock = [0.0]

        def sleep(s):
            self.sleeps.append(s)
            clock[0] += s
        rosgraph.time = types.SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)
        self.addCleanup(setattr, rosgraph, "time", self.orig)

    def test_waits_for_node_names(self):
        node = FakeNode([[], [U], [U], ["safety_gate"]])
        got = rosgraph._settle(node, ["/cmd_vel"], 0.5)
        self.assertEqual(got, {"/cmd_vel": {"pub": ["safety_gate"], "sub": ["twist_mux"]}})

    def test_keeps_most_complete_snapshot(self):
        node = FakeNode([["safety_gate", "predict_live"], ["safety_gate"]])  # издатель пропал из снимка — не теряем
        self.assertEqual(rosgraph._settle(node, ["/cmd_vel"], 0.5)["/cmd_vel"]["pub"], ["safety_gate", "predict_live"])

    def test_gives_up_on_names_after_extra_wait(self):
        node = FakeNode([[U]])
        got = rosgraph._settle(node, ["/cmd_vel"], 0.5)
        self.assertEqual(got["/cmd_vel"]["pub"], [U])
        self.assertAlmostEqual(sum(self.sleeps), 0.5 + rosgraph.UNRESOLVED_WAIT_S, delta=0.25)

    def test_wait_node_then_settle(self):
        cfg = Config({"NEUROBOT_WRITER_CHECK_S": "0.5"}, "/e/n.env")
        names = iter([[], ["twist_mux"], ["twist_mux", "motion_executor"]])
        node = FakeNode([["motion_executor"]])
        node.get_node_names = lambda: next(names)
        orig = rosgraph.session
        rosgraph.session = lambda c, role: contextlib.nullcontext(node)
        self.addCleanup(setattr, rosgraph, "session", orig)
        got = rosgraph.wait_node(cfg, "motion_executor", ["/neurobot/cmd_vel_req"], 5.0)
        self.assertEqual(got, {"/neurobot/cmd_vel_req": {"pub": ["motion_executor"], "sub": ["twist_mux"]}})
        node.get_node_names = lambda: ["twist_mux"]
        self.assertIsNone(rosgraph.wait_node(cfg, "motion_executor", ["/cmd_vel"], 1.0))

    def test_resolved_graph_returns_after_window(self):
        rosgraph._settle(FakeNode([["safety_gate"]]), ["/cmd_vel"], 0.5)
        self.assertAlmostEqual(sum(self.sleeps), 0.5, delta=0.25)
        self.sleeps.clear()
        rosgraph._settle(FakeNode([["safety_gate"]]), ["/cmd_vel"], 0.0)  # doctor: граф уже собран за наблюдение
        self.assertEqual(self.sleeps, [])


class ScanStatsTest(unittest.TestCase):
    def test_counts_unusable_rays(self):
        def scan(ranges, angle_min=-2.0923497677):
            return types.SimpleNamespace(range_min=0.02, range_max=5.6, angle_min=angle_min, angle_increment=0.0061359,
                                         ranges=ranges, header=types.SimpleNamespace(frame_id="laser_frame"))
        stats: dict = {}
        nan, inf = float("nan"), float("inf")
        rosgraph._scan_stats(stats, scan([1.0, nan, 0.006, inf]))  # коды ошибок URG ниже range_min (N14)
        rosgraph._scan_stats(stats, scan([1.0, 2.0, 0.006, 5.0]))
        self.assertEqual((stats["scans"], stats["bad"], stats["frame_id"]), (2, [0, 1, 2, 1], "laser_frame"))
        rosgraph._scan_stats(stats, scan([1.0, 1.0], angle_min=0.0))  # другая геометрия — счёт заново
        self.assertEqual((stats["scans"], stats["bad"]), (1, [0, 0]))


class CliTest(unittest.TestCase):
    """wait-graph и zero поверх подставного rosgraph: код выхода и строка причины в stderr."""
    cfg = Config({}, "/e/n.env")

    def run_cmd(self, fn, component: str = "", cfg: Config = cfg, **patch) -> tuple:
        for name, value in patch.items():
            self.addCleanup(setattr, rosgraph, name, getattr(rosgraph, name))
            setattr(rosgraph, name, value)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = fn(cfg, types.SimpleNamespace(component=component))
        return code, err.getvalue()

    def test_wait_graph(self):
        req = "/neurobot/cmd_vel_req"
        code, err = self.run_cmd(cli.cmd_wait_graph, "executor",
                                 wait_node=lambda c, n, t, s: {req: {"pub": ["motion_executor"]}})
        self.assertEqual(code, 0)
        self.assertIn("neurobot-wait: executor: ready: узел motion_executor не пишет в /cmd_vel", err)
        self.assertIn("; пишет в " + req, err)
        lost = {"/cmd_vel": {"pub": ["safety_gate", "velocity_publisher"]}}
        code, err = self.run_cmd(cli.cmd_wait_graph, "perception", wait_node=lambda c, n, t, s: lost)
        self.assertEqual(code, 1)
        self.assertIn("neurobot-wait: perception: cmd_vel_writer: узел velocity_publisher пишет в /cmd_vel", err)
        code, err = self.run_cmd(cli.cmd_wait_graph, "perception", wait_node=lambda c, n, t, s: None)
        self.assertEqual(code, 1)
        self.assertIn("node_missing: узла velocity_publisher нет в графе ROS за 30 с", err)

        def no_ros(*_):
            raise rosgraph.RosUnavailable("rclpy недоступен")
        code, err = self.run_cmd(cli.cmd_wait_graph, "executor", wait_node=no_ros)
        self.assertEqual(code, 1)
        self.assertIn("executor: ros_unavailable", err)

    def test_lost_remap_names_writer_and_leaves_mark(self):
        # потерянный ремап исполнителя: одна строка cmd_vel_writer — её doctor и назовёт (последняя с приоритетом ≤ 3)
        lost = {"/cmd_vel": {"pub": ["safety_gate", "motion_executor"]}, "/neurobot/cmd_vel_req": {"pub": []}}
        cfg = Config({"NEUROBOT_EXECUTOR_CMD": "python3 -m neurobot.executor --ros-args -r /cmd_vel:=/neurobot/x"},
                     "/e/n.env")
        self.addCleanup(setattr, report, "_stderr_is_journal", report._stderr_is_journal)
        report._stderr_is_journal = lambda: True  # приоритеты syslog — как в журнале юнита
        with tempfile.TemporaryDirectory() as state:
            for where, mark in ((state, True), (os.path.join(state, "нет"), False)):
                with self.subTest(state=where), mock.patch.dict(os.environ, {"STATE_DIRECTORY": where}):
                    code, err = self.run_cmd(cli.cmd_wait_graph, "executor", cfg, wait_node=lambda c, n, t, s: lost)
                    self.assertEqual(code, 1)
                    entries = [Entry(0.0, int(ln[1]), ln[3:], "i") for ln in err.splitlines()]
                    self.assertEqual(doctor._reason(entries).code, "cmd_vel_writer")
                    self.assertEqual([e.priority for e in entries], [3] if mark else [3, 4])
                    if mark:  # следующий precheck с той же командой откажет
                        (p,) = checks.check_writer_mark("executor", cfg, where)
                        self.assertEqual(p.code, "cmd_vel_writer")
                    else:
                        self.assertIn("метку для precheck не записать", entries[-1].message)

    def test_ros_init_failure_is_a_reason_line(self):
        # RCLError инициализации rclpy — RuntimeError: строка причины вместо трейсбека в ExecStopPost и ExecStartPost
        def rcl_error(*_a):
            raise RuntimeError("rcl_init failed")
        code, err = self.run_cmd(cli.cmd_zero, publish_zero=rcl_error)
        self.assertEqual(code, 1)
        self.assertIn("neurobot-zero: safety: ros_unavailable: явный ноль не отправлен: rcl_init failed", err)
        driver = Config({"NEUROBOT_DRIVER_lidar_TOPIC": "/scan"}, "/e/n.env")
        code, err = self.run_cmd(cli.cmd_wait_topic, "driver@lidar", driver, wait_topic=rcl_error)
        self.assertEqual(code, 1)
        self.assertIn("neurobot-wait: driver@lidar: ros_unavailable: rcl_init failed", err)

    def test_zero_counts_subscribers(self):
        code, err = self.run_cmd(cli.cmd_zero, publish_zero=lambda c: 1)
        self.assertEqual(code, 0)
        self.assertIn("neurobot-zero: safety: zero_sent: /cmd_vel: нули Twist, подписчиков 1", err)
        code, err = self.run_cmd(cli.cmd_zero, publish_zero=lambda c: 0)  # twist_mux не слушал: ноль не доставлен
        self.assertEqual(code, 1)
        self.assertIn("neurobot-zero: safety: zero_undelivered: явный ноль в /cmd_vel не доставлен", err)


class SessionTest(unittest.TestCase):
    def test_without_rcl_log_files(self):
        # без HOME и ROS_LOG_DIR (systemd-run, cron) файловый журнал rcl не находит каталог, и init падает: он выключен
        calls = []
        fake = types.SimpleNamespace(
            Context=lambda: "ctx", init=lambda args, context: calls.append(("init", args, context)),
            create_node=lambda name, context: types.SimpleNamespace(destroy_node=lambda: calls.append(("node", name))),
            shutdown=lambda context: calls.append(("shutdown", context)))
        with mock.patch.object(rosgraph, "_rclpy", return_value=fake), mock.patch.dict(os.environ):
            with rosgraph.session(Config({"ROS_DOMAIN_ID": "41"}, "/e/n.env"), "doctor"):
                self.assertEqual(os.environ["ROS_DOMAIN_ID"], "41")
        self.assertEqual(calls, [("init", ["--ros-args", "--disable-external-lib-logs"], "ctx"),
                                 ("node", "neurobot_doctor_{}".format(os.getpid())), ("shutdown", "ctx")])


if __name__ == "__main__":
    unittest.main()
