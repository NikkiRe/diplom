"""Пульт: логика живого шлюза без ROS (live.py) — кольцевой буфер и подключение (то же, что stream.connect; resume
по номеру этого запуска; отставший клиент; скан мимо буфера, но на своём месте по t — живой поток проходит
check_connect; новый поток сверх предела вытесняет самый старый), перевод сообщений графа в поток: safety (req/out,
запятые, уровень, защёлка, 2 Гц; warnings — тихо, кроме кодов с warn), запрос исполнителя — из safety_state, стопы,
принятые safety_gate (counters.stop_now), молчание safety_state (P1), скорости, жест → первая команда → исход (только
если робот стоял в t_end), K1 от t_end (кольцо K1_KEEP), камера, скан по /tf_static, юниты, RSS и CPU из /proc,
сводка. Каждое сообщение проходит stream.check (Hub проверяет). Только stdlib, Python 3.8+."""
from __future__ import annotations

import math
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from neurobot.ui import live, stream  # noqa: E402
from neurobot.ui.live import _decode  # noqa: E402

T0 = 1_760_000_000.0


def decoded(frames):
    out = []
    for f in frames:
        if b"data: " in f:
            out.append(_decode(f))
    return out


def ids(frames):
    return [f.split(b"\n", 1)[0].decode()[4:] for f in frames if f.startswith(b"id: ")]


def safety(state="slow", reasons=("lidar_blind: перед в слепом секторе лидара — только глубина, ≤ 0.15 м/с",),
           **extra):
    doc = {"boot": "b", "seq": 1, "state": state, "reasons": list(reasons), "warnings": [], "min_clearance_m": 0.84,
           "front_m": 1.2, "limits": {"v_max": 0.15, "v_rev": 0.05, "w_max": 0.8}, "req": [0.25, 0.0],
           "out": [0.15, 0.0], "cut": True, "tilt_deg": 0.4, "lidar_mask_deg": [[-61.9, 4.9]],
           "estop": {"engaged": False, "since": None, "reasons": []}, "counters": {"stop_now": 0}}
    doc.update(extra)
    return doc


class Sink(live.Hub):
    """Hub, который ещё и помнит всё опубликованное (для проверок перевода)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.msgs = []

    def publish(self, msg):
        m = super().publish(msg)
        self.msgs.append(dict(m))
        return m

    def kind(self, k):
        return [m for m in self.msgs if m["k"] == k]


class HubTest(unittest.TestCase):
    def feed(self, hub, n=60, dt=10.0):
        for i in range(n):
            hub.publish({"k": "safety", "t": T0 + i * dt, "state": "slow", "reasons": [], "level": 0})
            if i == 20:
                hub.publish({"k": "alarm", "t": T0 + i * dt, "key": "shock", "code": "shock", "prio": 1, "on": True,
                             "text": "удар", "n": 1, "src": "safety"})
            if i == 25:
                hub.publish({"k": "gesture", "t": T0 + i * dt, "id": "g1", "kind": "gesture", "name": "come"})

    def test_ring_buffer_and_connect_equals_stream_connect(self):
        hub = live.Hub(buffer_s=100.0, boot="b00t")
        self.feed(hub)
        frames, cursor, _ = hub.attach({"source": "двойник", "mode": "live"}, now=T0 + 600)
        msgs = decoded(frames)
        self.assertEqual(stream.check_connect(msgs), len(msgs))
        # то же, что stream.connect по тем же сообщениям: вытесненное — в снимок
        allm = decoded([f for _, _, f in hub.buf])
        self.assertLessEqual(allm[-1]["t"] - allm[0]["t"], 100.0)
        head, rest = msgs[0], msgs[1:]
        self.assertEqual(head["boot"], "b00t")
        self.assertEqual([a["key"] for a in head["active"]], ["shock"])       # стоит с 200 с, давно вытеснена
        snap = rest[:len(rest) - len(allm)]
        self.assertEqual(rest[len(snap):], allm)
        self.assertEqual({m["k"] for m in snap}, {"safety", "alarm", "gesture"})
        self.assertEqual(cursor, hub.seq)
        self.assertTrue(all(i.startswith("b00t.") for i in ids(frames)))

    def test_resume_and_lagging_client(self):
        hub = live.Hub(buffer_s=100.0, boot="b00t")
        self.feed(hub, n=30)
        frames, cursor, gen = hub.attach({"source": "робот"}, now=T0 + 300)
        last = ids(frames)[-1]
        hub.publish({"k": "safety", "t": T0 + 301, "state": "blocked", "reasons": [], "level": 0})
        again, _, _ = hub.attach({"source": "робот"}, last_id=last)
        msgs = decoded(again)
        self.assertTrue(msgs[0]["resume"])
        self.assertEqual([m["state"] for m in msgs[1:]], ["blocked"])
        # номер другого запуска или вытесненный — как новое подключение
        for bad in ("other.5", "b00t.1", "b00t.x", ""):
            fresh, _, _ = hub.attach({"source": "робот"}, last_id=bad)
            self.assertNotIn("resume", decoded(fresh)[0], bad)
        out, cursor2, _ = hub.wait(cursor, gen, 0.01)
        self.assertEqual([m["state"] for m in decoded(out)], ["blocked"])
        self.assertEqual(hub.wait(cursor2, gen, 0.01)[0], [])
        for i in range(30):          # клиент спал, буфер ушёл вперёд — переподключиться
            hub.publish({"k": "safety", "t": T0 + 400 + i * 10, "state": "slow", "reasons": [], "level": 0})
        self.assertIsNone(hub.wait(cursor2, gen, 0.01)[0])

    def test_scan_and_hb_bypass_buffer(self):
        hub = live.Hub(boot="b")
        hub.publish({"k": "safety", "t": T0, "state": "ok", "reasons": [], "level": 0})
        hub.publish({"k": "scan", "t": T0 + 0.1, "a0": -120.0, "da": 1.0, "r": [1.5, None]})
        hub.publish({"k": "scan", "t": T0 + 0.2, "a0": -120.0, "da": 1.0, "r": [1.4, None]})
        self.assertEqual(len(hub.buf), 1)
        frames, cursor, gen = hub.attach({"source": "робот"})
        msgs = decoded(frames)
        self.assertEqual([m["k"] for m in msgs], ["hello", "safety", "scan"])    # только последний скан, без номера
        self.assertEqual(msgs[-1]["r"][0], 1.4)
        self.assertFalse(frames[-1].startswith(b"id:"))
        hub.publish({"k": "scan", "t": T0 + 0.3, "a0": -120.0, "da": 1.0, "r": [1.3]})
        out, _, gen2 = hub.wait(cursor, gen, 0.01)
        self.assertEqual(decoded(out)[0]["r"], [1.3])
        self.assertNotEqual(gen, gen2)

    def test_live_stream_passes_check_connect(self):
        # скан мимо буфера старше сообщений, пришедших после него: в начале потока и в хвосте он — на своём месте по t
        hub = live.Hub(boot="b")
        hub.publish({"k": "safety", "t": T0, "state": "slow", "reasons": [], "level": 0})
        hub.publish({"k": "scan", "t": T0 + 0.1, "a0": -120.0, "da": 1.0, "r": [1.5]})
        hub.publish({"k": "safety", "t": T0 + 0.2, "state": "slow", "reasons": [], "level": 0})
        frames, cursor, gen = hub.attach({"source": "двойник", "mode": "live"}, now=T0 + 0.3)
        msgs = decoded(frames)
        self.assertEqual([m["k"] for m in msgs], ["hello", "safety", "scan", "safety"])
        hub.publish({"k": "cmd", "t": T0 + 0.4, "topic": "out", "v": 0.1, "w": 0.0})
        hub.publish({"k": "scan", "t": T0 + 0.5, "a0": -120.0, "da": 1.0, "r": [1.4]})
        hub.publish({"k": "cmd", "t": T0 + 0.6, "topic": "out", "v": 0.0, "w": 0.0})
        out, cursor, gen = hub.wait(cursor, gen, 0.01)
        tail = decoded(out)
        self.assertEqual([m["k"] for m in tail], ["cmd", "scan", "cmd"])
        self.assertEqual(stream.check_connect(msgs + tail), len(msgs) + len(tail))
        # скан старше всего буфера и снимка — сразу после hello, а не в конце
        old = live.Hub(boot="c", buffer_s=1.0)
        old.publish({"k": "scan", "t": T0, "a0": -120.0, "da": 1.0, "r": [1.0]})
        for i in range(5):
            old.publish({"k": "safety", "t": T0 + 1 + i, "state": "ok", "reasons": [], "level": 0})
        m = decoded(old.attach({"source": "робот"}, now=T0 + 10)[0])
        self.assertEqual(stream.check_connect(m), len(m))

    def test_time_never_goes_back_and_format_enforced(self):
        hub = live.Hub()
        hub.publish({"k": "safety", "t": T0 + 5, "state": "ok", "reasons": [], "level": 0})
        m = hub.publish({"k": "units", "t": T0 + 4, "up": 1, "total": 1})
        self.assertEqual(m["t"], T0 + 5)
        with self.assertRaises(stream.FormatError):
            hub.publish({"k": "units", "t": T0 + 6, "up": "1", "total": 1})

    def test_wait_wakes_on_publish_and_new_client_evicts_oldest(self):
        hub = live.Hub(max_clients=2)
        (a, x), (b, y) = hub.join(), hub.join()
        self.assertEqual((x, y), (None, None))
        c, old = hub.join()                          # мест нет: самый старый (застрявший туннель) — вон
        self.assertIs(old, a)
        self.assertTrue(a.evicted and not b.evicted)
        self.assertEqual(hub.stats()["clients"], 2)
        self.assertIsNone(hub.wait(0, 0, 0.01, a)[0])   # его цикл выходит на ближайшем wait
        t = time.monotonic()
        threading.Timer(0.05, lambda: hub.join()).start()
        self.assertIsNone(hub.wait(hub.seq, hub.scan_gen, 2.0, b)[0])   # и из ожидания — сразу
        self.assertLess(time.monotonic() - t, 1.0)
        hub.leave(a)                                  # вытесненный уходит без вреда для чужих мест
        self.assertEqual(hub.stats()["clients"], 2)
        threading.Timer(0.05, lambda: hub.publish({"k": "hb", "t": T0})).start()      # hb не будит
        threading.Timer(0.1, lambda: hub.publish({"k": "end", "t": T0})).start()
        t = time.monotonic()
        out, _, _ = hub.wait(0, 0, 2.0)
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertEqual(decoded(out)[0]["k"], "end")


class SafetyTest(unittest.TestCase):
    def setUp(self):
        self.hub = Sink()
        self.lv = live.Live(self.hub, clock=lambda: T0)

    def test_message_and_rate(self):
        self.lv.on_safety(safety(), T0 + 1)
        (m,) = self.hub.kind("safety")
        self.assertEqual(m["reasons"][0], "lidar_blind: перед в слепом секторе лидара — только глубина, ≤ 0,15 м/с")
        self.assertEqual((m["level"], m["req"], m["out"], m["cut"]), (0, {"v": 0.25, "w": 0.0}, {"v": 0.15, "w": 0.0},
                                                                        True))
        self.assertNotIn("estop", m)                        # защёлки нет — поля нет, как в «Записи»
        self.assertEqual(self.hub.kind("alarm"), [])          # slow с lidar_blind — норма
        for i in range(1, 10):                                # 5 Гц без перемен → 2 Гц
            self.lv.on_safety(safety(), T0 + 1 + i * 0.2)
        self.assertEqual(len(self.hub.kind("safety")), 4)
        self.lv.on_safety(safety("blocked", ["obstacle: препятствие в 0.06 м"]), T0 + 3.05)   # смена — сразу
        m = self.hub.kind("safety")[-1]
        self.assertEqual((m["state"], m["level"], m["reasons"]), ("blocked", 2, ["obstacle: препятствие в 0,06 м"]))
        (a,) = self.hub.kind("alarm")
        self.assertEqual((a["key"], a["prio"], a["on"], a["src"]), ("obstacle", 2, True, "safety"))

    def test_estop_and_warnings(self):
        doc = safety("estop", ["estop: защёлка (shock: удар 7.2 м/с²)"],
                     warnings=["override: есть издатели /cmd_vel_joy", "mask_outside: лучи -40…-30°"],
                     estop={"engaged": True, "since": T0 - 30, "reasons": ["shock: удар 7.2 м/с²"]})
        self.lv.on_safety(doc, T0)
        m = self.hub.kind("safety")[-1]
        self.assertEqual(m["estop"], {"engaged": True, "since": T0 - 30, "reasons": ["shock"]})
        self.assertEqual(m["level"], 1)
        # override — громкое предупреждение (warn), mask_outside — тихо: в safety.warnings, без тревоги
        self.assertEqual({a["key"] for a in self.hub.kind("alarm")}, {"estop", "override"})
        self.assertIn("mask_outside: лучи -40…-30°", m["warnings"])
        self.assertEqual(self.lv.state(), "estop")

    def test_quiet_warning_keeps_norm(self):
        self.lv.on_safety(safety(warnings=["mask_outside: лучи -40…-30°", "lidar_unknown: 12…14°"]), T0)
        m = self.hub.kind("safety")[-1]
        self.assertEqual(m["level"], 0)                       # вечная плашка P2 на всё время процесса — нет
        self.assertEqual(self.hub.kind("alarm"), [])

    def test_req_from_safety_state(self):
        self.lv.on_safety(safety(), T0 + 1)
        (c,) = [m for m in self.hub.kind("cmd") if m["topic"] == "req"]
        self.assertEqual((c["v"], c["w"]), (0.25, 0.0))
        for i in range(1, 5):                                 # 5 Гц без перемен — keepalive раз в 1 с
            self.lv.on_safety(safety(), T0 + 1 + i * 0.2)
        self.assertEqual(len([m for m in self.hub.kind("cmd") if m["topic"] == "req"]), 1)

    def test_stop_confirmed_by_counter(self):
        self.assertIsNone(self.lv.stop_mark())                # safety_state ещё не было — подтвердить нечем
        self.assertFalse(self.lv.stop_taken(None))
        self.lv.on_safety(safety(counters={"stop_now": 3}), T0)
        mark = self.lv.stop_mark()
        self.assertEqual(mark, ("b", 3))
        self.lv.on_safety(safety(counters={"stop_now": 3}), T0 + 0.2)
        self.assertFalse(self.lv.stop_taken(mark))
        self.lv.on_safety(safety(counters={"stop_now": 4}), T0 + 0.4)
        self.assertTrue(self.lv.stop_taken(mark))
        self.lv.on_safety(safety(boot="b2", counters={"stop_now": 1}), T0 + 0.6)   # safety_gate перезапущен
        self.assertTrue(self.lv.stop_taken(mark))
        self.lv.on_safety(safety(boot="b3", counters={"stop_now": 0}), T0 + 0.8)
        self.assertFalse(self.lv.stop_taken(mark))

    def test_silence_is_p1_and_clears_at_once(self):
        self.lv.tick(T0 + 2.0)                                # старт: граф собирается
        self.assertEqual(self.hub.kind("alarm"), [])
        self.lv.tick(T0 + 3.5)
        (a,) = self.hub.kind("alarm")
        self.assertEqual((a["key"], a["prio"], a["src"], a["on"]), ("ui_safety_silent", 1, "link", True))
        self.lv.on_safety(safety(), T0 + 4.0)
        self.assertFalse(self.hub.kind("alarm")[-1]["on"])
        self.lv.tick(T0 + 4.5)
        self.lv.tick(T0 + 5.01)                               # 1,01 с без safety_state
        self.assertEqual([a["on"] for a in self.hub.kind("alarm")], [True, False, True])
        self.assertEqual(self.hub.kind("alarm")[-1]["n"], 2)

    def test_garbage_is_ignored(self):
        self.lv.on_safety({"state": "fast"}, T0)
        self.lv.on_safety(safety(min_clearance_m="далеко", req=[1, "x"]), T0 + 1)
        (m,) = self.hub.kind("safety")
        self.assertNotIn("min_clearance_m", m)
        self.assertNotIn("req", m)


class CmdAndGestureTest(unittest.TestCase):
    def setUp(self):
        self.hub = Sink()
        self.lv = live.Live(self.hub, clock=lambda: T0)
        self.lv.on_cmd("out", 0.0, 0.0, T0)

    def gesture(self, t, name="go_left", t_end=None, seq=1):
        self.lv.on_gesture({"boot": "p", "seq": seq, "cls": 2, "name": name, "conf": 0.91, "margin": 0.3,
                            "side": "left", "n_boxes": 14, "t_end": t_end or t - 0.12, "t_rec": t}, t)

    def test_cmd_rate_zero_and_keepalive(self):
        for i in range(10):                      # разгон 20 Гц: изменение — не чаще 10 Гц
            self.lv.on_cmd("out", 0.01 * (i + 1), 0.0, T0 + 0.05 * (i + 1))
        outs = [m for m in self.hub.kind("cmd") if m["topic"] == "out"]
        self.assertLessEqual(len(outs), 7)
        self.assertEqual(outs[-1]["d_stop_m"] > 0.05, True)
        self.lv.on_cmd("out", 0.0, 0.0, T0 + 0.52)              # ноль — сразу
        self.assertEqual(self.hub.kind("cmd")[-1]["v"], 0.0)
        n = len(self.hub.kind("cmd"))
        self.lv.on_cmd("out", 0.0, 0.0, T0 + 0.9)
        self.assertEqual(len(self.hub.kind("cmd")), n)
        self.lv.on_cmd("out", 0.0, 0.0, T0 + 1.53)              # раз в 1 с, пока писатель жив
        self.assertEqual(len(self.hub.kind("cmd")), n + 1)
        self.lv.on_cmd("req", float("nan"), 0.0, T0 + 2)
        self.assertEqual(self.hub.kind("cmd")[-1]["topic"], "out")

    def test_gesture_to_first_command_and_done(self):
        self.gesture(T0 + 10.0)
        self.lv.on_cmd("out", 0.15, 0.0, T0 + 10.21)
        upd = self.hub.kind("gesture")
        self.assertEqual(upd[0]["ref"], "t_end")
        self.assertAlmostEqual(upd[0]["d_rec_s"], 0.12, places=3)
        self.assertAlmostEqual(upd[1]["d_cmd_s"], 0.33, places=3)        # от t_end, не от t_rec
        (k1,) = self.hub.kind("k1")
        self.assertEqual((k1["ref"], k1["last"]["n"], k1["run"]["p50"]), ("t_end", 1, 0.33))
        self.lv.on_cmd("out", 0.0, 0.0, T0 + 12.0)
        self.lv.tick(T0 + 12.3)
        self.assertNotIn("outcome", self.hub.kind("gesture")[-1])
        self.lv.tick(T0 + 12.6)
        self.assertEqual(self.hub.kind("gesture")[-1]["outcome"], "done")
        self.assertEqual(self.lv.stats(T0 + 13)["k1"]["n"], 1)

    def test_command_before_gesture_message(self):
        # DriveController кладёт манёвр раньше, чем perception публикует событие
        self.lv.on_cmd("out", 0.15, 0.0, T0 + 10.004)
        self.gesture(T0 + 10.01, t_end=T0 + 9.9)
        self.assertAlmostEqual(self.hub.kind("gesture")[1]["d_cmd_s"], 0.104, places=3)

    def test_no_reaction_unknown_and_moving(self):
        self.gesture(T0 + 10.0)
        self.lv.tick(T0 + 13.5)
        self.assertEqual(self.hub.kind("gesture")[-1]["outcome"], "none")
        self.gesture(T0 + 20.0, name="unknown", seq=2)
        self.assertEqual(self.hub.kind("gesture")[-1]["outcome"], "none")
        self.lv.on_cmd("out", 0.1, 0.0, T0 + 25.0)                      # робот уже едет: первая команда — не его
        self.gesture(T0 + 30.0, seq=3)
        self.lv.tick(T0 + 33.5)
        last = [m for m in self.hub.kind("gesture") if m["id"] == "p:3"]
        self.assertEqual([m.get("outcome") for m in last], [None, "unobserved"])
        self.assertFalse(any("d_cmd_s" in m for m in last))
        self.assertEqual(self.hub.kind("k1"), [])

    def test_cut_by_safety_and_preempted(self):
        self.gesture(T0 + 10.0)
        self.lv.on_cmd("out", 0.15, 0.0, T0 + 10.2)
        self.lv.on_event({"type": "task_end", "task": "gesture", "status": "blocked",
                          "reasons": ["obstacle: препятствие 0.1 м"]}, T0 + 11)
        m = self.hub.kind("gesture")[-1]
        self.assertEqual((m["outcome"], m["stopped_by"]), ("cut", ["obstacle"]))
        self.lv.on_cmd("out", 0.0, 0.0, T0 + 11.1)
        self.gesture(T0 + 20.0, seq=2)
        self.lv.on_event({"type": "task_end", "task": "gesture", "status": "preempted"}, T0 + 20.5)
        self.assertEqual(self.hub.kind("gesture")[-1]["outcome"], "preempted")
        self.lv.on_event({"type": "escape", "status": "start"}, T0 + 21)        # не исход жеста
        # жест 1: начало, команда, исход; жест 2: начало, исход
        self.assertEqual(len(self.hub.kind("gesture")), 5)

    def test_k1_is_a_ring(self):
        for i in range(live.K1_KEEP + 50):
            self.lv._cmd({"id": "g%d" % i, "t_end": T0, "name": "come"}, T0 + 0.3 + (i % 7) * 0.01, T0 + 1)
        self.assertEqual(len(self.lv.k1), live.K1_KEEP)      # шлюз работает неделями: список не растёт
        k1 = self.hub.kind("k1")[-1]
        self.assertEqual((k1["run"]["n"], k1["run_window"], k1["last"]["n"]), (live.K1_KEEP, live.K1_KEEP, 20))

    def test_stats(self):
        self.lv.on_safety(safety("ok", []), T0 + 1)
        self.lv.on_safety(safety("ok", []), T0 + 1.5)
        self.lv.on_safety(safety("blocked", ["obstacle: x"]), T0 + 2.0)
        self.lv.on_safety(safety("blocked", ["obstacle: x"]), T0 + 2.5)
        self.lv.on_safety(safety("blocked", ["obstacle: x"]), T0 + 10.0)    # молчание 7,5 с не в счёт
        self.lv.on_cmd("out", 0.1, 0.0, T0 + 3)
        self.lv.on_cmd("out", 0.1, 0.0, T0 + 3.5)
        self.lv.on_cmd("out", 0.0, 0.0, T0 + 4)
        s = self.lv.stats(T0 + 10)
        self.assertEqual(s["safety_share"], {"ok": 0.6667, "slow": 0.0, "blocked": 0.3333, "estop": 0.0})
        self.assertEqual(s["moving_s"], 1.0)
        stream.check(s)
        self.lv.tick(T0 + 10.5)
        self.assertEqual(len(self.hub.kind("stats")), 1)


class HealthUnitsMemTest(unittest.TestCase):
    def setUp(self):
        self.hub = Sink()
        self.lv = live.Live(self.hub, not_robot=["driver@twin"], clock=lambda: T0)

    def test_health(self):
        self.lv.on_health({"fps": 14.9, "frame_age_s": 0.07, "recoveries": 0, "state": "ok", "dark": False}, T0)
        self.lv.on_health({"fps": 15.0, "frame_age_s": 0.06, "recoveries": 0, "state": "ok", "dark": False}, T0 + 1)
        self.assertEqual(len(self.hub.kind("health")), 1)
        self.lv.on_health({"fps": 0.0, "frame_age_s": 2.5, "recoveries": 1, "state": "recovering"}, T0 + 2)
        self.assertEqual({a["key"] for a in self.hub.kind("alarm")}, {"ui_perception_recovering", "ui_camera_lost"})
        self.lv.on_health({"fps": 14.0, "frame_age_s": 0.1, "recoveries": 1, "state": "ok"}, T0 + 6)
        self.assertEqual([a["on"] for a in self.hub.kind("alarm")], [True, True, False, False])

    def test_units(self):
        rows = [{"comp": "safety", "unit": "neurobot-safety.service", "active": "active", "sub": "running",
                 "restarts": 0},
                {"comp": "perception", "unit": "neurobot-perception.service", "active": "activating",
                 "sub": "auto-restart", "restarts": 7}]
        self.lv.on_units(rows, T0)
        (u,) = self.hub.kind("units")
        self.assertEqual((u["up"], u["total"], u["down"], u["restarts"]), (1, 2, ["perception"],
                                                                           {"safety": 0, "perception": 7}))
        (a,) = self.hub.kind("alarm")
        self.assertEqual((a["key"], a["prio"]), ("ui_unit_down:perception", 2))
        rows[1].update(active="active", sub="running", restarts=8)
        self.lv.on_units(rows, T0 + 5)
        self.assertFalse(self.hub.kind("alarm")[-1]["on"])           # конец «не работает» известен точно

    def test_mem_and_cpu(self):
        self.lv.on_mem({"safety": (65536, 1000, 1), "driver@twin": (512000, 9000, 3)}, T0, 100)
        self.lv.on_mem({"safety": (66560, 1050, 1), "driver@twin": (512000, 19000, 3)}, T0 + 10, 100)
        m1, m2 = self.hub.kind("mem")
        self.assertEqual((m1["total_mib"], m1["metric"], m1["units"]["driver@twin"]), (64.0, "rss", 500.0))
        self.assertNotIn("cpu_pct", m1)
        self.assertEqual((m2["total_mib"], m2["cpu_pct"]), (65.0, 5.0))      # Gazebo двойника не в счёте


class ScanTest(unittest.TestCase):
    def scan(self, lv, frame="laser_frame", t=T0):
        n = 683
        a0, inc = -2.0923497677, 4.1846995354 / 682
        ranges = [3.0] * n
        ranges[100:110] = [math.nan] * 10
        ranges[200] = math.inf
        ranges[300] = 0.01                                             # код ошибки URG ниже range_min
        lv.on_scan(frame, a0, inc, ranges, 0.02, 5.6, t)

    def test_bins_in_base_frame(self):
        hub = Sink()
        lv = live.Live(hub, clock=lambda: T0)
        self.scan(lv)
        self.assertEqual(hub.kind("scan"), [])                         # без /tf_static не рисуем
        lv.on_tf_static([("base_link", "laser_frame", (0.1, 0.0, 0.67), (0.0, 0.0, 0.0, 1.0))])
        self.scan(lv, t=T0 + 1)
        (s,) = hub.kind("scan")
        self.assertEqual((s["da"], s["frame"]), (1.0, "base_link"))
        self.assertLessEqual(len(s["r"]), 242)
        hits = [r for r in s["r"] if r is not None]
        self.assertAlmostEqual(max(hits), 3.1, places=2)               # вперёд: 3 м от лидара + 0,1 м до центра
        self.assertTrue(min(hits) > 2.9)
        self.assertIsNone(s["r"][int(round((math.degrees(-2.0923497677 + 104 * 4.1846995354 / 682) - s["a0"])))])
        self.scan(lv, t=T0 + 1.1)                                      # 5 Гц
        self.assertEqual(len(hub.kind("scan")), 1)

    def test_upside_down_lidar_mirrors(self):
        tf = live.Tf()
        tf.add("base_link", "mast", (0.0, 0.0, 0.5), (0.0, 0.0, 0.0, 1.0))
        tf.add("mast", "laser", (0.0, 0.0, 0.17), (1.0, 0.0, 0.0, 0.0))     # крен 180°: «лево» лидара — право базы
        p = tf.lookup("base_link", "laser")((0.0, 1.0, 0.0))
        self.assertAlmostEqual(p[1], -1.0)
        self.assertAlmostEqual(p[2], 0.67)
        self.assertIsNone(tf.lookup("base_link", "camera"))
        p = live.Tf()
        p.add("a", "b", (0, 0, 0), (0, 0, 0, 1))
        p.add("b", "a", (0, 0, 0), (0, 0, 0, 1))
        self.assertIsNone(p.lookup("base_link", "a"))                   # цикл не вешает

    def test_quarter_turn(self):
        f = live._rotate((0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4)), (1.0, 0.0, 0.0))
        self.assertAlmostEqual(f[0], 0.0)
        self.assertAlmostEqual(f[1], 1.0)


class ProcTest(unittest.TestCase):
    def test_rss_and_cpu_by_unit(self):
        with tempfile.TemporaryDirectory() as proc:
            def put(pid, cgroup, rss_pages, utime, stime):
                os.makedirs(os.path.join(proc, pid))
                with open(os.path.join(proc, pid, "cgroup"), "w") as f:
                    f.write(cgroup)
                with open(os.path.join(proc, pid, "statm"), "w") as f:
                    f.write("1000 %d 100 1 0 50 0\n" % rss_pages)
                with open(os.path.join(proc, pid, "stat"), "w") as f:
                    f.write("%s (python3 (x)) S 1 2 3 4 5 6 7 8 9 10 %d %d 0 0 20 0 1 0\n" % (pid, utime, stime))
            put("100", "0::/system.slice/neurobot-safety.service\n", 1000, 50, 10)
            put("101", "0::/system.slice/neurobot-driver@lidar.service\n", 500, 5, 5)
            put("102", "12:pids:/system.slice/neurobot-driver@lidar.service\n1:name=systemd:/system.slice/"
                       "neurobot-driver@lidar.service\n", 500, 1, 1)                     # cgroup v1 (Nano)
            put("103", "0::/user.slice/user-1000.slice/session-2.scope\n", 9999, 1, 1)    # не наш
            os.makedirs(os.path.join(proc, "104"))                                        # вышел между чтениями
            r = live.ProcReader({"neurobot-safety.service": "safety", "neurobot-driver@lidar.service": "driver@lidar"},
                                proc)
            r.page_kib = 4
            self.assertEqual(r.sample(), {"safety": (4000, 60, 1), "driver@lidar": (4000, 12, 2)})


class QuantilesTest(unittest.TestCase):
    def test_same_as_metrics(self):
        self.assertEqual(live.quantiles([]), {"n": 0})
        self.assertEqual(live.quantiles([0.3, 0.1, 0.2, None]), {"n": 3, "p50": 0.2, "p95": 0.29, "max": 0.3})
        try:
            from neurobot.sim.runs import metrics
        except ImportError:
            self.skipTest("нет neurobot.sim.runs")
        vals = [0.61, 0.33, 0.52, 0.48, 1.2, 0.7, 0.66]
        self.assertEqual(live.quantiles(vals), metrics.quantiles(vals))


if __name__ == "__main__":
    unittest.main()
