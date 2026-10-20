"""predict_live v2: эпизод жеста, END_GAP, «не распознано», сторона и программа (gesture_logic.py); H10 — край
кадра и доля обрезки, гистерезис уверенности, решение с двумя порогами отрыва (Recognizer).

Без torch и rclpy, Python ≥ 3.8. Рамки — синтетические: темп детекции 8 к/с (шаг 1/8 с точен в двоичной
записи — границы END_GAP и окна проверяются без погрешности float), кадр 640×480.
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

PERCEPTION = Path(__file__).resolve().parents[1] / "perception"
if str(PERCEPTION) not in sys.path:
    sys.path.insert(0, str(PERCEPTION))

from gesture_logic import (Box, EpisodeSegmenter, Recognizer, decide, path_length,  # noqa: E402
                           program_for, side_of)

W = 640
DT = 0.125  # 8 к/с


def hand(cx: float, cy: float, size: float = 80.0) -> tuple:
    return cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2


def run(seg: EpisodeSegmenter, frames, t0: float = 0.0):
    """frames — рамки или None по кадрам; [(время кадра, эпизод)] для кадров, где эпизод кончился."""
    out = []
    for i, box in enumerate(frames):
        t = t0 + i * DT
        ep = seg.update(t, box)
        if ep is not None:
            out.append((t, ep))
    return out


def wave(n: int, amp: float = 150.0) -> list:
    """Горизонтальное махание: n кадров, центр ходит синусом вокруг середины кадра."""
    return [hand(W / 2 + amp * math.sin(2 * math.pi * i / 10), 240) for i in range(n)]


class Segmenter(unittest.TestCase):
    def test_end_after_end_gap_without_hand(self):
        seg = EpisodeSegmenter(W)
        events = run(seg, wave(10) + [None] * 10)
        self.assertEqual(len(events), 1)
        t, ep = events[0]
        self.assertTrue(ep.accepted)
        self.assertEqual(ep.t_end, 1.125)
        self.assertEqual(t - ep.t_end, 0.5)  # демо ждало 1,5 с (L1)
        self.assertEqual(len(ep.boxes), 10)
        self.assertEqual(len(seg), 0)

    def test_short_dropout_does_not_split_gesture(self):
        events = run(EpisodeSegmenter(W), wave(6) + [None] * 3 + wave(6) + [None] * 6)  # провал 0,375 с
        self.assertEqual([len(ep.boxes) for _, ep in events], [12])

    def test_buffer_always_cleared_even_when_rejected(self):
        seg = EpisodeSegmenter(W)
        (t, ep), = run(seg, [hand(100, 100), hand(500, 100)] + [None] * 6)
        self.assertFalse(ep.accepted)  # 2 рамки < min_boxes (G3: обрывок не прилипает к следующему жесту)
        self.assertEqual(len(seg), 0)
        (t, ep), = run(seg, wave(10) + [None] * 6, t0=10.0)
        self.assertEqual(len(ep.boxes), 10)

    def test_still_hand_bounded_and_rejected(self):
        seg = EpisodeSegmenter(W)
        jitter = [hand(320 + (3 if i % 2 else -3), 240 + (2 if i % 3 else -2)) for i in range(600)]  # 75 с
        sizes = []
        for i, box in enumerate(jitter):
            self.assertIsNone(seg.update(i * DT, box))
            sizes.append(len(seg))
        self.assertEqual(max(sizes), 65)  # окно 8 с при 8 к/с (G4)
        ep = seg.update(599 * DT + 0.5, None)
        self.assertFalse(ep.accepted)  # стоящая рука — не жест
        self.assertEqual(ep.path, 0.0)

    def test_max_boxes_bound(self):
        seg = EpisodeSegmenter(W, max_boxes=8)
        for i in range(100):
            seg.update(i * 0.001, hand(320, 240))
        self.assertEqual(len(seg), 8)

    def test_slow_gesture_kept_whole(self):
        # R7: у лаборатории жест длится 3–7,4 с (GIF слайда 12) — окно по умолчанию его не обрезает
        (t, ep), = run(EpisodeSegmenter(W), wave(60) + [None] * 6)  # 7,5 с
        self.assertEqual(len(ep.boxes), 60)
        self.assertTrue(ep.accepted)

    def test_window_keeps_last_seconds(self):
        (t, ep), = run(EpisodeSegmenter(W, window=3.0), wave(50) + [None] * 6)
        self.assertEqual(len(ep.boxes), 25)
        self.assertEqual(ep.boxes[0].t, 3.125)

    def test_min_path(self):
        seg = EpisodeSegmenter(W, min_path=0.2)  # 128 px (по умолчанию 0,1)
        (t, ep), = run(seg, [hand(300 + 11 * i, 240) for i in range(12)] + [None] * 6)
        self.assertAlmostEqual(ep.path, 110.0)  # шаг 11 px < мёртвой зоны 12,8 px — засчитывается через раз
        self.assertFalse(ep.accepted)
        (t, ep), = run(seg, [hand(300 + 13 * i, 240) for i in range(12)] + [None] * 6, t0=10.0)
        self.assertAlmostEqual(ep.path, 143.0)
        self.assertTrue(ep.accepted)

    def test_end_on_still_flag(self):
        stroke = [hand(200 + 30 * i, 240) for i in range(10)] + [hand(470, 240)] * 10
        self.assertEqual(run(EpisodeSegmenter(W), stroke), [])  # по умолчанию рука в кадре — жест не кончился
        (t, ep), = run(EpisodeSegmenter(W, end_on_still=True), stroke)
        self.assertTrue(ep.accepted)
        self.assertEqual(t, 9 * DT + 0.5)  # центр замер на кадре 9, END_GAP спустя — конец

    def test_expire_without_frames_drops_episode(self):
        # отказ камеры сразу после жеста: без кадров END_GAP не подтвердить — эпизод отбрасывается, а не
        # распознаётся через секунды, когда кадры вернутся (прогон стенда 15.10: манёвр через 4,4 с после жеста)
        seg = EpisodeSegmenter(W)
        run(seg, wave(10))                      # последняя рамка — 1,125 с
        self.assertFalse(seg.expire(1.5))       # 0,375 с < END_GAP — ещё ждём
        self.assertEqual(len(seg), 10)
        self.assertTrue(seg.expire(1.625))      # END_GAP без кадра
        self.assertEqual(len(seg), 0)
        self.assertEqual(run(seg, [None] * 10, t0=5.5), [])  # кадры вернулись — жест не всплывает
        self.assertFalse(seg.expire(9.0))       # пустой сегментатор нечего отбрасывать

    def test_reset_drops_boxes(self):
        seg = EpisodeSegmenter(W)
        run(seg, wave(5))
        seg.reset()
        self.assertEqual(len(seg), 0)
        self.assertEqual(run(seg, [None] * 10, t0=1.0), [])

    def test_invalid_params(self):
        for kw in ({"end_gap": 0}, {"window": 0.4}, {"min_boxes": 0}, {"min_path": -1}, {"max_boxes": 2},
                   {"edge_gap": 0.3}, {"edge_gap": 8.0}, {"edges": ("up",)}, {"edge_frac": 0.5},
                   {"start_conf": 0.35, "keep_conf": 0.5}, {"start_conf": 1.5}, {"gate": 0}):
            with self.assertRaises(ValueError, msg=str(kw)):
                EpisodeSegmenter(W, **kw)


def top(n: int, cx: float = 320) -> list:
    """Рука у верхнего края (рамка обрезана кадром: y1 = 0), n кадров с ходом вправо."""
    return [(cx + 20 * i, 0.0, cx + 20 * i + 80, 50.0) for i in range(n)]


class EdgeAndCut(unittest.TestCase):
    """H10 (1): рука ушла за край — ждём edge_gap; доля времени за краем — cut."""

    def test_hand_left_through_top_waits_edge_gap(self):
        seg = EpisodeSegmenter(W, edge_gap=1.0)
        (t, ep), = run(seg, wave(8) + top(2) + [None] * 10)  # за краем 1,25 с: дольше edge_gap
        self.assertEqual((ep.end, ep.t_end), ("edge", 1.125))
        self.assertEqual(t - ep.t_end, 1.0)  # а не END_GAP 0,5
        self.assertAlmostEqual(ep.cut, 1.0 / 2.125)  # ожидание за краем к времени эпизода с ним

    def test_return_within_edge_gap_continues_and_counts_cut(self):
        seg = EpisodeSegmenter(W, edge_gap=1.0)
        (t, ep), = run(seg, wave(8) + top(2) + [None] * 6 + wave(8) + [None] * 4)  # за краем 0,875 с
        self.assertEqual((ep.end, len(ep.boxes)), ("gap", 18))
        self.assertAlmostEqual(ep.cut, 0.875 / (23 * DT))  # эпизод — от первой рамки до последней

    def test_inside_frame_and_other_edges_keep_end_gap(self):
        (t, ep), = run(EpisodeSegmenter(W, edge_gap=1.0), wave(10) + [None] * 10)
        self.assertEqual((ep.end, t - ep.t_end), ("gap", 0.5))
        right = [(560.0 + 2 * i, 200.0, 640.0, 280.0) for i in range(10)]
        (t, ep), = run(EpisodeSegmenter(W, edge_gap=1.0), wave(4) + right + [None] * 10)  # у правого края
        self.assertEqual(t - ep.t_end, 0.5)
        (t, ep), = run(EpisodeSegmenter(W, edge_gap=1.0, edges=("right",)), wave(4) + right + [None] * 10)
        self.assertEqual((ep.end, t - ep.t_end), ("edge", 1.0))

    def test_off_by_default(self):
        (t, ep), = run(EpisodeSegmenter(W), wave(8) + top(2) + [None] * 10)
        self.assertEqual((ep.end, t - ep.t_end, ep.cut), ("gap", 0.5, 0.0))

    def test_expire_waits_edge_gap(self):
        seg = EpisodeSegmenter(W, edge_gap=1.0)
        run(seg, wave(8) + top(2))
        self.assertFalse(seg.expire(1.125 + 0.75))
        self.assertTrue(seg.expire(1.125 + 1.0))


class Hysteresis(unittest.TestCase):
    """H10 (2): слабая рамка продолжает эпизод только у прогноза по постоянной скорости, начать и продлить его не может:
    конец — END_GAP после последней уверенной рамки."""

    def seg(self):
        return EpisodeSegmenter(W, start_conf=0.35, keep_conf=0.1)

    def test_weak_box_cannot_start(self):
        seg = self.seg()
        for i, box in enumerate(wave(10)):
            self.assertIsNone(seg.update(i * DT, box, 0.2))
        self.assertEqual(len(seg), 0)
        self.assertIsNone(seg.update(10 * DT, hand(320, 240), 0.05))  # ниже keep_conf — вовсе не рамка

    def test_weak_box_near_prediction_continues(self):
        seg = self.seg()
        line = [hand(100 + 40 * i, 240) for i in range(16)]
        for i, box in enumerate(line):
            weak = 6 <= i < 9  # провал уверенности посреди жеста короче END_GAP: слабые рамки — точки траектории
            self.assertIsNone(seg.update(i * DT, box, 0.2 if weak else 0.8))
        self.assertEqual(len(seg), 16)
        self.assertEqual([b.conf for b in seg._boxes][5:10], [0.8, 0.2, 0.2, 0.2, 0.8])

    def test_weak_boxes_do_not_extend_episode(self):
        # провал уверенности дольше END_GAP: эпизод кончается, как без гистерезиса (слабых рамок детектор не дал бы)
        line = [hand(100 + 40 * i, 240) for i in range(16)]
        confs = [0.2 if 6 <= i < 11 else 0.8 for i in range(16)]
        seg, plain = self.seg(), EpisodeSegmenter(W)
        got = [(i * DT, seg.update(i * DT, box, c)) for i, (box, c) in enumerate(zip(line, confs))]
        ref = [(i * DT, plain.update(i * DT, box if c > 0.35 else None)) for i, (box, c) in enumerate(zip(line, confs))]
        (t, ep), = [(t, ep) for t, ep in got if ep is not None]
        (t_ref, ep_ref), = [(t, ep) for t, ep in ref if ep is not None]
        self.assertEqual((t, ep.end), (t_ref, "gap"))
        self.assertEqual(t, 5 * DT + 0.5)  # END_GAP после последней уверенной рамки
        self.assertEqual((len(ep.boxes), len(ep_ref.boxes)), (9, 6))  # слабые рамки — в траектории

    def test_still_weak_box_after_gesture_ends_episode(self):
        # ревью 20.10: после жеста на месте последней рамки стоит слабая ложная рамка (лицо, рукав, опущенная рука) —
        # без границы эпизод не кончался 6 с и копил её точки; теперь конец — END_GAP после последней уверенной
        seg = self.seg()
        events = []
        for i in range(12 + 48):  # жест 1,5 с, затем 6 с неподвижной рамки 0,15
            box, conf = (hand(100 + 30 * i, 240), 0.8) if i < 12 else (hand(100 + 30 * 11, 240), 0.15)
            ep = seg.update(i * DT, box, conf)
            if ep is not None:
                events.append((i * DT, ep))
        (t, ep), = events
        self.assertEqual((t, ep.end, ep.t_end <= t), (11 * DT + 0.5, "gap", True))
        self.assertLessEqual(len(ep.boxes), 12 + int(0.5 / DT))
        self.assertEqual(len(seg), 0)  # слабая рамка нового эпизода не начинает
        seg = self.seg()
        for i in range(15):
            seg.update(i * DT, hand(100 + 30 * min(i, 11), 240), 0.8 if i < 12 else 0.15)
        self.assertEqual(len(seg), 15)
        self.assertFalse(seg.expire(14 * DT + 0.05))
        self.assertTrue(seg.expire(11 * DT + 0.5))  # нет кадров — тоже от последней уверенной рамки

    def test_weak_box_far_from_prediction_is_a_miss(self):
        seg = self.seg()
        events = []
        for i, box in enumerate([hand(100 + 40 * i, 240) for i in range(6)] + [hand(600, 60)] * 6):
            ep = seg.update(i * DT, box, 0.8 if i < 6 else 0.2)  # слабая рамка в другом углу кадра
            if ep is not None:
                events.append(ep)
        self.assertEqual([len(ep.boxes) for ep in events], [6])

    def test_off_by_default(self):
        seg = EpisodeSegmenter(W)
        seg.update(0.0, hand(320, 240), 0.01)
        self.assertEqual(len(seg), 1)  # порог — у детектора (model.conf), сегментатор берёт всё


class Classifier:
    """Подставная SNN: суммы спайков по классам и счётчик вызовов."""

    def __init__(self, counts):
        self.counts, self.calls = counts, 0

    def __call__(self, boxes):
        self.calls += 1
        return self.counts


class TwoThresholds(unittest.TestCase):
    """H10 (3): уверенный жест — решение спустя early_gap без руки внутри кадра; иначе — по концу, без второй SNN."""

    STEPS = 125

    def recognizer(self, counts, **kw):
        snn = Classifier(counts)
        seg = EpisodeSegmenter(W, edge_gap=1.0)
        return Recognizer(seg, snn, self.STEPS, 0.04, **dict({"early_margin": 0.9, "early_gap": 0.25}, **kw)), snn

    def steps(self, rec, frames):
        out = []
        for i, box in enumerate(frames):
            res = rec.step(i * DT, box)
            if res is not None:
                out.append((i * DT, res))
        return out

    def test_confident_gesture_decided_early(self):
        rec, snn = self.recognizer([0, 125, 0, 0])
        (t, res), = self.steps(rec, wave(10) + [None] * 6)
        self.assertEqual((res.end, res.decision.cls, snn.calls), ("early", 1, 1))
        self.assertEqual(t - res.episode.t_end, 0.25)  # а не END_GAP 0,5
        self.assertEqual(len(rec.segmenter), 0)

    def test_not_confident_waits_end_without_second_snn(self):
        rec, snn = self.recognizer([0, 60, 10, 0])  # отрыв 0,4 < 0,9
        (t, res), = self.steps(rec, wave(10) + [None] * 6)
        self.assertEqual((res.end, res.decision.cls, t - res.episode.t_end), ("gap", 1, 0.5))
        self.assertEqual(snn.calls, 1)  # рамок не прибавилось — спайки прошлого прогона

    def test_hand_back_after_probe_reclassifies(self):
        rec, snn = self.recognizer([0, 60, 10, 0])
        (t, res), = self.steps(rec, wave(6) + [None] * 3 + wave(6) + [None] * 6)
        self.assertEqual((len(res.episode.boxes), snn.calls), (12, 2))

    def test_no_early_decision_at_edge(self):
        rec, snn = self.recognizer([0, 125, 0, 0])
        (t, res), = self.steps(rec, wave(8) + top(2) + [None] * 10)
        self.assertEqual((res.end, t - res.episode.t_end), ("edge", 1.0))

    def test_strong_cut_is_unknown_without_snn(self):
        rec, snn = self.recognizer([0, 125, 0, 0], max_cut=0.4)
        (t, res), = self.steps(rec, wave(4) + top(2) + [None] * 10)  # видно 0,625 с, за краем — 1 с
        self.assertEqual((res.end, res.decision.cls, snn.calls), ("cut", -1, 0))

    def test_rejected_episode_has_no_decision(self):
        rec, snn = self.recognizer([0, 125, 0, 0])
        (t, res), = self.steps(rec, [hand(100, 100), hand(500, 100)] + [None] * 6)
        self.assertIsNone(res.decision)
        self.assertEqual(snn.calls, 0)

    def test_off_like_before(self):
        rec, snn = self.recognizer([0, 125, 0, 0], early_margin=0.0)
        (t, res), = self.steps(rec, wave(10) + [None] * 6)
        self.assertEqual((res.end, t - res.episode.t_end, snn.calls), ("gap", 0.5, 1))

    def test_invalid_params(self):
        seg = EpisodeSegmenter(W)
        for kw in ({"early_gap": 0.5}, {"early_gap": 0}, {"early_margin": -1}, {"max_cut": 1.5}):
            with self.assertRaises(ValueError, msg=str(kw)):
                Recognizer(seg, Classifier([0] * 4), self.STEPS, 0.04, **kw)


class PathLength(unittest.TestCase):
    def test_deadband_ignores_jitter(self):
        boxes = [Box(i, *hand(320 + (4 if i % 2 else -4), 240)) for i in range(50)]
        self.assertEqual(path_length(boxes, 12.8), 0.0)
        self.assertEqual(path_length(boxes, 0.0), 49 * 8.0)

    def test_empty(self):
        self.assertEqual(path_length([], 1.0), 0.0)


class Decide(unittest.TestCase):
    STEPS = 25 * 5

    def test_no_spikes_is_unknown_not_class_0(self):
        d = decide([0, 0, 0, 0], self.STEPS, 0.04)
        self.assertEqual((d.cls, d.spikes, d.conf, d.margin), (-1, 0, 0.0, 0.0))

    def test_tie_is_unknown_even_without_margin(self):
        self.assertEqual(decide([7, 7, 0, 1], self.STEPS, 0.0).cls, -1)

    def test_small_margin_is_unknown(self):
        d = decide([10, 6, 0, 0], self.STEPS, 0.04)  # отрыв 4/125 = 0,032
        self.assertEqual(d.cls, -1)
        self.assertAlmostEqual(d.margin, 0.032)

    def test_clear_winner(self):
        d = decide([1.0, 2.0, 30.0, 0.0], self.STEPS, 0.04)
        self.assertEqual((d.cls, d.spikes), (2, 33))
        self.assertAlmostEqual(d.conf, 30 / 33)
        self.assertAlmostEqual(d.margin, 28 / 125)


class SideAndProgram(unittest.TestCase):
    def test_side_like_demo(self):
        left_to_right = [Box(i, *hand(100 + 40 * i, 240)) for i in range(10)]
        self.assertEqual(side_of(left_to_right), "left")
        self.assertEqual(side_of(left_to_right[::-1]), "right")
        self.assertEqual(side_of([Box(0, *hand(300, 240))] * 3), "right")  # x1 равны — как в демо (:327)

    def test_smoothed_side_ignores_last_swing(self):
        # махание с общим сносом вправо; последний взмах уходит влево дальше первой рамки
        xs = [200, 300, 250, 350, 300, 400, 350, 450, 400, 150]
        boxes = [Box(i, *hand(x, 240)) for i, x in enumerate(xs)]
        self.assertEqual(side_of(boxes), "right")
        self.assertEqual(side_of(boxes, smoothed=True), "left")

    def test_program_table(self):
        self.assertEqual([program_for(c, "left") for c in (0, 1, 2, 3)], ["turn180", "come", "go_left", "think"])
        self.assertEqual(program_for(2, "right"), "go_right")
        self.assertIsNone(program_for(-1, "left"))  # «не распознано» — робот стоит (G1)
        self.assertIsNone(program_for(7, "left"))


if __name__ == "__main__":
    unittest.main()
