"""Очередь прогонов (queue.py): разбор шагов, готовый шаг пропускается, недоделанный каталог уходит в interrupted и
шаг идёт заново, неудачный шаг очередь не останавливает, без mockllm шаг не запускается, команды шагов."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from neurobot.sim.runs import queue as Q  # noqa: E402

MOCK = "http://127.0.0.1:18150"


class ParseTest(unittest.TestCase):
    def test_steps(self):
        self.assertEqual(Q.parse_step("run:a1:long:long30"), Q.Step("run", "long", ("a1", "long30")))
        self.assertEqual(Q.parse_step("budget:budget:2406:main"), Q.Step("budget", "budget", ("2406", "main")))
        self.assertEqual(Q.parse_step("start:start_4:25:no_camera,port_busy"),
                         Q.Step("start", "start_4", ("25", "no_camera,port_busy")))
        self.assertEqual(Q.parse_step("run:a1:lab_inf:main_af:world=lab,no_return=inf"),
                         Q.Step("run", "lab_inf", ("a1", "main_af", "--no-return=inf", "--world=lab")))
        for bad in ("run:a2:x:main", "budget:b:lots:main", "start:s", "start::5", "walk:x:y",
                    "run:a0:x:main:world=lab", "run:a1:x:main:world=moon", "run:a1:x:main:speed=9"):
            with self.assertRaises(ValueError):
                Q.parse_step(bad)

    def test_argv(self):
        run = Q.argv(Q.parse_step("run:a0:main_a0:main"), "/b", MOCK)
        self.assertEqual(run[2:], ["neurobot.sim.runs.runner", "run", "--system", "a0", "--label", "main_a0", "--sched",
                                   "/b/sched_main", "--out", "/b/main_a0", "--mockllm", MOCK])
        run = Q.argv(Q.parse_step("run:a1:hall_inf:main_af:no_return=inf"), "/b", MOCK)
        self.assertEqual(run[-3:], ["--mockllm", MOCK, "--no-return=inf"])
        self.assertIn("/b/sched_main_af", run)
        budget = Q.argv(Q.parse_step("budget:bud:2406:main"), "/b", MOCK)
        self.assertEqual(budget[budget.index("--mem") + 1], "2406")
        start = Q.argv(Q.parse_step("start:s1:25:no_camera"), "/b", MOCK)
        self.assertEqual(start[:2], ["/bin/sh", "-c"])
        self.assertIn("start_gesture prepare --sched /b/sched_start --mockllm %s && exec neurobot start-test "
                      "--cycles 25 --broken=no_camera --out /b/s1 --gesture-cmd" % MOCK, start[2])
        self.assertIn("--cycles 25 --broken= --out", Q.argv(Q.parse_step("start:s1:25"), "/b", MOCK)[2])
        self.assertNotIn("--broken", Q.argv(Q.parse_step("start:s1:25:all"), "/b", MOCK)[2])


class RunQueueTest(unittest.TestCase):
    def test_skip_done_move_interrupted_and_go_on_after_failure(self):
        with tempfile.TemporaryDirectory() as base:
            os.makedirs(os.path.join(base, "done"))
            with open(os.path.join(base, "done", "run.json"), "w") as f:
                json.dump({"system": "a1"}, f)
            os.makedirs(os.path.join(base, "half"))
            with open(os.path.join(base, "half", "run.json"), "w") as f:
                json.dump({"error": "KeyboardInterrupt: SIGTERM"}, f)
            calls = []

            def call(cmd, log_path):
                label = cmd[cmd.index("--label") + 1] if "--label" in cmd else "s"
                calls.append(label)
                if label == "half":
                    os.makedirs(os.path.join(base, label))
                    with open(os.path.join(base, label, "run.json"), "w") as f:
                        json.dump({"system": "a1"}, f)
                    return 0
                return 1
            steps = [Q.parse_step(s) for s in ("run:a1:done:main", "run:a1:half:main", "start:s:2")]
            log = os.path.join(base, "q.jsonl")
            bad = Q.run_queue(steps, base, MOCK, log, call, alive=lambda m: True)
            self.assertEqual((bad, calls), (1, ["half", "s"]))
            self.assertEqual(len(os.listdir(os.path.join(base, "interrupted"))), 1)
            rows = [json.loads(line) for line in open(log)]
            self.assertEqual([(r["step"], r["status"]) for r in rows],
                             [("done", "skip"), ("half", "moved"), ("half", "start"), ("half", "end"), ("s", "start"),
                              ("s", "end")])
            self.assertTrue(Q.done(steps[1], base))
            self.assertFalse(Q.done(steps[2], base))

    def test_no_mockllm_no_step(self):
        with tempfile.TemporaryDirectory() as base:
            bad = Q.run_queue([Q.parse_step("run:a1:x:main")], base, MOCK, os.path.join(base, "q.jsonl"),
                              lambda cmd, log: self.fail("шаг без mockllm"), alive=lambda m: False)
            self.assertEqual(bad, 1)

    def test_main_rejects_bad_and_repeated_steps(self):
        self.assertEqual(Q.main(["--mockllm", MOCK, "walk:x"]), 2)
        self.assertEqual(Q.main(["--mockllm", MOCK, "run:a1:x:main", "budget:x:2406:main"]), 2)


if __name__ == "__main__":
    unittest.main()
