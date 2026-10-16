"""Эпизод жеста и решение по спайкам SNN для predict_live v2 — без torch, cv2 и rclpy (Python ≥ 3.8).

EpisodeSegmenter (L1, L4, G3, G4) заменяет в демо «1,5 с без рамки и ≥ 6 рамок»:
- эпизод — рамки руки подряд; конец — END_GAP (по умолчанию 0,5 с) без рамки; под флагом end_on_still —
  ещё и «рука остановилась»: путь набран, а центр рамки END_GAP не сдвигался дальше мёртвой зоны;
- в эпизоде держатся только рамки последних window секунд (по умолчанию 8 с: у лаборатории жест длится
  3–7,4 с — R7) и не больше max_boxes — буфер ограничен при руке или ложном объекте, стоящих в кадре часами;
- эпизод идёт в распознавание, только если рамок ≥ min_boxes и путь центров ≥ min_path (доля ширины кадра);
  путь считается с мёртвой зоной deadband, чтобы дрожание рамки стоящей руки не копилось в «путь»;
- по концу эпизода буфер очищается всегда, принят эпизод или нет; reset() — пауза детекции в движении;
- без кадров (отказ камеры) эпизод старше END_GAP отбрасывается (expire), а не распознаётся с опозданием.

decide (G1, G2): класс по суммам выходных спайков за все голосования; «не распознано» (-1), если спайков нет,
у первых двух классов ничья или отрыв меньше min_margin. Демо брало argmax по нулям — класс 0, разворот.
side_of (G5): сторона для go_left/go_right — как в демо (x1 первой и последней рамки) или под флагом по
сглаженному смещению: средние центры первой и последней трети траектории.
program_for: класс → программа DriveController, как в исходном predict_live.py лаборатории (ветка ros2_feature,
SHA-256 d186195d…: выбор программы по pred в main_loop); «не распознано» — без движения.
"""
from __future__ import annotations

import collections
import math
from typing import Iterable, List, NamedTuple, Optional, Sequence


class Box(NamedTuple):
    t: float
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def center(self):
        return (self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2


class Episode(NamedTuple):
    boxes: tuple
    t_end: float     # время последней рамки
    path: float      # путь центров с мёртвой зоной, px
    accepted: bool   # идёт в распознавание


def path_length(boxes: Iterable[Box], deadband: float) -> float:
    """Путь центров рамок: шаг засчитывается, когда центр ушёл от опорной точки дальше deadband (px)."""
    it = iter(boxes)
    first = next(it, None)
    if first is None:
        return 0.0
    ax, ay = first.center
    total = 0.0
    for b in it:
        x, y = b.center
        d = math.hypot(x - ax, y - ay)
        if d >= deadband:
            total += d
            ax, ay = x, y
    return total


class EpisodeSegmenter:
    def __init__(self, frame_width: int, end_gap: float = 0.5, window: float = 8.0, min_boxes: int = 3,
                 min_path: float = 0.1, deadband: float = 0.02, end_on_still: bool = False,
                 max_boxes: int = 256):
        if not (end_gap > 0 and window > end_gap and min_boxes >= 1 and min_path >= 0 and deadband >= 0
                and max_boxes >= min_boxes):
            raise ValueError("сегментатор: нужно end_gap > 0, window > end_gap, 1 ≤ min_boxes ≤ max_boxes, "
                             "min_path ≥ 0, deadband ≥ 0")
        self.end_gap = end_gap
        self.window = window
        self.min_boxes = min_boxes
        self.min_path_px = min_path * frame_width
        self.deadband_px = deadband * frame_width
        self.end_on_still = end_on_still
        self._boxes = collections.deque(maxlen=max_boxes)
        self._anchor = None   # (x, y) опорная точка для «рука остановилась»
        self._moved_t = 0.0   # когда центр последний раз ушёл из мёртвой зоны

    def __len__(self) -> int:
        return len(self._boxes)

    def reset(self) -> None:
        self._boxes.clear()
        self._anchor = None

    def expire(self, t: float) -> bool:
        """Хорошего кадра нет (камера стоит или восстанавливается): эпизод, у которого END_GAP истёк без кадра,
        отбрасывается — ушла рука или кадры, неизвестно, а распознанный с опозданием жест дал бы манёвр через
        секунды после жеста. True — эпизод отброшен."""
        if self._boxes and t - self._boxes[-1].t >= self.end_gap:
            self.reset()
            return True
        return False

    def update(self, t: float, box: Optional[Sequence[float]]) -> Optional[Episode]:
        """Очередной обработанный кадр: время и рамка руки (x1, y1, x2, y2) или None. Эпизод — по его концу."""
        if box is None:
            if self._boxes and t - self._boxes[-1].t >= self.end_gap:
                return self._finish()
            return None
        b = Box(t, *box)
        while self._boxes and t - self._boxes[0].t > self.window:
            self._boxes.popleft()
        self._boxes.append(b)
        x, y = b.center
        if self._anchor is None or math.hypot(x - self._anchor[0], y - self._anchor[1]) >= self.deadband_px:
            self._anchor = (x, y)
            self._moved_t = t
        if (self.end_on_still and t - self._moved_t >= self.end_gap
                and path_length(self._boxes, self.deadband_px) >= self.min_path_px):
            return self._finish()
        return None

    def _finish(self) -> Episode:
        boxes = tuple(self._boxes)
        path = path_length(boxes, self.deadband_px)
        self.reset()
        accepted = len(boxes) >= self.min_boxes and path >= self.min_path_px
        return Episode(boxes, boxes[-1].t, path, accepted)


class Decision(NamedTuple):
    cls: int        # 0..3 или -1 — «не распознано»
    conf: float     # доля спайков лучшего класса среди всех выходных спайков
    margin: float   # (спайки 1-го − спайки 2-го класса) / число шагов (шаги × голосования)
    spikes: int     # всего выходных спайков


def decide(counts: Sequence[float], steps: int, min_margin: float) -> Decision:
    """Решение по суммам выходных спайков классов за steps шагов (G1). Ничья — всегда «не распознано»."""
    counts = [int(round(c)) for c in counts]
    total = sum(counts)
    order = sorted(range(len(counts)), key=lambda i: -counts[i])
    first, second = counts[order[0]], counts[order[1]]
    margin = (first - second) / steps
    conf = first / total if total else 0.0
    if first == 0 or first == second or margin < min_margin:
        return Decision(-1, conf, margin, total)
    return Decision(order[0], conf, margin, total)


# Классы SNN → программы демо; класс 2 — go_left/go_right по стороне жеста
PROGRAMS = {0: "turn180", 1: "come", 3: "think"}


def program_for(cls: int, side: str) -> Optional[str]:
    """Программа для класса или None: -1 («не распознано», G1) и неизвестные классы не двигают робота."""
    if cls == 2:
        return "go_" + side
    return PROGRAMS.get(cls)


def side_of(boxes: Sequence[Box], smoothed: bool = False) -> str:
    """'right', если рука ушла влево по кадру (после зеркалирования), иначе 'left' — как direction в исходном
    write_map_number лаборатории (x1 первой и последней рамки)."""
    if not smoothed:
        return "right" if boxes[0].x1 >= boxes[-1].x1 else "left"
    k = max(1, len(boxes) // 3)
    head: List[float] = [b.center[0] for b in boxes[:k]]
    tail: List[float] = [b.center[0] for b in boxes[-k:]]
    return "right" if sum(head) / k >= sum(tail) / k else "left"
