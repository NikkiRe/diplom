"""replay: логика конца жеста perception v2 офлайн с подставной сетью — кадры и порог детектора, пауза детекции на
манёвр (L6), отнесение сообщений к сегментам истины, ранняя реакция на руке за верхним краем и её снятие edge_gap
(H10), сводка; слабые рамки gesturegen (--miss-conf). Без torch и ROS, Python ≥ 3.8."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from neurobot.sim.gestures import gesturegen as gg  # noqa: E402
from neurobot.sim.gestures import replay  # noqa: E402
from neurobot.sim.gestures.timeline import Timeline  # noqa: E402

COME = [0, 125, 0, 0]  # подставная SNN: всегда класс 1 (come, 3 с) с полным отрывом


class Snn:
    def __init__(self, counts=COME):
        self.counts, self.calls = counts, 0

    def __call__(self, boxes):
        self.calls += 1
        return self.counts


def timeline(boxes) -> Timeline:
    """[(t, (x1, y1, x2, y2) | None, conf)] → таймлайн (сегмент 0)."""
    recs = [{"t": 0.0, "bbox": None, "conf": None, "seg": None}]
    recs += [{"t": t, "bbox": list(b) if b else None, "conf": c if b else None, "seg": 0} for t, b, c in boxes]
    return Timeline({}, recs)


def stroke(t0: float, n: int, x0: float = 100.0, y: float = 200.0, dx: float = 30.0, dt: float = 0.1, conf=0.8):
    return [(round(t0 + k * dt, 4), (x0 + dx * k, y, x0 + dx * k + 80, y + 80), conf) for k in range(n)]


def truth(t_start: float, t0: float, kind: str = "gesture", cls: int = 1) -> dict:
    return {"seg": 0, "kind": kind, "cls": cls, "name": "come" if cls == 1 else "empty", "shape": "v", "side": None,
            "t_start": t_start, "t0": t0, "cut": 0.0, "cut_top": 0.0}


class RunTimeline(unittest.TestCase):
    def test_one_gesture_one_message_and_detector_threshold(self):
        tl = timeline(stroke(1.0, 15) + [(2.5, None, None)])
        msgs = replay.run_timeline(tl, replay.ReplayConfig(), Snn())
        self.assertEqual(len(msgs), 1)
        m = msgs[0]
        # кадры 15 к/с, рамки таймлайна — 10 к/с: рамок эпизода — кадров с рукой за 1,5 с
        self.assertEqual((m["cls"], m["program"], m["end"], m["n_boxes"]), (1, "come", "gap", 23))
        self.assertGreaterEqual(m["t"] - 2.4, 0.5)  # END_GAP после последней рамки
        weak = timeline(stroke(1.0, 15, conf=0.3) + [(2.5, None, None)])
        self.assertEqual(replay.run_timeline(weak, replay.ReplayConfig(), Snn()), [])  # 0,3 ≤ model.conf 0,35

    def test_detection_pauses_for_own_maneuver(self):
        # второй жест через 1 с после первого — робот ещё едет (come, 3 с): детекция стоит, рамки не копятся
        tl = timeline(stroke(1.0, 10) + [(2.0, None, None)] + stroke(3.0, 10) + [(4.0, None, None)]
                      + stroke(8.0, 10) + [(9.0, None, None)])
        msgs = replay.run_timeline(tl, replay.ReplayConfig(), Snn())
        self.assertEqual(len(msgs), 2)
        self.assertGreater(msgs[1]["t"], 9.0)

    def test_hand_over_top_edge_early_then_fixed_by_edge_gap(self):
        # рука уходит за верхний край на 0,8 с посреди жеста (O4 §2.1): с END_GAP 0,5 — префикс раньше t0
        top = [(round(2.0 + 0.1 * k, 4), (400.0 + 10 * k, 0.0, 480.0 + 10 * k, 40.0), 0.8) for k in range(3)]
        tl = timeline(stroke(1.0, 10) + top + [(2.3, None, None)] + stroke(3.1, 10, x0=500, dx=-30)
                      + [(4.1, None, None)])
        rows = replay.judge([truth(0.5, 4.0)], replay.run_timeline(tl, replay.ReplayConfig(), Snn()))
        self.assertTrue(rows[0]["early"])
        cfg = replay.config_of({"edge_gap": 1.0}, replay.ReplayConfig())
        rows = replay.judge([truth(0.5, 4.0)], replay.run_timeline(tl, cfg, Snn()))
        self.assertFalse(rows[0]["early"])
        self.assertEqual((rows[0]["outcome"], rows[0]["msgs"]), ("correct", 1))
        self.assertGreater(rows[0]["k1"], 0.5)


class Judge(unittest.TestCase):
    def test_segments_outcomes_and_spontaneous(self):
        segs = [truth(0.0, 5.0), dict(truth(20.0, 30.0, kind="negative", cls=-1), seg=1)]
        msgs = [{"t": 4.0, "cls": -1, "program": None}, {"t": 5.6, "cls": 1, "program": "come", "side": "left"},
                {"t": 25.0, "cls": 1, "program": "come", "side": "left"},
                {"t": 41.0, "cls": 1, "program": "come", "side": "left"}]  # позже t0 + 10 с — самопроизвольное
        for m in msgs:
            m.update({"margin": 1.0, "end": "gap", "cut": 0.0, "n_boxes": 9})
        rows = replay.judge(segs, msgs)
        g, neg, extra = rows
        self.assertEqual((g["outcome"], g["msgs"], g["actions"], g["k1"], g["early"]), ("correct", 2, 1, 0.6, False))
        self.assertEqual(neg["actions"], 1)
        s = replay.summarize(rows)
        self.assertEqual(s["k7"], {"false_actions": 1, "spontaneous": 1, "total": 2})
        self.assertEqual((s["split"], s["early"], s["outcomes"]["correct"]), (1, 0, 1))

    def test_config_of_rejects_unknown(self):
        with self.assertRaises(ValueError):
            replay.config_of({"edge_gpa": 1.0}, replay.ReplayConfig())


class WeakBoxes(unittest.TestCase):
    def test_weak_boxes_only_on_misses(self):
        base = dict(seed=3, gestures=6, negatives=0, p_miss=0.3)
        _, plain, _ = gg.generate(gg.GenConfig(**base))
        h, weak, _ = gg.generate(gg.GenConfig(miss_conf=(0.1, 0.3), **base))
        self.assertEqual(h["params"]["miss_conf"], (0.1, 0.3))
        strong = [r for r in weak if r["bbox"] and r["conf"] > 0.35]
        low = [r for r in weak if r["bbox"] and r["conf"] <= 0.35]
        self.assertTrue(low)
        self.assertTrue(all(0.1 <= r["conf"] <= 0.3 for r in low))
        self.assertGreater(len(strong) + len(low), sum(1 for r in plain if r["bbox"]))
        self.assertNotIn("miss_conf", gg.generate(gg.GenConfig(**base))[0]["params"])  # по умолчанию — как до H10


if __name__ == "__main__":
    unittest.main()
