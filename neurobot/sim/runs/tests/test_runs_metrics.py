"""Метрики прогона (metrics.py) и сводка (compare.py) на синтетическом каталоге: глухие жесты по probe, касания с
дребезгом, видимость перезапуска по PLAN §1 для пути жеста и агентского пути, остановки защитой, агент и LLM,
память и дрейф."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from neurobot.sim.runs import compare, metrics as M  # noqa: E402

T0 = 1000.0     # t_mono якоря


def seg(i, t_start, t_leave, kind="gesture"):
    return {"seg": i, "kind": kind, "t_start": t_start, "t0": t_leave - 0.5, "t_leave": t_leave}


def probe_rows(spans, pid=1):
    """Строки probe раз в секунду; в spans (rel) детектор работает (10 вызовов/с), вне — стоит."""
    out, calls = [], 0
    for rel in range(0, 120):
        if any(a <= rel < b for a, b in spans):
            calls += 10
        out.append({"m": T0 + rel, "pid": pid, "det_calls": calls})
    return out


class DeafTest(unittest.TestCase):
    def test_gesture_is_deaf_when_detector_ran_less_than_half(self):
        segs = [seg(0, 10, 20), seg(1, 40, 50), seg(2, 70, 80, kind="negative")]
        probe = probe_rows([(0, 30), (45, 120)])      # 40…45 — стоит: 5 с из 10, ровно половина — не глух
        self.assertEqual(M.deaf_gestures(segs, probe, T0, 10.0), [])
        probe = probe_rows([(0, 30), (47, 120)])
        self.assertEqual(M.deaf_gestures(segs, probe, T0, 10.0), [1])
        self.assertEqual(M.deaf_gestures(segs, [], T0, 10.0), [0, 1])

    def test_restart_does_not_give_negative_progress(self):
        rows = [{"m": T0 + 1, "pid": 1, "det_calls": 50}, {"m": T0 + 2, "pid": 2, "det_calls": 0},
                {"m": T0 + 3, "pid": 2, "det_calls": 10}]
        self.assertEqual(M.det_rate(rows, 0, 5, T0), 10)


class ContactsTest(unittest.TestCase):
    def test_flicker_is_one_episode_and_gap_splits(self):
        truth = [{"m": t, "contacts": c, "clearance_m": d, "rtf": 1.0, "rtf_ok": True}
                 for t, c, d in ((0.0, [], 0.5), (1.0, ["chair"], 0.0), (1.2, [], 0.01), (1.4, ["chair"], 0.0),
                                 (2.0, ["chair"], 0.0), (5.0, ["wall_east"], 0.0), (6.0, [], 0.3))]
        got = M.contacts(truth)
        self.assertEqual(got["episodes"], 2)
        self.assertEqual(got["models"], {"chair": 1, "wall_east": 1})
        self.assertEqual(got["min_clearance_m"], 0.0)
        self.assertEqual(got["rtf_ok_share"], 1.0)


class FaultsTest(unittest.TestCase):
    def run_doc(self, faults, ops=(), agent=()):
        ev = [dict(f, kind="fault") for f in faults] + [dict(o, kind="operator") for o in ops]
        ev.append({"kind": "end", "m": T0 + 200})
        return {"anchor": {"t_mono": T0}, "events": sorted(ev, key=lambda e: e["m"]), "agent": list(agent)}

    def test_gesture_path_visibility(self):
        segs = [seg(0, 10, 15), seg(1, 30, 35), seg(2, 60, 65)]
        per = {0: {"reaction": "correct"}, 1: {"reaction": "none"}, 2: {"reaction": "correct"}}
        run = self.run_doc([{"fault": "kill_perception", "m": T0 + 16, "ttr_s": 3.0},
                            {"fault": "camera_delay", "m": T0 + 20, "ttr_s": 40.0},
                            {"fault": "kill_executor", "m": T0 + 50, "ttr_s": None, "skipped": "нет компонента"}],
                           ops=[{"m": T0 + 45, "action": "restart", "what": "demo"}])
        rows = M.fault_rows(run, segs, per, deaf=[])
        self.assertEqual(rows[0]["visible"], False)                       # 3 с < паузы 14 с до жеста
        self.assertEqual(rows[1]["lost_gestures"], [1])
        self.assertIn("понадобился человек", rows[1]["why"])
        self.assertTrue(any("паузы" in w for w in rows[1]["why"]))
        self.assertEqual(rows[2]["skipped"], "нет компонента")

    def test_agent_path_visibility_by_failed_requests(self):
        run = self.run_doc([{"fault": "kill_mcp", "m": T0 + 10, "ttr_s": 2.0},
                            {"fault": "kill_rosbridge", "m": T0 + 50, "ttr_s": None}],
                           agent=[{"n": 0, "m": T0 + 11, "ok": False}, {"n": 1, "m": T0 + 60, "ok": False}])
        rows = M.fault_rows(run, [seg(0, 100, 105)], {}, [])
        self.assertEqual((rows[0]["path"], rows[0]["agent_failed"], rows[0]["visible"]), ("агент", [0], True))
        self.assertEqual(rows[1]["why"], ["не восстановилось само", "реплик без ответа: 1"])
        self.assertEqual(M.fault_role("stop_perception"), "perception")
        self.assertEqual(M.fault_role("camera_black"), "camera")


class AgentAndMemoryTest(unittest.TestCase):
    def test_agent_summary_and_llm_outside_requests(self):
        run = {"label": "main", "system": "a1", "t_start": 0, "t_end": 100,
               "agent": [{"n": 0, "request": "status", "ok": True, "latency_s": 0.05},
                         {"n": 1, "request": "move", "ok": False, "error": "pico: refused", "tools": []}]}
        llm = [{"type": "llm", "model": "gpt-4o-mini", "ts_wall": 10, "n_tools": 3, "usage": {"prompt_tokens": 900},
                "oracle": {"input_ids": ["main-a1-0"]}},
               {"type": "llm", "model": "gpt-4o-mini", "ts_wall": 11, "n_tools": 3, "usage": {"prompt_tokens": 950},
                "oracle": {"input_ids": ["main-a1-0"]}},
               {"type": "llm", "model": "openai/gpt-4o-mini", "ts_wall": 12, "oracle": {"input_ids": ["main-a0-0"]}},
               {"type": "llm", "model": "gpt-4o-mini", "ts_wall": 50, "oracle": {"input_ids": []}},
               {"type": "control", "ts_wall": 1}]
        a = M.agent_summary(run, llm)
        self.assertEqual((a["requests"], a["ok"], a["llm_calls"]), (2, 1, 2))
        self.assertEqual(a["llm_calls_per_request"]["p50"], 2)
        self.assertEqual(a["n_tools_in_catalog"], [3])
        self.assertEqual(M.llm_outside_requests(run, llm, "gpt-4o-mini"), 1)

    def test_memory_slope_and_drift(self):
        samples = [{"type": "header"}] + [
            {"type": "sample", "t_mono": 3600.0 * k / 10, "units": {"a": {"procs": [{"pss_kib": 1024 * (100 + k)}]}}}
            for k in range(11)]
        for s in samples[1:]:
            s["units"]["twin_driver"] = {"procs": [{"pss_kib": 1024 * 500}]}
        mem = M.memory(samples, skip_s=0)
        self.assertEqual(mem["a"]["slope_mib_h"], 10.0)
        self.assertEqual(mem["_total"]["pss_mib_start"], 100.0)          # двойник (Gazebo) — не робот
        self.assertEqual((mem["a"]["pss_mib_start"], mem["a"]["pss_mib_end"]), (100.0, 110.0))
        self.assertEqual(M.memory(samples[:3]), {})
        per = [{"seg": i, "kind": "gesture", "lat": {"cmd": 0.5 + (0.1 if i >= 6 else 0)}} for i in range(9)]
        d = M.drift(per)
        self.assertEqual((d["first"]["p50"], d["last"]["p50"]), (0.5, 0.6))

    def test_safety_stops(self):
        segs = [seg(0, 10, 15), seg(1, 30, 35)]
        per = {0: {"reaction": "interrupted"}, 1: {"reaction": "none"}}
        safety = [{"m": T0 + 16, "state": "blocked", "reasons": ["lidar_nodata: нет данных…"]},
                  {"m": T0 + 60, "state": "blocked", "reasons": ["obstacle: …"]}]
        got = M.safety_stops(per, segs, safety, T0)
        self.assertEqual(got, {"n": 1, "by_seg": {"0": {"reaction": "interrupted", "reasons": ["lidar_nodata"]}}})


class SummarizeTest(unittest.TestCase):
    def test_summarize_and_compare_on_a_synthetic_run(self):
        with tempfile.TemporaryDirectory() as d:
            segs = [seg(0, 10, 15), seg(1, 30, 35), seg(2, 50, 55, kind="negative")]
            with open(os.path.join(d, "truth_schedule.jsonl"), "w") as f:
                f.write(json.dumps({"header": {}}) + "\n" + "\n".join(json.dumps(s) for s in segs) + "\n")
            with open(os.path.join(d, "probe.jsonl"), "w") as f:
                f.write("\n".join(json.dumps(r) for r in probe_rows([(0, 120)])) + "\n")
            run = {"system": "a1", "label": "main", "plan": {"profile": "main"}, "t_start": 0, "t_end": 120,
                   "anchor": {"t_mono": T0}, "agent": [], "restarts": {"perception": 1},
                   "events": [{"kind": "fault", "fault": "kill_perception", "m": T0 + 16, "ttr_s": 3.0},
                              {"kind": "end", "m": T0 + 110}]}
            with open(os.path.join(d, "run.json"), "w") as f:
                json.dump(run, f)
            with open(os.path.join(d, "summary.json"), "w") as f:
                json.dump({"latency_s": {"cmd": {"n": 2, "p50": 0.6, "p95": 0.7}}, "reactions": {"correct": 2},
                           "unsafe_no_fresh_cmd": {"n": 0, "total_s": 0}, "k7": {"total": 0, "per_hour": 0.0}}, f)
            m = M.summarize(d)
            self.assertEqual(m["segments"], {"gestures": 2, "negatives": 1})
            self.assertEqual(m["K3"]["deaf_gestures_n"], 0)
            self.assertEqual(m["K5_faults"][0]["visible"], False)
            text = compare.build(m, m, None, None, None, None)
            self.assertIn("| K1 t0 → первая ненулевая /cmd_vel, p50 / p95, с | 0,60 / 0,70 (n=2)", text)
            self.assertIn("kill_perception (16 с)", text)
            self.assertIn("error", M.summarize(os.path.join(d, "nope")))
            start = {"ok": {"cycles": 100, "passed": 99, "failed": [17]}, "no_camera": {"cycles": 1, "passed": 1},
                     "port_busy": {"cycles": 1, "passed": 1}}
            rows = "\n".join(compare.verdicts(m, m, start, {"oom_kill": 0}))
            self.assertIn("| start_test: 100 из 100 холодных стартов (K9) | нет механизма | 99 из 100 | "
                          "**не выполнено** |", rows)
            self.assertIn("2 из 2 | выполнено |", rows)
            self.assertIn("| бюджет 4 ГБ: 0 OOM за прогон | — | 0 | выполнено |", rows)
            self.assertIn("| 0 «глухих» состояний: путь жеста сам поднялся после каждого отказа | 0 | 0 | выполнено |",
                          rows)


if __name__ == "__main__":
    unittest.main()
