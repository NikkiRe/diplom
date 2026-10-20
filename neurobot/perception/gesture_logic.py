"""Эпизод жеста и решение по спайкам SNN для predict_live v2 — без torch, cv2 и rclpy (Python ≥ 3.8).

EpisodeSegmenter (L1, L4, G3, G4) заменяет в демо «1,5 с без рамки и ≥ 6 рамок»:
- эпизод — рамки руки подряд; конец — END_GAP (по умолчанию 0,5 с) без рамки; под флагом end_on_still —
  ещё и «рука остановилась»: путь набран, а центр рамки END_GAP не сдвигался дальше мёртвой зоны;
- в эпизоде держатся только рамки последних window секунд (по умолчанию 8 с: у лаборатории жест длится
  3–7,4 с — R7) и не больше max_boxes — буфер ограничен при руке или ложном объекте, стоящих в кадре часами;
- эпизод идёт в распознавание, только если рамок ≥ min_boxes и путь центров ≥ min_path (доля ширины кадра);
  путь считается с мёртвой зоной deadband, чтобы дрожание рамки стоящей руки не копилось в «путь»;
- по концу эпизода буфер очищается всегда, принят эпизод или нет; reset() — пауза детекции в движении;
- без кадров (отказ камеры) эпизод старше END_GAP отбрасывается (expire), а не распознаётся с опозданием;
- H10 (1): рука ушла за край кадра (последняя рамка у края edges ближе edge_frac размера кадра) — ждать
  edge_gap, а не END_GAP: на двойнике все ранние реакции — рука, ушедшая за верхний край посреди жеста
  (O4 §2.1). Доля cut эпизода — время за краем (вылазки, после которых рука вернулась, и ожидание edge_gap, если
  не вернулась) к времени эпизода вместе с ним;
- H10 (2): гистерезис уверенности (идея ByteTrack): эпизод начинает рамка с уверенностью ≥ start_conf, а
  продолжает и слабая (≥ keep_conf), если её центр не дальше gate размеров рамки от прогноза по постоянной
  скорости. Слабая рамка вдали от прогноза — как кадр без руки. Слабые рамки дают только точки траектории, эпизод
  они не продлевают: конец — END_GAP (у края — edge_gap) после последней уверенной рамки, как без гистерезиса.
  Иначе неподвижная слабая рамка (лицо, рукав, опущенная рука) у прогноза держала бы эпизод сколько угодно:
  скорость обнуляется, и прогноз совпадает с ней.

Recognizer (H10 (3), Köpüklü и др. 2019) — решение по эпизоду с двумя порогами отрыва: на первом кадре без руки
спустя early_gap (рука исчезла внутри кадра, не у края) SNN считается по уже собранным рамкам, и при отрыве
≥ early_margin команда идёт сразу; иначе — по концу эпизода с min_margin, без второго прогона SNN, если рамок не
прибавилось. Эпизод с долей cut ≥ max_cut — «не распознано» без SNN: обрезанная траектория — не та форма.
По умолчанию всё новое выключено: поведение то же, что до H10.

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
from typing import Callable, Iterable, List, NamedTuple, Optional, Sequence

EDGES = ("top", "bottom", "left", "right")


class Box(NamedTuple):
    t: float
    x1: float
    y1: float
    x2: float
    y2: float
    conf: float = 1.0

    @property
    def center(self):
        return (self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2


class Episode(NamedTuple):
    boxes: tuple
    t_end: float     # время последней рамки
    path: float      # путь центров с мёртвой зоной, px
    accepted: bool   # идёт в распознавание
    end: str = "gap"   # чем кончился: gap — END_GAP без руки, edge — edge_gap после ухода за край, still — рука встала
    cut: float = 0.0   # доля времени эпизода, когда рука была за краем кадра (с ожиданием edge_gap, если не вернулась)


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
                 max_boxes: int = 256, frame_height: int = 480, edge_gap: Optional[float] = None,
                 edge_frac: float = 0.02, edges: Sequence[str] = ("top",), start_conf: float = 0.0,
                 keep_conf: Optional[float] = None, gate: float = 1.0):
        edge_gap = end_gap if edge_gap is None else edge_gap
        keep_conf = start_conf if keep_conf is None else keep_conf
        if not (end_gap > 0 and window > max(end_gap, edge_gap) and edge_gap >= end_gap and min_boxes >= 1
                and min_path >= 0 and deadband >= 0 and max_boxes >= min_boxes and 0 <= edge_frac < 0.5
                and set(edges) <= set(EDGES) and 0 <= keep_conf <= start_conf <= 1 and gate > 0):
            raise ValueError("сегментатор: нужно end_gap > 0, end_gap ≤ edge_gap < window, 1 ≤ min_boxes ≤ max_boxes, "
                             "min_path ≥ 0, deadband ≥ 0, 0 ≤ edge_frac < 0,5, края из {}, 0 ≤ keep_conf ≤ start_conf "
                             "≤ 1, gate > 0".format(", ".join(EDGES)))
        self.end_gap = end_gap
        self.edge_gap = edge_gap
        self.window = window
        self.min_boxes = min_boxes
        self.min_path_px = min_path * frame_width
        self.deadband_px = deadband * frame_width
        self.end_on_still = end_on_still
        self.start_conf, self.keep_conf, self.gate = start_conf, keep_conf, gate
        self._limits = {"top": edge_frac * frame_height, "bottom": (1 - edge_frac) * frame_height,
                        "left": edge_frac * frame_width, "right": (1 - edge_frac) * frame_width}
        self._edges = tuple(edges) if edge_gap > end_gap else ()
        self._boxes = collections.deque(maxlen=max_boxes)
        self._anchor = None   # (x, y) опорная точка для «рука остановилась»
        self._moved_t = 0.0   # когда центр последний раз ушёл из мёртвой зоны
        self._missed = False  # после последней рамки были кадры без руки
        self._cut = 0.0       # с: рука была за краем и вернулась
        self._strong_t = 0.0  # время последней уверенной рамки: от него отсчитывается конец эпизода

    def __len__(self) -> int:
        return len(self._boxes)

    def reset(self) -> None:
        self._boxes.clear()
        self._anchor = None
        self._missed = False
        self._cut = 0.0

    def at_edge(self, b: Box) -> bool:
        """Рамка у края кадра из edges: рука, пропавшая после неё, скорее ушла за край, чем кончила жест."""
        lim = self._limits
        return any((e == "top" and b.y1 <= lim["top"]) or (e == "bottom" and b.y2 >= lim["bottom"])
                   or (e == "left" and b.x1 <= lim["left"]) or (e == "right" and b.x2 >= lim["right"])
                   for e in self._edges)

    def gap(self) -> float:
        """Сколько ждать руку после последней уверенной рамки: edge_gap, если последняя рамка у края, иначе END_GAP."""
        return self.edge_gap if self._boxes and self.at_edge(self._boxes[-1]) else self.end_gap

    def expire(self, t: float) -> bool:
        """Хорошего кадра нет (камера стоит или восстанавливается): эпизод, у которого END_GAP истёк без кадра,
        отбрасывается — ушла рука или кадры, неизвестно, а распознанный с опозданием жест дал бы манёвр через
        секунды после жеста. True — эпизод отброшен."""
        if self._boxes and t - self._strong_t >= self.gap():
            self.reset()
            return True
        return False

    def update(self, t: float, box: Optional[Sequence[float]], conf: float = 1.0) -> Optional[Episode]:
        """Очередной обработанный кадр: время, рамка руки (x1, y1, x2, y2) или None и её уверенность. Эпизод — по
        его концу."""
        if box is not None and not self._takes(t, box, conf):
            box = None  # слабая рамка вдали от прогноза — как кадр без руки
        if box is None:
            if self._boxes:
                self._missed = True
                if t - self._strong_t >= self.gap():
                    return self._finish("edge" if self.gap() > self.end_gap else "gap")
            return None
        b = Box(t, *box, conf)
        if conf >= self.start_conf:
            self._strong_t = t
        if self._missed and self.at_edge(self._boxes[-1]):
            self._cut += t - self._boxes[-1].t  # рука вернулась из-за края
        self._missed = False
        while self._boxes and t - self._boxes[0].t > self.window:
            self._boxes.popleft()
        self._boxes.append(b)
        x, y = b.center
        if self._anchor is None or math.hypot(x - self._anchor[0], y - self._anchor[1]) >= self.deadband_px:
            self._anchor = (x, y)
            self._moved_t = t
        if (self.end_on_still and t - self._moved_t >= self.end_gap
                and path_length(self._boxes, self.deadband_px) >= self.min_path_px):
            return self._finish("still")
        return None

    def _takes(self, t: float, box: Sequence[float], conf: float) -> bool:
        """Рамка идёт в эпизод: уверенная (≥ start_conf) — всегда; слабая (≥ keep_conf) — только продолжение до
        конца эпизода по последней уверенной рамке, если центр не дальше gate размеров последней рамки от прогноза
        по постоянной скорости."""
        if conf >= self.start_conf:
            return True
        if conf < self.keep_conf or not self._boxes or t - self._strong_t >= self.gap():
            return False
        last = self._boxes[-1]
        px, py = last.center
        if len(self._boxes) > 1 and last.t > self._boxes[-2].t:
            prev = self._boxes[-2]
            k = (t - last.t) / (last.t - prev.t)
            px, py = px + (px - prev.center[0]) * k, py + (py - prev.center[1]) * k
        x1, y1, x2, y2 = box
        size = max(last.x2 - last.x1, last.y2 - last.y1)
        return math.hypot((x1 + x2) / 2 - px, (y1 + y2) / 2 - py) <= self.gate * size

    def pending(self, t: float, after: float) -> Optional[Episode]:
        """Эпизод, который можно решить раньше конца (Recognizer): руки нет дольше after, но меньше END_GAP, и она
        пропала внутри кадра (не у края), рамок и пути хватает. Буфер не трогается."""
        if not (self._boxes and self._missed) or self.at_edge(self._boxes[-1]):
            return None
        if not after <= t - self._boxes[-1].t < self.end_gap:
            return None
        boxes = tuple(self._boxes)
        path = path_length(boxes, self.deadband_px)
        if len(boxes) < self.min_boxes or path < self.min_path_px:
            return None
        return Episode(boxes, boxes[-1].t, path, True, "early", self._cut_share(boxes))

    def _cut_share(self, boxes: tuple, out: float = 0.0) -> float:
        """Доля времени за краем: вылазки с возвратом и out — ожидание руки, которая за краем и осталась."""
        span = boxes[-1].t - boxes[0].t + out
        return min(1.0, (self._cut + out) / span) if span > 0 else 0.0

    def _finish(self, end: str) -> Episode:
        boxes = tuple(self._boxes)
        path = path_length(boxes, self.deadband_px)
        cut = self._cut_share(boxes, self.edge_gap if end == "edge" else 0.0)
        self.reset()
        accepted = len(boxes) >= self.min_boxes and path >= self.min_path_px
        return Episode(boxes, boxes[-1].t, path, accepted, end, cut)


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


class Result(NamedTuple):
    episode: Episode
    decision: Optional[Decision]   # None — эпизод не принят (мало рамок или пути), SNN не считалась
    end: str                       # gap | edge | still | early (два порога) | cut (обрезан — «не распознано»)


class Recognizer:
    """Эпизоды сегментатора → решения (H10 (3)). classify(boxes) → суммы выходных спайков по классам за steps
    шагов (SNN с зерном: тот же холст — те же спайки). early_margin 0 — без раннего решения; max_cut 0 — без
    правила обрезки."""

    def __init__(self, segmenter: EpisodeSegmenter, classify: Callable[[tuple], Sequence[float]], steps: int,
                 min_margin: float, early_margin: float = 0.0, early_gap: float = 0.25, max_cut: float = 0.0):
        if not (early_margin >= 0 and 0 < early_gap < segmenter.end_gap and 0 <= max_cut <= 1):
            raise ValueError("распознаватель: нужно early_margin ≥ 0, 0 < early_gap < end_gap, 0 ≤ max_cut ≤ 1")
        self.segmenter = segmenter
        self.classify = classify
        self.steps = steps
        self.min_margin, self.early_margin, self.early_gap, self.max_cut = min_margin, early_margin, early_gap, max_cut
        self._probe = None  # (последняя рамка, число рамок, спайки) — SNN по эпизоду, не решённому рано

    def reset(self) -> None:
        self.segmenter.reset()
        self._probe = None

    def expire(self, t: float) -> bool:
        return self.segmenter.expire(t)

    def step(self, t: float, box: Optional[Sequence[float]], conf: float = 1.0) -> Optional[Result]:
        """Кадр → решение по эпизоду, если оно есть: по концу эпизода или раньше (два порога)."""
        ep = self.segmenter.update(t, box, conf)
        if ep is not None:
            return self._final(ep)
        if not self.early_margin:
            return None
        ep = self.segmenter.pending(t, self.early_gap)
        if ep is None or (self.max_cut and ep.cut >= self.max_cut) or self._probed(ep):
            return None
        counts = self.classify(ep.boxes)
        self._probe = (ep.t_end, len(ep.boxes), counts)
        d = decide(counts, self.steps, self.early_margin)
        if d.cls < 0:
            return None  # не уверены — ждём конца эпизода (тогда порог min_margin)
        self.reset()
        return Result(ep, d, "early")

    def _probed(self, ep: Episode) -> bool:
        return self._probe is not None and self._probe[:2] == (ep.t_end, len(ep.boxes))

    def _final(self, ep: Episode) -> Result:
        counts = self._probe[2] if self._probed(ep) else None
        self._probe = None
        if not ep.accepted:
            return Result(ep, None, ep.end)
        if self.max_cut and ep.cut >= self.max_cut:
            return Result(ep, Decision(-1, 0.0, 0.0, 0), "cut")
        if counts is None:
            counts = self.classify(ep.boxes)
        return Result(ep, decide(counts, self.steps, self.min_margin), ep.end)


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
