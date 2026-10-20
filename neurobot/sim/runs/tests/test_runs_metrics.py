"""Метрики прогона (metrics.py) и сводка (compare.py) на синтетическом каталоге: глухие жесты по probe (короткие окна,
занятость манёвром, защёлка), касания с дребезгом, видимость перезапуска по PLAN §1 для пути жеста и агентского пути
(человек — только за роль, которую чинил, и за защёлку, вставшую из-за отказа; реплики — последнему отказу), остановки
защитой, разбор верной реакции, тупики защиты (и тупики touch с отъездами исполнителя), пробег без переносов, исходы
реплик «вперёд», K6 в перерывах мира двойника, агент и LLM (служебные вызовы хода), память и дрейф, столбцы сводки и
сведение частей start_test, серии K6."""
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
        self.assertEqual(M.deaf_gestures(segs, probe, T0, 10.0)["deaf"], [])
        probe = probe_rows([(0, 30), (47, 120)])
        self.assertEqual(M.deaf_gestures(segs, probe, T0, 10.0)["deaf"], [1])
        self.assertEqual(M.deaf_gestures(segs, [], T0, 10.0)["deaf"], [0, 1])

    def test_short_gesture_between_probe_rows_is_not_deaf(self):
        """Жест 1,2 с между строками probe (раз в секунду) — вызовы делятся по перекрытию, а не теряются."""
        probe = probe_rows([(0, 120)])
        self.assertEqual(M.det_rate(probe, 10.3, 11.5, T0), 12.0)
        self.assertEqual(M.deaf_gestures([seg(0, 10.3, 11.5)], probe, T0, 10.0)["deaf"], [])

    def test_busy_and_latched_are_not_deaf(self):
        segs = [seg(0, 10, 20), seg(1, 40, 50), seg(2, 70, 80)]
        probe = probe_rows([(0, 5)])                  # детектор стоит весь прогон
        truth = [{"m": T0 + t, "v": 0.15 if 10 <= t <= 18 else 0.0, "w": 0.0} for t in range(0, 100)]
        safety = [{"m": T0 + 72, "state": "estop"}]
        got = M.deaf_gestures(segs, probe, T0, 10.0, truth, safety)
        self.assertEqual(got, {"deaf": [1], "busy": [0], "latched": [2]})
        # робот ответил манёвром (распознал раньше конца окна и перестал смотреть) — не глух
        got = M.deaf_gestures(segs, probe, T0, 10.0, truth, safety, reacted=[1])
        self.assertEqual(got["deaf"], [])

    def test_restart_does_not_give_negative_progress(self):
        rows = [{"m": T0 + 1, "pid": 1, "det_calls": 50}, {"m": T0 + 2, "pid": 2, "det_calls": 0},
                {"m": T0 + 3, "pid": 2, "det_calls": 10}, {"m": T0 + 4, "pid": 2, "det_calls": 3}]
        self.assertEqual(M.det_rate(rows, 0, 5, T0), 10)
        self.assertIsNone(M.det_rate(rows[:1], 0, 5, T0))


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
                            {"fault": "kill_rosbridge", "m": T0 + 50, "ttr_s": None},
                            {"fault": "kill_agent", "m": T0 + 100, "ttr_s": 30.0}],
                           agent=[{"n": 0, "m": T0 + 11, "ok": False}, {"n": 1, "m": T0 + 60, "ok": False},
                                  {"n": 2, "m": T0 + 110, "ok": False}, {"n": 3, "m": T0 + 150, "ok": False}])
        rows = M.fault_rows(run, [seg(0, 100, 105)], {}, [])
        self.assertEqual((rows[0]["path"], rows[0]["agent_failed"], rows[0]["visible"]), ("агент", [0], True))
        # невосстановившийся rosbridge — до конца прогона; реплика — последнему отказу, в окно которого попала
        self.assertEqual(rows[1]["agent_failed"], [1, 3])
        self.assertEqual(rows[1]["why"], ["не восстановилось само", "реплик без ответа: 2"])
        self.assertEqual(rows[2]["agent_failed"], [2])
        self.assertEqual(M.fault_role("stop_perception"), "perception")
        self.assertEqual(M.fault_role("camera_black"), "camera")

    def test_human_counts_only_for_the_role_he_repaired(self):
        segs = [seg(0, 10, 15), seg(1, 60, 65)]
        per = {0: {"reaction": "interrupted"}, 1: {"reaction": "correct"}}
        run = self.run_doc([{"fault": "camera_delay", "m": T0 + 5, "ttr_s": 40.0},
                            {"fault": "kill_rosbridge", "m": T0 + 50, "ttr_s": 90.0}],
                           ops=[{"m": T0 + 20, "action": "relocate"}, {"m": T0 + 22, "action": "estop_release"},
                                {"m": T0 + 130, "action": "doctor", "what": "neurobot-rosbridge.service"}])
        rows = M.fault_rows(run, segs, per, deaf=[], excluded=[0])
        self.assertEqual((rows[0]["visible"], rows[0]["lost_gestures"]), (True, []))   # TTR 40 с > паузы 5 с до жеста
        self.assertNotIn("понадобился человек", rows[0]["why"])
        self.assertEqual(rows[1]["operator"], ["doctor:neurobot-rosbridge.service"])
        self.assertEqual(M.op_roles({"action": "restart", "what": "demo"}), {"camera", "perception"})
        self.assertEqual(M.op_roles({"action": "start_failed", "units": ["neurobot-mcp"]}), {"mcp"})
        self.assertEqual(M.op_roles({"action": "relocate"}), set())

    def test_latch_caused_by_fault_needs_human_and_touch_latch_does_not(self):
        # смерть базы двойника: мир заново, удар IMU → защёлка в окне отказа, снял человек — отказ видимый; защёлка
        # от касания рядом с отказом камеры — не его следствие
        segs = [seg(0, 100, 105), seg(1, 140, 145)]
        run = self.run_doc([{"fault": "kill_base", "m": T0 + 20, "ttr_s": 8.5},
                            {"fault": "camera_delay", "m": T0 + 60, "ttr_s": 8.0}],
                           ops=[{"m": T0 + 60, "action": "estop_release"}, {"m": T0 + 96, "action": "estop_release"}])
        safety = [{"m": T0 + 10, "state": "slow"}, {"m": T0 + 29, "state": "estop", "reasons": ["shock: удар 9.8"]},
                  {"m": T0 + 60.5, "state": "slow"}, {"m": T0 + 63, "state": "estop", "reasons": ["shock: удар 7"]},
                  {"m": T0 + 95, "state": "slow"}]
        truth = [{"m": T0 + 62.5, "contacts": ["chair"]}]
        rows = M.fault_rows(run, segs, {}, [], safety=safety, truth=truth)
        self.assertEqual((rows[0]["visible"], rows[0]["why"], rows[0]["operator"]),
                         (True, ["защёлка safety_gate (shock): снял человек"], ["estop_release:"]))
        self.assertEqual(rows[0]["latch"], [{"at_s": 29.0, "reasons": ["shock"], "released": True}])
        self.assertEqual((rows[1]["visible"], rows[1]["latch"]), (False, []))
        got = M.latches(safety, [{"m": T0 + 60, "action": "estop_release"}])
        self.assertEqual([(c["m0"] - T0, c["released"]) for c in got], [(29.0, True), (63.0, False)])


class AgentAndMemoryTest(unittest.TestCase):
    def test_agent_summary_and_llm_outside_requests(self):
        run = {"label": "main", "system": "a1", "t_start": 0, "t_end": 100,
               "agent": [{"n": 0, "request": "status", "ok": True, "latency_s": 0.05, "t": 9.9},
                         {"n": 1, "request": "move", "ok": False, "error": "pico: refused", "tools": [], "t": 30.0,
                          "latency_s": 0.1}]}
        llm = [{"type": "llm", "model": "gpt-4o-mini", "ts_wall": 10, "n_tools": 3, "usage": {"prompt_tokens": 900},
                "oracle": {"input_ids": ["main-a1-0"]}},
               {"type": "llm", "model": "gpt-4o-mini", "ts_wall": 11, "n_tools": 3, "usage": {"prompt_tokens": 950},
                "oracle": {"input_ids": ["main-a1-0"]}},
               {"type": "llm", "model": "openai/gpt-4o-mini", "ts_wall": 12, "oracle": {"input_ids": ["main-a0-0"]}},
               {"type": "llm", "model": "gpt-4o-mini", "ts_wall": "12.5", "oracle": {"input_ids": []}},
               {"type": "llm", "model": "openai/gpt-4o-mini", "ts_wall": 12.6, "oracle": {"input_ids": []}},
               {"type": "llm", "model": "gpt-4o-mini", "ts_wall": 50, "oracle": {"input_ids": []}},
               {"type": "control", "ts_wall": 1}]
        a = M.agent_summary(run, llm, "gpt-4o-mini")
        self.assertEqual((a["requests"], a["ok"], a["llm_calls"]), (2, 1, 2))
        self.assertEqual(a["llm_calls_per_request"]["p50"], 2)
        self.assertEqual(a["n_tools_in_catalog"], [3])
        self.assertEqual((a["service_calls"], a["service_calls_by_request"]), (1, {0: 1}))   # сжатие истории хода
        self.assertEqual(M.llm_outside_requests(run, llm, "gpt-4o-mini"), 1)                 # 50 с — вне реплик
        self.assertEqual([(lo, round(hi, 2), n) for lo, hi, n in M.request_windows(run)],
                         [(9.9, 14.95, 0), (30.0, 35.1, 1)])

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

    def test_vm_pause_is_found_by_wall_clock_jump(self):
        ev = [{"t": 100.0, "m": T0 + 10}, {"t": 110.0, "m": T0 + 20}, {"t": 1030.0, "m": T0 + 40},
              {"t": 1033.0, "m": T0 + 43}]
        self.assertEqual(M.pauses(ev, T0), [{"after_s": 20.0, "before_s": 40.0, "wall_jump_s": 900.0}])
        self.assertEqual(M.pauses(ev[:2], T0), [])

    def test_safety_stops(self):
        segs = [seg(0, 10, 15), seg(1, 30, 35)]
        per = {0: {"reaction": "interrupted"}, 1: {"reaction": "none"}}
        safety = [{"m": T0 + 16, "state": "blocked", "reasons": ["lidar_nodata: нет данных…"]},
                  {"m": T0 + 60, "state": "blocked", "reasons": ["obstacle: …"]}]
        got = M.safety_stops(per, segs, safety, T0)
        self.assertEqual(got, {"n": 1, "by_seg": {"0": {"reaction": "interrupted", "reasons": ["lidar_nodata"]}}})


class TravelTest(unittest.TestCase):
    def test_distance_moving_time_and_safety_shares(self):
        truth = [{"m": 0.0, "x": 0.0, "y": 0.0, "v": 0.1, "w": 0.0}, {"m": 1.0, "x": 0.1, "y": 0.0, "v": 0.1, "w": 0.0},
                 {"m": 2.0, "x": 0.1, "y": 0.0, "v": 0.0, "w": 0.0},
                 {"m": 9.0, "x": 5.0, "y": 0.0, "v": 0.0, "w": 0.0},  # перерыв 7 с — пауза ВМ, не в счёт
                 {"m": 10.0, "x": 5.0, "y": 0.0, "v": 0.0, "w": 0.0},
                 {"m": 10.2, "x": 0.0, "y": 0.0, "v": 0.0, "w": 0.0},  # перенос оператором — не пробег
                 {"m": 10.4, "x": 0.1, "y": 0.0, "v": 0.5, "w": 0.0}]
        safety = [{"m": 0.0, "state": "slow"}, {"m": 1.0, "state": "blocked"}, {"m": 2.0, "state": "blocked"},
                  {"m": 3.0, "state": "blocked"}, {"m": 4.0, "state": "ok"}, {"m": 5.0, "state": "ok"}]
        t = M.travel(truth, safety)
        self.assertEqual((t["distance_m"], t["moving_s"], t["teleports"]), (0.2, 2.0, {"n": 1, "m": 5.0}))
        self.assertEqual(t["safety_share"], {"ok": 0.2, "slow": 0.2, "blocked": 0.6, "estop": 0.0})

    def test_move_outcomes_only_motion_or_reported_refusal_succeed(self):
        truth = [{"m": 100.0 + k * 0.5, "x": 0.02 * k if k < 4 else 0.06, "y": 0.0} for k in range(40)]
        unconfirmed = '{\n  "ok": false,\n  "status": "unconfirmed",\n  "reasons": []'
        run = {"agent": [{"n": 1, "request": "move", "m": 100.0, "latency_s": 1.5, "ok": True, "refused": []},
                         {"n": 2, "request": "move", "m": 105.0, "latency_s": 0.2, "ok": True, "refused": ["blocked"]},
                         {"n": 3, "request": "move", "m": 108.0, "latency_s": 1.5, "ok": True, "refused": []},
                         {"n": 4, "request": "move", "m": 111.0, "latency_s": 1.5, "ok": False,
                          "tools": [{"ok": False, "refused": None, "content": unconfirmed}]},
                         {"n": 5, "request": "move", "m": 114.0, "latency_s": 60.0, "ok": False, "tools": []},
                         {"n": 6, "request": "status", "m": 100.0, "latency_s": 1.0, "ok": True}]}
        mv = M.move_requests(run, truth)
        self.assertEqual([r["outcome"] for r in mv["rows"]], ["moved", "refused", "false_ok", "unconfirmed",
                                                             "no_answer"])
        self.assertEqual((mv["n"], mv["success"], mv["false_ok"]), (5, 2, 1))


class BreakdownTest(unittest.TestCase):
    def test_reaction_breakdown_by_recognized_class_and_safety(self):
        segs = [seg(i, 20 * i, 20 * i + 5) for i in range(5)] + [seg(5, 200, 205, kind="negative")]
        per = {0: {"cls": 1, "rec": {"cls": 1}, "reaction": "correct"},
               1: {"cls": 1, "rec": {"cls": 1}, "reaction": "interrupted"},
               2: {"cls": 2, "rec": {"cls": 2}, "reaction": "none"},
               3: {"cls": 2, "rec": {"cls": 0}, "reaction": "wrong"},
               4: {"cls": 3, "rec": {"cls": -1}, "reaction": "none"}}
        stopped = {"by_seg": {"1": {"reasons": ["lidar_nodata"]}, "2": {"reasons": ["obstacle", "lidar_nodata"]}}}
        got = M.reaction_breakdown(per, segs, stopped)
        self.assertEqual(got["by_class"], {"ok": {"correct": 1, "interrupted": 1, "none": 1}, "wrong": {"wrong": 1},
                                           "none": {"none": 1}})
        self.assertEqual(got["safety"], {"interrupted": 1, "none": 1, "reasons": {"lidar_nodata": 2, "obstacle": 1}})
        # демо A0 класс не публикует — судить можно только по манёвру
        a0 = M.reaction_breakdown({0: {"cls": 1, "reaction": "correct"}}, segs[:1], {})
        self.assertEqual(a0["by_class"], {"unknown": {"correct": 1}})


class DeadEndTest(unittest.TestCase):
    def test_long_blocked_standstill_with_gestures_is_a_dead_end(self):
        # у ножки стула: вперёд нельзя (obstacle), поворот заденет (touch) — жесты 1 и 2 не выводят робота
        segs = [seg(0, 5, 10), seg(1, 40, 45), seg(2, 80, 85), seg(3, 150, 155)]
        safety = [{"m": T0 + t, "state": "blocked", "reasons": ["obstacle: …", "touch: …"]} for t in range(30, 110)]
        safety += [{"m": T0 + 110, "state": "slow"}, {"m": T0 + 120, "state": "blocked"},
                   {"m": T0 + 200, "state": "slow"}]
        truth = [{"m": T0 + t, "v": 0.0, "w": 0.0} for t in range(0, 220)]
        truth[170]["w"] = 0.5                     # второй blocked: робот поворачивался — не тупик
        got = M.dead_ends(safety, truth, segs, T0)
        self.assertEqual((got["n"], got["total_s"], got["gestures"]), (1, 80.0, 2))
        self.assertEqual(got["spans"][0], {"at_s": 30.0, "s": 80.0, "reasons": ["obstacle", "touch"],
                                           "gestures": [1, 2]})
        self.assertEqual(M.dead_ends(safety, truth, segs, T0, min_s=100.0)["n"], 0)

    def test_touch_episodes_and_escapes(self):
        # повторная приёмка 18.10: тупик (touch и вперёд нельзя или только подползти — approach) — вышел ли отъездом
        # исполнителя (escape start в эпизоде); blocked без touch — не тупик (поворот можно); slow с touch, но впереди
        # свободно (lidar_blind) — не тупик; второй тупик без отъезда (reverse_limit)
        safety = ([{"m": T0 + t, "state": "blocked", "reasons": ["obstacle: …", "touch: …"]} for t in (10, 11)]
                  + [{"m": T0 + t, "state": "slow", "reasons": ["approach: …", "touch: …"]} for t in (12, 13)]
                  + [{"m": T0 + 14, "state": "blocked", "reasons": ["obstacle: …"]}]
                  + [{"m": T0 + t, "state": "slow", "reasons": ["lidar_blind: …", "touch: …"]} for t in (20, 21)]
                  + [{"m": T0 + 22, "state": "slow", "reasons": ["lidar_blind: …"]}]
                  + [{"m": T0 + t, "state": "blocked", "reasons": ["obstacle: …", "touch: …", "reverse_limit: …"]}
                     for t in (50, 51)] + [{"m": T0 + 52, "state": "slow", "reasons": []}])
        executor = [{"m": T0 + 13.1, "type": "escape", "status": "start"},
                    {"m": T0 + 15.1, "type": "task_end", "task": "escape", "status": "done"},
                    {"m": T0 + 20.0, "type": "task_end", "task": "gesture", "status": "blocked"}]
        got = M.escapes(executor, safety, T0)
        self.assertEqual((got["episodes"], got["escaped"], got["starts"], got["max_s"]), (2, 1, 1, 4.0))
        self.assertEqual(got["outcomes"], {"done": 1})
        self.assertEqual(got["spans"][1], {"at_s": 50.0, "s": 2.0, "escaped": False,
                                           "reasons": ["obstacle", "reverse_limit", "touch"]})
        self.assertEqual(compare.escape({"system": "a1", "escapes": got}),
                         "2, вышел отъездом 1; дольше всего 4 с; отъездов 1 (done 1)")
        self.assertEqual(compare.escape({"system": "a1", "escapes": M.escapes([], [], T0)}), "0")
        self.assertEqual(compare.escape({"system": "a0"}), "нет safety_gate")


class OutageTest(unittest.TestCase):
    def test_k6_episode_in_twin_world_outage(self):
        # смерть базы на ходу: истины нет 8 с, трекер наблюдателя держит «едет» — это не езда без команды
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "anchor.json"), "w") as f:
                json.dump({"t_wall": 0.0, "t_mono": T0, "timeline_sha256": "abc", "pid": 1, "boot_id": ""}, f)
            ev = [{"header": {"m": T0 - 5, "timeline_sha256": "abc"}}]
            for a, b, x in ((10, 11, 0.15), (11.2, 30, 0.0), (60, 61, 0.15), (61.1, 90, 0.0)):
                ev.append({"ev": "cmd", "topic": "/cmd_vel", "m0": T0 + a, "m1": T0 + b, "x": x, "z": 0.0})
            for m, moving in ((10.1, True), (20, False), (60.1, True), (64, False)):
                ev.append({"ev": "motion", "src": "gt", "m": T0 + m, "moving": moving})
            with open(os.path.join(d, "events.jsonl"), "w") as f:
                f.write("\n".join(json.dumps(e) for e in ev) + "\n")
            truth = [{"m": T0 + k * 0.2} for k in range(500) if not 11 < k * 0.2 < 19]
            got = M.k6_outage(d, truth)
            self.assertEqual((got["n"], got["episodes"]), (1, [{"a_s": 11.5, "b_s": 20.0, "cause": "ignored_zero"}]))
            self.assertIsNone(M.k6_outage(os.path.join(d, "nope"), truth))


def k6_doc(zero, brake, scan):
    """Серия verify/k6.py: замеры mcp (ноль и начало торможения в /cmd_vel) и scan (ноль после скана)."""
    return {"mcp_stop": {"runs": [{"req_zero_s": 0.002, "brake_s": b, "zero_s": z} for z, b in zip(zero, brake)]},
            "scan_stop": {"runs": [{"zero_after_scan_s": v, "contacts": []} for v in scan]}}


class SummarizeTest(unittest.TestCase):
    def run_dir(self, d):
        segs = [seg(0, 10, 15), seg(1, 30, 35), seg(2, 50, 55, kind="negative")]
        with open(os.path.join(d, "truth_schedule.jsonl"), "w") as f:
            f.write(json.dumps({"header": {}}) + "\n" + "\n".join(json.dumps(s) for s in segs) + "\n")
        with open(os.path.join(d, "probe.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(r) for r in probe_rows([(0, 120)])) + "\n")
        run = {"system": "a1", "label": "main", "plan": {"profile": "main"}, "t_start": 0, "t_end": 120,
               "anchor": {"t_mono": T0}, "agent": [], "restarts": {"perception": 1},
               "events": [{"kind": "start", "t": 0.0, "m": T0 - 5},
                          {"kind": "fault", "fault": "kill_perception", "t": 21.0, "m": T0 + 16, "ttr_s": 3.0},
                          {"kind": "end", "t": 115.0, "m": T0 + 110}]}
        with open(os.path.join(d, "run.json"), "w") as f:
            json.dump(run, f)
        with open(os.path.join(d, "summary.json"), "w") as f:
            json.dump({"latency_s": {"cmd": {"n": 3, "p50": 0.6, "p95": 0.7}}, "reactions": {"correct": 2},
                       "unsafe_no_fresh_cmd": {"n": 0, "total_s": 0}, "k7": {"total": 0, "per_hour": 0.0}}, f)
        with open(os.path.join(d, "gestures.jsonl"), "w") as f:
            for p in ({"seg": 0, "kind": "gesture", "lat": {"cmd": 0.6, "motion": 0.7}, "rtf": 0.99},
                      {"seg": 1, "kind": "gesture", "lat": {"cmd": 0.7}, "rtf": 0.98},
                      {"seg": 3, "kind": "gesture", "lat": {"cmd": -0.4}, "rtf": 0.99, "early": True},
                      {"seg": 4, "kind": "gesture", "lat": {"cmd": 0.2}, "rtf": 0.5}):
                f.write(json.dumps(p) + "\n")

    def test_summarize_and_observer_redo(self):
        with tempfile.TemporaryDirectory() as d:
            self.run_dir(d)
            m = M.summarize(d)
            # приёмка: K1 — без ранних жестов (реакция раньше t0), они — отдельной строкой; RTF < 0,95 — вне счёта
            self.assertEqual((m["K1_t0_to_cmd_s"]["n"], m["K1_t0_to_cmd_s"]["p50"]), (2, 0.65))
            self.assertEqual((m["early_t0_to_cmd_s"]["n"], m["early_t0_to_cmd_s"]["p50"]), (1, -0.4))
            self.assertEqual(m["segments"], {"gestures": 2, "negatives": 1})
            self.assertEqual((m["duration_s"], m["wall_duration_s"], m["vm_pauses"]), (115.0, 120, []))
            self.assertEqual((m["K3"]["deaf_gestures_n"], m["K5_faults"][0]["visible"]), (0, False))
            self.assertIsNone(m["K6_world_outage"])                      # записи наблюдателя нет
            self.assertIn("error", M.summarize(os.path.join(d, "nope")))
            # разбор наблюдателя заново (--observer): реакции и задержки — из него, остальное — из прогона
            redo = os.path.join(d, "observer")
            os.makedirs(redo)
            with open(os.path.join(redo, "summary.json"), "w") as f:
                json.dump({"latency_s": {"cmd": {"n": 3, "p50": 0.6, "p95": 0.7}}, "reactions": {"correct": 3}}, f)
            with open(os.path.join(redo, "gestures.jsonl"), "w") as f:
                f.write(json.dumps({"seg": 0, "kind": "gesture", "lat": {"cmd": 0.5}, "rtf": 0.99}) + "\n")
            again = M.summarize(d, observer_dir=redo)
            self.assertEqual((again["reactions"], again["K1_t0_to_cmd_s"]["n"]), ({"correct": 3}, 1))
            self.assertEqual(again["duration_s"], m["duration_s"])

    def test_compare_columns_verdicts_and_k6(self):
        with tempfile.TemporaryDirectory() as d:
            self.run_dir(d)
            m = M.summarize(d)
            a0 = dict(m, system="a0", label="main_a0",
                      K5_faults=[{"fault": "kill_executor", "at_s": 50.0, "seg": 2, "skipped": "нет компонента"}])
            old = compare.column("A1 16.10", m, also=[dict(m, label="long", touches={"episodes": 1})],
                                 start={"ok": {"cycles": 100, "passed": 99, "failed": [17],
                                               "t_doctor_s": {"max": 21.5}},
                                        "no_camera": {"cycles": 1, "passed": 1}},
                                 budget={"oom_kill": 0})
            executor = {"fault": "kill_executor", "at_s": 50.0, "seg": 2, "ttr_s": 3.1, "path": "жест",
                        "visible": False}
            new = compare.column("A1 18.10", dict(m, label="a1v2", K5_faults=m["K5_faults"] + [executor]),
                                 k6=[("s1", k6_doc([0.5] * 10, [0.03] * 10, [0.001] * 9 + [0.4])),
                                     ("s2", k6_doc([0.5] * 10, [0.03] * 10, [0.001] * 10))],
                                 start={"ok": {"cycles": 21, "passed": 21, "t_doctor_s": {"max": 19.0}}})
            text = compare.build(a0, [old, new])
            self.assertIn("| K1 t0 → первая ненулевая /cmd_vel, p50 / p95, с (без ранних) | 0,65 / 0,69 (n=2) | "
                          "0,65 / 0,69 (n=2) | 0,65 / 0,69 (n=2) |", text)
            self.assertIn("| …ранние (реакция раньше t0), p50, с | -0,40 (n=1)", text)
            self.assertIn("| safety_gate: доля времени ok / slow / blocked / estop | нет safety_gate |", text)
            self.assertIn("| K6 «едет без свежей команды > 0,5 с» | эпизодов 0, 0 с (молчание писателя 0, ноль не "
                          "исполнен 0) |", text)
            self.assertIn("| Тупики защиты: blocked без движения ≥ 60 с при жестах | нет safety_gate | 0 | 0 | 0 |",
                          text)
            stuck = dict(m, dead_ends={"n": 1, "total_s": 221.2, "gestures": 9,
                                       "spans": [{"at_s": 1079.0, "reasons": ["obstacle", "touch"]}]})
            self.assertEqual(compare.dead_end(stuck), "1: 221 с, жестов без реакции 9 (с 1079 с: obstacle, touch)")
            # отказ, которого у A0 в расписании не было, — «не прогонялся»; недоступность perception — заново по probe:
            # детектор снова вызывается через 2 с (две строки probe нового процесса), а не TTR runner.py 3,0 с
            self.assertIn("| kill_perception (16 с) | жест | не прогонялся | 2,0 с — нет | 2,0 с — нет |", text)
            self.assertIn("| kill_executor (50 с) | жест | нет компонента | не прогонялся | 3,1 с — нет |", text)
            rows = "\n".join(compare.verdicts(a0, [old, new]))
            self.assertIn("| Критерий | A0 | A1 16.10 | A1 18.10 | Итог: A1 18.10 |", rows)
            # приёмка: «0 касаний» — по всем прогонам столбца; один инцидент в любом — не выполнено; у нуля — граница
            # в час по Пуассону (−ln 0,05 / 0,032 ч)
            self.assertIn("все прогоны столбца | 0 (≤ 93,8 в час за 0,03 ч) | 1 в 2 прогонах (main 0, long 1) | "
                          "0 (≤ 93,8 в час за 0,03 ч) | выполнено |", rows)
            # 21 холодный старт из 21 — меньше 100: не «выполнено» и не «не выполнено», а мало данных
            self.assertIn("холодных стартов — рестарт neurobot.target, не перезагрузка ОС (K9) | — | 99 из 100 | "
                          "21 из 21 (≥ 83,9 %, 95 % CP) | мало данных |", rows)
            self.assertIn("«ОК» doctor ≤ T = 26 с (стенд; на роботе T — по HIL) | — | max 21,5 с | max 19,0 с | "
                          "выполнено |", rows)
            self.assertIn("ноль в /cmd_vel ≤ 100 мс (p95, каждая серия), K6 | — | — | s1: p95 500 мс (n=10), "
                          "превышений 10 — доля ≤ 100,0 %; s2: p95 500 мс (n=10), превышений 10 — доля ≤ 100,0 % | "
                          "**не выполнено** |", rows)
            # 10 из 10 в пределе — p95 не доказан: доля превышений до 25,9 % (нужно ≥ 59 подряд, PROTOCOL §3)
            self.assertIn("до решения 1а 19.10 стоп агента был плавным | — | — | s1: p95 30 мс (n=10), превышений 0 — "
                          "доля ≤ 25,9 %; s2: p95 30 мс (n=10), превышений 0 — доля ≤ 25,9 % | "
                          "справочно: мало данных |", rows)
            # скан: выброс в одной серии — p95 вне предела, критерий не выполнен
            self.assertIn("после скана (p95, каждая серия), K6 | — | — | s1: p95 220 мс (n=10), превышений 1 — доля ≤ "
                          "39,4 %; s2: p95 1,0 мс (n=10), превышений 0 — доля ≤ 25,9 % | **не выполнено** |", rows)
            self.assertIn("| 0 «глухих» состояний: путь жеста сам поднялся после каждого отказа | — | 0 (≤ 95,0 % из 1 "
                          "отказов пути жеста) | 0 (≤ 77,6 % из 2 отказов пути жеста) | выполнено |", rows)
            inf = dict(m, label="lab_inf", world="lab", no_return="inf", K6_world_outage={"n": 1, "total_s": 7.5},
                       K6_unsafe_no_fresh_cmd={"n": 1},
                       stopped_by_safety={"n": 2, "by_seg": {"3": {"reasons": ["lidar_nodata"]},
                                                             "5": {"reasons": ["obstacle"]}}})
            runs = "\n".join(compare.runs_table([("a1v2", m), ("lab_inf", inf)]))
            self.assertIn("| lab_inf | lab, +inf | 2 из 2 | 2 (1) |", runs)
            self.assertIn("| a1v2 | hall, NaN | 2 из 2 | 0 (0) |", runs)
            self.assertIn("| 0; 0 с |", runs)
            self.assertIn("| 0 / 0 (до 20.10: 0) | 0 (+1 в перерыв мира) |", runs)
            st = compare.k6_stats(k6_doc([0.5, 0.05], [0.03, None], [0.001, 0.4]))
            self.assertEqual((st["mcp"]["brake_ok"], st["mcp"]["zero_ok"], st["scan"]["ok"]), (1, 1, 1))
            k6 = "\n".join(compare.k6_tables([old, new], [("до правки", "s0", k6_doc([0.5], [0.2], []))]))
            self.assertIn("| до правки: s0 | 1 | 2,0 мс / 2,0 мс | 200 мс / 200 мс; 0 из 1 |", k6)
            self.assertIn("| A1 18.10: s1 | 10 | 2,0 мс / 2,0 мс | 30 мс / 30 мс; 10 из 10 | 500 мс / 500 мс; "
                          "0 из 10 | 0 |", k6)
            self.assertIn("| A1 18.10: s1 | 10 | 1,0 мс / 220 мс / 400 мс; 9 из 10 | 0 |", k6)

    def test_main_labels_start_parts_and_notes(self):
        with tempfile.TemporaryDirectory() as d:
            self.run_dir(d)
            parts = []
            for k, plan in enumerate((["ok"] * 2, ["ok", "no_camera", "ok"])):
                part = os.path.join(d, "start_%d" % k)
                os.makedirs(part)
                with open(os.path.join(part, "cycles.jsonl"), "w") as f:
                    for n, name in enumerate(plan, 1):
                        f.write(json.dumps({"cycle": n, "scenario": name, "passed": name == "ok" or k == 1,
                                            "why": "", "t_doctor_s": 10.0 + n}) + "\n")
                parts.append(part)
            merged = compare.start_summary(parts)
            self.assertEqual((merged["ok"]["cycles"], merged["ok"]["passed"]), (4, 4))
            self.assertEqual(compare.start_counts(merged), ((4, 4), (1, 1)))
            self.assertIsNone(compare.start_summary([]))
            self.assertEqual(compare.start_counts({"ok": {"cycles": 25, "passed": 25}}), ((25, 25), None))
            with open(os.path.join(d, "notes.md"), "w") as f:
                f.write("## Разбор\n")
            out = os.path.join(d, "compare.md")
            rc = compare.main(["--a0", d, "--a1", "A1=" + d, "--start", "A1=" + parts[0], "--start", "A1=" + parts[1],
                               "--long", "A1=" + d, "--runs", "повтор=" + d, "--notes", os.path.join(d, "notes.md"),
                               "--out", out])
            text = open(out, encoding="utf-8").read()
            self.assertEqual(rc, 0)
            self.assertTrue(text.endswith("\n## Разбор\n"))
            self.assertIn("## K9: start_test, A1", text)
            self.assertIn("## Длительный прогон, A1 (`main`)", text)
            self.assertIn("| повтор | hall, NaN |", text)
            self.assertEqual(compare.main(["--a0", d, "--a1", "A1=" + d, "--also", "A2=" + d, "--out", out]), 2)
            self.assertEqual(compare.main(["--a0", d, "--a1", d, "--out", out]), 2)


if __name__ == "__main__":
    unittest.main()
