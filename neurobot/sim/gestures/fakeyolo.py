"""FakeYolo — подставной детектор руки с интерфейсом модели torch.hub YOLOv5 (AutoShape), как её
использует predict_live.py лаборатории, без зависимости от yolov5.

Что используется в predict_live.py и есть здесь: torch.hub.load(…).eval() (:519–531); атрибуты max_det, conf,
iou (:553–555); .to(device) (:557); .half() на CUDA (:561–562); вызов model(color_image, size=320) (:625);
результат .xyxy[0] — строки x1, y1, x2, y2, conf, cls (:300). Как в YOLOv5: боксы с conf ≤ model.conf
отбрасываются, остаются max_det самых уверенных, фильтр classes; класс 0 = "hand".

Бокс берётся из таймлайна gesturegen в момент захвата кадра: время читается из метки кадра (clock.py),
которую ставит подставной pyrealsense2; кадр без метки и не чёрный — время «сейчас» (счётчик marker_miss),
чёрный кадр — детекций нет. Якорь создаётся при первом вызове (см. clock.py); если прочитанный якорь
старше конца таймлайна (файл от прошлого прогона того же таймлайна), детектор ничего не увидит — об этом
предупреждение в stderr и событие anchor_stale в журнале probe.

Окружение (from_env / hub_load):
  NEUROBOT_GESTURES_TIMELINE  — timeline.jsonl (обязательно);
  NEUROBOT_GESTURES_ANCHOR    — файл якоря (обязательно; свой на каждый прогон);
  NEUROBOT_FAKEYOLO_LATENCY_MS — имитация времени инференса, мс (по умолчанию 0);
  NEUROBOT_FAKEYOLO_TENSOR    — auto | numpy | torch: тип .xyxy[0] (auto — torch, если импортируется).
"""
from __future__ import annotations

import logging
import os
import time

import numpy as np

from .clock import ENV_ANCHOR, ENV_TIMELINE, decode_marker, load_or_create_anchor, mono
from .probe import probe as default_probe
from .timeline import Timeline

ENV_LATENCY = "NEUROBOT_FAKEYOLO_LATENCY_MS"
ENV_TENSOR = "NEUROBOT_FAKEYOLO_TENSOR"
NAMES = {0: "hand"}

log = logging.getLogger("fakeyolo")


def _tensor_kind(kind: str) -> str:
    if kind not in ("auto", "numpy", "torch"):
        raise ValueError("тип результата: auto | numpy | torch, а не %r" % kind)
    if kind != "auto":
        return kind
    try:
        import torch  # noqa: F401
    except ImportError:
        return "numpy"
    return "torch"


class Detections:
    """Подмножество yolov5 models.common.Detections: xyxy (= pred) — список N×6 на изображение, names, n."""

    def __init__(self, pred: list):
        self.pred = pred
        self.xyxy = pred
        self.names = NAMES
        self.n = len(pred)

    def __len__(self) -> int:
        return self.n


class FakeYolo:
    def __init__(self, timeline: Timeline, anchor_path: str, latency_s: float = 0.0, tensor: str = "auto",
                 probe=None):
        self.timeline = timeline
        self.anchor_path = anchor_path
        self.latency_s = latency_s
        self.anchor = None
        self._kind = _tensor_kind(tensor)
        self._probe = probe or default_probe()
        self.max_det = 1000
        self.conf = 0.25
        self.iou = 0.45
        self.classes = None

    @classmethod
    def from_env(cls, env=None) -> FakeYolo:
        env = os.environ if env is None else env
        missing = [k for k in (ENV_TIMELINE, ENV_ANCHOR) if not env.get(k)]
        if missing:
            raise RuntimeError("FakeYolo: не заданы %s" % ", ".join(missing))
        return cls(Timeline.load(env[ENV_TIMELINE]), env[ENV_ANCHOR],
                   latency_s=float(env.get(ENV_LATENCY, "0")) / 1000.0, tensor=env.get(ENV_TENSOR, "auto"))

    def eval(self) -> FakeYolo:
        return self

    def to(self, *args, **kwargs) -> FakeYolo:
        return self

    def half(self) -> FakeYolo:
        return self

    def __call__(self, ims, size=640, augment=False, profile=False) -> Detections:
        """ims — кадр H×W×3 (uint8) или список кадров; size, augment, profile не влияют на результат."""
        if self.anchor is None:
            self.anchor = load_or_create_anchor(self.anchor_path, self.timeline.sha256)
            self._check_age()
        batch = ims if isinstance(ims, (list, tuple)) else [ims]
        pred = [self._detect(im) for im in batch]
        self._probe.inc("det_calls", len(batch))
        if self.latency_s > 0:
            time.sleep(self.latency_s)
        return Detections(pred)

    def _check_age(self) -> None:
        rel = self.anchor.rel_of(mono())
        if rel > self.timeline.end:
            log.warning("якорь %s старше таймлайна (%.1f с от якоря, таймлайн кончается на %.1f с): боксов не будет",
                        self.anchor_path, rel, self.timeline.end)
            self._probe.event("anchor_stale", rel_s=round(rel, 3), end_s=self.timeline.end)

    def _detect(self, im):
        rows = []
        t = decode_marker(im)
        if t is None and np.any(im):
            self._probe.inc("marker_miss")
            t = mono()
        if t is not None:
            box = self.timeline.box_at(self.anchor.rel_of(t))
            if box is not None and box[4] > self.conf and (self.classes is None or 0 in self.classes):
                rows.append(list(box) + [0.0])
        arr = np.asarray(rows[:self.max_det], dtype=np.float32).reshape(-1, 6)
        if self._kind == "torch":
            import torch
            return torch.from_numpy(arr)
        return arr


def hub_load(repo_or_dir=None, model=None, *args, **kwargs) -> FakeYolo:
    """Замена torch.hub.load для обвязки «демо как есть»: аргументы игнорируются, модель — из окружения."""
    return FakeYolo.from_env()
