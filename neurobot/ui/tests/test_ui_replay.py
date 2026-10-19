"""Пульт: сборщик «Записи» (replay.py) — на синтетическом прогоне (поток по формату, тревоги, жесты, LLM, сводка
теми же функциями metrics.py) и на настоящих прогонах двойника, если они есть локально (experiments/results в git не
входит): ячейки «до/после» совпадают с compare.md, итог потока — с metrics.summarize. Python 3.8+; сравнение с
metrics.summarize — только где есть numpy (разбор K6 наблюдателя)."""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from neurobot.sim.runs import compare as C  # noqa: E402
from neurobot.sim.runs import metrics as M  # noqa: E402
from neurobot.ui import __main__ as cli  # noqa: E402
from neurobot.ui import replay, stream  # noqa: E402

BLIND = "lidar_blind: перед в слепом секторе лидара — только глубина, ≤ 0.15 м/с"
RES = ROOT / "experiments" / "results"
A0 = RES / "neurobot_20261016_a0" / "vm"
A1V2 = RES / "neurobot_20261018_a1v2" / "vm"
LAB5 = RES / "neurobot_20261018_acceptfix2" / "runs" / "lab_v5"


def jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def make_run(d):
    """Синтетический прогон A1 «syn»: якорь t_wall 1000 = t_mono 100, запись 990…1100 с."""
    off = 900.0
    segs = [{"seg": 0, "kind": "gesture", "t_start": 5, "t0": 10, "t_leave": 12},
            {"seg": 1, "kind": "gesture", "t_start": 25, "t0": 30, "t_leave": 32},
            {"seg": 2, "kind": "negative", "t_start": 40, "t0": 42, "t_leave": 45},
            {"seg": 3, "kind": "gesture", "t_start": 60, "t0": 65, "t_leave": 70},
            {"seg": 4, "kind": "gesture", "t_start": 85, "t0": 88, "t_leave": 92}]
    jsonl(os.path.join(d, "truth_schedule.jsonl"), [{"header": {"classes": {}}}] + segs)
    jsonl(os.path.join(d, "gestures.jsonl"), [
        {"seg": 0, "kind": "gesture", "t0": 1010.0, "lat": {"cmd": 0.6, "rec": 0.5, "motion": 0.7}, "early": False,
         "reaction": "correct", "expected": "come", "maneuver": "come", "phases": [["F", 3.0]], "rtf": 0.98,
         "rec": {"cls": 1, "name": "come", "conf": 0.9, "margin": 0.5, "n_boxes": 20, "side": None}},
        {"seg": 1, "kind": "gesture", "t0": 1030.0, "lat": {"cmd": -0.3, "motion": -0.2}, "early": True,
         "reaction": "wrong", "expected": "think", "maneuver": "turn180", "phases": [["R", 7.0]], "rtf": 0.98},
        {"seg": 2, "kind": "negative", "t0": 1042.0, "lat": {"cmd": 1.0}, "actions": 1, "name": "passerby",
         "phases": [["R", 7.0]]},
        {"seg": 3, "kind": "gesture", "t0": 1065.0, "lat": {}, "early": False, "reaction": "none", "expected": "come",
         "maneuver": None},
        {"seg": 4, "kind": "gesture", "t0": 1088.0, "lat": {"cmd": 0.4}, "early": False, "reaction": "correct",
         "expected": "come", "maneuver": "come", "phases": [["F", 2.0]], "rtf": 0.9}])     # RTF < 0,95: не в K1
    with open(os.path.join(d, "summary.json"), "w") as f:
        json.dump({"reactions": {"correct": 1, "wrong": 1, "none": 1, "interrupted": 0, "unobserved": 0},
                   "k7": {"total": 1, "per_hour": 32.7}}, f)
    safety = []
    for t in range(992, 1100):
        if 1081 <= t <= 1084:
            continue                                        # safety_gate молчит
        state, reasons = "slow", [BLIND]
        if 1060 <= t <= 1062:
            state, reasons = "estop", ["shock: удар: всплеск ускорения 9.8 м/с² > 6"]
        if 1066 <= t <= 1070:
            state, reasons = "blocked", ["obstacle: глубина — путь 0.06 м от края корпуса, вперёд нельзя", BLIND]
        safety.append({"m": t - off, "t": float(t), "state": state, "reasons": reasons, "min_clearance_m": 0.5})
    jsonl(os.path.join(d, "safety.jsonl"), safety)
    truth = []
    x = 0.0
    for i in range(int((200 - 92) / 0.25)):
        m = 92 + i * 0.25
        mv = 110 <= m < 120
        if mv:
            x += 0.025
        truth.append({"m": m, "x": round(x, 4), "y": 0.0, "v": 0.1 if mv else 0.0, "w": 0.0, "clearance_m": 0.5,
                      "contacts": ["box"] if 150.0 <= m <= 150.5 else [], "rtf": 0.98, "rtf_ok": True})
    jsonl(os.path.join(d, "truth.jsonl"), truth)
    llm = [
        {"type": "control", "ts_wall": 1039.9, "action": "expect", "input_id": "syn-a1-0"},
        {"type": "llm", "model": "gpt-4o-mini", "ts_wall": 1040.1, "oracle": {"input_ids": ["syn-a1-0"]},
         "usage": {"prompt_tokens": 800, "completion_tokens": 10, "cached_tokens": 0}, "latency_ms": {"total": 2.0}},
        {"type": "llm", "model": "gpt-4o-mini", "ts_wall": 1040.3, "oracle": {"input_ids": ["syn-a1-0"]},
         "usage": {"prompt_tokens": 900, "completion_tokens": 5, "cached_tokens": 0}, "latency_ms": {"total": 2.0}},
        {"type": "llm", "model": "gpt-4o-mini", "ts_wall": 1041.0, "oracle": {"input_ids": ["other-a1-0"]},
         "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        {"type": "llm", "model": "gpt-4o-mini", "ts_wall": 1042.0,
         "usage": {"prompt_tokens": 300, "completion_tokens": 50}},
        {"type": "llm", "model": "openai/gpt-4o-mini", "ts_wall": 1050.0, "usage": {"prompt_tokens": 1}},
        {"type": "llm", "model": "gpt-4o-mini", "ts_wall": 1090.0,
         "usage": {"prompt_tokens": 100, "completion_tokens": 1}},
    ]
    jsonl(os.path.join(d, "mockllm.jsonl"), llm)
    collect = [{"type": "header", "clk_tck": 100}]
    for i, t in enumerate(range(995, 1100, 10)):
        perc = [] if t == 1035 else [{"pid": 1, "pss_kib": 102400, "utime": 10 * i, "stime": 0}]
        safety_procs = [{"pid": 2, "pss_kib": 51200, "utime": 5 * i, "stime": 0}]
        collect.append({"type": "sample", "t_wall": float(t), "t_mono": t - off, "units": {
            "perception": {"procs": perc}, "safety": {"procs": safety_procs},
            "twin_driver": {"procs": [{"pid": 3, "pss_kib": 999999, "utime": 0, "stime": 0}]}}})
    jsonl(os.path.join(d, "collect.jsonl"), collect)
    jsonl(os.path.join(d, "events.jsonl"), [
        {"header": {}},
        {"ev": "cmd", "topic": "/cmd_vel", "t0": 995.0, "t1": 1010.5, "x": 0.0, "z": 0.0, "n": 150},
        {"ev": "cmd", "topic": "/neurobot/cmd_vel_req", "t0": 1010.5, "t1": 1013.6, "x": 0.15, "z": 0.0, "n": 60},
        {"ev": "cmd", "topic": "/cmd_vel", "t0": 1010.6, "t1": 1013.6, "x": 0.1499, "z": -0.0, "n": 60},
        {"ev": "health", "t": 1001.0, "m": 101.0, "msg": {"state": "ok", "fps": 10.0, "frame_age_s": 0.05}},
        {"ev": "health", "t": 1031.0, "m": 131.0, "msg": {"state": "recovering", "fps": 0.0, "frame_age_s": 2.0}},
        {"ev": "health", "t": 1036.0, "m": 136.0, "msg": {"state": "ok", "fps": 10.0, "frame_age_s": 0.05}},
        {"ev": "gesture", "t": 1010.5, "m": 110.5,
         "msg": {"t_end": 1010.1, "t_rec": 1010.5, "name": "come", "cls": 1}},
    ])
    agent = [{"n": 0, "request": "move", "t": 1040.0, "m": 140.0, "latency_s": 0.5, "ok": True, "tools": [],
              "refused": []}]
    run = {"system": "a1", "label": "syn", "plan": {"profile": "smoke"}, "t_start": 990.0, "t_end": 1100.0,
           "world": "neurobot/sim/runs/worlds/hall.sdf", "anchor": {"t_wall": 1000.0, "t_mono": 100.0},
           "events": [{"kind": "start", "t": 990.0, "m": 90.0}, {"kind": "ready", "t": 992.0, "m": 92.0},
                      {"kind": "fault", "fault": "kill_perception", "plan_kind": "kill", "seg": 1, "at_s": 30.0,
                       "t": 1030.0, "m": 130.0, "ttr_s": 3.0},
                      {"kind": "fault", "fault": "camera_delay", "plan_kind": "camera", "seg": 2, "at_s": 41.0,
                       "t": 1041.0, "m": 141.0, "ttr_s": 8.0},
                      {"kind": "agent", "n": 0, "t": 1040.0, "m": 140.0},
                      {"kind": "operator", "action": "estop_release", "reasons": ["shock: удар"], "t": 1063.0,
                       "m": 163.0},
                      {"kind": "end", "t": 1100.0, "m": 200.0}],
           "agent": agent, "restarts": {"perception": 1},
           "units": {"perception": "neurobot-perception", "safety": "neurobot-safety",
                     "twin_driver": "neurobot-driver@twin"}}
    with open(os.path.join(d, "run.json"), "w") as f:
        json.dump(run, f, ensure_ascii=False)


def kinds(msgs, k):
    return [m for m in msgs if m["k"] == k]


class SyntheticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        make_run(cls.tmp.name)
        cls.built = replay.build_run(cls.tmp.name, price=lambda model, p, c, o: 1e-5 * p + 4e-5 * o)
        cls.msgs = cls.built["stream"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_stream_is_valid_and_sorted(self):
        self.assertGreater(stream.check_stream(self.msgs), 100)
        self.assertEqual(self.msgs[-1]["k"], "end")
        self.assertEqual(self.built["source"], "запись")
        self.assertTrue(self.built["has_safety"])

    def test_k1_uses_metrics_latency(self):
        k1 = kinds(self.msgs, "k1")
        per = [json.loads(x) for x in open(os.path.join(self.tmp.name, "gestures.jsonl"))]
        self.assertEqual(k1[-1]["run"], M.latency(per, "cmd")["on_time"])
        self.assertEqual(k1[-1]["run"]["n"], 1)                      # ранний жест в K1 не входит

    def test_k1_rule_same_in_browser(self):
        # браузер рисует точки K1 по флагам жеста (не ранний, rtf_ok) — их число совпадает с n у metrics.latency
        g, inside = {}, set()
        for m in self.msgs:
            if m["k"] == "gesture":
                g.setdefault(m["id"], {}).update(m)
                x = g[m["id"]]
                if x.get("kind") == "gesture" and "d_cmd_s" in x and not x["early"] and x["rtf_ok"]:
                    inside.add(m["id"])
            elif m["k"] == "k1":
                self.assertEqual(len(inside), m["run"]["n"], m["t"])
        self.assertFalse(g["seg4"]["rtf_ok"])
        self.assertTrue(g["seg0"]["rtf_ok"] and g["seg3"]["rtf_ok"])

    def test_reasons_with_commas(self):
        # «запятая в дробях» — и в текстах причин safety_gate; код не трогается
        texts = [r for m in kinds(self.msgs, "safety") for r in m["reasons"]]
        self.assertIn("obstacle: глубина — путь 0,06 м от края корпуса, вперёд нельзя", texts)
        self.assertIn("shock: удар: всплеск ускорения 9,8 м/с² > 6", texts)
        self.assertFalse([r for r in texts if re.search(r"\d\.\d", r)])
        self.assertEqual(kinds(self.msgs, "operator")[0]["why"], "shock: удар")

    def test_stats_only_what_browser_cannot_count(self):
        st = kinds(self.msgs, "stats")[-1]
        self.assertNotIn("gestures", st)                 # исходы жестов браузер видит сам — по жестам потока
        self.assertNotIn("tokens", st["llm"])            # токены — по сообщениям llm
        self.assertEqual(set(st["llm"]), {"gesture", "request", "service", "rub_est"})

    def test_gestures_partial_updates(self):
        g = {}
        for m in kinds(self.msgs, "gesture"):
            g.setdefault(m["id"], {}).update(m)
        self.assertEqual(g["seg0"]["outcome"], "done")
        self.assertAlmostEqual(g["seg0"]["d_rec_s"], 0.4, places=3)    # t_rec − t_end сообщения perception
        self.assertEqual(g["seg0"]["d_cmd_s"], 0.6)
        self.assertTrue(g["seg1"]["early"])
        self.assertEqual(g["seg1"]["outcome"], "wrong")
        self.assertEqual(g["seg2"]["outcome"], "false_action")
        self.assertEqual(g["seg3"]["outcome"], "none")
        self.assertEqual(g["seg3"]["stopped_by"], ["lidar_blind", "obstacle"])   # metrics.safety_stops

    def test_llm_kinds_and_k4(self):
        llm = kinds(self.msgs, "llm")
        self.assertEqual([m["kind"] for m in llm], ["request", "request", "service", "gesture"])
        run = json.load(open(os.path.join(self.tmp.name, "run.json")))
        rows = [json.loads(x) for x in open(os.path.join(self.tmp.name, "mockllm.jsonl"))]
        self.assertEqual(sum(m["kind"] == "gesture" for m in llm), M.llm_outside_requests(run, rows, "gpt-4o-mini"))
        self.assertEqual(kinds(self.msgs, "agent")[0]["llm_calls"], 2)
        self.assertEqual(kinds(self.msgs, "agent")[0]["outcome"], "false_ok")
        self.assertEqual(llm[0]["rub_fact"], 0.0)
        self.assertAlmostEqual(llm[0]["rub_est"], 1e-5 * 800 + 4e-5 * 10)

    def test_alarms(self):
        on = [(m["key"], m["prio"]) for m in kinds(self.msgs, "alarm") if m["on"]]
        self.assertIn(("shock", 1), on)
        self.assertIn(("obstacle", 2), on)
        self.assertIn(("ui_safety_silent", 1), on)
        self.assertIn(("ui_unit_down:perception", 2), on)
        self.assertIn(("ui_perception_recovering", 2), on)
        self.assertIn(("ui_camera_lost", 2), on)
        self.assertNotIn("lidar_blind", [k for k, _ in on])         # норма — не тревога
        silent = [m for m in kinds(self.msgs, "alarm") if m["key"] == "ui_safety_silent"]
        self.assertEqual([(m["t"], m["on"]) for m in silent], [(1082.5, True), (1085.0, False)])
        down = [m for m in kinds(self.msgs, "alarm") if m["key"] == "ui_unit_down:perception"]
        self.assertEqual([(m["t"], m["on"]) for m in down], [(1030.0, True), (1033.0, False)])
        # к концу записи все тревоги сняты
        state = {}
        for m in kinds(self.msgs, "alarm"):
            state[m["key"]] = m["on"]
        self.assertFalse(any(state.values()))

    def test_safety_levels_and_estop(self):
        s = kinds(self.msgs, "safety")
        self.assertEqual({m["level"] for m in s if m["state"] == "slow"}, {0})
        est = [m for m in s if m["state"] == "estop"]
        self.assertEqual({m["estop"]["since"] for m in est}, {1060.0})

    def test_cmd_keepalive_and_dstop(self):
        out = [m for m in kinds(self.msgs, "cmd") if m["topic"] == "out"]
        moving = [m for m in out if m["v"] > 0]
        self.assertEqual(moving[0]["v"], 0.15)
        self.assertEqual(moving[0]["d_stop_m"], replay.robot.d_stop(0.15))
        gaps = [b["t"] - a["t"] for a, b in zip(out, out[1:]) if b["t"] <= 1013.6]
        self.assertTrue(gaps and max(gaps) <= 1.0 + 1e-6)

    def test_resources_and_units(self):
        mem = kinds(self.msgs, "mem")
        self.assertEqual(mem[0]["total_mib"], 150.0)                   # драйвер двойника не в сумме
        self.assertIn("cpu_pct", mem[1])
        units = kinds(self.msgs, "units")
        self.assertTrue(any(u["down"] == ["perception"] for u in units))
        self.assertEqual(units[-1]["up"], 3)

    def test_final_stats_equal_metrics(self):
        st = kinds(self.msgs, "stats")[-1]
        truth = [json.loads(x) for x in open(os.path.join(self.tmp.name, "truth.jsonl"))]
        safety = [json.loads(x) for x in open(os.path.join(self.tmp.name, "safety.jsonl"))]
        tr = M.travel(truth, safety)
        self.assertEqual(st["t"], 1100.0)
        self.assertEqual((st["distance_m"], st["moving_s"]), (tr["distance_m"], tr["moving_s"]))
        self.assertEqual(st["safety_share"], tr["safety_share"])
        self.assertEqual(st["touches"], M.contacts(truth)["episodes"])
        self.assertEqual(st["llm"]["gesture"], 1)
        self.assertEqual(len(kinds(self.msgs, "touch")), 1)
        m = self.built["summary"]["metrics"]
        self.assertEqual(self.built["summary"]["compare"][0], C.lat(m["K1_t0_to_cmd_s"]))
        self.assertEqual(len(self.built["summary"]["compare"]), len(replay.compare_rows()))

    def test_same_input_same_bytes(self):
        # одинаковый вход — одинаковые байты, из какого бы каталога ни собирали (без времени сборки и путей машины)
        out = []
        cwd = os.getcwd()
        try:
            for where in (str(ROOT), self.tmp.name):
                os.chdir(where)
                with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
                    path = os.path.join(tmp, "b.json.gz")
                    self.assertEqual(cli.main(["replay", "--runs", "syn=" + self.tmp.name, "--out", path]), 0)
                    with open(path, "rb") as f:
                        out.append(f.read())
        finally:
            os.chdir(cwd)
        self.assertEqual(out[0], out[1])
        doc = json.loads(gzip.decompress(out[0]))
        self.assertNotIn("built", doc)
        self.assertEqual(doc["runs"][0]["dir"], os.path.basename(self.tmp.name))     # вне репозитория — только имя
        self.assertEqual(doc["runs"][0]["date"], "01.01")                            # по часам лаборатории (МСК)
        self.assertEqual(replay.rel_dir(str(A1V2)), "experiments/results/neurobot_20261018_a1v2/vm")

    def test_cli_replay(self):
        with tempfile.TemporaryDirectory() as out:
            path = os.path.join(out, "b.json.gz")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["replay", "--runs", "syn1=" + self.tmp.name, "--out", path]), 0)
            doc = stream.read_bundle(path)
            self.assertEqual(stream.check_bundle(doc), ["syn1"])
            self.assertEqual(doc["geometry"]["robot_radius"], 0.195)
            self.assertIn("shock", doc["alarms"])
            with self.assertRaises(SystemExit):
                cli.main(["replay", "--runs", "bad id=" + self.tmp.name, "--out", path])


def table(md_path):
    """Таблица «A0 / A1» compare.md: {метрика: {заголовок столбца: ячейка}}."""
    lines = Path(md_path).read_text(encoding="utf-8").splitlines()
    i = lines.index("## Таблица A0 / A1") + 2
    head = [c.strip() for c in lines[i].strip("|").split("|")]
    out = {}
    for line in lines[i + 2:]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        out[cells[0]] = dict(zip(head, cells))
    return out


def column(tab, prefix):
    head = next(iter(tab.values()))
    return next(h for h in head if h.startswith(prefix))


@unittest.skipUnless(A0.is_dir() and A1V2.is_dir(), "нет прогонов двойника (experiments/results — только локально)")
class RealRunsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        specs = [("a0", str(A0)), ("a1v2", str(A1V2))] + ([("lab_v5", str(LAB5))] if LAB5.is_dir() else [])
        cls.bundle = replay.build(specs)
        cls.runs = {r["id"]: r for r in cls.bundle["runs"]}

    def check_column(self, md, run_id, prefix):
        tab = table(md)
        col = column(tab, prefix)
        cells = self.runs[run_id]["summary"]["compare"]
        for row, cell in zip(self.bundle["compare_rows"], cells):
            self.assertIn(row["name"], tab, row["name"])
            self.assertEqual(cell, tab[row["name"]][col], (run_id, row["name"]))
            self.assertEqual(row["goal"], tab[row["name"]]["Цель"], row["name"])

    def test_cells_match_compare_md_a1v2(self):
        md = RES / "neurobot_20261018_a1v2" / "compare.md"
        self.check_column(md, "a0", "A0")
        self.check_column(md, "a1v2", "A1 18.10")

    def test_cells_match_compare_md_acceptfix2(self):
        md = RES / "neurobot_20261018_acceptfix2" / "compare.md"
        self.check_column(md, "a0", "A0")
        self.check_column(md, "a1v2", "A1 18.10")
        if "lab_v5" in self.runs:
            self.check_column(md, "lab_v5", "A1 итог")

    def test_key_numbers(self):
        a0, a1 = self.runs["a0"]["summary"]["compare"], self.runs["a1v2"]["summary"]["compare"]
        self.assertEqual((a0[0], a1[0]), ("1,81 / 2,11 (n=69)", "0,68 / 0,97 (n=57)"))
        self.assertTrue(a0[8].startswith("23 (") and a1[8].startswith("0 ("))       # касания
        self.assertEqual((a0[3], a1[3]), ("0", "0"))                                   # LLM на жест

    def test_stream_end_equals_metrics(self):
        for rid, run in self.runs.items():
            stream.check_stream(run["stream"])
            st = kinds(run["stream"], "stats")[-1]
            m = run["summary"]["metrics"]
            self.assertEqual(st["distance_m"], m["travel"]["distance_m"], rid)
            self.assertEqual(st.get("safety_share", {}), m["travel"]["safety_share"], rid)
            self.assertEqual(st["touches"], m["touches"]["episodes"], rid)
            self.assertEqual(st["llm"]["gesture"], m["K4"]["llm_calls_outside_requests"], rid)
            self.assertEqual(st["k1"], m["K1_t0_to_cmd_s"], rid)
            self.assertEqual(st["llm"]["request"], m["K4"]["agent"]["llm_calls"], rid)
            self.assertEqual(st["llm"]["service"], m["K4"]["agent"]["service_calls"], rid)

    def test_partial_metrics_equal_summarize(self):
        try:
            import numpy  # noqa: F401 — summarize разбирает K6 наблюдателя через numpy
        except ImportError:
            self.skipTest("нет numpy")
        for rid, path in (("a0", A0), ("a1v2", A1V2)):
            full = M.summarize(str(path))
            part = self.runs[rid]["summary"]["metrics"]
            for key in ("K1_t0_to_cmd_s", "K2_t0_to_motion_s", "early_t0_to_cmd_s", "touches", "travel",
                        "reactions", "K7", "duration_s", "segments", "stopped_by_safety"):
                self.assertEqual(part[key], full[key], (rid, key))
            agent = dict(full["K4"]["agent"], failed=[{"n": f["n"], "request": f["request"]}
                                                       for f in full["K4"]["agent"]["failed"]])
            self.assertEqual(part["K4"], dict(full["K4"], agent=agent), rid)
            self.assertEqual(part["K8"]["memory"], full["K8"]["memory"], rid)


if __name__ == "__main__":
    unittest.main()
