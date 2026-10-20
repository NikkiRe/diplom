"""predict_live v2: загрузка моделей (R3, R4), распознавание с зерном и голосованием (G2), флаги предобработки.

Нужны torch, snntorch, cv2, rclpy и подставной pyrealsense2 стенда (ВМ sim); иначе тесты пропускаются.
Веса SNN лаборатории грузятся только с weights_only=True (чистый state_dict, R1 §5.2); yolo_hand.pt не
исполняется — torch.hub.load подменяется.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "neurobot" / "perception", ROOT / "neurobot" / "sim" / "gestures" / "shim"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
SNN_WEIGHTS = ROOT / "third_party" / "neurochip" / "demo" / "demo-ros2_feature" / "models" / "snn_emnist_small.pth"

try:
    pl = importlib.import_module("predict_live")
    import torch
    from PIL import Image, ImageDraw
except ImportError:
    pl = None


def opt(*argv):
    return pl.build_parser().parse_args(list(argv))


@unittest.skipIf(pl is None, "нужны torch, rclpy и подставные модули стенда (ВМ sim)")
class YoloLoading(unittest.TestCase):
    def test_local_hub_finite_attempts_then_success(self):
        model = mock.MagicMock()
        model.eval.return_value = model
        with mock.patch.object(pl.torch.hub, "load", side_effect=[RuntimeError("нет сети"), model]) as load, \
                self.assertLogs("predict_live", "ERROR"):
            got = pl.load_yolo_model(opt("--yolo-retry-s", "0", "--yolov5-dir", "/opt/yolov5"))
        self.assertIs(got, model)
        self.assertEqual(load.call_count, 2)
        args, kwargs = load.call_args
        self.assertEqual(args[:2], ("/opt/yolov5", "custom"))
        self.assertEqual(kwargs["source"], "local")
        self.assertEqual(os.environ["YOLOv5_AUTOINSTALL"], "false")

    def test_local_hub_gives_up(self):
        with mock.patch.object(pl.torch.hub, "load", side_effect=RuntimeError("битый кэш")) as load, \
                self.assertLogs("predict_live", "ERROR"), self.assertRaises(pl.ModelError):
            pl.load_yolo_model(opt("--yolo-retry-s", "0", "--yolo-attempts", "3"))
        self.assertEqual(load.call_count, 3)

    def test_fake_factory_by_dotted_name(self):
        got = pl.load_yolo_model(opt("--yolo", "fake", "--fake-yolo", "types:SimpleNamespace"))
        self.assertEqual(type(got).__name__, "SimpleNamespace")
        got = pl.load_yolo_model(opt("--yolo", "fake", "--fake-yolo", "datetime:datetime.now"))
        self.assertEqual(type(got).__name__, "datetime")


@unittest.skipIf(pl is None, "нужны torch, rclpy и подставные модули стенда (ВМ sim)")
class Options(unittest.TestCase):
    def test_defaults(self):
        o = pl.parse_args([])
        self.assertEqual((o.mode, o.end_gap, o.window, o.snn_votes, o.ignore_cls), ("legacy", 0.5, 8.0, 5, []))
        self.assertEqual((o.speed, o.turn, o.scan_timeout, o.reset_on_start), (0.5, 1.0, 0.5, True))
        self.assertFalse(o.show or o.polyline or o.canvas_norm or o.side_by_sum or o.end_on_still)
        self.assertFalse(pl.parse_args(["--no-reset-on-start"]).reset_on_start)
        # H10 по умолчанию выключен — как до H10, до A/B на записях лаборатории (README, «Конец жеста (H10)»)
        self.assertEqual((o.edge_gap, o.edges, o.max_cut, o.conf_start, o.conf_keep, o.early_margin, o.early_gap),
                         (0.5, ["top"], 0.0, 0.35, 0.35, 0.0, 0.3))
        self.assertEqual(pl.parse_args(["--end-gap", "1.5"]).edge_gap, 1.5)  # за краем — не меньше END_GAP
        # профиль стенда для A/B (README) — допустимые флаги
        o = pl.parse_args("--edge-gap 1.0 --max-cut 0.4 --conf-keep 0.1 --early-margin 0.9".split())
        self.assertEqual((o.edge_gap, o.max_cut, o.conf_keep, o.early_margin, o.early_gap), (1.0, 0.4, 0.1, 0.9, 0.3))

    def test_snn_seed(self):
        self.assertIsInstance(pl.snn_generator(pl.parse_args([]).snn_seed), torch.Generator)
        self.assertIsNone(pl.snn_generator(pl.parse_args(["--snn-seed", "-1"]).snn_seed))  # как в демо

    def test_ignore_cls(self):
        self.assertEqual(pl.parse_args(["--ignore-cls", "3"]).ignore_cls, [3])

    def test_invalid_values_exit_2(self):
        for bad in (["--ignore-cls", "4"], ["--snn-votes", "0"], ["--end-gap", "0"], ["--window", "0.4"],
                    ["--frame-timeout-ms", "0"], ["--min-path", "-1"], ["--reset-pause-s", "-1"],
                    ["--speed", "-0.5"], ["--speed", "0.6"], ["--turn", "0"], ["--turn", "1.5"],
                    ["--scan-timeout", "0"], ["--snn-seed", "-2"],
                    ["--speed", "nan"], ["--end-gap", "inf"], ["--window", "inf"], ["--obstacle-dist", "inf"],
                    ["--window", "0.9", "--edge-gap", "1.0"], ["--conf-keep", "0.5"], ["--conf-start", "1.5"],
                    ["--early-gap", "0.5"],
                    ["--max-cut", "1.5"], ["--edge-frac", "0.5"], ["--conf-gate", "0"], ["--edges", "up"]):
            with self.assertRaises(SystemExit, msg=bad) as e, mock.patch("sys.stderr"):
                pl.parse_args(bad)
            self.assertEqual(e.exception.code, 2)

    def test_non_finite_named(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr") as err:
            pl.parse_args(["--speed", "inf"])
        self.assertIn("--speed — конечное число", "".join(c.args[0] for c in err.write.call_args_list))

    def test_fake_factory_error_is_model_error(self):
        with self.assertRaises(pl.ModelError):
            pl.load_yolo_model(opt("--yolo", "fake", "--fake-yolo", "neurobot.nonexistent:Factory"))


@unittest.skipIf(pl is None or not SNN_WEIGHTS.is_file(), "нужны torch и веса SNN лаборатории (ВМ sim)")
class SnnLoading(unittest.TestCase):
    def test_lab_weights_strict_without_compile(self):
        net = pl.load_snn(str(SNN_WEIGHTS), compile_net=False)
        self.assertIsInstance(net, pl.SNN)
        self.assertEqual(tuple(net.fc1.weight.shape), (128, 784))
        self.assertGreater(float(net.fc1.weight.std()), 0.04)  # обученные веса, не случайная инициализация

    def test_errors_raise_instead_of_random_init(self):
        with tempfile.TemporaryDirectory() as d:
            partial = os.path.join(d, "partial.pth")
            torch.save({"model_state_dict": {"fc1.weight": torch.zeros(128, 784)}}, partial)
            for path in (partial, os.path.join(d, "missing.pth")):
                with self.assertRaises(pl.ModelError, msg=path):
                    pl.load_snn(path, compile_net=False)


@unittest.skipIf(pl is None or not SNN_WEIGHTS.is_file(), "нужны torch и веса SNN лаборатории (ВМ sim)")
class Recognition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.net = pl.load_snn(str(SNN_WEIGHTS), compile_net=False)

    def wave_map(self, polyline: bool):
        coords = [(300 + 120 * ((i % 4) - 1.5), 220, 380 + 120 * ((i % 4) - 1.5), 300) for i in range(16)]
        return pl.write_map_number(coords, scale=0.1, polyline=polyline)

    def test_seeded_votes_are_reproducible(self):
        img = self.wave_map(polyline=True)
        runs = []
        for _ in range(3):
            g = torch.Generator().manual_seed(0)
            runs.append(pl.recognize_gesture(img, self.net, votes=5, generator=g))
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(runs[0], runs[2])
        self.assertEqual(len(runs[0]), 4)
        self.assertTrue(all(isinstance(c, int) and 0 <= c <= 125 for c in runs[0]))

    def test_polyline_is_brighter_than_lab_squares(self):
        squares, line = self.wave_map(False), self.wave_map(True)
        _, sq, _ = pl.preprocess_single_image(squares)
        _, ln, _ = pl.preprocess_single_image(line)
        self.assertGreater(ln.mean(), 3 * sq.mean())

    def test_canvas_norm_scales_to_one(self):
        img = Image.new("L", (256, 256), 0)
        ImageDraw.Draw(img).rectangle([100, 100, 104, 160], fill=40)
        plain = pl.preprocess_single_image(img)[2]
        normed = pl.preprocess_single_image(img, normalize=True)[2]
        self.assertLess(float(plain.max()), 0.5)
        self.assertAlmostEqual(float(normed.max()), 1.0, places=5)


if __name__ == "__main__":
    unittest.main()
