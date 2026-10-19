"""Расписание прогона (schedule.py): одно на A0 и A1, отказы и запросы агенту — между сегментами, формат отказов
камеры — как у подставного pyrealsense2."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from neurobot.sim.gestures.realsense import load_schedule  # noqa: E402
from neurobot.sim.runs import schedule  # noqa: E402


def seg(i, t_start, t_leave, kind="gesture"):
    return {"seg": i, "kind": kind, "t_start": t_start, "t0": t_leave - 0.5, "t_leave": t_leave}


class PlaceTest(unittest.TestCase):
    def test_every_nth_segment_after_last_box_but_not_the_last_segment(self):
        truth = [seg(i, 10.0 * i, 10.0 * i + 4) for i in range(10)]
        got = schedule.place(truth, 1, 3, 1.0)
        self.assertEqual(got, [(1, 15.0), (4, 45.0), (7, 75.0)])
        self.assertEqual(schedule.place(truth, 0, 0, 1.0), [])
        self.assertNotIn(9, [s for s, _ in schedule.place(truth, 0, 1, 1.0)])

    def test_events_cycle_faults_and_moves_sorted(self):
        truth = [seg(i, 20.0 * i, 20.0 * i + 5) for i in range(40)]
        prof = {"fault_first": 1, "fault_every": 2, "agent_first": 2, "agent_every": 4, "moves": (1,)}
        ev = schedule.build_events(truth, prof)
        self.assertEqual([e["at_s"] for e in ev], sorted(e["at_s"] for e in ev))
        faults = [e for e in ev if e["kind"] != "agent"]
        self.assertEqual([f.get("target") or f.get("fault") for f in faults[:len(schedule.FAULT_CYCLE)]],
                         [f.get("target") or f.get("fault") for f in schedule.FAULT_CYCLE])
        agent = [e for e in ev if e["kind"] == "agent"]
        self.assertEqual([a["request"] for a in agent[:3]], ["status", "move", "status"])
        for e in ev:
            s = truth[e["seg"]]
            self.assertGreater(e["at_s"], s["t_leave"])
            self.assertLess(e["at_s"], truth[e["seg"] + 1]["t_start"])

    def test_camera_faults_are_accepted_by_fake_realsense(self):
        ev = [{"kind": "camera", "fault": "delay", "duration_s": 7.0, "at_s": 12.5},
              {"kind": "kill", "target": "mcp", "at_s": 20.0}]
        faults = schedule.camera_faults(ev)
        self.assertEqual(faults, [{"at_s": 12.5, "kind": "delay", "duration_s": 7.0}])
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(faults, f)
        try:
            self.assertEqual(len(load_schedule(f.name)), 1)
        finally:
            os.remove(f.name)


class MakeTest(unittest.TestCase):
    def test_faults_drivers_and_agent_never_share_a_segment(self):
        for name in ("main", "long", "long30", "smoke"):
            prof = schedule.PROFILES[name]
            faults = set(range(prof["fault_first"], 10 ** 4, prof["fault_every"]))
            agent = set(range(prof["agent_first"], 10 ** 4, prof["agent_every"]))
            drivers = set(range(prof["driver_first"], 10 ** 4, prof["driver_every"]))
            self.assertFalse(faults & agent, name)
            self.assertFalse(drivers & (faults | agent), name)

    def test_driver_faults_do_not_move_old_events(self):
        # приёмка 17.10: kill -9 узлов urg_node2, realsense2_camera и базы, SIGSTOP узлов лидара и камеры — своими
        # сегментами: жесты, прежние отказы и реплики те же, что у прогонов 16.10 (A0 сравним по ним)
        truth = [seg(i, 20.0 * i, 20.0 * i + 5) for i in range(100)]
        prof = dict(schedule.PROFILES["main"])
        new = schedule.build_events(truth, prof)
        old = schedule.build_events(truth, dict(prof, driver_every=0))
        self.assertEqual([e for e in new if e.get("cycle") != "driver"], old)
        drivers = [e for e in new if e.get("cycle") == "driver"]
        self.assertEqual(len(drivers), 12)
        self.assertEqual({(e["kind"], e["target"]) for e in drivers},
                         {("kill", "lidar"), ("kill", "camera"), ("kill", "base"), ("stop", "lidar"),
                          ("stop", "camera")})
        self.assertTrue(all(e.get("duration_s", 30.0) == 30.0 for e in drivers))

    def test_make_writes_plan_faults_and_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            plan = schedule.make("smoke", d, 7)
            for name in ("timeline.jsonl", "truth.jsonl", "faults.json", "plan.json"):
                self.assertTrue(os.path.exists(os.path.join(d, name)), name)
            self.assertEqual(plan["profile"], "smoke")
            kinds = {e["kind"] for e in plan["events"]}
            self.assertEqual(kinds, {"camera", "kill", "stop", "agent"})
            # дымовой проходит весь цикл драйверов, в том числе kill -9 базы
            self.assertEqual([(e["kind"], e["target"]) for e in plan["events"] if e.get("cycle") == "driver"],
                             [(d["kind"], d["target"]) for d in schedule.DRIVER_CYCLE])
            info = schedule.info(d)
            self.assertEqual(info["gestures"]["seed"], 7)
            with self.assertRaises(FileExistsError):
                schedule.make("smoke", d, 7)
            self.assertEqual(schedule.main(["make", "--profile", "smoke", "--out", d]), 2)

    def test_same_seed_same_plan_and_unknown_profile_is_an_error(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            self.assertEqual(schedule.make("start", a, 3), schedule.make("start", b, 3))
        with self.assertRaises(ValueError):
            schedule.make("nope", "/nonexistent", 1)


if __name__ == "__main__":
    unittest.main()
