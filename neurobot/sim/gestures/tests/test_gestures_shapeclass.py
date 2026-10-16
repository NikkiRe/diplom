"""shapeclass: боксы сегментов — как их собрало бы демо, сводка по формам и холстам; с torch и кодом лаборатории
(ВМ sim) — классификация настоящей сетью на маленьком выходе gesturegen."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from neurobot.sim.gestures import shapeclass as sc  # noqa: E402
from neurobot.sim.gestures.gesturegen import GenConfig, write_outputs  # noqa: E402

try:
    import cv2  # noqa: F401
    import rclpy  # noqa: F401
    import snntorch  # noqa: F401
    import torch  # noqa: F401
    MISSING = None if sc.LAB_SRC.is_file() and sc.LAB_WEIGHTS.is_file() else "нет кода или весов лаборатории"
except ImportError as e:
    MISSING = "нет %s (тест — для ВМ sim)" % e.name


def row(seg: int, shape: str, cls: int, demo, poly=None, kind: str = "gesture", side=None, side_ok=None) -> dict:
    r = {"seg": seg, "kind": kind, "cls": cls, "name": shape, "shape": shape if kind == "gesture" else None,
         "side": side, "boxes": 10}
    if demo is not None:
        r["demo"] = {"hist": demo, "mute": demo[0], "margin": 0.0}
        r["poly"] = {"hist": poly, "mute": 0, "margin": 0.5}
    if side_ok is not None:
        r["side_ok"] = side_ok
    return r


class LogicTest(unittest.TestCase):
    def test_segment_boxes_as_demo_collects_them(self):
        records = [{"t": 0.0, "bbox": None, "conf": None, "seg": None},
                   {"t": 1.0, "bbox": [10.7, 20.2, 30.9, 40.5], "conf": 0.8, "seg": 0},
                   {"t": 1.1, "bbox": [11.0, 21.0, 31.0, 41.0], "conf": 0.35, "seg": 0},       # не выше порога
                   {"t": 1.2, "bbox": None, "conf": None, "seg": 0},
                   {"t": 9.0, "bbox": [50.0, 60.0, 70.0, 80.0], "conf": 0.5, "seg": 1}]
        self.assertEqual(sc.segment_boxes(records), {0: [(10, 20, 30, 40)], 1: [(50, 60, 70, 80)]})

    def test_aggregate_by_shape_and_canvas(self):
        rows = [row(0, "circle", 0, [4, 0, 0, 0], [3, 1, 0, 0]), row(1, "circle", 0, [2, 2, 0, 0], [4, 0, 0, 0]),
                row(2, "circle", 0, None), row(3, "swipe", 2, [0, 0, 4, 0], [0, 0, 4, 0], side="left", side_ok=True),
                row(4, "hand_still", -1, [4, 0, 0, 0], [4, 0, 0, 0], kind="negative")]
        t = sc.aggregate(rows)
        circle = t["groups"]["circle"]
        self.assertEqual((circle["segments"], circle["below_6"]), (3, 1))
        self.assertEqual(circle["demo"],
                         {"p": [0.75, 0.25, 0.0, 0.0], "mute": 0.75, "margin": 0.0, "match_label": 0.75})
        self.assertEqual(circle["poly"]["p"], [0.875, 0.125, 0.0, 0.0])
        self.assertEqual(t["groups"]["swipe"]["side_ok"], 1.0)
        self.assertNotIn("match_label", t["groups"]["neg:hand_still"]["demo"])
        self.assertEqual(t["gestures"], {"gestures": 4, "below_6": 1,
                                         "demo": {"mute": 0.5, "pred0": 0.5, "match_label": 0.833},
                                         "poly": {"mute": 0.0, "pred0": 0.583, "match_label": 0.917}})
        self.assertIn("circle", sc.format_table(t))


@unittest.skipIf(MISSING, MISSING)
class LabNetTest(unittest.TestCase):
    def test_cli_on_small_generator_output(self):
        with tempfile.TemporaryDirectory() as d:
            write_outputs(GenConfig(seed=4, gestures=8, negatives=2, classes=(0, 2), fit=True), os.path.join(d, "g"))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(sc.main(["--gen", os.path.join(d, "g"), "--out", os.path.join(d, "sc"),
                                          "--votes", "5"]), 0)
            table = json.loads(Path(d, "sc", "table.json").read_text(encoding="utf-8"))
            rows = [json.loads(ln) for ln in Path(d, "sc", "per_segment.jsonl").read_text().splitlines()]
            self.assertIn("форма", out.getvalue())
        self.assertEqual(len(rows), 10)
        for r in rows:
            if "demo" in r:
                self.assertEqual(sum(r["demo"]["hist"]), 5)
                self.assertTrue(0 <= r["poly"]["mute"] <= 5)
        self.assertEqual(table["gestures"]["gestures"], 8)
        self.assertEqual(table["weights_sha256"][:12], "bda97d30463d")
        self.assertEqual(table["groups"]["swipe"]["poly"]["p"][2], 1.0)    # горизонталь — класс 2 (R1 §6.2)


if __name__ == "__main__":
    unittest.main()
