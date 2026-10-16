"""FakeYolo: интерфейс модели — ровно как его использует predict_live.py (проверяется по AST их файла),
бокс по метке времени кадра, порог conf и max_det, чёрный кадр, кадр без метки, якорь: создание, чужой таймлайн,
чужая загрузка ОС, недописанный файл, якорь старше таймлайна."""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from neurobot.sim.gestures import fakeyolo as fy  # noqa: E402
from neurobot.sim.gestures.clock import boot_id, encode_marker, load_or_create_anchor, mono, read_anchor  # noqa: E402
from neurobot.sim.gestures.probe import Probe  # noqa: E402
from neurobot.sim.gestures.timeline import Timeline, write_jsonl  # noqa: E402

PREDICT_LIVE = ROOT / "third_party/neurochip/demo/demo-ros2_feature/src/camera/predict_live.py"

RECORDS = [
    {"t": 0.0, "bbox": None, "conf": None, "seg": None},
    {"t": 1.0, "bbox": [100.0, 120.0, 160.0, 220.0], "conf": 0.8, "seg": 0},
    {"t": 1.1, "bbox": [110.0, 120.0, 170.0, 220.0], "conf": 0.3, "seg": 0},
    {"t": 1.2, "bbox": None, "conf": None, "seg": None},
]


def frame_at(t_mono: float) -> np.ndarray:
    img = np.full((480, 640, 3), 64, dtype=np.uint8)
    encode_marker(img, t_mono)
    return img


def write_bbox_as_demo(results):
    """Цикл write_bbox из predict_live.py:300–306 без отрисовки."""
    coords = []
    for *xyxy, _, _ in results.xyxy[0]:
        coords.append((int(xyxy[0]), int(xyxy[1]), int(xyxy[2]), int(xyxy[3])))
    return coords


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.timeline_path = os.path.join(self.dir, "timeline.jsonl")
        write_jsonl(self.timeline_path, {"version": 1, "duration_s": 2.0, "det_fps": 10.0}, RECORDS)
        self.anchor_path = os.path.join(self.dir, "anchor.json")
        self.probe = Probe()

    def tearDown(self):
        self.tmp.cleanup()

    def model(self, **kw):
        kw.setdefault("tensor", "numpy")
        return fy.FakeYolo(Timeline.load(self.timeline_path), self.anchor_path, probe=self.probe, **kw)

    def demo_model(self):
        """Последовательность predict_live.py:522–562 (torch.hub.load заменён на hub_load)."""
        env = {fy.ENV_TIMELINE: self.timeline_path, fy.ENV_ANCHOR: self.anchor_path, fy.ENV_TENSOR: "numpy"}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            m = fy.hub_load("ultralytics/yolov5", "custom", path="/home/jetson/NeuroChip/demo/models/yolo_hand.pt",
                            force_reload=False).eval()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        m.max_det = 1
        m.conf = 0.35
        m.iou = 0.45
        m = m.to("cpu")
        m.half()
        return m


@unittest.skipUnless(PREDICT_LIVE.exists(), "нет кода лаборатории")
class DemoContractTest(Base):
    """То, что predict_live.py делает с моделью, есть у FakeYolo (разбор их исходника, без исполнения)."""

    def test_attributes_and_calls_used_by_demo_exist(self):
        tree = ast.parse(PREDICT_LIVE.read_text(encoding="utf-8"))
        used_attrs, calls = set(), []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) \
                    and node.value.attr == "yolo_model":
                used_attrs.add(node.attr)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "yolo_model":
                calls.append(node)
        self.assertEqual(used_attrs, {"max_det", "conf", "iou", "to", "half"})
        self.assertEqual(len(calls), 1)
        self.assertEqual([k.arg for k in calls[0].keywords], ["size"])
        m = self.model()
        for name in used_attrs | {"eval"}:
            self.assertTrue(hasattr(m, name), name)
        src = PREDICT_LIVE.read_text(encoding="utf-8")
        self.assertIn("results.xyxy[0]", src)
        self.assertIn(').eval()', src)

    def test_demo_sequence_end_to_end(self):
        m = self.demo_model()
        r0 = m(np.zeros((480, 640, 3), np.uint8), size=320)
        self.assertEqual(write_bbox_as_demo(r0), [])
        img = frame_at(m.anchor.mono_of(1.05))[:, ::-1]            # cv2.flip(img, 1)
        self.assertEqual(write_bbox_as_demo(m(img, size=320)), [(100, 120, 160, 220)])


class DetectTest(Base):
    def test_box_by_marker_time_and_conf_threshold(self):
        m = self.model()
        m.conf = 0.35
        m(np.zeros((480, 640, 3), np.uint8))
        a = m.anchor
        self.assertEqual(m(frame_at(a.mono_of(0.5))).xyxy[0].shape, (0, 6))
        row = m(frame_at(a.mono_of(1.0))).xyxy[0]
        np.testing.assert_allclose(row, [[100, 120, 160, 220, 0.8, 0]], rtol=1e-6)
        self.assertEqual(len(m(frame_at(a.mono_of(1.15))).xyxy[0]), 0)       # conf 0,3 ≤ 0,35
        m.conf = 0.25
        self.assertEqual(len(m(frame_at(a.mono_of(1.15))).xyxy[0]), 1)
        m.max_det = 0
        self.assertEqual(len(m(frame_at(a.mono_of(1.0))).xyxy[0]), 0)
        m.max_det, m.classes = 1, [1]
        self.assertEqual(len(m(frame_at(a.mono_of(1.0))).xyxy[0]), 0)

    def test_frozen_frame_repeats_box_and_black_frame_is_empty(self):
        m = self.model()
        m(np.zeros((480, 640, 3), np.uint8))
        frozen = frame_at(m.anchor.mono_of(1.0))
        for _ in range(3):
            self.assertEqual(len(m(frozen).xyxy[0]), 1)
        self.assertEqual(len(m(np.zeros((480, 640, 3), np.uint8)).xyxy[0]), 0)
        self.assertEqual(self.probe.get("det_calls"), 5)

    def test_frame_without_marker_uses_now(self):
        m = self.model()
        img = np.full((480, 640, 3), 50, np.uint8)
        m(img)
        self.assertEqual(self.probe.get("marker_miss"), 1)
        time.sleep(0.01)
        self.assertEqual(len(m(img).xyxy[0]), 0)       # «сейчас» ≈ 0,01 с от якоря — бокса нет

    def test_batch_and_detections_fields(self):
        m = self.model()
        m(np.zeros((480, 640, 3), np.uint8))
        r = m([frame_at(m.anchor.mono_of(1.0)), np.zeros((480, 640, 3), np.uint8)], size=320)
        self.assertEqual((r.n, len(r), [len(x) for x in r.xyxy], r.names), (2, 2, [1, 0], {0: "hand"}))
        self.assertIs(r.pred, r.xyxy)

    def test_latency(self):
        m = self.model(latency_s=0.05)
        t = time.monotonic()
        m(np.zeros((480, 640, 3), np.uint8))
        self.assertGreaterEqual(time.monotonic() - t, 0.05)


class AnchorTest(Base):
    def test_created_once_and_reused(self):
        m1 = self.model()
        m1(np.zeros((480, 640, 3), np.uint8))
        m2 = self.model()
        m2(np.zeros((480, 640, 3), np.uint8))
        self.assertEqual(m1.anchor, m2.anchor)

    def write_anchor(self, age_s: float, wall_shift_s: float = 0.0, boot: str = None) -> None:
        """Якорь, созданный age_s назад; wall_shift_s сдвигает t_wall (другое смещение стенных и монотонных)."""
        Path(self.anchor_path).write_text(json.dumps({
            "t_wall": time.time() - age_s + wall_shift_s, "t_mono": mono() - age_s, "pid": 1,
            "timeline_sha256": Timeline.load(self.timeline_path).sha256,
            "boot_id": boot_id() if boot is None else boot}), encoding="utf-8")

    def test_anchor_of_other_timeline_rejected(self):
        load_or_create_anchor(self.anchor_path, "0" * 64)
        with self.assertRaises(RuntimeError):
            self.model()(np.zeros((480, 640, 3), np.uint8))

    def test_anchor_of_other_boot_rejected_live_only(self):
        self.write_anchor(5.0, wall_shift_s=1000.0, boot="other-boot")
        with self.assertRaisesRegex(RuntimeError, "загрузке ОС"):
            self.model()(np.zeros((480, 640, 3), np.uint8))
        self.assertEqual(read_anchor(self.anchor_path, live=False).boot_id, "other-boot")

    def test_unfinished_anchor_is_awaited(self):
        Path(self.anchor_path).write_text("{", encoding="utf-8")
        writer = threading.Timer(0.1, self.write_anchor, args=(0.0,))
        writer.start()
        m = self.model()
        m(np.zeros((480, 640, 3), np.uint8))
        writer.join()
        self.assertEqual(m.anchor.pid, 1)

    def test_anchor_older_than_timeline_is_reported(self):
        self.write_anchor(100.0)
        self.probe = Probe(os.path.join(self.dir, "probe.jsonl"))
        m = self.model()
        with self.assertLogs("fakeyolo", "WARNING"):
            self.assertEqual(len(m(frame_at(mono())).xyxy[0]), 0)
        events = [json.loads(ln) for ln in Path(self.probe.path).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["ev"] for e in events], ["anchor_stale"])
        self.assertEqual(events[0]["end_s"], 1.2)

    def test_from_env_requires_paths_and_tensor_kind(self):
        with self.assertRaises(RuntimeError):
            fy.FakeYolo.from_env({fy.ENV_TIMELINE: self.timeline_path})
        with self.assertRaises(ValueError):
            self.model(tensor="jax")
        m = fy.FakeYolo.from_env({fy.ENV_TIMELINE: self.timeline_path, fy.ENV_ANCHOR: self.anchor_path,
                                  fy.ENV_LATENCY: "20", fy.ENV_TENSOR: "numpy"})
        self.assertAlmostEqual(m.latency_s, 0.02)


class TorchTest(Base):
    def setUp(self):
        super().setUp()
        try:
            import torch  # noqa: F401
        except ImportError:
            self.skipTest("torch не установлен")

    def test_tensor_result_iterates_like_demo(self):
        import torch
        m = self.model(tensor="torch")
        m(np.zeros((480, 640, 3), np.uint8))
        r = m(frame_at(m.anchor.mono_of(1.0)), size=320)
        self.assertIsInstance(r.xyxy[0], torch.Tensor)
        self.assertEqual(write_bbox_as_demo(r), [(100, 120, 160, 220)])


if __name__ == "__main__":
    unittest.main()
