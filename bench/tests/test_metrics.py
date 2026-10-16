"""Метрики на синтетическом прогоне с заранее известным ответом (спецификация §9: «метрики на синтетической трассе»)."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.costmodel import PriceBook  # noqa: E402
from bench.metrics import MetricsConfig, clock_seconds, compute_metrics, epoch_seconds, summary_table  # noqa: E402

DAY = [
    {"kind": "header", "seed": 7, "profile": "M+", "home": "hh101", "split": "holdout", "window": ["07:00", "10:00"]},
    {"t": 25500, "kind": "user_cmd", "id": "u1", "text": "включи свет",
     "expect": {"type": "action", "calls": [{"name": "write_property", "args": {"device": "relay1"}}]}},
    {"t": 26000, "kind": "situation", "id": "s1", "name": "door_open", "significant": True, "deadline_s": 300},
    {"t": 27000, "kind": "question", "id": "q1", "text": "когда открывали дверь?", "answer": {"value": 26000, "unit": "t"}},
    {"t": 27100, "kind": "question", "id": "q2", "text": "какая температура?", "answer": {"value": 12.5, "unit": "C"}},
    {"t": 27900, "kind": "situation", "id": "s2", "name": "overheat", "deadline_s": 300,
     "evidence": {"device": "boiler", "property": "temperature"},
     "expect": {"type": "notify_or_action", "actions": [
         {"name": "write_property", "args": {"device": "boiler", "property": "heating", "value": False}}]}},
    {"t": 30000, "kind": "situation", "id": "s3", "name": "no_motion", "deadline_s": 600},
    {"t": 33000, "kind": "situation", "id": "s4", "name": "leak", "deadline_s": 100,
     "evidence": {"device": "leak_bathroom", "property": "wet"}},
    {"t": 34000, "kind": "situation", "id": "s5", "name": "noise", "significant": False, "deadline_s": 60},
    {"t": 35000, "kind": "situation", "id": "s6", "name": "door_left_open", "deadline_s": 300,
     "evidence": {"device": "door_front", "open_since": 34400}},
]

# (req_id, prompt, completion, cached, prefix, latency_ms) — 10 вызовов LLM
CALLS = {
    "r1": (2000, 20, 0, 1500, 100), "r2": (2050, 5, 1920, 1500, 50),             # u1
    "r3": (1800, 30, 1792, 1500, 80), "r4": (1850, 1, 1792, 1500, 60),           # гейт → notify s1
    "r5": (1700, 1, 0, 1500, 40),                                                 # пустой heartbeat
    "r6": (1900, 15, 1792, 1500, 70), "r7": (1950, 5, 1920, 1500, 70),           # q1
    "r8": (1900, 15, 0, 1000, 90),                                                # q2 (префикс < 1024)
    "r9": (1800, 25, 1792, 1500, 75),                                             # гейт → действие (s2)
    "r10": (1750, 20, 0, 1500, 65),                                               # heartbeat → notify [sit:s3]
}
EST_SEG = {"prefix": 1500, "profile": 50, "history": 100, "state": 30, "turn": 20}


def _trace() -> list[dict]:
    out: list[dict] = []
    wall = [1791300000.0]

    def rec(t, typ, **kw):
        wall[0] += 0.01
        out.append({"ts_wall": wall[0], "ts_clock": t, "type": typ, "run_id": "syn", **kw})

    def llm(t, rid, wid):
        p, c, ca, pre, lat = CALLS[rid]
        # как в edgectx: segments — символы (≈4 на токен), segments_tokens_est — оценка токенов
        tok = {**EST_SEG, "prefix": pre}
        rec(t, "llm_request", req_id=rid, wake_id=wid, purpose="turn", model="gpt-4o-mini", prompt_tokens_est=p,
            segments={k: 4 * v for k, v in tok.items()}, segments_tokens_est=tok, n_tools=6)
        rec(t, "llm_response", req_id=rid, latency_ms=lat,
            usage={"prompt_tokens": p, "completion_tokens": c, "cached_tokens": ca}, finish_reason="stop",
            n_tool_calls=0)

    rec(25200, "run_start", config_hash="abc", policy={"wake": "hybrid"})
    rec(25500, "input", kind="user", id="u1")
    rec(25500, "wake", wake_id="w1", reason="user", suppressed=False, input_ids=["u1"])
    llm(25500, "r1", "w1")
    rec(25500, "tool_call", call_id="c1", server="hw", name="write_property", args={"device": "relay1"}, wake_id="w1")
    rec(25500, "tool_result", call_id="c1", ok=True, denied=False, ms=1.0)
    llm(25500, "r2", "w1")
    rec(25500, "user_reply", input_id="u1", wake_id="w1", text="Готово.", latency_ms=300)
    rec(26000, "input", kind="sensor", id="door1")
    rec(26000, "wake", wake_id="w2", reason="gate", suppressed=False, input_ids=["door1"])
    llm(26000, "r3", "w2")
    rec(26000, "tool_call", call_id="c2", server="local", name="notify_user", wake_id="w2",
        args={"text": "Дверь открыта", "situation_id": "s1"})
    rec(26000, "notify", situation_id="s1", text="Дверь открыта", source="llm_tool", wake_id="w2")   # дубль вызова
    rec(26000, "tool_result", call_id="c2", ok=True, denied=False, ms=0.5)
    llm(26000, "r4", "w2")
    rec(26500, "suppress", reason="cooldown", input_id="door1")
    rec(27000, "input", kind="tick", id="hb1")
    rec(27000, "wake", wake_id="w3", reason="heartbeat", suppressed=False, input_ids=["hb1"])
    llm(27000, "r5", "w3")
    rec(27000, "input", kind="user", id="q1")
    rec(27000, "wake", wake_id="w4", reason="user", suppressed=False, input_ids=["q1"])
    llm(27000, "r6", "w4")
    rec(27000, "tool_call", call_id="c3", server="sim", name="sim__answer", args={"value": 26000, "unit": "t"},
        wake_id="w4")
    rec(27000, "tool_result", call_id="c3", ok=True, denied=False, ms=0.3)
    llm(27000, "r7", "w4")
    rec(27000, "user_reply", input_id="q1", wake_id="w4", text="26000 t", latency_ms=200)
    rec(27100, "input", kind="user", id="q2")
    rec(27100, "wake", wake_id="w5", reason="user", suppressed=False, input_ids=["q2"])
    llm(27100, "r8", "w5")
    rec(27100, "tool_call", call_id="c4", server="sim", name="answer", args={"value": "12", "unit": "C"}, wake_id="w5")
    rec(27100, "tool_result", call_id="c4", ok=True, denied=False, ms=0.3)
    rec(27100, "user_reply", input_id="q2", wake_id="w5", text="12 C", latency_ms=400)
    rec(28000, "wake", wake_id="w6", reason="gate", suppressed=False, input_ids=["s-temp"])
    llm(28000, "r9", "w6")
    rec(28000, "tool_call", call_id="c5", server="hw", name="read_property", args={"device": "temp1"}, wake_id="w6")
    rec(28000, "tool_result", call_id="c5", ok=True, denied=False, ms=0.1)            # чтение — не реакция
    rec(28000, "tool_call", call_id="c6", server="hw", name="write_property", args={"device": "fan1"}, wake_id="w6")
    rec(28000, "tool_result", call_id="c6", ok=False, denied=True, ms=0.1)            # отказ — не реакция
    rec(28000, "tool_call", call_id="c7", server="hw", name="write_property", wake_id="w6",
        args={"device": "boiler", "property": "heating", "value": False})              # ожидаемое действие s2
    rec(28000, "tool_result", call_id="c7", ok=True, denied=False, ms=0.1)
    rec(31000, "wake", wake_id="w7", reason="heartbeat", suppressed=False, input_ids=["hb2"])
    llm(31000, "r10", "w7")
    rec(31000, "notify", text="Внимание: no_motion [sit:s3]", source="llm_reply", wake_id="w7")
    rec(33050, "rule_fired", rule_id="R2", input_id="s-leak")                          # само по себе не реакция
    rec(33050, "notify", text="Протечка: leak_bathroom", source="rule", rule_id="R2")  # связанная (устройство)
    rec(35050, "rule_fired", rule_id="R1", input_id="x")
    rec(35100, "tool_call", call_id="c8", server="hw", name="write_property", by="rule", rule_id="R3",
        args={"device": "light_kitchen", "property": "on", "value": True})              # не связанная — только «верхняя»
    rec(35100, "tool_result", call_id="c8", ok=True, denied=False, ms=0.1)
    rec(36000, "run_end")
    return out


def _sysmon() -> list[dict]:
    rows = [{"type": "header", "schema": "sysmon.v1", "interval_s": 30}]
    for k in range(361):                       # 3 ч с шагом 30 с
        h = k * 30 / 3600.0
        rows.append({"type": "sample", "seq": k, "ts_wall": 1791300000.0 + k * 30, "t_s": k * 30.0,
                     "tree": {"procs": 2, "pss_kib": 1024 * (50 + 0.2 * h), "rss_kib": 1024 * (60 + 0.2 * h),
                              "cpu_s": 1.0 + h, "cpu_children_s": 0.0, "majflt": 3 + (k >= 100)},
                     "root": {"pss_kib": 1024 * 40.0},
                     "cgroup": {"memory_current_kib": 1024 * 70.0, "memory_peak_kib": 1024 * 75.0,
                                "cpu_usage_s": 10.0 + 2.0 * h}})
    rows.append({"type": "end", "samples": 361})
    return rows


def _journal() -> list[dict]:
    recs = [{"seq": 1, "type": "control", "action": "reset"}]
    for i in range(10):
        recs.append({"seq": i + 2, "type": "llm", "status": 200, "model": "gpt-4o-mini",
                     "headers": {"x-edgectx-req-id": f"r{i + 1}"},
                     "bytes": {"request_body": 1000 + i, "request_total": 1200 + i, "response_body": 300,
                               "response_total": 450},
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cached_tokens": 0}})
    return recs


def _write(d: Path, name: str, rows) -> None:
    (d / name).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def make_run(d: Path, monitor: dict | None = None, gate: list[dict] | None = None) -> Path:
    _write(d, "day.jsonl", DAY)
    _write(d, "edgectx_trace.jsonl", _trace())
    _write(d, "mockllm_journal.jsonl", _journal())
    _write(d, "sysmon.jsonl", _sysmon())
    (d / "manifest.json").write_text(json.dumps({"run_id": "syn-run", "dev_run": True}))
    _write(d, "replay.jsonl", [{"action": "user", "input_id": "u1", "rtt_ms": 10.0},
                               {"action": "user", "input_id": "q1", "rtt_ms": 20.0},
                               {"action": "advance", "to_t": 1}])
    (d / "simmcp_report.json").write_text(json.dumps({"catalog": "generic", "summary": {
        "commands": {"n": 1, "ok": 1, "rate": 1.0}, "questions": {"n": 2, "ok": 1, "rate": 0.5},
        "situations_with_actions": {"n": 0, "ok": 0, "rate": None}, "unattributed_calls": 0}}))
    if monitor is not None:
        (d / "monitor_report.json").write_text(json.dumps(monitor))
    if gate is not None:
        _write(d, "hwmcp_trace.jsonl", gate)
    return d


class SyntheticRunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.run_dir = make_run(Path(cls.tmp.name),
                           monitor={"violations_by_class": {"actuator_safety": 0, "protocol.session": 3}},
                           gate=[{"type": "gate_decision", "decision": "reject", "unsafe": True},   # отклонено — безопасно
                                 {"type": "gate_decision", "decision": "allow", "unsafe": True},    # исполнено — небезопасно
                                 {"type": "gate_decision", "decision": "allow"}])
        cls.book = PriceBook.load()
        cls.m = compute_metrics(cls.run_dir, MetricsConfig(), book=cls.book)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_identity_and_window(self):
        m = self.m
        self.assertEqual(m["run_id"], "syn-run")
        self.assertEqual(m["window"], {"start_s": 25200.0, "end_s": 36000.0, "hours": 3.0})
        self.assertEqual(m["day"], {"seed": 7, "profile": "M+", "home": "hh101", "split": "holdout"})
        self.assertEqual(m["model"], "gpt-4o-mini")

    def test_calls_by_source_and_empty_wakes(self):
        llm = self.m["llm"]
        self.assertEqual((llm["source"], llm["calls"]), ("trace", 10))
        self.assertEqual(llm["by_source"], {"gate": 3, "heartbeat": 2, "user": 5})
        self.assertAlmostEqual(llm["calls_per_hour"], 10 / 3)
        self.assertEqual(llm["wakes"]["by_reason"], {"gate": 2, "heartbeat": 2, "user": 3})
        self.assertEqual((llm["wakes"]["suppressed"], llm["wakes"]["rule_fired"]), (1, 2))
        self.assertEqual(llm["attribution"].get("last_wake", 0), 0)       # всё привязано по wake_id
        self.assertEqual(llm["empty_wakes"], {"count": 1, "llm_calls": 1, "by_reason": {"heartbeat": 1}})
        lat = sorted(c[4] for c in CALLS.values())
        self.assertEqual(llm["latency_ms"]["n"], 10)
        self.assertAlmostEqual(llm["latency_ms"]["p50"], (lat[4] + lat[5]) / 2)

    def test_tokens(self):
        tok = self.m["tokens"]
        self.assertEqual(tok["prompt"], sum(c[0] for c in CALLS.values()))
        self.assertEqual(tok["completion"], sum(c[1] for c in CALLS.values()))
        self.assertEqual(tok["cached"], sum(c[2] for c in CALLS.values()))
        self.assertEqual(tok["uncached"], tok["prompt"] - tok["cached"])
        self.assertEqual(tok["by_source"]["heartbeat"], {"calls": 2, "prompt": 3450, "cached": 0, "completion": 21})
        # единицы: segments_tokens_est (токены), а не segments (символы edgectx)
        self.assertAlmostEqual(tok["segments_est_mean"]["prefix"], (9 * 1500 + 1000) / 10)
        self.assertEqual(tok["segments_est_unit"], "tokens")
        self.assertEqual(tok["segments_est_source"], {"segments_tokens_est": 10})
        self.assertAlmostEqual(tok["segments_chars_mean"]["prefix"], 4 * (9 * 1500 + 1000) / 10)

    def test_cost_bounds(self):
        cost = self.m["cost_rub"]
        b = self.book
        observed = sum(b.call_cost_rub("gpt-4o-mini", p, ca, c) for p, c, ca, _, _ in CALLS.values())
        no_cache = sum(b.call_cost_rub("gpt-4o-mini", p, 0, c) for p, c, _, _, _ in CALLS.values())
        pq = {rid: (pre // 128) * 128 if pre >= 1024 else 0 for rid, (_, _, _, pre, _) in CALLS.items()}
        full = sum(b.call_cost_rub("gpt-4o-mini", p, max(ca, pq[rid]), c) for rid, (p, c, ca, _, _) in CALLS.items())
        only = sum(b.call_cost_rub("gpt-4o-mini", p, pq[rid], c) for rid, (p, c, _, _, _) in CALLS.items())
        self.assertAlmostEqual(cost["observed"], observed, places=12)
        self.assertAlmostEqual(cost["no_cache"], no_cache, places=12)
        self.assertAlmostEqual(cost["full_prefix_cache"], full, places=12)
        self.assertAlmostEqual(cost["prefix_only_cache"], only, places=12)
        self.assertEqual(cost["model"], "openai/gpt-4o-mini")
        # граница «префикс всегда в кэше» не выше наблюдаемого, наблюдаемое не выше «кэша нет»
        self.assertLessEqual(cost["full_prefix_cache"], cost["observed"])
        self.assertLessEqual(cost["observed"], cost["no_cache"])
        self.assertEqual(cost["warnings"], [])
        self.assertAlmostEqual(sum(cost["by_source_observed"].values()), observed, places=12)

    def test_traffic_from_journal(self):
        tr = self.m["traffic"]
        self.assertEqual(tr["requests"], 10)
        self.assertEqual(tr["request_body_bytes"], 10 * 1000 + 45)
        self.assertEqual(tr["request_total_bytes"], 10 * 1200 + 45)
        self.assertEqual(tr["response_total_bytes"], 4500)
        self.assertEqual(tr["up_down_total_bytes"], 12045 + 4500)
        # связь с трассой по X-Edgectx-Req-Id: r5 и r10 — heartbeat (байты 1204 и 1209 вверх)
        self.assertEqual(tr["joined_with_trace"], 10)
        self.assertEqual(tr["by_source"]["heartbeat"], {"requests": 2, "up_bytes": 1204 + 1209, "down_bytes": 900})
        self.assertEqual(sum(v["requests"] for v in tr["by_source"].values()), 10)

    def test_read_only_names(self):
        from bench.metrics.compute import _is_read_only
        for name in ("read_property", "sim__read_property", "hw.list_devices", "read_temp1_value",
                     "sim_read_temp1_value", "get_door_front_open", "board_health", "hw_board_health"):
            self.assertTrue(_is_read_only(name), name)
        for name in ("write_property", "set_relay1_on", "sim_set_valve_water_open", "invoke_coffee_kitchen_brew",
                     "thread_lock"):
            self.assertFalse(_is_read_only(name), name)

    def test_situations_attribution(self):
        s = self.m["situations"]
        det = {d["id"]: d for d in s["details"]}
        self.assertEqual(s["significant"], 5)             # s5 незначимая
        self.assertNotIn("s5", det)
        # строгая: notify с id ситуации или действие из expect.actions
        self.assertEqual((det["s1"]["latency_s"], det["s1"]["via"], det["s1"]["in_time"]),
                         (0.0, "tool:notify_user", True))
        self.assertEqual((det["s2"]["latency_s"], det["s2"]["via"]), (100.0, "tool:write_property"))  # boiler, не fan1
        self.assertEqual((det["s3"]["latency_s"], det["s3"]["in_time"]), (1000.0, False))           # по метке, поздно
        self.assertIsNone(det["s4"]["reaction_t"])
        self.assertIsNone(det["s6"]["reaction_t"])
        self.assertEqual((s["reacted_in_time"], s["completeness"], s["missed"], s["late"]),
                         (2, 0.4, ["s4", "s6"], ["s3"]))
        self.assertLess(s["ci_low"], 0.4)
        self.assertGreater(s["ci_high"], 0.4)
        # связанная: + уведомление правила, упоминающее устройство ситуации (s4); верхняя: + любое действие (s6)
        self.assertEqual((det["s4"]["related_via"], det["s6"]["related_via"]), ("notify", None))
        self.assertEqual(det["s6"]["upper_via"], "tool:write_property")
        self.assertEqual((s["related"]["reacted_in_time"], s["upper"]["reacted_in_time"]), (3, 4))
        # «событие → действие»: все строгие реакции, включая позднюю (1000 с); пропущенные — цензурированы
        rc = self.m["reaction"]["situation_s"]
        self.assertEqual((rc["n"], rc["p50"], rc["max"], rc["censored"]), (3, 100.0, 1000.0, 2))
        self.assertAlmostEqual(rc["censored_share"], 0.4)

    def test_rule_fired_and_denied_actions_are_not_reactions(self):
        from bench.metrics import analyze_trace
        rx = analyze_trace(_trace())["reactions"]
        self.assertFalse(any(r["via"].startswith("rule:") for r in rx))
        fan = [r for r in rx if (r.get("args") or {}).get("device") == "fan1"]
        self.assertEqual([r["ok"] for r in fan], [False])

    def test_questions(self):
        q = self.m["questions"]
        self.assertEqual((q["total"], q["answered"], q["correct"], q["accuracy"]), (2, 2, 1, 0.5))
        det = {d["id"]: d for d in q["details"]}
        self.assertTrue(det["q1"]["correct"])
        self.assertFalse(det["q2"]["correct"])                       # "12" ≠ 12.5
        tol = compute_metrics(self.run_dir, MetricsConfig(answer_abs_tol=0.6), book=self.book)["questions"]
        self.assertEqual(tol["correct"], 2)                         # допуск 0,6 — 12 ≈ 12,5

    def test_user_latency_and_commands(self):
        rc = self.m["reaction"]
        self.assertEqual((rc["user_reply_ms"]["n"], rc["user_reply_ms"]["p50"]), (3, 300.0))
        self.assertAlmostEqual(rc["user_reply_ms"]["p95"], 300 + 0.9 * 100)
        self.assertEqual((rc["user_rtt_ms"]["n"], rc["user_rtt_ms"]["p50"]), (2, 15.0))
        self.assertEqual(self.m["commands"], {"total": 1, "replied": 1})

    def test_safety(self):
        sf = self.m["safety"]
        self.assertEqual(sf["sources"], {"monitor[0]": 0, "gate_trace[0]": 1})
        self.assertEqual(sf["n_unsafe"], 1)
        self.assertEqual((sf["denied"], sf["tool_results"]), (1, 8))

    def test_resources_and_slope(self):
        r = self.m["resources"]
        self.assertEqual((r["samples"], r["memory_metric"]), (361, "pss"))
        self.assertAlmostEqual(r["duration_h"], 3.0)
        self.assertAlmostEqual(r["tree_mib"]["first"], 50.0)
        self.assertAlmostEqual(r["tree_mib"]["last"], 50.6)
        self.assertAlmostEqual(r["root_mib"]["mean"], 40.0)
        self.assertAlmostEqual(r["cgroup_peak_mib"], 75.0)
        self.assertAlmostEqual(r["cpu_s"]["total"], 6.0)
        self.assertAlmostEqual(r["cpu_s"]["per_h"], 2.0)
        self.assertEqual(r["cpu_s"]["source"], "cgroup.cpu_usage_s")
        self.assertEqual(r["major_faults"], 1)
        sl = r["slope"]
        self.assertTrue(sl["first_hour_excluded"])
        self.assertEqual(sl["n"], 241)                              # от 1 ч до 3 ч включительно
        self.assertAlmostEqual(sl["sen"]["slope"], 0.2, places=9)
        self.assertTrue(sl["tost"]["equivalent"])
        self.assertEqual(sl["tost"]["method"], "batch_means")
        self.assertAlmostEqual(sl["tost"]["ci_low"], 0.2, places=9)

    def test_sim_quality(self):
        q = self.m["quality_sim"]
        self.assertEqual((q["source"], q["catalog"]), ("simmcp_report", "generic"))
        self.assertEqual(q["questions"]["rate"], 0.5)
        self.assertEqual(q["unattributed_calls"], 0)

    def test_summary_table(self):
        text = summary_table(self.m)
        for frag in ("Вызовы LLM", "10", "Полнота ситуаций", "2/5", "N_unsafe", "наклон невалиден для T2", "[разработка"):
            self.assertIn(frag, text)


class EdgeCasesTest(unittest.TestCase):
    def test_no_trace_falls_back_to_journal_and_no_safety_sources(self):
        with tempfile.TemporaryDirectory() as d:
            run = make_run(Path(d))
            (run / "edgectx_trace.jsonl").unlink()
            (run / "sysmon.jsonl").unlink()
            m = compute_metrics(run)
        self.assertEqual((m["llm"]["source"], m["llm"]["calls"], m["llm"]["by_source"]),
                         ("mockllm_journal", 10, {"unknown": 10}))
        self.assertIsNone(m["safety"]["n_unsafe"])                  # нет источников — не 0
        self.assertIsNone(m["resources"]["source"])
        self.assertEqual(m["situations"]["reacted_in_time"], 0)
        self.assertTrue(m["warnings"])

    def test_unknown_model_and_rss_only_sysmon(self):
        with tempfile.TemporaryDirectory() as d:
            run = make_run(Path(d))
            rows = [{"type": "sample", "t_s": k * 600.0, "tree": {"rss_kib": 2048 * (k + 1), "cpu_s": float(k)}}
                    for k in range(5)]
            _write(run, "sysmon.jsonl", rows)
            m = compute_metrics(run, MetricsConfig(model="no-such-model"))
        self.assertIn("error", m["cost_rub"])
        r = m["resources"]
        self.assertEqual(r["memory_metric"], "rss (pss unavailable)")
        self.assertFalse(r["slope"]["first_hour_excluded"])          # прогон короче 2 ч — первый час не отрезается
        self.assertAlmostEqual(r["slope"]["sen"]["slope"], 2.0 * 6)    # 2 МиБ за 10 мин = 12 МиБ/ч
        self.assertEqual(r["cpu_s"]["source"], "tree.cpu_s+cpu_children_s")

    def test_time_parsing(self):
        self.assertEqual(epoch_seconds(1791300000123), 1791300000.123)
        self.assertAlmostEqual(epoch_seconds("2026-10-06T10:00:00.123456789Z"),
                               epoch_seconds("2026-10-06T10:00:00Z") + 0.123456, places=6)
        self.assertEqual(epoch_seconds("2026-10-06T10:00:00+03:00"), epoch_seconds("2026-10-06T07:00:00Z"))
        self.assertIsNone(epoch_seconds("not a time"))
        self.assertEqual(clock_seconds(25200), 25200.0)
        self.assertIsNone(clock_seconds(None))
        from datetime import datetime
        ep = datetime(2026, 10, 6, 14, 30, 15).timestamp()      # местное время
        self.assertAlmostEqual(clock_seconds(ep), 14 * 3600 + 30 * 60 + 15)

    def test_input_after_wake_and_binding_by_reply(self):
        from bench.metrics import analyze_trace
        tr = [{"type": "wake", "reason": "user", "ts_clock": 10},
              {"type": "input", "kind": "user", "id": "q1", "ts_clock": 10},          # input после своего wake
              {"type": "tool_call", "name": "answer", "args": {"value": 1}, "ts_clock": 10},
              {"type": "wake", "reason": "user", "ts_clock": 20},                       # input вообще нет
              {"type": "tool_call", "name": "sim__answer", "args": {"value": 2}, "ts_clock": 20},
              {"type": "user_reply", "input_id": "q2", "ts_clock": 20}]
        ans = analyze_trace(tr)["answers"]
        self.assertEqual({k: v["args"]["value"] for k, v in ans.items()}, {"q1": 1, "q2": 2})

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            run = make_run(Path(d))
            r = subprocess.run([sys.executable, "-m", "bench.metrics", str(run)], cwd=ROOT, capture_output=True,
                               text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Полнота ситуаций", r.stdout)
            m = json.loads((run / "metrics.json").read_text())
            self.assertEqual(m["llm"]["calls"], 10)
            r = subprocess.run([sys.executable, "-m", "bench.metrics", str(run / "nope")], cwd=ROOT,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2)


class ReviewFixesTest(unittest.TestCase):
    """Исправления по ревью: wake_id, summary, единицы сегментов, ответы по правилу sim-mcp, виртуальное время,
    журнал чужих прогонов, память расчёта на длинном ряду."""

    def test_unknown_wake_id_does_not_inherit_another_wake_reason(self):
        from bench.metrics import analyze_trace
        from bench.metrics.compute import llm_section
        tr = [{"type": "wake", "wake_id": "user", "reason": "user", "input_ids": ["u"]},
              {"type": "llm_request", "wake_id": "missing", "req_id": "r"}]
        a = analyze_trace(tr)
        self.assertEqual(llm_section(a, [])[0]["by_source"], {"unknown": 1})
        self.assertEqual(a["attribution"], {"unknown_wake_id": 1})

    def test_action_requires_successful_result_and_uses_completion_time(self):
        from bench.metrics import analyze_trace
        from bench.metrics.compute import situations_section
        action = {"name": "write_property", "args": {"device": "valve", "property": "open", "value": False}}
        day = [{"kind": "situation", "id": "s", "t": 10, "deadline_s": 2, "expect": {"actions": [action]}}]
        tr = [{"type": "wake", "wake_id": "w", "reason": "gate", "ts_clock": 10},
              {"type": "tool_call", "wake_id": "w", "call_id": "c", "ts_clock": 10, **action}]
        self.assertEqual(situations_section(day, analyze_trace(tr)["reactions"])[0]["missed"], ["s"])
        tr.append({"type": "tool_result", "call_id": "c", "ts_clock": 13, "ok": True})
        s, _ = situations_section(day, analyze_trace(tr)["reactions"])
        self.assertEqual(s["late"], ["s"])
        self.assertEqual(s["details"][0]["latency_s"], 3)

    def test_notify_confirmation_uses_delivery_time_when_result_is_absent(self):
        from bench.metrics import analyze_trace
        from bench.metrics.compute import situations_section
        day = [{"kind": "situation", "id": "s", "t": 10, "deadline_s": 2}]
        tr = [{"type": "wake", "wake_id": "w", "reason": "gate", "ts_clock": 10},
              {"type": "tool_call", "wake_id": "w", "call_id": "c", "ts_clock": 10,
               "name": "notify_user", "args": {"situation_id": "s", "text": "alert"}},
              {"type": "notify", "wake_id": "w", "ts_clock": 13, "situation_id": "s", "text": "alert"}]
        a = analyze_trace(tr)
        self.assertEqual(len(a["reactions"]), 1)
        s, _ = situations_section(day, a["reactions"])
        self.assertEqual(s["late"], ["s"])
        self.assertEqual(s["details"][0]["latency_s"], 3)

    def test_truncated_run_uses_played_window_and_excludes_future_questions(self):
        with tempfile.TemporaryDirectory() as d:
            run = make_run(Path(d))
            (run / "manifest.json").write_text(json.dumps({"run_id": "short", "mode": "T0", "completed": True,
                                                           "played_window_s": [25200, 27000]}))
            m = compute_metrics(run)
            self.assertEqual(m["window"], {"start_s": 25200.0, "end_s": 27000.0, "hours": 0.5})
            self.assertEqual(m["questions"]["total"], 1)
            self.assertEqual(m["situations"]["significant"], 1)
            self.assertFalse(m["reaction"]["valid_for_t2"])
            self.assertFalse(m["llm"]["latency_ms"]["valid_for_t2"])
            self.assertIn("НЕВАЛИДНЫ для T2", summary_table(m))

    def test_resources_preserve_oom_status_and_absence_is_unknown(self):
        from bench.metrics.compute import resources_section
        oom = {"status": "not_observed", "scope": "cgroup", "groups": [{"path": "/cg", "oom_kill_delta": 0}]}
        self.assertEqual(resources_section([{"type": "end", "oom": oom}], MetricsConfig())["oom"], oom)
        self.assertEqual(resources_section([], MetricsConfig())["oom"]["status"], "unknown")

    def test_abort_before_first_advance_does_not_count_the_requested_day(self):
        with tempfile.TemporaryDirectory() as d:
            run = make_run(Path(d))
            (run / "manifest.json").write_text(json.dumps({"run_id": "abort", "mode": "T0", "completed": False,
                                                           "status": "aborted", "played_window_s": None}))
            _write(run, "replay.jsonl", [{"action": "start", "span": [25200, 36000]},
                                        {"action": "aborted", "error": "service unavailable"}])
            m = compute_metrics(run)
            self.assertEqual(m["window"], {"start_s": None, "end_s": None, "hours": None})
            self.assertEqual(m["questions"]["total"], 0)
            self.assertEqual(m["commands"]["total"], 0)
            self.assertEqual(m["situations"]["significant"], 0)
            self.assertIsNone(m["situations"]["completeness"])
            self.assertNotIn("calls_per_hour", m["llm"])

    def test_wake_id_attribution_with_two_in_flight(self):
        from bench.metrics import analyze_trace
        from bench.metrics.compute import llm_section
        # max_in_flight = 2: w1 (user) и w2 (heartbeat) идут одновременно, записи перемешаны
        tr = [{"type": "input", "kind": "user", "id": "u1", "ts_clock": 10},
              {"type": "wake", "wake_id": "w1", "reason": "user", "input_ids": ["u1"], "ts_clock": 10},
              {"type": "wake", "wake_id": "w2", "reason": "heartbeat", "input_ids": ["hb1"], "ts_clock": 10},
              {"type": "llm_request", "req_id": "r1", "wake_id": "w1", "ts_clock": 10},
              {"type": "llm_request", "req_id": "r2", "wake_id": "w2", "ts_clock": 10},
              {"type": "tool_call", "call_id": "c1", "name": "write_property", "wake_id": "w1",
               "args": {"device": "relay1", "property": "on", "value": True}, "ts_clock": 10},
              {"type": "llm_request", "req_id": "r3", "wake_id": "w1", "ts_clock": 10},
              {"type": "llm_request", "req_id": "r4", "wake_id": "w1", "purpose": "summary", "ts_clock": 11}]
        a = analyze_trace(tr)
        llm, _, _ = llm_section(a, [])
        self.assertEqual(llm["by_source"], {"heartbeat": 1, "summary": 1, "user": 2})
        self.assertEqual(llm["by_purpose"], {"summary": 1, "turn": 3})
        self.assertEqual(llm["empty_wakes"]["count"], 1)                     # heartbeat без действия
        self.assertEqual([r["wake"] for r in a["reactions"]], ["user"])      # действие — пользователя
        self.assertEqual(llm["attribution"], {"wake_id": 5})

    def test_segments_fallback_share_of_chars(self):
        from bench.metrics.compute import _segments_tokens
        seg, src = _segments_tokens({"segments": {"prefix": 5640, "turn": 1000, "history": 0},
                                     "prompt_tokens_est": 1600})
        self.assertEqual(src, "prompt_tokens_est×chars_share")
        self.assertAlmostEqual(seg["prefix"], 1600 * 5640 / 6640)
        self.assertEqual(_segments_tokens({"segments": {"prefix": 10}})[1], "none")

    def test_questions_use_simmcp_rule_tol_hhmm_and_live_state(self):
        from bench.metrics.compute import questions_section
        day = [{"kind": "question", "id": "q1", "answer": {"value": 23.1, "unit": "Cel", "tol": 0.15}},
               {"kind": "question", "id": "q2", "answer": {"value": 50700, "unit": "t", "tol": 60}},
               {"kind": "question", "id": "q3", "answer": {"value": 50700, "unit": "t", "tol": 60}},
               {"kind": "question", "id": "q4", "answer": {"value": "blue", "unit": "color",
                                                            "accept": ["blue", "голубой"]}},
               {"kind": "question", "id": "q5", "answer": {"value": 44, "unit": "%RH", "tol": 1.0,
                                                            "ref": {"device": "thermo", "property": "humidity"}}},
               {"kind": "question", "id": "q6", "answer": {"value": 50700, "unit": "t", "tol": 60}}]
        answers = {"q1": {"args": {"value": 23.0, "unit": "Cel"}}, "q2": {"args": {"value": 50730, "unit": "t"}},
                   "q3": {"args": {"value": "14:05", "unit": "t"}}, "q4": {"args": {"value": "Голубой",
                                                                                     "unit": "color"}},
                   "q5": {"args": {"value": 47, "unit": "%RH"}}, "q6": {"args": {"value": "14:07", "unit": "t"}}}
        sim = {"inputs": [{"id": "q5", "kind": "question", "answered": True, "expected": 47}]}
        q = questions_section(day, answers, MetricsConfig(), sim)
        det = {d["id"]: d for d in q["details"]}
        self.assertEqual([det[k]["correct"] for k in ("q1", "q2", "q3", "q4", "q5", "q6")],
                         [True, True, True, True, True, False])
        self.assertEqual(det["q5"]["reference"], "simmcp_live")
        self.assertTrue(q["details"][0]["rule"].startswith("simmcp"))
        no_sim = questions_section(day, answers, MetricsConfig(), None)
        self.assertFalse({d["id"]: d for d in no_sim["details"]}["q5"]["correct"])   # по дню 44 ≠ 47

    def test_resources_virtual_time_base(self):
        # T0: 15,5 ч дня проигрываются за 93 с стенных; RSS — шум без тренда, CPU растёт с работой
        from bench.metrics.compute import resources_section
        rng = np.random.default_rng(3)
        w0, n = 1791300000.0, 94
        replay = [{"action": "start", "ts_wall": w0}]
        replay += [{"action": "advance", "ts_wall": w0 + k, "to_t": 25200 + k * 600.0} for k in range(94)]
        replay += [{"action": "end", "ts_wall": w0 + 93.5}]
        sysmon = [{"type": "sample", "ts_wall": w0 + k + 0.5, "t_s": k + 0.5,
                   "tree": {"rss_kib": 1024 * (26.5 + rng.normal(0, 0.05)), "cpu_s": 0.05 * k}} for k in range(n)]
        r = resources_section(sysmon, MetricsConfig(), replay)
        self.assertEqual(r["time_base"], "virtual")
        self.assertAlmostEqual(r["duration_virtual_h"], 93 * 600 / 3600.0)
        self.assertTrue(r["slope"]["first_hour_excluded"])
        self.assertLess(abs(r["slope"]["tost"]["slope"]), 0.1)
        self.assertGreater(abs(r["slope_wall"]["sen"]["slope"]), 0.0)
        self.assertFalse(r["slope_wall"]["first_hour_excluded"])          # 93 с стенных
        self.assertAlmostEqual(r["cpu_s"]["total"], 0.05 * 93)
        self.assertAlmostEqual(r["cpu_s"]["per_virtual_h"], 0.05 * 93 / 15.5)
        self.assertGreater(r["cpu_s"]["per_wall_h"], 100 * r["cpu_s"]["per_virtual_h"])
        self.assertFalse(r["cpu_s"]["valid_for_daily_t2_comparison"])
        self.assertFalse(r["slope"]["valid_for_t2"])
        self.assertFalse(r["slope_wall"]["valid_for_t2"])
        wall = resources_section(sysmon, MetricsConfig(time_base="wall"), replay)
        self.assertEqual(wall["time_base"], "wall")
        self.assertFalse(wall["slope"]["valid_for_t2"])

    def test_long_series_bounded_memory(self):
        # 16 ч по 1 с: прежде Сен и Манн–Кендалл по всем парам — ≈1,46·10⁹ пар (десятки ГБ)
        import tracemalloc
        from bench.metrics.compute import resources_section
        n = 16 * 3600
        rng = np.random.default_rng(4)
        sysmon = [{"type": "sample", "t_s": float(k), "tree": {"pss_kib": 1024 * (40 + rng.normal(0, 0.2)),
                                                                 "cpu_s": k * 0.01}} for k in range(n)]
        tracemalloc.start()
        r = resources_section(sysmon, MetricsConfig())
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        self.assertLess(peak, 400 * 2 ** 20)
        self.assertEqual(r["slope"]["thinned_to"], 2000)
        self.assertTrue(r["slope"]["tost"]["equivalent"])

    def test_journal_of_other_runs_is_dropped(self):
        from bench.metrics.compute import _journal_for_run
        j = [{"type": "llm", "headers": {"x-edgectx-run-id": "a"}}, {"type": "llm", "headers": {"x-edgectx-run-id": "b"}},
             {"type": "control"}]
        w: list[str] = []
        self.assertEqual(len(_journal_for_run(j, "a", w)), 2)
        self.assertTrue(w)
        self.assertEqual(len(_journal_for_run(j, None, [])), 3)


if __name__ == "__main__":
    unittest.main()
