"""gesturegen: детерминизм (в том числе между версиями Python), формы классов, геометрия по высотам и обрезка
кадром, концы жеста, отрицательные сегменты, согласованность таймлайна и истины, проверка параметров, CLI."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from neurobot.sim.gestures import gesturegen as gg  # noqa: E402
from neurobot.sim.gestures.shapes import SHAPES, shapes_of  # noqa: E402
from neurobot.sim.gestures.timeline import Timeline, dumps, read_jsonl  # noqa: E402

# sha256 сериализации generate(GenConfig(seed=5, gestures=8, negatives=4)); одинаков под 3.8 и 3.12.
GOLDEN = "ec7e6c184e1acc7b0cb7e27acf9971ba6fb7a4ef411ec8e89befee9b7ce98970"


def serialized(cfg: gg.GenConfig) -> bytes:
    h, tl, tr = gg.generate(cfg)
    return (dumps(h) + "".join(dumps(r) for r in tl) + "".join(dumps(r) for r in tr)).encode("utf-8")


def clean(**kw) -> gg.GenConfig:
    """Без шума и пропусков, с плотной сеткой и вписыванием в кадр — чтобы проверять геометрию форм."""
    base = dict(det_fps=60.0, p_miss=0.0, jitter=0.0, size_jitter=0.0, rotate_deg=0.0, aspect=0.0, negatives=0,
                hold_pre_s=0.0, end=("leave",), dist_m=(1.0, 1.0), amp_m=(0.4, 0.4), fit=True)
    base.update(kw)
    return gg.GenConfig(**base)


def centers_by_seg(timeline: list) -> dict:
    out = {}
    for r in timeline:
        if r["bbox"] is not None:
            x1, y1, x2, y2 = r["bbox"]
            out.setdefault(r["seg"], []).append(((x1 + x2) / 2, (y1 + y2) / 2, r))
    return out


def turning(points: list):
    """(сумма |поворотов|, максимальный поворот) траектории в градусах; короткие шаги пропускаются."""
    heads = []
    for (x0, y0, _), (x1, y1, _) in zip(points, points[1:]):
        if math.hypot(x1 - x0, y1 - y0) > 0.5:
            heads.append(math.atan2(y1 - y0, x1 - x0))
    turns = [abs(math.degrees(math.atan2(math.sin(b - a), math.cos(b - a)))) for a, b in zip(heads, heads[1:])]
    return sum(turns), max(turns) if turns else 0.0


class DeterminismTest(unittest.TestCase):
    def test_golden_bytes(self):
        got = hashlib.sha256(serialized(gg.GenConfig(seed=5, gestures=8, negatives=4))).hexdigest()
        self.assertEqual(got, GOLDEN)

    def test_same_seed_same_bytes_and_seeds_differ(self):
        cfg = dict(gestures=12, negatives=4)
        self.assertEqual(serialized(gg.GenConfig(seed=3, **cfg)), serialized(gg.GenConfig(seed=3, **cfg)))
        self.assertNotEqual(serialized(gg.GenConfig(seed=3, **cfg)), serialized(gg.GenConfig(seed=4, **cfg)))

    def test_files_identical(self):
        with tempfile.TemporaryDirectory() as d:
            a = gg.write_outputs(gg.GenConfig(seed=9), os.path.join(d, "a"))
            b = gg.write_outputs(gg.GenConfig(seed=9), os.path.join(d, "b"))
            for pa, pb in zip(a, b):
                self.assertEqual(Path(pa).read_bytes(), Path(pb).read_bytes())
                self.assertEqual(stat.S_IMODE(os.stat(pa).st_mode), 0o644)


class ShapeTest(unittest.TestCase):
    def gen(self, cls: int, n: int = 14, **kw):
        _, tl, truth = gg.generate(clean(seed=11, gestures=n, classes=(cls,), **kw))
        return truth, centers_by_seg(tl)

    def test_round_shapes_turn_smoothly(self):
        truth, cs = self.gen(0)
        for s in truth:
            total, biggest = turning(cs[s["seg"]])
            self.assertGreater(total, 250.0, s["shape"])
            self.assertLess(biggest, 60.0, s["shape"])

    def test_angular_shapes_have_corners_or_turn(self):
        truth, cs = self.gen(1)
        for s in truth:
            total, biggest = turning(cs[s["seg"]])
            if s["shape"] in ("v", "n", "two", "two_m"):
                self.assertGreater(biggest, 100.0, s["shape"])
            else:                                       # крючок «Вперёд» и S — плавные, но не прямые
                self.assertGreater(total, 150.0, s["shape"])

    def test_hook_goes_up_then_over_the_top(self):
        truth, cs = self.gen(1, shapes=("hook",), n=4)
        for s in truth:
            ys = [p[1] for p in cs[s["seg"]]]
            if s["reverse"]:
                ys.reverse()
            first, top, last = ys[0], min(ys), ys[-1]
            self.assertLess(top, first - 50)            # вверх (y кадра растёт вниз)
            self.assertLess(top, last - 20)             # и вниз после верха, но не до начала: нет правой ножки
            self.assertLess(last, first)

    def test_horizontal_side_as_demo_reads_it(self):
        truth, cs = self.gen(2, n=12)
        self.assertEqual(sorted(s["side"] for s in truth), ["left"] * 6 + ["right"] * 6)
        for s in truth:
            pts = cs[s["seg"]]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            self.assertGreater(max(xs) - min(xs), 2 * (max(ys) - min(ys)) + 1, s["shape"])
            fx1, lx1 = pts[0][2]["bbox"][0], pts[-1][2]["bbox"][0]
            direction = 1 if fx1 >= lx1 else 0          # predict_live.py:327–328
            self.assertEqual(direction, 0 if s["side"] == "left" else 1, s)

    def test_vertical_shapes(self):
        truth, cs = self.gen(3)
        for s in truth:
            xs = [p[0] for p in cs[s["seg"]]]
            ys = [p[1] for p in cs[s["seg"]]]
            self.assertGreater(max(ys) - min(ys), 2.5 * (max(xs) - min(xs)), s["shape"])

    def test_all_shapes_used_and_classes_balanced(self):
        _, _, truth = gg.generate(gg.GenConfig(seed=2, gestures=44, negatives=0))
        per_cls = {c: [s for s in truth if s["cls"] == c] for c in range(4)}
        self.assertEqual({c: len(v) for c, v in per_cls.items()}, {0: 11, 1: 11, 2: 11, 3: 11})
        self.assertEqual({s["shape"] for s in truth}, set(SHAPES))
        for c, v in per_cls.items():
            self.assertTrue({s["shape"] for s in v} <= set(shapes_of(c)))

    def test_distance_and_focal_length_scale_boxes(self):
        def width(**kw):
            _, tl, _ = gg.generate(clean(seed=1, gestures=4, **kw))
            ws = [r["bbox"][2] - r["bbox"][0] for r in tl if r["bbox"] and 0 < r["bbox"][0] and r["bbox"][2] < 640]
            return sum(ws) / len(ws)
        self.assertAlmostEqual(width(dist_m=(0.6, 0.6)) / width(dist_m=(2.4, 2.4)), 4.0, delta=0.4)
        self.assertAlmostEqual(width(f_px=616.0) / width(f_px=462.0), 616.0 / 462.0, delta=0.05)

    def test_fit_keeps_gesture_in_frame(self):
        _, tl, truth = gg.generate(gg.GenConfig(seed=4, gestures=40, negatives=0, dist_m=(0.5, 0.7), amp_m=(0.6, 0.6),
                                                fit=True))
        self.assertTrue(any(s["fit"] < 1.0 for s in truth))
        self.assertTrue(all(s["cut"] == 0 and s["h_m"] is None for s in truth))
        for r in tl:
            if r["bbox"]:
                x1, y1, x2, y2 = r["bbox"]
                self.assertTrue(0 <= x1 < x2 <= 640 and 0 <= y1 < y2 <= 480)


class HeightGeometryTest(unittest.TestCase):
    """Без fit: вертикаль из высот камеры и кисти, траектория обрезается краями кадра (R7b §3.4, §4.5)."""

    def gen(self, **kw):
        base = dict(seed=6, gestures=20, negatives=0, p_miss=0.0, jitter=0.0, size_jitter=0.0, end=("leave",))
        base.update(kw)
        return gg.generate(gg.GenConfig(**base))

    def test_hand_at_camera_height_is_centred(self):
        _, tl, truth = self.gen(hand_h_m=(0.66, 0.66), amp_m=(0.1, 0.1), dist_m=(2.0, 2.0))
        cs = centers_by_seg(tl)
        for s in truth:
            ys = [p[1] for p in cs[s["seg"]]]
            self.assertAlmostEqual((min(ys) + max(ys)) / 2, 240, delta=30, msg=s["shape"])
            self.assertEqual((s["cut"], s["h_m"], s["fit"]), (0.0, 0.66, 1.0))

    def test_raised_hand_is_cut_by_top_edge_and_kept_in_view(self):
        _, tl, truth = self.gen(hand_h_m=(1.4, 1.4), dist_m=(1.0, 1.0), amp_m=(0.5, 0.5))
        band = (240 - 0.18 * 616 / 2 - 4) / 616            # видимый ход центра кисти на 1 м, м
        for s in truth:
            self.assertAlmostEqual(s["h_m"], round(0.66 + band, 3), delta=0.002)   # кисть опущена до края поля
        self.assertGreater(sum(s["cut_top"] for s in truth) / len(truth), 0.1)
        self.assertTrue(all(s["cut_top"] <= s["cut"] for s in truth))
        boxes = [r["bbox"] for r in tl if r["bbox"]]
        self.assertTrue(all(b[3] - b[1] >= 0.5 * 0.18 * 616 / 1.0 - 1 for b in boxes))   # видна ≥ половина бокса

    def test_low_hand_far_away_is_not_cut(self):
        _, _, truth = self.gen(hand_h_m=(0.7, 0.7), dist_m=(2.5, 2.5), amp_m=(0.3, 0.3))
        self.assertEqual({s["cut"] for s in truth}, {0.0})


class EndTest(unittest.TestCase):
    def last_boxes(self, end: str):
        _, tl, truth = gg.generate(clean(seed=3, gestures=6, end=(end,), det_fps=10.0, hold_post_s=0.5))
        out = []
        for s in truth:
            boxes = [r for r in tl if r["seg"] == s["seg"] and r["bbox"]]
            out.append((s, boxes))
        return out

    def test_fist_shrinks_then_hand_leaves(self):
        for s, boxes in self.last_boxes("fist"):
            self.assertEqual(s["end"], "fist")
            after = [r for r in boxes if r["t"] > s["t0"] + 0.1 + 1e-9]
            self.assertTrue(1 <= len(after) <= 3, after)
            widths = [r["bbox"][2] - r["bbox"][0] for r in boxes[-len(after) - 1:]]
            self.assertTrue(all(b < a for a, b in zip(widths, widths[1:])), widths)
            self.assertEqual(boxes[-1]["t"], s["t_leave"])

    def test_leave_cuts_right_after_the_trajectory(self):
        for s, boxes in self.last_boxes("leave"):
            self.assertLess(boxes[-1]["t"] - s["t0"], 0.1 + 1e-9)

    def test_hold_keeps_hand_still(self):
        stays = [boxes[-1]["t"] - s["t0"] for s, boxes in self.last_boxes("hold")]
        self.assertTrue(all(d <= 0.6 for d in stays) and max(stays) > 0.1, stays)


class NegativeTest(unittest.TestCase):
    def gen(self, kind: str, n: int = 6):
        return gg.generate(gg.GenConfig(seed=21, gestures=0, negatives=n, neg_kinds=(kind,)))

    def test_hand_still_barely_moves(self):
        _, tl, truth = self.gen("hand_still")
        cs = centers_by_seg(tl)
        for s in truth:
            self.assertEqual((s["kind"], s["cls"], s["name"]), ("negative", -1, "hand_still"))
            pts = cs[s["seg"]]
            w = pts[0][2]["bbox"][2] - pts[0][2]["bbox"][0]
            spread = max(math.hypot(p[0] - pts[0][0], p[1] - pts[0][1]) for p in pts)
            self.assertLess(spread, 0.25 * w)
            self.assertGreaterEqual(s["t0"] - s["t_start"], 1.5)

    def test_passerby_crosses_frame(self):
        _, tl, truth = self.gen("passerby")
        cs = centers_by_seg(tl)
        for s in truth:
            xs = [p[0] for p in cs[s["seg"]]]
            self.assertGreater(max(xs) - min(xs), 0.75 * 640)
            steps = [b - a for a, b in zip(xs, xs[1:])]
            self.assertTrue(all(d > 0 for d in steps) or all(d < 0 for d in steps))

    def test_false_boxes_scattered_and_some_below_threshold(self):
        _, tl, truth = self.gen("false_boxes", n=8)
        boxes = [r for r in tl if r["bbox"]]
        self.assertTrue(boxes)
        self.assertTrue(any(r["conf"] <= 0.35 for r in boxes) and any(r["conf"] > 0.35 for r in boxes))
        xs = [(r["bbox"][0] + r["bbox"][2]) / 2 for r in boxes]
        self.assertGreater(max(xs) - min(xs), 300)

    def test_empty_has_no_boxes(self):
        _, tl, truth = self.gen("empty", n=3)
        self.assertFalse([r for r in tl if r["bbox"]])
        self.assertTrue(all(s["t0"] - s["t_start"] >= 5.0 for s in truth))


class FormatTest(unittest.TestCase):
    def test_timeline_and_truth_consistent(self):
        cfg = gg.GenConfig(seed=8, gestures=20, negatives=8)
        header, tl, truth = gg.generate(cfg)
        self.assertEqual(tl[0], {"t": 0.0, "bbox": None, "conf": None, "seg": None})
        ts = [r["t"] for r in tl]
        self.assertEqual(ts, sorted(set(ts)))
        segs = {s["seg"]: s for s in truth}
        n_boxes = {}
        for r in tl:
            if r["bbox"]:
                s = segs[r["seg"]]
                self.assertTrue(s["t_start"] <= r["t"] <= s["t_leave"])
                self.assertTrue(0.0 < r["conf"] < 1.0)
                n_boxes[r["seg"]] = n_boxes.get(r["seg"], 0) + 1
        for s in truth:
            self.assertTrue(s["t_start"] <= s["t0"] <= s["t_leave"] + 1e-9, s)
            self.assertEqual(s["n_boxes"], n_boxes.get(s["seg"], 0))
        for a, b in zip(truth, truth[1:]):
            self.assertGreaterEqual(b["t_start"] - a["t_leave"], cfg.pause_s[0])
        self.assertEqual(truth[0]["t_start"], cfg.lead_in_s)
        self.assertGreater(header["duration_s"], truth[-1]["t_leave"])
        self.assertEqual((header["det_fps"], header["f_px"]), (10.0, 616.0))

    def test_grid_matches_det_fps(self):
        _, tl, _ = gg.generate(gg.GenConfig(seed=8, gestures=4, negatives=0, det_fps=12.5))
        for r in tl:
            self.assertAlmostEqual(r["t"] * 12.5, round(r["t"] * 12.5), places=3)

    def test_timeline_lookup_is_step_function(self):
        _, tl, _ = gg.generate(gg.GenConfig(seed=8, gestures=4, negatives=0))
        t = Timeline({"duration_s": 100.0}, tl)
        box_rec = next(r for r in tl if r["bbox"])
        self.assertIsNone(t.at(-0.1))
        self.assertEqual(t.box_at(box_rec["t"] + 0.05), tuple(box_rec["bbox"]) + (box_rec["conf"],))
        self.assertIsNone(t.box_at(0.5))
        self.assertEqual(t.end, tl[-1]["t"])

    def test_validation(self):
        for bad in (dict(classes=(5,)), dict(shapes=("nope",)), dict(neg_kinds=("dance",)), dict(det_fps=0.5),
                    dict(gestures=0, negatives=0), dict(dur_s=(2.0, 1.0)), dict(p_miss=1.0),
                    dict(classes=(0,), shapes=("v",)), dict(dur_s=(0.0, 0.0)), dict(dist_m=(0.0, 0.0)),
                    dict(amp_m=(0.0, 0.0)), dict(passer_speed=(0.0, 0.0)), dict(passer_dist_m=(0.0, 1.0)),
                    dict(hand_m=(0.0, 0.18)), dict(f_px=0.0), dict(end=("wave",)), dict(end=()),
                    dict(conf=(0.5, 1.5)), dict(still_s=(-1.0, 2.0)), dict(dur_s=(1.0, math.inf)),
                    dict(cam_h_m=0.0), dict(hand_m=(math.nan, 0.18))):
            with self.assertRaisesRegex(ValueError, "^(%s)" % "|".join(bad), msg=bad):     # имя параметра — первым
                gg.generate(gg.GenConfig(**bad))

    def test_scalar_validation_names_the_parameter(self):
        """Одиночные числа вне границ (раньше — молча или AssertionError из глубины генератора)."""
        for name, v in (("lead_in_s", -3.0), ("hold_pre_s", -1.0), ("hold_post_s", -1.0), ("margin_px", -50.0),
                        ("margin_px", 300.0), ("jitter", -0.1), ("size_jitter", -0.1), ("rotate_deg", -1.0),
                        ("aspect", 1.5), ("aspect", -0.1), ("p_miss", math.nan), ("det_fps", math.nan),
                        ("lead_in_s", math.inf), ("f_px", math.inf)):
            with self.assertRaisesRegex(ValueError, "^%s" % name, msg=(name, v)):
                gg.generate(gg.GenConfig(**{name: v}))


class CliTest(unittest.TestCase):
    def test_gen_info_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(gg.main(["gen", "--seed", "3", "--gestures", "8", "--negatives", "4",
                                          "--out-dir", d, "--dist-m", "1,2", "--classes", "1,2", "--f-px", "462",
                                          "--false-conf", "0.2,0.5", "--hand-m", "0.1,0.2", "--end", "hold",
                                          "--fit"]), 0)
                self.assertEqual(gg.main(["gen", "--seed", "3", "--out-dir", d]), 2)
                self.assertEqual(gg.main(["gen", "--seed", "3", "--out-dir", d + "/z", "--dur-s", "0,0"]), 2)
                self.assertEqual(gg.main(["gen", "--out-dir", d + "/z", "--lead-in-s", "-3"]), 2)
                self.assertEqual(gg.main(["gen", "--out-dir", d + "/z", "--aspect", "1.5"]), 2)
            self.assertFalse(os.path.exists(d + "/z"))                 # неверные параметры — каталог не создан
            for name in ("dur_s", "lead_in_s", "aspect"):
                self.assertIn("gesturegen: %s" % name, err.getvalue())
            header, truth = read_jsonl(os.path.join(d, gg.TRUTH_NAME))
            p = header["params"]
            self.assertEqual((p["dist_m"], p["false_conf"], p["hand_m"], p["end"], p["fit"], header["f_px"]),
                             ([1.0, 2.0], [0.2, 0.5], [0.1, 0.2], ["hold"], True, 462.0))
            self.assertEqual({s["cls"] for s in truth if s["kind"] == "gesture"}, {1, 2})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(gg.main(["info", d]), 0)
            info = json.loads(out.getvalue())
            self.assertEqual(info["gestures"], {"1:angular": 4, "2:horizontal": 4})
            self.assertEqual((sum(info["negatives"].values()), info["ends"], info["f_px"]), (4, {"hold": 8}, 462.0))
            self.assertEqual(info["cut"]["gestures_cut"], 0)

    def test_bad_paths_are_code_2(self):
        """Ошибки путей — код 2 и сообщение в stderr без трассировки: info без файла, с пустым файлом и с таймлайном
        вместо истины; gen в каталог под обычным файлом и в каталог без прав на запись."""
        with tempfile.TemporaryDirectory() as d:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(gg.main(["gen", "--gestures", "2", "--negatives", "0", "--out-dir", d]), 0)
            empty = os.path.join(d, "empty.jsonl")
            open(empty, "w").close()
            locked = os.path.join(d, "locked")
            os.mkdir(locked, 0o500)
            cases = [["info", os.path.join(d, "nosuch")], ["info", empty], ["info", os.path.join(d, gg.TIMELINE_NAME)],
                     ["gen", "--out-dir", os.path.join(empty, "sub")]]
            if os.geteuid() != 0:                       # root пишет и в каталог без прав
                cases.append(["gen", "--out-dir", os.path.join(locked, "sub")])
            for argv in cases:
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(gg.main(argv), 2, argv)
                self.assertTrue(err.getvalue().startswith("gesturegen: "), argv)
            self.assertEqual(os.listdir(locked), [])


if __name__ == "__main__":
    unittest.main()
