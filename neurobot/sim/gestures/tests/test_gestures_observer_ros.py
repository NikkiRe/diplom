"""Наблюдатель как узел ROS 2 (только где есть rclpy — ВМ sim): процесс observer в отдельном ROS_DOMAIN_ID,
сценарий публикует жесты, команды (TwistStamped в /cmd_vel и Twist во вход стража), одометрию, истинную
позу и здоровье по истине gesturegen; после SIGTERM проверяется запись и разбор. Отдельно: издатель другого
типа в теме скоростей (код 3 и событие в записи), остановка без якоря (код 1), существующий --out, неверно
заданные препятствия, нет каталога для --out, --summary или --per-gesture и флаги разбора без --truth и --anchor
(код 2 до записи); сводку не удалось записать после прогона (код 2, запись цела).

Манёвры в сценарии укорочены (таблица --maneuvers), чтобы прогон занимал ≈25 с.
"""
from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

try:
    import rclpy
    from geometry_msgs.msg import PoseStamped, Twist, TwistStamped
    from nav_msgs.msg import Odometry
    from std_msgs.msg import String
except ImportError:
    rclpy = None

from neurobot.sim.gestures.analyze import REFLEX, run  # noqa: E402
from neurobot.sim.gestures.clock import load_or_create_anchor, mono  # noqa: E402
from neurobot.sim.gestures.gesturegen import GenConfig, write_outputs  # noqa: E402
from neurobot.sim.gestures.timeline import file_sha256, read_jsonl  # noqa: E402

SHORT = {"come": [["F", 0.5]], "turn180": [["R", 0.6]], "go_left": [["L", 0.3], ["F", 0.3], ["R", 0.3]],
         "go_right": [["R", 0.3], ["F", 0.3], ["L", 0.3]], "think": [["R", 0.3], ["F", 0.3], ["L", 0.5], ["F", 0.3]]}
SIGN = {"F": (0.2, 0.0), "L": (0.0, 0.8), "R": (0.0, -0.8)}


def _stamp(msg, m: float) -> None:
    msg.header.stamp.sec = int(m)
    msg.header.stamp.nanosec = int((m - int(m)) * 1e9)


@unittest.skipUnless(rclpy is not None, "нет rclpy (тест — для ВМ sim)")
class ObserverRosTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.domain = os.environ.get("NEUROBOT_TEST_DOMAIN") or str(70 + os.getpid() % 30)
        os.environ["ROS_DOMAIN_ID"] = self.domain
        timeline, truth = write_outputs(GenConfig(seed=1, gestures=4, negatives=1, lead_in_s=1.0, pause_s=(2.5, 3.0),
                                                  dur_s=(0.8, 1.0), neg_kinds=("hand_still",), still_s=(1.0, 1.5)), d)
        self.truth, self.timeline = truth, timeline
        self.anchor_path = os.path.join(d, "anchor.json")
        self.paths = {k: os.path.join(d, k) for k in ("events.jsonl", "summary.json", "gestures.jsonl",
                                                       "maneuvers.json", "walls.json")}
        Path(self.paths["maneuvers.json"]).write_text(json.dumps({"maneuvers": SHORT, "reflex": REFLEX}))
        Path(self.paths["walls.json"]).write_text(json.dumps([{"name": "lab_wall", "cx": 3.0, "cy": 0.0, "sx": 0.1,
                                                                 "sy": 4.0}]))

    def tearDown(self):
        self.tmp.cleanup()

    def start_observer(self, *args):
        path = os.pathsep.join(p for p in (str(ROOT), os.environ.get("PYTHONPATH")) if p)    # rclpy — из окружения ROS
        env = dict(os.environ, PYTHONPATH=path, PYTHONDONTWRITEBYTECODE="1", ROS_DOMAIN_ID=self.domain)
        cmd = [sys.executable, "-m", "neurobot.sim.gestures.observer", "--out", self.paths["events.jsonl"],
               "--node-name", "gesture_observer_test"] + list(args or (
                   "--truth", self.truth, "--anchor", self.anchor_path, "--summary", self.paths["summary.json"],
                   "--per-gesture", self.paths["gestures.jsonl"], "--cmd-vel-type", "twist_stamped",
                   "--extra-cmd", "/cmd_vel_gesture", "--maneuvers", self.paths["maneuvers.json"],
                   "--walls", self.paths["walls.json"], "--min-phase-s", "0.15", "--dur-tol-abs", "0.15",
                   "--probe", os.path.join(self.tmp.name, "no_probe.jsonl")))
        return subprocess.Popen(cmd, cwd=str(ROOT), env=env, stderr=subprocess.PIPE, text=True)

    def stop_observer(self, proc) -> str:
        proc.send_signal(signal.SIGTERM)
        return self.finish(proc)

    @staticmethod
    def finish(proc) -> str:
        """stderr наблюдателя, который должен выйти сам; не вышел за 60 с — убит: тест падает, процесса не остаётся."""
        try:
            return proc.communicate(timeout=60)[1]
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise

    def test_type_mismatch_missing_anchor_and_existing_out(self):
        events = Path(self.paths["events.jsonl"])
        rclpy.init()
        node = rclpy.create_node("gesture_mismatch_test")
        pub = node.create_publisher(TwistStamped, "/cmd_vel", 10)

        def spin_until(cond) -> bool:
            deadline = mono() + 20.0
            while mono() < deadline and not cond():
                pub.publish(TwistStamped())
                rclpy.spin_once(node, timeout_sec=0.1)
            return cond()

        try:
            proc = self.start_observer("--cmd-vel-type", "twist")
            found = spin_until(lambda: events.exists() and "cmd_type_mismatch" in events.read_text())
            err = self.stop_observer(proc)
            self.assertTrue(found, err)
            self.assertEqual(proc.returncode, 3, err)
            self.assertIn("geometry_msgs/msg/TwistStamped", err)
            proc = self.start_observer("--truth", self.truth, "--anchor", self.anchor_path, "--append")
            spin_until(lambda: "gesture_observer_test" in node.get_node_names())
            err = self.stop_observer(proc)                    # обработчики сигналов стоят раньше узла
            self.assertEqual(proc.returncode, 1, err)
            self.assertIn("якоря", err)
            self.assertNotIn("Traceback", err)
        finally:
            node.destroy_node()
            rclpy.shutdown()
        proc = self.start_observer()
        self.finish(proc)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(sum(1 for ln in events.read_text().splitlines() if '"header"' in ln), 2)

    def test_bad_walls_exit_2_before_recording(self):
        sdf = os.path.join(self.tmp.name, "empty.sdf")
        Path(sdf).write_text('<sdf version="1.9"><world name="w"/></sdf>')
        for args in (("--walls", os.path.join(self.tmp.name, "missing.json")), ("--world-sdf", sdf),
                     ("--world-sdf", sdf, "--wall-regex", "(")):
            proc = self.start_observer(*args)
            err = self.finish(proc)
            self.assertEqual(proc.returncode, 2, err)
            self.assertNotIn("Traceback", err)
        self.assertFalse(Path(self.paths["events.jsonl"]).exists())

    def test_unwritable_outputs_exit_2(self):
        events = Path(self.paths["events.jsonl"])
        missing = os.path.join(self.tmp.name, "missing")
        run_args = ("--truth", self.truth, "--anchor", self.anchor_path)
        no_pair = "нужны --truth и --anchor"
        for args, why in ((("--out", os.path.join(missing, "events.jsonl")), "--out"),
                          (run_args + ("--summary", os.path.join(missing, "summary.json")), "--summary"),
                          (run_args + ("--per-gesture", os.path.join(missing, "gestures.jsonl")), "--per-gesture"),
                          (run_args + ("--summary", self.tmp.name), "--summary"),
                          (("--summary", self.paths["summary.json"]), no_pair),
                          (("--per-gesture", self.paths["gestures.jsonl"], "--truth", self.truth), no_pair),
                          (("--probe", self.paths["summary.json"], "--anchor", self.anchor_path), no_pair)):
            proc = self.start_observer(*args)
            err = self.finish(proc)
            self.assertEqual(proc.returncode, 2, (args, err))
            self.assertIn(why, err)
            self.assertEqual(no_pair in err, why == no_pair, err)
            self.assertNotIn("Traceback", err)
            self.assertFalse(events.exists(), args)
        out_dir = os.path.join(self.tmp.name, "out")              # каталог сводки пропал за время прогона
        os.mkdir(out_dir)
        load_or_create_anchor(self.anchor_path, file_sha256(self.timeline))
        proc = self.start_observer("--truth", self.truth, "--anchor", self.anchor_path,
                                   "--summary", os.path.join(out_dir, "summary.json"))
        rclpy.init()
        node = rclpy.create_node("gesture_unwritable_test")
        try:
            deadline = mono() + 20.0
            while mono() < deadline and "gesture_observer_test" not in node.get_node_names():
                rclpy.spin_once(node, timeout_sec=0.1)
        finally:
            node.destroy_node()
            rclpy.shutdown()
        os.rmdir(out_dir)
        err = self.stop_observer(proc)
        self.assertEqual(proc.returncode, 2, err)
        self.assertIn("summary.json", err)
        self.assertNotIn("Traceback", err)
        _, summary = run(str(events), self.truth, self.anchor_path)        # запись цела — разбор офлайн
        self.assertEqual(summary["segments"], {"gestures": 4, "negatives": 1})

    def plan(self, segments: list) -> list:
        """[(момент CLOCK_MONOTONIC, что публиковать, данные)] по истине."""
        wall = self.anchor.t_wall - self.anchor.t_mono
        out = []
        for i, s in enumerate(segments):
            t0 = self.anchor.mono_of(s["t0"])
            if s["kind"] == "gesture":
                key = "%d:%s" % (s["cls"], s["side"]) if s["side"] else str(s["cls"])
                steps = SHORT[REFLEX[key]]
                msg = {"boot": "test", "seq": i, "cls": s["cls"], "side": s["side"], "conf": 0.9, "n_boxes": 9,
                       "t_end": t0 + wall, "t_rec": t0 + 0.15 + wall}
                out.append((t0 + 0.2, "gesture", json.dumps(msg)))
                out.append((t0 + 0.22, "gate", SIGN[steps[0][0]]))
            else:
                steps = [["F", 0.5]]
            m = t0 + 0.25
            for letter, dur in steps:
                for k in range(int(round(dur * 10))):
                    out.append((m + 0.1 * k, "cmd", SIGN[letter]))
                m += dur
            for k in range(5):
                out.append((m + 0.1 * k, "cmd", (0.0, 0.0)))
        return sorted(out, key=lambda e: e[0])

    def test_observer_end_to_end(self):
        _, segments = read_jsonl(self.truth)
        proc = self.start_observer()
        rclpy.init()
        node = rclpy.create_node("gesture_scenario_test")
        pubs = {"cmd": node.create_publisher(TwistStamped, "/cmd_vel", 10),
                "gate": node.create_publisher(Twist, "/cmd_vel_gesture", 10),
                "gesture": node.create_publisher(String, "/neurobot/gesture", 10),
                "health": node.create_publisher(String, "/neurobot/perception_health", 10),
                "odom": node.create_publisher(Odometry, "/odom", 10),
                "gt": node.create_publisher(PoseStamped, "/ground_truth/pose", 10)}
        try:
            deadline = mono() + 20.0
            while mono() < deadline and min(p.get_subscription_count() for p in pubs.values()) < 1:
                rclpy.spin_once(node, timeout_sec=0.1)
            self.assertGreaterEqual(min(p.get_subscription_count() for p in pubs.values()), 1, "observer не подписался")
            self.anchor = load_or_create_anchor(self.anchor_path, file_sha256(self.timeline))   # как FakeYolo
            plan = self.plan(segments)
            x = y = yaw = 0.0
            v = w = 0.0
            pending = []
            next_tick, next_health = mono(), mono()
            end = plan[-1][0] + 1.0
            while mono() < end:
                now = mono()
                while plan and plan[0][0] <= now:
                    _, kind, data = plan.pop(0)
                    if kind == "gesture":
                        pubs["gesture"].publish(String(data=data))
                    elif kind == "gate":
                        msg = Twist()
                        msg.linear.x, msg.angular.z = data
                        pubs["gate"].publish(msg)
                    else:
                        msg = TwistStamped()
                        msg.twist.linear.x, msg.twist.angular.z = data
                        pubs["cmd"].publish(msg)
                        pending.append((now + 0.1, data))
                if now >= next_tick:
                    while pending and pending[0][0] <= now:
                        v, w = pending.pop(0)[1]
                    x += v * math.cos(yaw) * 0.05
                    y += v * math.sin(yaw) * 0.05
                    yaw += w * 0.05
                    odom = Odometry()
                    _stamp(odom, now)
                    odom.twist.twist.linear.x, odom.twist.twist.angular.z = v, w
                    pubs["odom"].publish(odom)
                    gt = PoseStamped()
                    _stamp(gt, now)
                    gt.pose.position.x, gt.pose.position.y = x, y
                    gt.pose.orientation.z, gt.pose.orientation.w = math.sin(yaw / 2), math.cos(yaw / 2)
                    pubs["gt"].publish(gt)
                    next_tick += 0.05
                if now >= next_health:
                    pubs["health"].publish(String(data=json.dumps({"fps": 10, "frame_age_s": 0.1, "recoveries": 0,
                                                                   "state": "ok"})))
                    next_health += 1.0
                rclpy.spin_once(node, timeout_sec=0.005)
        finally:
            node.destroy_node()
            rclpy.shutdown()
            err = self.stop_observer(proc)
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn("разбор без него", err)                   # журнала probe нет — разбор всё равно строится
        s = json.loads(Path(self.paths["summary.json"]).read_text())
        self.assertEqual(s["reactions"], {"correct": 4, "wrong": 0, "interrupted": 0, "none": 0, "unobserved": 0}, s)
        self.assertEqual(s["false_actions"], {"total": 1, "by_kind": {"hand_still": 1}})
        self.assertEqual((s["spontaneous_actions"], s["unsafe_no_fresh_cmd"]["n"], s["wall"]["n"]), (0, 0, 0))
        self.assertEqual((s["cmd"]["type_mismatch"], s["input"]["dropped_events"]), ([], 0))
        self.assertIsNotNone(s["wall"]["min_d"])
        self.assertEqual(s["recognition"]["msgs"], 4)
        for k, lo, hi in (("rec", 0.14, 0.17), ("pub", 0.19, 0.3), ("cmd", 0.24, 0.35), ("motion", 0.3, 0.6)):
            self.assertEqual(s["latency_s"][k]["n"], 4, k)
            self.assertTrue(lo <= s["latency_s"][k]["p50"] <= hi, (k, s["latency_s"][k]))
        self.assertGreaterEqual(s["deaf"]["health_msgs"], 10)
        per = [json.loads(ln) for ln in Path(self.paths["gestures.jsonl"]).read_text().splitlines()]
        self.assertTrue(all(0.2 <= p["lat_extra"]["/cmd_vel_gesture"] <= 0.3 for p in per if p["kind"] == "gesture"))
        header = json.loads(Path(self.paths["events.jsonl"]).read_text().splitlines()[0])["header"]
        self.assertEqual(header["args"]["cmd_vel_type"], "twist_stamped")
        self.assertEqual(header["timeline_sha256"], file_sha256(self.timeline))


if __name__ == "__main__":
    unittest.main()
