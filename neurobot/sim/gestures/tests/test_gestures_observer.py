"""Наблюдатель без ROS: запись (record.py), геометрия стен (walls.py) и разбор (analyze.py) на записанных
сообщениях — сценарий с верной, неверной, оборванной реакцией, без реакции, поздней, ложным действием,
движением по «не распознано», самопроизвольным действием, «едет без свежей команды» (молчание писателя,
торможение после нуля, база игнорирует ноль, база едет до конца записи), классом без действия, глухотой (health,
probe), касанием; сессии записи и якорь от одного прогона; рестарт наблюдателя (несколько сессий записи);
несовпадение типа /cmd_vel."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from neurobot.sim.gestures import analyze as an  # noqa: E402
from neurobot.sim.gestures.clock import Anchor, mono  # noqa: E402
from neurobot.sim.gestures.probe import Probe  # noqa: E402
from neurobot.sim.gestures.record import CmdRuns, MotionTracker, PoseVelocity, Recorder, RtfMeter  # noqa: E402
from neurobot.sim.gestures.timeline import write_jsonl  # noqa: E402
from neurobot.sim.gestures.walls import (RADIUS_NEUROCHIP_BASE, Circle, Rect, footprint, load_walls,  # noqa: E402
                                         nearest_wall, point_poly_distance, poly_distance, walls_from_sdf)

OFFSET = 500.0                      # t − m у всех событий сценария
ANCHOR = Anchor(t_wall=1000.0, t_mono=500.0, timeline_sha256="x" * 64, pid=1)


def ev(kind: str, m: float, **kw) -> dict:
    return dict(kw, ev=kind, t=m + OFFSET, m=m)


def run(m0: float, m1: float, x: float = 0.0, z: float = 0.0, topic: str = "/cmd_vel") -> dict:
    return {"ev": "cmd", "topic": topic, "t0": m0 + OFFSET, "m0": m0, "t1": m1 + OFFSET, "m1": m1,
            "n": int(round((m1 - m0) * 10)) + 1, "x": x, "z": z}


SIGN = {"F": (0.5, 0.0), "B": (-0.5, 0.0), "L": (0.0, 1.0), "R": (0.0, -1.0)}


def maneuver(m: float, steps: list) -> list:
    """Серии команд как у DriveController демо: 10 Гц, на стыке шагов — нулевая серия."""
    out = []
    for letter, dur in steps:
        out.append(run(m, m + dur - 0.1, *SIGN[letter]))
        m += dur
    out.append(run(m, m + 0.5))
    return out


def seg(i: int, kind: str, cls: int, t_start: float, t0: float, side=None, name=None) -> dict:
    return {"seg": i, "kind": kind, "cls": cls, "name": name or str(cls), "side": side, "shape": None,
            "t_start": t_start, "t0": t0, "t_leave": t0}


def health(a: int, b: int, fps: float = 10) -> list:
    """Здоровье perception 1 Гц, моменты a…b включительно."""
    return [ev("health", float(m), msg={"fps": fps, "state": "ok"}) for m in range(a, b + 1)]


def probe_lines(ms: list) -> list:
    """Строки журнала probe в моменты ms, det_calls растёт."""
    return [{"t": m + OFFSET, "m": m, "pid": 7, "frames": i, "det_calls": i, "timeouts": 0, "marker_miss": 0,
             "faults": []} for i, m in enumerate(ms)]


def scenario():
    segs = [seg(0, "gesture", 1, 10, 11.5), seg(1, "gesture", 2, 30, 31, side="left"), seg(2, "gesture", 3, 50, 51),
            seg(3, "gesture", 0, 70, 71), seg(4, "gesture", 1, 90, 91),
            seg(5, "negative", -1, 110, 116, name="hand_still"), seg(6, "gesture", 1, 130, 131),
            seg(7, "gesture", 1, 150, 151), seg(8, "gesture", 1, 170, 171)]
    e = []
    e.append(ev("gesture", 513.0, msg={"boot": "b", "seq": 1, "cls": 1, "conf": 0.9, "t_rec": 512.95 + OFFSET}))
    e.append(ev("gesture", 513.01, msg={"boot": "b", "seq": 1, "cls": 1, "conf": 0.9, "t_rec": 512.95 + OFFSET}))
    e.append(run(512.98, 515.88, 0.5, 0.0, topic="/cmd_vel_gesture"))
    e += maneuver(513.05, [("F", 3.0)])
    e += [ev("motion", 513.25, src="odom", moving=True), ev("motion", 516.3, src="odom", moving=False),
          ev("motion", 516.2, src="gt", moving=True), ev("motion", 516.5, src="gt", moving=False)]
    e += maneuver(532.6, [("L", 3.0), ("F", 2.0), ("R", 3.0)])
    e += maneuver(552.6, [("R", 3.0), ("F", 2.0), ("L", 3.0)])
    e.append(run(572.6, 574.5, 0.0, -1.0))
    e += maneuver(617.6, [("F", 3.0)])
    e.append(ev("gesture", 632.5, msg={"boot": "b", "seq": 2, "cls": -1, "conf": 0.1}))
    e += maneuver(632.6, [("F", 3.0)])
    e += maneuver(654.5, [("F", 3.0)])
    e += maneuver(671.6, [("F", 3.0)])
    e.append(run(900.0, 900.9, 0.5, 0.0))
    e += [ev("motion", 905.0, src="odom", moving=True), ev("motion", 909.0, src="odom", moving=False)]
    for m in range(510, 911, 10):
        e.append(ev("rtf", float(m), src="odom", rtf=0.5 if m == 680 else 1.0, wall_s=10.0, sim_s=10.0))
    for m in range(500, 701):
        if 650 <= m < 657:
            continue
        e.append(ev("health", float(m), msg={"fps": 0 if 600 <= m <= 605 else 10, "frame_age_s": 0.1,
                                             "recoveries": 0, "state": "ok"}))
    e.append(ev("health", 640.5, raw="{oops", error="bad json"))
    e += [ev("wall", 700.0, touch=True, d=0.0, wall="lab_wall"),
          ev("wall", 702.0, touch=False, d=0.05, wall="lab_wall"),
          ev("pose", 701.0, x=1.0, y=0.0, yaw=0.0, d=0.0)]
    e.append(ev("stop", 920.0))
    probe = []
    calls = 0
    for m in range(500, 561):
        if not 520 <= m < 524:
            calls += 10
        probe.append({"t": m + OFFSET, "m": float(m), "pid": 7, "frames": calls, "det_calls": calls, "timeouts": 0,
                      "marker_miss": 0, "faults": []})
    probe.append({"t": 520 + OFFSET, "m": 520.0, "pid": 7, "ev": "fault_start", "kind": "delay"})
    return segs, e, probe


class AnalyzeScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        segs, events, probe = scenario()
        cls.per, cls.s = an.analyze([events], segs, ANCHOR, probe)
        cls.by = {p["seg"]: p for p in cls.per}

    def test_reactions(self):
        self.assertEqual(self.s["reactions"], {"correct": 5, "wrong": 1, "interrupted": 1, "none": 1, "unobserved": 0})
        self.assertEqual({k: (p.get("reaction"), p.get("maneuver")) for k, p in self.by.items() if k != 5},
                         {0: ("correct", "come"), 1: ("correct", "go_left"), 2: ("wrong", "go_right"),
                          3: ("interrupted", "turn180"), 4: ("none", None), 6: ("correct", "come"),
                          7: ("correct", "come"), 8: ("correct", "come")})
        self.assertEqual(self.by[2]["expected"], "think")
        self.assertEqual((self.s["late"], self.s["visible_failures"], self.s["early"]), (1, 4, 0))
        self.assertTrue(self.by[7]["late"])

    def test_timeline_marks(self):
        lat = self.by[0]["lat"]
        for k, v in {"rec": 1.45, "pub": 1.5, "cmd": 1.55, "motion": 1.75, "cmd_to_motion": 0.2}.items():
            self.assertAlmostEqual(lat[k], v, places=6, msg=k)
        self.assertAlmostEqual(self.by[0]["lat_extra"]["/cmd_vel_gesture"], 1.48, places=6)
        self.assertEqual(self.by[0]["rec"]["cls"], 1)
        self.assertEqual(self.by[0]["n_msgs"], 1)                  # повтор boot+seq отброшен
        self.assertEqual(self.by[0]["t0"], 1011.5)
        self.assertNotIn("motion", self.by[1]["lat"])

    def test_counters(self):
        s = self.s
        self.assertEqual(s["false_actions"], {"total": 1, "by_kind": {"hand_still": 1}})
        self.assertEqual((s["moved_on_unknown"], s["spontaneous_actions"], s["extra_actions"]), (1, 1, 0))
        self.assertEqual(s["recognition"]["confusion"], {"0": {"none": 1}, "1": {"1": 1, "none": 3, "-1": 1},
                                                         "2": {"none": 1}, "3": {"none": 1}})
        self.assertEqual(s["segments"], {"gestures": 8, "negatives": 1})
        k7 = s["k7"]
        self.assertEqual((k7["moved_on_unknown"], k7["moved_when_ignored"], k7["false_actions"], k7["total"]),
                         (1, 0, 1, 3))                    # «не распознано», hand_still, самопроизвольное
        self.assertAlmostEqual(k7["per_hour"], 3 / (420 / 3600), places=2)
        self.assertEqual(s["cmd"], {"msgs": {"/cmd_vel": 382, "/cmd_vel_gesture": 30}, "type_mismatch": [],
                                    "moving_without_cmd": False})

    def test_latency_excludes_low_rtf(self):
        cmd = self.s["latency_s"]["cmd"]
        self.assertEqual((cmd["n"], self.s["latency_s"]["excluded_rtf"]), (6, 1))
        self.assertAlmostEqual(cmd["max"], 3.5)
        self.assertEqual(self.by[8]["rtf"], 0.5)
        self.assertEqual(self.s["rtf"]["min"], 0.5)

    def test_unsafe_episodes(self):
        u = self.s["unsafe_no_fresh_cmd"]
        self.assertEqual((u["n"], u["total_s"], u["silent"]["n"], u["braking_after_zero"]["n"]), (1, 4.0, 1, 1))
        self.assertEqual(u["stop_after_last_cmd_s"]["max"], 8.1)          # последняя ненулевая 900,9 → 909
        self.assertEqual(u["not_stopped_by_end"]["n"], 0)                 # база встала до конца записи (920)
        self.assertEqual((u["by_src"]["odom"]["silent"]["n"], u["by_src"]["gt"]["braking_after_zero"]["n"]), (1, 1))

    def test_deaf_and_wall(self):
        d = self.s["deaf"]
        self.assertEqual(d["health"], {"n": 1, "total_s": 6.0, "max_s": 6.0})
        self.assertEqual(d["health_missing"]["n"], 2)                # разрыв 649–657 и молчание после 700
        self.assertEqual((d["health_bad_msgs"], d["probe"]["n"], d["probe_standing"]["n"]), (1, 1, 1))
        self.assertAlmostEqual(d["probe"]["total_s"], 4.0)            # строки 519…523 с одним det_calls
        self.assertEqual(self.s["probe"]["fault_starts"], {"delay": 1})
        self.assertEqual(self.s["wall"], {"n": 1, "total_s": 2.0, "max_s": 2.0, "min_d": 0.0})


class SeveralActionsTest(unittest.TestCase):
    def test_two_actions_on_one_gesture(self):
        """Два действия на один жест: реакция, её метки и решающее сообщение — по первому (верный come), второе
        (turn180 по следующему сообщению perception) — лишнее действие, а не ложное и не самопроизвольное."""
        events = health(598, 630) + [ev("gesture", 602.3, msg={"boot": "b", "seq": 1, "cls": 1}),
                                     *maneuver(602.5, [("F", 3.0)]),
                                     ev("gesture", 606.3, msg={"boot": "b", "seq": 2, "cls": 0}),
                                     *maneuver(606.5, [("R", 7.0)]), ev("stop", 630.0)]
        per, s = an.analyze([events], [seg(0, "gesture", 1, 100, 102)], ANCHOR)
        p = per[0]
        self.assertEqual((p["reaction"], p["maneuver"], p["rec"]["cls"], p["n_msgs"], p["actions"], p["lat"]),
                         ("correct", "come", 1, 2, 2, {"pub": 0.3, "cmd": 0.5}))
        self.assertEqual((s["extra_actions"], s["visible_failures"], s["spontaneous_actions"], s["k7"]["total"]),
                         (1, 0, 0, 0))

    def test_extra_topic_window_ends_at_next_segment(self):
        """Сегменты ближе окна реакции (пауза короче --match-window-s): окно сегмента кончается началом следующего, и
        заявка исполнителя на следующий жест — метка lat_extra этого жеста, а не предыдущего сегмента."""
        req = "/neurobot/cmd_vel_req"
        segs = [seg(0, "negative", -1, 100, 102, name="hand_still"), seg(1, "gesture", 1, 104, 106)]
        events = health(598, 630) + [ev("gesture", 606.3, msg={"boot": "b", "seq": 1, "cls": 1}),
                                     run(606.55, 609.5, 0.5, topic=req), *maneuver(606.6, [("F", 3.0)]),
                                     ev("stop", 630.0)]
        per, s = an.analyze([events], segs, ANCHOR)
        self.assertEqual([(p["actions"], p.get("lat_extra")) for p in per], [(0, None), (1, {req: 0.55})])
        self.assertEqual((per[1]["reaction"], s["false_actions"]["total"]), ("correct", 0))


class NoFreshCmdTest(unittest.TestCase):
    """K6: интервал движения без свежей ненулевой команды делится по явному нулю писателя."""
    cfg = an.AnalyzeConfig()
    cmd = run(3.0, 4.5, 0.5)                              # последняя ненулевая — 4,5

    def classify(self, stop_at: float, zero_at=None):
        runs = [self.cmd] + ([run(zero_at, 12.0)] if zero_at is not None else [])
        return [(u["cause"], round(u["a"], 3), round(u["b"], 3))
                for u in an.no_fresh_cmd([(3.1, stop_at)], runs, self.cfg)]

    def test_braking_after_guard_zero_is_not_a_violation(self):
        self.assertEqual(self.classify(5.4, zero_at=4.9), [("braking_after_zero", 5.0, 5.4)])

    def test_silent_writer_counts_at_any_length(self):
        self.assertEqual(self.classify(5.15), [("silent", 5.0, 5.15)])

    def test_base_ignoring_zero(self):
        self.assertEqual(self.classify(7.0, zero_at=4.6), [("ignored_zero", 5.0, 7.0)])

    def test_late_zero_splits_silence_and_braking(self):
        self.assertEqual(self.classify(5.6, zero_at=5.3), [("silent", 5.0, 5.3), ("braking_after_zero", 5.3, 5.6)])

    def test_stop_is_when_base_stood_still_not_the_zero(self):
        found = an.no_fresh_cmd([(3.1, 5.6)], [self.cmd, run(5.3, 12.0)], self.cfg)
        self.assertEqual([(u["cause"], u["stop"], u["last_cmd"]) for u in found],
                         [("silent", 5.6, 4.5), ("braking_after_zero", 5.6, 4.5)])
        resumed = an.no_fresh_cmd([(3.1, 9.0)], [self.cmd, run(6.0, 9.0, 0.5)], self.cfg, open_end=9.0)
        self.assertEqual([(u["cause"], u["a"], u["b"], u["stop"], u["to_end"]) for u in resumed],
                         [("silent", 5.0, 6.0, None, None)])

    def test_moving_at_record_end_is_not_a_stop(self):
        found = an.no_fresh_cmd([(3.1, 9.0)], [self.cmd, run(5.3, 12.0)], self.cfg, open_end=9.0)
        self.assertEqual([(u["cause"], u["b"], u["stop"], u["to_end"]) for u in found],
                         [("silent", 5.3, None, 9.0), ("ignored_zero", 9.0, None, 9.0)])

    def test_summary_stop_after_last_cmd_counts_braking(self):
        """README: «последняя ненулевая → остановка», а не «→ ноль» (поздний ноль и торможение после него)."""
        events = [run(603.0, 604.5, 0.5), run(605.3, 606.0), ev("motion", 603.1, src="odom", moving=True),
                  ev("motion", 605.6, src="odom", moving=False), run(607.0, 607.5, 0.5),
                  ev("motion", 607.1, src="odom", moving=True), ev("motion", 610.0, src="odom", moving=False),
                  run(608.5, 610.0, 0.5), ev("stop", 611.0)]
        _, s = an.analyze([events], [seg(0, "negative", -1, 1, 2, name="empty")], ANCHOR)
        u = s["unsafe_no_fresh_cmd"]
        self.assertEqual((u["n"], u["silent"]["n"], u["braking_after_zero"]["n"]), (2, 2, 1))
        stop = u["stop_after_last_cmd_s"]                     # второй интервал кончился свежей командой
        self.assertEqual((stop["n"], stop["max"]), (1, 1.1))

    def test_summary_base_moving_at_record_end_did_not_stop(self):
        """Писатель замолчал, база едет до конца записи (hold, cmd_vel_timeout 0): «не встала», а не «встала через
        N с», где N — когда оператор остановил наблюдателя; конец интервала у каждого источника свой."""
        events = [run(603.0, 604.5, 0.5), ev("motion", 603.1, src="odom", moving=True),
                  ev("motion", 603.2, src="gt", moving=True), ev("motion", 606.0, src="gt", moving=False),
                  ev("stop", 640.0)]
        _, s = an.analyze([events], [seg(0, "negative", -1, 1, 2, name="empty")], ANCHOR)
        u = s["unsafe_no_fresh_cmd"]
        self.assertEqual((u["silent"]["n"], u["stop_after_last_cmd_s"]["n"]), (1, 0))
        self.assertEqual(u["not_stopped_by_end"], {"n": 1, "total_s": 35.0, "max_s": 35.0})
        self.assertEqual((u["by_src"]["odom"]["not_stopped_by_end"]["n"], u["by_src"]["gt"]["not_stopped_by_end"]["n"],
                          u["by_src"]["gt"]["silent"]["total_s"]), (1, 0, 1.0))

    def test_summary_late_zero_ignored_to_record_end(self):
        """Поздний ноль, а база едет до конца записи: молчание писателя не кончилось остановкой — «не встала» от
        устаревания команды до конца записи, остаток — «база игнорирует ноль»."""
        events = [run(603.0, 604.5, 0.5), run(605.3, 606.0), ev("motion", 603.1, src="odom", moving=True),
                  ev("stop", 640.0)]
        _, s = an.analyze([events], [seg(0, "negative", -1, 1, 2, name="empty")], ANCHOR)
        u = s["unsafe_no_fresh_cmd"]
        self.assertEqual((u["silent"]["total_s"], u["ignored_zero"]["total_s"], u["stop_after_last_cmd_s"]["n"]),
                         (0.3, 34.7, 0))
        self.assertEqual(u["not_stopped_by_end"], {"n": 1, "total_s": 35.0, "max_s": 35.0})

    def test_summary_stop_as_last_event_is_a_stop(self):
        """Наблюдатель убит без события stop: остановка базы — последнее событие записи, и она считается."""
        events = [run(603.0, 604.5, 0.5), ev("motion", 603.1, src="odom", moving=True),
                  ev("motion", 606.0, src="odom", moving=False)]
        _, s = an.analyze([events], [seg(0, "negative", -1, 1, 2, name="empty")], ANCHOR)
        u = s["unsafe_no_fresh_cmd"]
        self.assertEqual((u["stop_after_last_cmd_s"]["n"], u["stop_after_last_cmd_s"]["max"],
                          u["not_stopped_by_end"]["n"]), (1, 1.5, 0))


class SessionsTest(unittest.TestCase):
    """Рестарт наблюдателя (--append): сессии записи одного прогона. Между ними наблюдения нет — состояния
    закрываются концом своей сессии, перерыв не входит ни в «сообщений нет», ни во время записи."""

    def test_restart_mid_maneuver_with_fresh_commands(self):
        """Наблюдатель остановлен посреди манёвра, команды свежие; конец манёвра пришёлся на перерыв, во второй сессии
        база стоит до следующего жеста. Ни K6, ни «не встала», ни «сообщений нет» за перерыв; манёвр, конец которого
        не виден, — unobserved, а не interrupted; у следующего жеста есть метка motion."""
        segs = [seg(0, "gesture", 1, 100, 102), seg(1, "gesture", 1, 175, 179)]
        s1 = health(598, 604) + [ev("gesture", 602.2, msg={"boot": "b", "seq": 1, "cls": 1}), run(602.5, 604.4, 0.5),
                                 ev("motion", 602.7, src="odom", moving=True),
                                 ev("motion", 602.8, src="gt", moving=True), ev("stop", 604.5)]
        s2 = health(612, 700) + [ev("gesture", 679.2, msg={"boot": "b", "seq": 2, "cls": 1}),
                                 *maneuver(679.5, [("F", 3.0)]), ev("motion", 679.7, src="odom", moving=True),
                                 ev("motion", 679.8, src="gt", moving=True),
                                 ev("motion", 682.8, src="odom", moving=False),
                                 ev("motion", 682.9, src="gt", moving=False), ev("stop", 700.0)]
        per, s = an.analyze([s2, s1], segs, ANCHOR)
        by = {p["seg"]: p for p in per}
        u = s["unsafe_no_fresh_cmd"]
        self.assertEqual((u["n"], u["not_stopped_by_end"]["n"], u["stop_after_last_cmd_s"]["n"]), (0, 0, 0))
        self.assertEqual(s["reactions"], {"correct": 1, "wrong": 0, "interrupted": 0, "none": 0, "unobserved": 1})
        self.assertEqual((by[0]["cut"], by[0]["phases"], by[0]["lat"]["cmd"], by[0]["lat"]["motion"]),
                         ("end", [("F", 2.0)], 0.5, 0.7))
        self.assertNotIn("moving_at_cmd", by[1])
        self.assertEqual((by[1]["lat"]["motion"], by[1]["lat"]["cmd_to_motion"]), (0.7, 0.2))
        self.assertEqual((s["visible_failures"], s["extra_actions"], s["spontaneous_actions"]), (0, 0, 0))
        self.assertEqual((s["deaf"]["health_missing"]["n"], s["deaf"]["health_msgs"]), (0, 96))
        self.assertEqual((s["observer_gaps"], s["duration_s"]), ({"n": 1, "total_s": 7.5, "max_s": 7.5}, 94.5))
        self.assertAlmostEqual(s["k7"]["hours"], 94.5 / 3600, places=4)
        _, s = an.analyze([s1, health(612, 700) + [ev("stop", 700.0)]], segs, ANCHOR)      # база стоит всю 2-ю сессию
        u = s["unsafe_no_fresh_cmd"]
        self.assertEqual((u["n"], u["not_stopped_by_end"]["n"], s["reactions"]["none"]), (0, 0, 1))

    def test_silent_writer_until_restart_then_hold(self):
        """Писатель замолчал, база держит команду до конца 1-й сессии — «не встала до конца» (сессии), а не
        «встала через N с» по остановке во 2-й. Во 2-й база едет с начала сессии, но до первой команды писателя (в
        пределах connect_s) K6 не судится: подписка могла ещё не соединиться; потом ноль и торможение."""
        s1 = health(598, 608) + [run(602.5, 603.4, 0.5), ev("motion", 602.7, src="odom", moving=True),
                                 ev("stop", 608.0)]
        s2 = health(610, 640) + [ev("motion", 610.05, src="odom", moving=True), run(611.0, 611.5),
                                 ev("motion", 611.4, src="odom", moving=False), ev("stop", 640.0)]
        per, s = an.analyze([s1, s2], [seg(0, "gesture", 1, 100, 102)], ANCHOR)
        u = s["unsafe_no_fresh_cmd"]
        self.assertEqual((u["n"], u["total_s"], u["silent"]["n"], u["braking_after_zero"]["n"]), (1, 4.1, 1, 1))
        self.assertEqual((u["not_stopped_by_end"], u["stop_after_last_cmd_s"]["n"]),
                         ({"n": 1, "total_s": 4.1, "max_s": 4.1}, 0))
        self.assertEqual(u["by_src"]["odom"]["not_stopped_by_end"]["n"], 1)
        self.assertEqual((per[0]["reaction"], s["cmd"]["moving_without_cmd"]), ("interrupted", False))

    def test_quick_restart_continues_the_maneuver(self):
        """Перерыв короче манёвра: 2-я сессия начинается с его продолжения (база уже едет, первая команда темы
        ненулевая, подписки соединились через 0,7 с после заголовка). Продолжение — не новое действие (ни ложного, ни
        лишнего, ни самопроизвольного), не начало для lat_extra и не повод для K6; метка motion 1-го эпизода не
        берётся из следующей сессии."""
        req = "/neurobot/cmd_vel_req"
        segs = [seg(0, "gesture", 1, 100, 102), seg(1, "negative", -1, 103.8, 104.5, name="hand_still")]
        s1 = health(598, 603) + [run(598.0, 602.9), run(603.0, 603.4, 0.5), run(602.95, 603.4, 0.5, topic=req),
                                 ev("stop", 603.5)]
        s2 = [ev("start", 603.6), ev("health", 604.3, msg={"fps": 10}), ev("motion", 604.35, src="odom", moving=True),
              run(604.38, 605.4, 0.5, topic=req), run(604.4, 605.4, 0.5), run(605.5, 606.0),
              run(605.5, 606.0, topic=req),
              ev("motion", 605.8, src="odom", moving=False), *health(605, 620), ev("stop", 620.0)]
        per, s = an.analyze([s1, s2], segs, ANCHOR)
        by = {p["seg"]: p for p in per}
        self.assertEqual((by[0]["reaction"], by[0]["cut"], by[0]["lat"]["cmd"]), ("unobserved", "end", 1.0))
        self.assertNotIn("motion", by[0]["lat"])
        self.assertEqual(by[0]["lat_extra"], {req: 0.95})
        self.assertEqual((by[1]["cut"], by[1]["actions"]), ("start", 0))
        self.assertNotIn("false_action", by[1])
        self.assertNotIn("lat_extra", by[1])
        self.assertEqual((s["false_actions"]["total"], s["extra_actions"], s["spontaneous_actions"], s["k7"]["total"]),
                         (0, 0, 0, 0))
        self.assertEqual((s["unsafe_no_fresh_cmd"]["n"], s["observer_gaps"]["total_s"]), (0, 0.1))
        _, s = an.analyze([s1, s2], [seg(0, "gesture", 1, 90, 92)], ANCHOR)       # манёвр вне окна жеста
        self.assertEqual(s["spontaneous_actions"], 1)                              # продолжение — не второе

    def test_continuation_does_not_hide_the_next_reaction(self):
        """Наблюдатель перезапущен в хвосте длинного манёвра (think 11 с), хвост зашёл в окно следующего жеста, а 2-я
        сессия началась до его t0. Продолжение — не действие и не реакция: судится первый эпизод жеста с видимым началом
        (неверный back — wrong, видимый сбой), метки и решающее сообщение — по нему, а не по «не распознано» перед
        хвостом, как и без рестарта. Других эпизодов нет — unobserved без меток cmd и motion, даже если хвост пришёл
        позже t0 + sla."""
        segs = [seg(0, "gesture", 3, 100, 102), seg(1, "gesture", 1, 114, 116)]
        think = maneuver(603.5, [("R", 2.0), ("F", 2.0), ("L", 5.0), ("F", 2.0)])
        g0 = ev("gesture", 603.2, msg={"boot": "b", "seq": 1, "cls": 3})
        back = [ev("gesture", 614.6, msg={"boot": "b", "seq": 2, "cls": -1}),
                ev("gesture", 616.4, msg={"boot": "b", "seq": 3, "cls": 1}), *maneuver(616.5, [("B", 3.0)]),
                ev("motion", 616.7, src="odom", moving=True), ev("motion", 619.7, src="odom", moving=False)]
        s1 = health(598, 611) + [g0, *think[:2], run(607.5, 611.9, 0.0, 1.0),
                                 ev("motion", 603.7, src="odom", moving=True), ev("stop", 612.0)]

        def tail(start: float, at: float) -> list:
            """2-я сессия: заголовок в start, первая команда (хвост think) в at, база встаёт."""
            return [ev("start", start), run(at, at + 0.3, 0.5), run(at + 0.4, at + 0.9),
                    ev("motion", at + 0.02, src="odom", moving=True), ev("motion", at + 0.6, src="odom", moving=False),
                    *health(int(start) + 1, 640), ev("stop", 640.0)]

        per, s = an.analyze([s1, tail(614.05, 615.0) + back], segs, ANCHOR)
        self.assertEqual([(p["reaction"], p.get("cut")) for p in per], [("unobserved", "end"), ("wrong", None)])
        self.assertEqual((per[1]["maneuver"], per[1]["rec"]["cls"], per[1]["n_msgs"], per[1]["lat"], per[1]["actions"]),
                         ("back", 1, 2, {"pub": 0.4, "cmd": 0.5, "motion": 0.7, "cmd_to_motion": 0.2}, 1))
        self.assertNotIn("moved_on_unknown", per[1])
        self.assertEqual((s["visible_failures"], s["extra_actions"], s["moved_on_unknown"], s["latency_s"]["cmd"]["n"]),
                         (1, 0, 0, 2))
        one = health(598, 640) + [g0, *think, ev("motion", 603.7, src="odom", moving=True),
                                  ev("motion", 614.7, src="odom", moving=False), *back, ev("stop", 640.0)]
        single, _ = an.analyze([one], segs, ANCHOR)                                  # тот же прогон без рестарта
        self.assertEqual({k: single[1][k] for k in ("reaction", "maneuver", "lat", "rec")},
                         {k: per[1][k] for k in ("reaction", "maneuver", "lat", "rec")})
        # После хвоста действий нет: окно жеста в двух сессиях, во 2-й целиком; хвост позже t0 + sla.
        for start, at in ((614.05, 614.1), (613.95, 614.1), (619.4, 619.45)):
            per, s = an.analyze([s1, tail(start, at)], segs, ANCHOR)
            self.assertEqual((per[1]["reaction"], per[1]["cut"], per[1]["actions"], per[1]["late"], per[1]["early"],
                              s["visible_failures"]), ("unobserved", "start", 0, False, False, 0), start)
            self.assertEqual(per[1]["lat"], {}, start)

    def test_reaction_may_have_started_in_the_gap(self):
        """t0 жеста пришёлся на перерыв наблюдателя: реакция могла начаться в перерыве (2-я сессия видит её хвост или
        не видит ничего), а первое видимое действие — повтор после «не распознано». Жест — unobserved: решающее
        сообщение могло прийти в перерыве, поэтому меток (cmd, motion, rec, pub) нет, в матрице ошибок — unobserved, нет
        «поздно» и moved_on_unknown; повтор — действие сегмента. Заявка исполнителя (дополнительная тема) судится так
        же: её первый видимый эпизод — заявка повтора, и lat_extra нет, в том числе когда страж не пустил её в /cmd_vel.
        Та же запись, где 2-я сессия началась до t0, судится по первому действию: поздний come по «не распознано»."""
        req = "/neurobot/cmd_vel_req"
        segs = [seg(0, "gesture", 1, 100, 102), seg(1, "gesture", 1, 114, 116)]
        s1 = health(598, 614) + [ev("gesture", 602.3, msg={"boot": "b", "seq": 1, "cls": 1}),
                                 run(602.45, 605.4, 0.5, topic=req), *maneuver(602.5, [("F", 3.0)]),
                                 ev("motion", 602.7, src="odom", moving=True),
                                 ev("motion", 605.7, src="odom", moving=False), ev("stop", 615.0)]
        cont = maneuver(618.55, [("F", 1.05)]) + [run(618.52, 619.5, 0.5, topic=req),
                                                  ev("motion", 618.56, src="odom", moving=True),
                                                  ev("motion", 619.8, src="odom", moving=False)]
        repeat = [ev("gesture", 620.5, msg={"boot": "b", "seq": 3, "cls": -1}), run(620.65, 623.6, 0.5, topic=req),
                  *maneuver(620.7, [("F", 3.0)]), ev("motion", 620.9, src="odom", moving=True),
                  ev("motion", 623.9, src="odom", moving=False), *health(619, 640), ev("stop", 640.0)]
        for s2 in (cont + repeat, repeat):
            per, s = an.analyze([s1, [ev("start", 618.5)] + s2], segs, ANCHOR)
            p = per[1]
            self.assertEqual((p["reaction"], p["maneuver"], p.get("cut"), p["actions"], p["late"]),
                             ("unobserved", None, None, 1, False), len(s2))
            self.assertEqual((p["lat"], p["n_msgs"], per[0]["lat_extra"]), ({}, 1, {req: 0.45}), len(s2))
            self.assertNotIn("rec", p)
            self.assertNotIn("moved_on_unknown", p)
            self.assertNotIn("lat_extra", p)
            self.assertEqual((s["visible_failures"], s["late"], s["k7"]["total"], s["latency_s"]["pub"]["n"],
                              s["recognition"]["confusion"]), (0, 0, 0, 1, {"1": {"1": 1, "unobserved": 1}}), len(s2))
        blocked = [ev("start", 618.5), run(620.65, 623.6, 0.5, topic=req), *health(619, 640), ev("stop", 640.0)]
        per, _ = an.analyze([s1, blocked], segs, ANCHOR)
        self.assertEqual((per[1]["reaction"], per[1]["actions"]), ("unobserved", 0))
        self.assertNotIn("lat_extra", per[1])
        per, s = an.analyze([s1, [ev("start", 615.8)] + repeat], segs, ANCHOR)
        self.assertEqual((per[1]["reaction"], per[1]["maneuver"], per[1]["lat"]["cmd"], per[1]["lat"]["pub"],
                          per[1]["late"], per[1]["moved_on_unknown"], per[1]["lat_extra"]),
                         ("correct", "come", 4.7, 4.5, True, True, {req: 4.65}))
        self.assertEqual((s["visible_failures"], s["k7"]["moved_on_unknown"], s["recognition"]["confusion"]),
                         (1, 1, {"1": {"1": 1, "-1": 1}}))

    def test_reaction_start_hidden_while_subscriptions_connect(self):
        """Наблюдатель перезапущен за 0,1 с до начала жеста длиной 1 с: окно сегмента видно целиком, но реакция
        (заявка и /cmd_vel) пришла в первые connect_s сессии — первой командой темы. Это эпизод без начала, и после t0
        он может быть реакцией, начавшейся, пока подписки соединялись: начала не видно. Жест — unobserved (и в матрице
        ошибок) без меток, решающего сообщения и lat_extra, а не поздняя реакция и moved_on_unknown по повтору после
        «не распознано»; повтор — действие сегмента. Тот же прогон без рестарта — верный come. Сессия после t0 видит
        только такой эпизод и сообщение жеста — решающего сообщения тоже нет: оно могло прийти и до сессии."""
        req = "/neurobot/cmd_vel_req"
        segs = [seg(0, "gesture", 1, 101, 102)]
        s1 = health(590, 600) + [ev("stop", 600.5)]
        react = [run(602.55, 605.5, 0.5, topic=req), *maneuver(602.6, [("F", 3.0)])]
        g1 = ev("gesture", 602.45, msg={"boot": "b", "seq": 1, "cls": 1})
        repeat = [ev("gesture", 606.2, msg={"boot": "b", "seq": 2, "cls": -1}), run(606.35, 609.3, 0.5, topic=req),
                  *maneuver(606.4, [("F", 3.0)])]
        s2 = [ev("start", 600.9), *health(601, 630), g1, *react, *repeat, ev("stop", 630.0)]
        per, s = an.analyze([s1, s2], segs, ANCHOR)
        p = per[0]
        self.assertEqual((p["reaction"], p["maneuver"], p["lat"], p["actions"], p["late"], p["n_msgs"]),
                         ("unobserved", None, {}, 1, False, 2))
        for key in ("rec", "lat_extra", "moved_on_unknown"):
            self.assertNotIn(key, p)
        self.assertEqual((s["visible_failures"], s["late"], s["k7"]["total"], s["recognition"]["confusion"]),
                         (0, 0, 0, {"1": {"unobserved": 1}}))
        per, s = an.analyze([health(590, 630) + [g1, *react, *repeat, ev("stop", 630.0)]], segs, ANCHOR)
        self.assertEqual((per[0]["reaction"], per[0]["lat"]["cmd"], per[0]["lat_extra"], per[0]["rec"]["cls"],
                          s["visible_failures"], s["extra_actions"]), ("correct", 0.6, {req: 0.55}, 1, 0, 1))
        per, s = an.analyze([s1, [ev("start", 602.4), g1, *react, *health(603, 630), ev("stop", 630.0)]], segs, ANCHOR)
        self.assertEqual((per[0]["reaction"], per[0]["cut"], per[0]["n_msgs"], s["recognition"]["confusion"]),
                         ("unobserved", "start", 1, {"1": {"unobserved": 1}}))
        self.assertNotIn("rec", per[0])

    def test_wrong_start_cut_by_session_end_is_judged(self):
        """Эпизод у конца сессии, уже не похожий на ожидаемый манёвр, судится: конец сессии не прячет ошибку."""
        s1 = health(598, 604) + [run(602.5, 603.9, 0.0, -1.0), ev("stop", 604.0)]
        per, s = an.analyze([s1], [seg(0, "gesture", 1, 100, 102)], ANCHOR)
        self.assertEqual((per[0]["cut"], per[0]["reaction"], per[0]["maneuver"]),
                         ("end", "wrong", "partial:go_right|think|turn180"))
        self.assertEqual(s["visible_failures"], 1)

    def test_gesture_and_states_across_the_gap(self):
        """Жест целиком в перерыве — unobserved (и в матрице ошибок), а не «реакции нет»; fps = 0 и касание, не
        закрытые к концу сессии, кончаются с ней; строки probe из перерыва не учитываются."""
        segs = [seg(0, "gesture", 1, 100, 102), seg(1, "gesture", 1, 115, 117)]
        s1 = health(598, 608) + health(609, 611, fps=0) + [
            ev("gesture", 602.2, msg={"boot": "b", "seq": 1, "cls": 1}), *maneuver(602.5, [("F", 3.0)]),
            ev("motion", 602.7, src="odom", moving=True), ev("motion", 605.8, src="odom", moving=False),
            ev("wall", 610.0, touch=True, d=0.0, wall="lab_wall"), ev("stop", 611.5)]
        s2 = health(640, 660) + [ev("stop", 660.0)]
        probe = [{"t": m + OFFSET, "m": float(m), "pid": 7, "frames": m, "det_calls": m if not 615 <= m <= 635 else 615,
                  "timeouts": 0, "marker_miss": 0, "faults": []} for m in range(598, 661)]
        per, s = an.analyze([s1, s2], segs, ANCHOR, probe)
        self.assertEqual([p["reaction"] for p in per], ["correct", "unobserved"])
        self.assertEqual((s["recognition"]["confusion"], s["visible_failures"]), ({"1": {"1": 1, "unobserved": 1}}, 0))
        d = s["deaf"]
        self.assertEqual((d["health"], d["health_missing"]["n"]), ({"n": 1, "total_s": 2.0, "max_s": 2.0}, 0))
        self.assertEqual((d["probe"]["n"], d["probe_missing"]["n"]), (0, 0))
        self.assertEqual((s["wall"]["total_s"], s["duration_s"]), (1.5, 33.5))

    def test_window_is_seen_from_segment_start(self):
        """Окно сегмента видно целиком, только если сессия записи началась не позже начала сегмента (t_start), а не t0:
        реакция бывает и до конца жеста (early). Сессия началась между t_start и t0, реакции нет — unobserved, а не none
        и не видимый сбой."""
        segs = [seg(0, "gesture", 1, 100, 104)]
        for start, reaction, failures in ((602.0, "unobserved", 0), (599.5, "none", 1)):
            per, s = an.analyze([[ev("start", start), *health(int(start) + 1, 630), ev("stop", 630.0)]], segs, ANCHOR)
            self.assertEqual((per[0]["reaction"], s["visible_failures"], s["recognition"]["confusion"]),
                             (reaction, failures, {"1": {reaction: 1}}), start)

    def test_perception_silent_from_session_start(self):
        """Рестарт наблюдателя не прячет глухоту perception: тишина с начала сессии — «сообщений нет» (health и probe)
        от начала сессии + connect_s до первого сообщения, а сессия без сообщений — целиком. Тишина до первого
        сообщения темы в прогоне — прогрев perception, не глухота: и в одной сессии (наблюдатель запущен раньше
        perception), и через рестарт наблюдателя до первого сообщения. В прогоне без темы тишины нет."""
        segs = [seg(0, "negative", -1, 1, 2, name="empty")]
        s1 = health(598, 608) + [ev("stop", 608.5)]
        late = [ev("start", 610.0), *health(640, 660), ev("stop", 660.0)]           # perception ожило на 640
        dead = [ev("start", 610.0), ev("stop", 660.0)]                              # молчит всю 2-ю сессию
        for s2, connect_s, total in ((late, 2.0, 28.0), (late, 5.0, 25.0), (dead, 2.0, 48.0)):
            ms = [e["m"] for e in s1 + s2 if e["ev"] == "health"]
            _, s = an.analyze([s1, s2], segs, ANCHOR, probe_lines(ms), an.AnalyzeConfig(connect_s=connect_s))
            for key in ("health_missing", "probe_missing"):
                self.assertEqual(s["deaf"][key], {"n": 1, "total_s": total, "max_s": total}, (key, connect_s, total))
        _, s = an.analyze([[ev("start", 598.0), ev("stop", 608.5)], dead], segs, ANCHOR, [])
        self.assertEqual((s["deaf"]["health_missing"]["n"], s["deaf"]["probe_missing"]["n"], s["deaf"]["health_msgs"]),
                         (0, 0, 0))
        warm = [ev("start", 610.0), *health(632, 660), ev("stop", 660.0)]           # наблюдатель запущен до perception
        warm_restart = [[ev("start", 600.0), ev("stop", 608.0)], warm]              # и перезапущен до его здоровья
        for sessions in ([warm], warm_restart):
            ms = [e["m"] for evs in sessions for e in evs if e["ev"] == "health"]
            _, s = an.analyze(sessions, segs, ANCHOR, probe_lines(ms))
            self.assertEqual((s["deaf"]["health_missing"]["n"], s["deaf"]["probe_missing"]["n"]), (0, 0), len(sessions))

    def test_motion_without_writer_commands_in_one_session(self):
        """В 2-й сессии база едет, а команд писателя нет вовсе — moving_without_cmd, хотя в 1-й команды были; после
        connect_s от начала сессии это и K6 (молчание писателя). Движение только в первые connect_s (база дотормаживает
        после рестарта) — ни то, ни другое."""
        s1 = [ev("health", 598.0, msg={"fps": 10}), run(600.0, 601.0, 0.5), run(601.1, 601.5),
              ev("motion", 600.2, src="odom", moving=True), ev("motion", 601.6, src="odom", moving=False),
              ev("stop", 610.0)]
        s2 = [ev("health", 620.0, msg={"fps": 10}), ev("motion", 625.0, src="odom", moving=True),
              ev("motion", 630.0, src="odom", moving=False), ev("stop", 640.0)]
        segs = [seg(0, "negative", -1, 1, 2, name="empty")]
        _, s = an.analyze([s1, s2], segs, ANCHOR)
        u = s["unsafe_no_fresh_cmd"]
        self.assertEqual((s["cmd"]["moving_without_cmd"], u["n"], u["silent"]["total_s"]), (True, 1, 5.0))
        self.assertEqual((u["stop_after_last_cmd_s"]["n"], u["not_stopped_by_end"]["n"]), (0, 0))
        _, s = an.analyze([s1], segs, ANCHOR)
        self.assertFalse(s["cmd"]["moving_without_cmd"])
        s3 = [ev("start", 650.0), ev("motion", 650.3, src="odom", moving=True),
              ev("motion", 650.9, src="odom", moving=False), ev("stop", 660.0)]
        _, s = an.analyze([s1, s3], segs, ANCHOR)
        self.assertEqual((s["cmd"]["moving_without_cmd"], s["unsafe_no_fresh_cmd"]["n"]), (False, 0))

    def test_writer_dead_through_restart(self):
        """Писатель замолчал и не ожил, база держит команду и через рестарт наблюдателя: 2-я сессия (start — момент
        заголовка) видит движение с начала. K6 судится с начала сессии + connect_s: «не встала до конца» в обеих
        сессиях, перерыв в K6 не входит; --connect-s сдвигает начало суда."""
        s1 = health(598, 608) + [run(602.5, 603.4, 0.5), ev("motion", 602.7, src="odom", moving=True),
                                 ev("stop", 608.0)]
        s2 = [ev("start", 610.0), ev("motion", 610.4, src="odom", moving=True), *health(611, 640), ev("stop", 640.0)]
        segs = [seg(0, "negative", -1, 1, 2, name="empty")]
        for connect_s, total in ((2.0, 32.1), (5.0, 29.1)):
            _, s = an.analyze([s1, s2], segs, ANCHOR, cfg=an.AnalyzeConfig(connect_s=connect_s))
            u = s["unsafe_no_fresh_cmd"]
            self.assertEqual((u["silent"]["n"], u["silent"]["total_s"]), (2, total), connect_s)
            self.assertEqual((u["not_stopped_by_end"]["n"], u["stop_after_last_cmd_s"]["n"]), (2, 0), connect_s)
            self.assertEqual((s["observer_gaps"]["total_s"], s["duration_s"]), (2.0, 40.0))


class ReflexTableTest(unittest.TestCase):
    """Класс без действия («none» в таблице рефлекса, напр. класс 3 под --ignore-cls), «не распознано».
    Запись начата до прогона: первое событие сессии (здоровье) задолго до команд."""
    start = ev("health", 500.0, msg={"fps": 10})

    def setUp(self):
        self.segs = [seg(0, "gesture", 3, 10, 11), seg(1, "gesture", 3, 30, 31),
                     seg(2, "negative", -1, 50, 55, name="hand_still"), seg(3, "gesture", 1, 70, 71)]
        self.cfg = an.AnalyzeConfig(reflex=dict(an.REFLEX, **{"3": "none"}))

    def test_stand_still_class(self):
        events = maneuver(532.0, [("F", 3.0)]) + [ev("stop", 600.0), self.start]
        per, s = an.analyze([events], self.segs, ANCHOR, cfg=self.cfg)
        by = {p["seg"]: p for p in per}
        self.assertEqual((by[0]["reaction"], by[0]["expected"]), ("correct", None))
        self.assertEqual((by[1]["reaction"], by[1]["maneuver"], by[1]["moved_when_ignored"]), ("wrong", "come", True))
        self.assertEqual((s["k7"]["moved_when_ignored"], s["k7"]["total"]), (1, 1))

    def test_moved_on_unknown_on_negative_segment_and_after_valid_class(self):
        events = [self.start, ev("gesture", 552.0, msg={"boot": "b", "seq": 1, "cls": -1}),
                  *maneuver(552.5, [("F", 3.0)]),
                  ev("gesture", 572.0, msg={"boot": "b", "seq": 2, "cls": -1}),
                  ev("gesture", 572.3, msg={"boot": "b", "seq": 3, "cls": 1}), *maneuver(572.5, [("F", 3.0)]),
                  ev("stop", 600.0)]
        per, s = an.analyze([events], self.segs, ANCHOR, cfg=self.cfg)
        by = {p["seg"]: p for p in per}
        self.assertTrue(by[2]["moved_on_unknown"] and by[2]["false_action"])
        self.assertNotIn("moved_on_unknown", by[3])          # последним до движения было «1»
        self.assertEqual((s["moved_on_unknown"], s["k7"]["total"]), (1, 1))
        self.assertEqual((by[3]["rec"]["cls"], by[3]["n_msgs"]), (1, 2))
        self.assertEqual(s["recognition"]["confusion"]["1"], {"1": 1})

    def test_decisive_message_of_a_cut_gesture(self):
        """perception разрезал длинный жест: первая часть «не распознано» задолго до t0, по второй (класс 3) робот
        начал think, и манёвр оборвался. Распознавание и метки — по второй, манёвр назван по её классу."""
        segs = [seg(0, "gesture", 1, 10, 15)]
        events = [ev("gesture", 512.15, msg={"boot": "b", "seq": 1, "cls": -1, "t_rec": 512.1 + OFFSET}),
                  ev("gesture", 515.5, msg={"boot": "b", "seq": 2, "cls": 3, "t_rec": 515.45 + OFFSET}),
                  run(515.6, 517.5, 0.0, -1.0), ev("stop", 530.0)]
        per, s = an.analyze([events], segs, ANCHOR)
        p = per[0]
        self.assertEqual((p["rec"]["cls"], p["n_msgs"], p["reaction"], p["maneuver"]), (3, 2, "wrong", "partial:think"))
        self.assertEqual((p["lat"]["rec"], p["lat"]["pub"]), (0.45, 0.5))
        self.assertNotIn("moved_on_unknown", p)
        self.assertEqual(s["recognition"]["confusion"], {"1": {"3": 1}})

    def test_negative_with_any_recognized_message_is_false_recognition(self):
        events = [ev("gesture", 551.0, msg={"boot": "b", "seq": 1, "cls": 2}),
                  ev("gesture", 553.0, msg={"boot": "b", "seq": 2, "cls": -1}), ev("stop", 600.0)]
        per, s = an.analyze([events], self.segs, ANCHOR, cfg=self.cfg)
        self.assertEqual(({p["seg"]: p for p in per}[2]["rec"]["cls"], s["false_recognitions"]), (-1, 1))

    def test_reaction_before_end_of_gesture_is_early(self):
        events = [self.start, *maneuver(570.5, [("F", 3.0)]), ev("stop", 600.0)]   # жест 3: t0 = 571, команда с 570,5
        per, s = an.analyze([events], self.segs, ANCHOR, cfg=self.cfg)
        self.assertEqual((s["early"], {p["seg"]: p["early"] for p in per if p["kind"] == "gesture"}[3]), (1, True))
        self.assertEqual({p["seg"]: p["lat"]["cmd"] for p in per if "cmd" in p["lat"]}, {3: -0.5})

    def test_unknown_class_in_table_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "нет класса 3"):
            an.analyze([[ev("stop", 600.0)]], self.segs, ANCHOR, cfg=an.AnalyzeConfig(reflex={"1": "come"}))


class JudgeTest(unittest.TestCase):
    cfg = an.AnalyzeConfig()

    def test_durations_separate_think_and_go_right(self):
        self.assertEqual(an.judge([("R", 2.0), ("F", 2.0), ("L", 5.0), ("F", 2.0)], "think", self.cfg),
                         ("correct", "think"))
        self.assertEqual(an.judge([("R", 2.0), ("F", 2.0), ("L", 5.0)], "think", self.cfg), ("interrupted", "think"))
        self.assertEqual(an.judge([("R", 3.0), ("F", 2.0), ("L", 3.0)], "think", self.cfg), ("wrong", "go_right"))

    def test_unknown_and_partial(self):
        self.assertEqual(an.judge([("F", 1.0), ("L", 1.0)], "come", self.cfg), ("wrong", "unknown:FL"))
        self.assertEqual(an.judge([("L", 1.0)], "come", self.cfg), ("wrong", "partial:go_left"))
        self.assertEqual(an.judge([("F", 9.0)], "come", self.cfg), ("wrong", "unknown:F"))

    def test_ambiguous_partial_is_named_by_request(self):
        ambiguous = ("wrong", "partial:go_right|think|turn180")
        self.assertEqual(an.judge([("R", 2.0)], "come", self.cfg), ambiguous)
        self.assertEqual(an.judge([("R", 2.0)], "come", self.cfg, "think"), ("wrong", "partial:think"))
        self.assertEqual(an.judge([("R", 2.0)], "come", self.cfg, "go_left"), ambiguous)   # не из подходящих
        self.assertEqual(an.requested_maneuver(an.REFLEX, {"cls": 2, "side": "right"}), "go_right")
        for msg in ({"cls": -1}, {"cls": None}, {"cls": 7}, {}):
            self.assertIsNone(an.requested_maneuver(an.REFLEX, msg), msg)

    def test_durations_optional(self):
        cfg = an.AnalyzeConfig(maneuvers={"go": [["F", None]]}, reflex={"1": "go"})
        self.assertEqual(an.judge([("F", 9.0)], "go", cfg), ("correct", "go"))

    def test_phases_drop_transients(self):
        ep = {"runs": [run(0.0, 2.9, 0.0, -1.0), run(3.0, 3.0, 0.0, 1.0), run(3.1, 4.9, 0.5, 0.0)]}
        self.assertEqual(an.phases(ep, 0.25), [("R", 3.1), ("F", 1.9)])


class StatsAndIntervalsTest(unittest.TestCase):
    def test_union_subtract(self):
        self.assertEqual(an.union([(3, 4), (1, 2), (1.5, 2.5)]), [(1, 2.5), (3, 4)])
        self.assertEqual(an.subtract([(0, 10)], [(2, 3), (5, 12)]), [(0, 2), (3, 5)])
        self.assertEqual(an.iv_stats([]), {"n": 0, "total_s": 0, "max_s": 0.0})

    def test_latency_stats(self):
        self.assertEqual(an.latency_stats([]), {"n": 0})
        small = an.latency_stats([0.5, 0.6, 0.7])
        self.assertEqual((small["n"], small["p50"]), (3, 0.6))
        self.assertNotIn("ci95", small)
        big = an.latency_stats([0.5 + 0.01 * (i % 7) for i in range(40)])
        lo, hi = big["ci95"]["p50"]
        self.assertTrue(lo <= big["p50"] <= hi)
        self.assertIsNone(big["ci95"]["p95"])                 # для p95 40 наблюдений мало

    def test_rank_quantile_ci(self):
        v = list(range(1, 101))
        self.assertEqual(an.quantile_ci(v, 0.5), [40, 61])    # Conover: n = 100, p = 0,5 → X(40), X(61)
        self.assertIsNone(an.quantile_ci(v[:20], 0.95))


class RecordTest(unittest.TestCase):
    def test_cmd_runs(self):
        r = CmdRuns("/cmd_vel", gap_s=0.25)
        out = []
        for i in range(5):
            out += r.feed(i, 0.1 * i, 0.5, 0.0)
        out += r.feed(9, 0.5, 0.5, 0.0004)               # ниже EPS — то же значение
        self.assertEqual(out, [])
        out += r.feed(10, 0.6, 0.0, 0.0)
        self.assertEqual([(e["n"], e["x"], e["m0"], e["m1"]) for e in out], [(6, 0.5, 0.0, 0.5)])
        self.assertEqual(r.flush(0.7), [])
        self.assertEqual(len(r.flush(0.9)), 1)
        r.feed(11, 1.0, 0.0, 1.0)
        out = r.feed(12, 1.4, 0.0, 1.0)                  # пауза > gap_s — новая серия
        self.assertEqual(len(out), 1)
        self.assertEqual(len(r.close()), 1)

    def test_motion_pose_velocity_rtf(self):
        m = MotionTracker("odom")
        self.assertIsNone(m.feed(0, 0, 0.01, 0.0, 0))
        self.assertTrue(m.feed(0, 1, 0.0, 0.2, 1)["moving"])
        self.assertIsNone(m.feed(0, 2, 0.1, 0.0, 2))
        self.assertFalse(m.feed(0, 3, 0.0, 0.0, 3)["moving"])
        pv = PoseVelocity()
        self.assertIsNone(pv.feed(0.0, 0.0, 3.1, 10.0))
        v, w = pv.feed(0.1, 0.0, -3.1, 10.5)
        self.assertAlmostEqual(v, 0.2)
        self.assertAlmostEqual(w, (2 * math.pi - 6.2) / 0.5)
        self.assertIsNone(pv.feed(0.2, 0.0, 0.0, 10.5))
        rtf = RtfMeter("odom", window_s=10.0)
        self.assertIsNone(rtf.feed(0, 0.0, 100.0))
        self.assertIsNone(rtf.feed(0, 5.0, 102.5))
        self.assertEqual(rtf.feed(0, 10.0, 105.0)["rtf"], 0.5)

    def test_publisher_type_mismatch_reported_once(self):
        events = []
        rec = Recorder(events.append, ["/cmd_vel"])
        ours = "geometry_msgs/msg/Twist"
        self.assertFalse(rec.on_publishers("/cmd_vel", ours, [ours], 1.0, 1.0))
        self.assertFalse(rec.on_publishers("/cmd_vel", ours, [], 1.0, 1.0))
        self.assertTrue(rec.on_publishers("/cmd_vel", ours, [ours, "geometry_msgs/msg/TwistStamped"], 2.0, 2.0))
        self.assertFalse(rec.on_publishers("/cmd_vel", ours, ["geometry_msgs/msg/TwistStamped"], 3.0, 3.0))
        self.assertEqual([(e["ev"], e["theirs"]) for e in events],
                         [("cmd_type_mismatch", ["geometry_msgs/msg/TwistStamped"])])
        events += [ev("motion", 10.0, src="odom", moving=True), ev("motion", 12.0, src="odom", moving=False),
                   ev("stop", 13.0)]
        _, s = an.analyze([events], [seg(0, "negative", -1, 1, 2, name="empty")], Anchor(0.0, 0.0, "x", 1))
        self.assertEqual((s["cmd"]["type_mismatch"], s["cmd"]["moving_without_cmd"]), (["/cmd_vel"], True))

    def test_recorder_end_to_end(self):
        events = []
        rec = Recorder(events.append, ["/cmd_vel", "/cmd_vel_agent"],
                       walls=[Rect("lab_wall", 1.5, 0.0, 0.0, 0.1, 3.0)], pose_period_s=0.5)
        for i in range(20):
            m = 0.05 * i
            rec.on_cmd("/cmd_vel", m, m, 0.2, 0.0)
            rec.on_odom(m, m, 0.2, 0.0, m)
            rec.on_gt(m, m, 1.2 + 0.01 * i, 0.0, 0.0, m)
        rec.on_json("gesture", 1.0, 1.0, '{"seq": 1, "cls": 2}')
        rec.on_json("health", 1.0, 1.0, "[1, 2]")
        rec.tick(2.0)
        rec.close(3.0, 3.0)
        kinds = [e["ev"] for e in events]
        self.assertEqual(kinds.count("cmd"), 1)
        self.assertEqual(kinds[-1], "stop")
        self.assertTrue(any(e["ev"] == "motion" and e["src"] == "odom" and e["moving"] for e in events))
        self.assertTrue(any(e["ev"] == "motion" and e["src"] == "gt" and e["moving"] for e in events))
        touch = [e for e in events if e["ev"] == "wall"]
        self.assertEqual([e["touch"] for e in touch], [True])
        self.assertEqual(len([e for e in events if e["ev"] == "pose"]), 2)
        self.assertEqual([e["msg"]["cls"] for e in events if e["ev"] == "gesture"], [2])
        self.assertIn("error", [e for e in events if e["ev"] == "health"][0])


SDF = """<?xml version="1.0"?><sdf version="1.9"><world name="lab">
<model name="ground_plane"><link name="l"><collision name="c">
 <geometry><plane><size>9 9</size></plane></geometry></collision></link></model>
<model name="lab_wall"><static>true</static><pose>1.5 0 0.5 0 0 1.5707963</pose>
 <link name="link"><pose>0 0.5 0 0 0 0</pose><collision name="col">
  <geometry><box><size>3 0.1 1</size></box></geometry></collision></link></model>
<model name="post_back"><static>true</static><pose>-0.6 0 0.5 0 0 0</pose>
 <link name="link"><collision name="collision">
  <geometry><cylinder><radius>0.1</radius><length>1.0</length></cylinder></geometry></collision></link></model>
</world></sdf>"""


class WallsTest(unittest.TestCase):
    def test_poly_distance(self):
        a = Rect("a", 0, 0, 0, 1, 1).corners()
        self.assertAlmostEqual(poly_distance(a, Rect("b", 2, 0, 0, 1, 1).corners()), 1.0)
        self.assertEqual(poly_distance(a, Rect("c", 0.5, 0.5, 0.3, 1, 1).corners()), 0.0)
        self.assertAlmostEqual(poly_distance(a, Rect("d", 2, 2, 0, 1, 1).corners()), math.sqrt(2))

    def test_footprint_faces_yaw(self):
        fp = footprint(0, 0, math.pi / 2)
        self.assertAlmostEqual(max(y for _, y in fp), 0.08)

    def test_point_poly_and_circles(self):
        sq = Rect("a", 0, 0, 0, 1, 1).corners()
        self.assertEqual(point_poly_distance(0.1, 0.2, sq), 0.0)
        self.assertAlmostEqual(point_poly_distance(1.5, 0.0, sq), 1.0)
        wall, post = Rect("wall", 1.0, 0.0, 0.0, 0.1, 3.0), Circle("post", -0.6, 0.0, 0.1)
        r = RADIUS_NEUROCHIP_BASE
        self.assertAlmostEqual(nearest_wall([wall], 0, 0, 1.0, r)[0], 0.95 - r)       # круг: курс не важен
        self.assertAlmostEqual(nearest_wall([post], 0, 0, 0.0, r)[0], 0.5 - r)
        self.assertAlmostEqual(nearest_wall([post], 0, 0, 0.0)[0], 0.5 - 0.197)       # контур TB3: задний край
        self.assertEqual(nearest_wall([post], -0.4, 0, 0.0, r)[0], 0.0)
        rotated = nearest_wall([wall], 0, 0, math.pi / 4)[0]                         # угол прямоугольника ближе
        self.assertLess(rotated, nearest_wall([wall], 0, 0, 0.0)[0])

    def test_sdf_and_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "w.sdf")
            Path(p).write_text(SDF, encoding="utf-8")
            (w,) = walls_from_sdf(p, "wall")
            (post,) = walls_from_sdf(p, "post")
            self.assertEqual(post, Circle("post_back/link/collision", -0.6, 0.0, 0.1))
            self.assertAlmostEqual(w.cx, 1.0)
            self.assertAlmostEqual(w.cy, 0.0, places=6)
            self.assertEqual((w.sx, w.sy), (3.0, 0.1))
            d_front, name = nearest_wall([w], 0.0, 0.0, 0.0)
            self.assertAlmostEqual(d_front, 1.0 - 0.05 - 0.08, places=6)
            self.assertEqual(name, "lab_wall/link/col")
            with self.assertRaises(ValueError):
                walls_from_sdf(p, "nothing")
            j = os.path.join(d, "w.json")
            Path(j).write_text(json.dumps([{"name": "x", "cx": 1, "cy": 2, "sx": 3, "sy": 4},
                                           {"name": "p", "cx": 0, "cy": 1, "r": 0.2}]), encoding="utf-8")
            self.assertEqual(load_walls(j), [Rect("x", 1.0, 2.0, 0.0, 3.0, 4.0), Circle("p", 0.0, 1.0, 0.2)])

    def test_bad_sources_are_value_errors(self):
        """Ошибки задания препятствий — ValueError (у наблюдателя код 2, как у других параметров), нет файла —
        OSError; трассировки из глубины json/xml/re наружу не выходят."""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x")
            for text in ("{oops", '{"name": "x"}', '[{"name": "x", "cx": 1}]', '[{"name": "x", "cx": 1, "cy": 2, '
                         '"r": null}]', "[3]"):
                Path(p).write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "^%s: препятствие" % p, msg=text):
                    load_walls(p)
            bad_pose = SDF.replace("<pose>-0.6 0 0.5 0 0 0</pose>", "<pose>-0.6 0</pose>")
            for text, regex, msg in (("<sdf", "wall", "не XML"), ("<sdf/>", "wall", "нет <world>"),
                                     (SDF, "(", "регулярное"), (bad_pose, "post", "модель post_back")):
                Path(p).write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, msg):
                    walls_from_sdf(p, regex)
            for load in (load_walls, walls_from_sdf):
                with self.assertRaises(OSError):
                    load(os.path.join(d, "missing"))


class DeafTest(unittest.TestCase):
    def test_paused_or_moving_is_not_deaf(self):
        segs = [seg(0, "negative", -1, 1, 2, name="empty")]
        for state, n in (("paused", 0), ("moving", 0), ("ok", 1)):
            events = [ev("health", 500.0 + i, msg={"fps": 0 if i >= 3 else 10, "state": state if i >= 3 else "ok"})
                      for i in range(12)]
            _, s = an.analyze([events], segs, ANCHOR)
            self.assertEqual(s["deaf"]["health"]["n"], n, state)

    def test_probe_journal_thread_feeds_deaf_detection(self):
        with tempfile.TemporaryDirectory() as d:
            p = Probe(os.path.join(d, "probe.jsonl"), period_s=0.05)
            p.inc("det_calls", 5)
            m0 = mono()
            p.start()
            time.sleep(0.4)
            p.path = None                                   # поток журнала завершается
            lines = an.read_probe(os.path.join(d, "probe.jsonl"))
        self.assertGreaterEqual(len(lines), 4)
        self.assertEqual({(x["det_calls"], x["pid"]) for x in lines}, {(5, os.getpid())})
        events = [{"ev": "health", "t": m0, "m": m0, "msg": {"fps": 10}}, {"ev": "stop", "t": m0 + 1, "m": m0 + 1}]
        anchor = Anchor(t_wall=m0, t_mono=m0, timeline_sha256="x", pid=1)
        _, s = an.analyze([events], [seg(0, "negative", -1, 0.5, 0.6, name="empty")], anchor, lines,
                          an.AnalyzeConfig(deaf_min_s=0.2))
        self.assertEqual(s["deaf"]["probe"]["n"], 1)
        self.assertEqual(s["probe"]["pids"], 1)


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = {n: os.path.join(self.tmp.name, n) for n in ("ev.jsonl", "tr.jsonl", "a.json", "p.jsonl")}
        segs, self.events, probe = scenario()
        write_jsonl(self.paths["tr.jsonl"], {"version": 1, "timeline_sha256": ANCHOR.timeline_sha256}, segs)
        Path(self.paths["a.json"]).write_text(json.dumps(ANCHOR.__dict__), encoding="utf-8")
        Path(self.paths["p.jsonl"]).write_text("".join(json.dumps(p) + "\n" for p in probe), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def write_events(self, *sessions, tail: str = "") -> None:
        with open(self.paths["ev.jsonl"], "w", encoding="utf-8") as fh:
            for header, events in sessions:
                fh.write(json.dumps({"header": header}) + "\n")
                fh.writelines(json.dumps(e) + "\n" for e in events)
            fh.write(tail)

    def analyze(self, *extra) -> tuple:
        out, per = os.path.join(self.tmp.name, "s.json"), os.path.join(self.tmp.name, "g.jsonl")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = an.main(["--events", self.paths["ev.jsonl"], "--truth", self.paths["tr.jsonl"], "--anchor",
                          self.paths["a.json"], "--probe", self.paths["p.jsonl"], "--out", out, "--per-gesture", per,
                          *extra])
        s = json.loads(Path(out).read_text(encoding="utf-8")) if rc == 0 else None
        return rc, s, err.getvalue(), per

    def test_cli_and_partial_last_line(self):
        self.write_events(({"observer": "test"}, self.events), tail='{"ev": "cmd", "top')
        rc, s, err, per = self.analyze("--sla-s", "4")
        self.assertEqual(rc, 0)
        self.assertIn("недописанная", err)
        self.assertEqual((s["late"], s["config"]["sla_s"], s["input"]["dropped_events"]), (0, 4.0, 0))
        self.assertEqual(len(Path(per).read_text(encoding="utf-8").splitlines()), 9)

    def test_sessions_of_other_runs_are_dropped(self):
        before = [{"ev": "cmd", "topic": "/cmd_vel", "t0": 1100.0, "m0": 100.0, "t1": 1103.0, "m1": 103.0, "n": 30,
                   "x": 0.3, "z": 0.0}, ev("motion", 100.1, src="odom", moving=True),
                  ev("motion", 107.0, src="odom", moving=False), ev("stop", 108.0)]
        other = [ev("motion", 800.0, src="odom", moving=True), ev("motion", 805.0, src="odom", moving=False)]
        self.write_events(({"observer": "old"}, before), ({"timeline_sha256": "y" * 64}, other),
                          ({"timeline_sha256": ANCHOR.timeline_sha256}, self.events))
        rc, s, err, _ = self.analyze()
        self.assertEqual(rc, 0)
        self.assertEqual(s["input"], {"sessions": 3, "dropped_sessions": {"before_anchor": 1, "timeline": 1},
                                      "dropped_events": 6})
        self.assertEqual((s["spontaneous_actions"], s["unsafe_no_fresh_cmd"]["n"], s["duration_s"]), (1, 1, 420.0))
        self.assertIn("отброшены", err)

    def test_sessions_of_one_run_are_kept_apart(self):
        """Рестарт наблюдателя: две сессии одного прогона — обе в разборе, перерыв между ними — observer_gaps.
        Сессия начинается моментом заголовка (m), а не первым событием."""
        cut = [e for e in self.events if e.get("m", e.get("m0")) < 770] + [ev("stop", 770.0)]
        rest = [e for e in self.events if e.get("m", e.get("m0")) >= 790]
        header = {"timeline_sha256": ANCHOR.timeline_sha256}
        self.write_events((dict(header, t=999.5, m=499.5), cut), (dict(header, t=1288.0, m=788.0), rest))
        rc, s, _, _ = self.analyze()
        self.assertEqual(rc, 0)
        self.assertEqual(s["input"], {"sessions": 2, "dropped_sessions": {}, "dropped_events": 0})
        self.assertEqual((s["observer_gaps"], s["duration_s"]), ({"n": 1, "total_s": 18.0, "max_s": 18.0}, 402.5))
        self.assertEqual(s["reactions"], {"correct": 5, "wrong": 1, "interrupted": 1, "none": 1, "unobserved": 0})
        self.assertEqual((s["spontaneous_actions"], s["unsafe_no_fresh_cmd"]["n"]), (1, 1))

    def test_missing_file_is_code_2(self):
        """Нет файла записи, истины, якоря, probe или каталога для сводки — код 2 и имя файла в stderr."""
        self.write_events(({}, self.events))
        missing = os.path.join(self.tmp.name, "нет", "f")
        for name in ("ev.jsonl", "tr.jsonl", "a.json", "p.jsonl"):
            good, self.paths[name] = self.paths[name], missing
            rc, _, err, _ = self.analyze()
            self.paths[name] = good
            self.assertEqual((rc, err.startswith("analyze: "), missing in err), (2, True, True), name)
        rc, _, err, _ = self.analyze("--out", missing)
        self.assertEqual((rc, missing in err), (2, True))

    def test_truth_of_other_timeline_is_an_error(self):
        self.write_events(({}, self.events))
        write_jsonl(self.paths["tr.jsonl"], {"version": 1, "timeline_sha256": "y" * 64}, [])
        rc, _, err, _ = self.analyze()
        self.assertEqual(rc, 2)
        self.assertIn("разных таймлайнов", err)

    def test_maneuver_table_file(self):
        p = os.path.join(self.tmp.name, "m.json")
        table = {"maneuvers": {"go": [["F", None]]}, "reflex": {"1": "go", "3": "none"}}
        Path(p).write_text(json.dumps(table), encoding="utf-8")
        cfg = an.load_config(p, sla_s=2.0)
        self.assertEqual((cfg.maneuvers, cfg.reflex, cfg.sla_s), (table["maneuvers"], table["reflex"], 2.0))
        for bad in ({"maneuvers": {"go": [["X", 1]]}}, {"maneuvers": {"go": [["F", "1"]]}},
                    {"reflex": {"1": "fly"}}, {"maneuvers": []}):
            Path(p).write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError, msg=bad):
                an.load_config(p)

    def test_config_flags_come_from_dataclass(self):
        parser = argparse.ArgumentParser()
        an.add_config_args(parser)
        cfg = an.config_from_args(parser.parse_args(["--brake-allow-s", "0.9", "--cmd-topic", "/x"]))
        self.assertEqual((cfg.brake_allow_s, cfg.cmd_topic), (0.9, "/x"))


if __name__ == "__main__":
    unittest.main()
