"""gesturegen — детерминированный (seed) генератор потока боксов руки для стенда neurobot.

Выход: таймлайн «что отдал бы детектор руки» и истина по сегментам (формат — timeline.py). Сегменты —
жесты 4 классов (формы — shapes.py, по зондированию весов и слайду 12) и отрицательные: рука стоит в кадре,
проход человека, ложные боксы, пустой кадр. Между сегментами — паузы, достаточные, чтобы робот демо закончил
манёвр (до 11 с, predict_live.py:443–454) и снова стоял.

Геометрия — камера-обскура с фокусом f_px: 616 px по умолчанию [оценка R7b RS4: кадр 640×480 вырезан из 16:9,
HFOV ≈ 55°; ответ даст rs-enumerate-devices -c] или 462 px (HFOV 69,4° полного кадра 16:9 [ист. Intel]).
Бокс кисти — hand_m метров, размах жеста — amp_m; размеры в пикселях падают как 1/дистанция.
Положение по вертикали — из высот (R7b §3.4, §4.5): камера на cam_h_m (0,66 м, мачта телескопическая), центр
кисти на высоте из hand_h_m (0,8–1,4 м), но не выше и не ниже поля зрения на этой дистанции — оператор
держит руку в кадре; траектория вокруг неё в кадр не вписывается и обрезается краями (cut, cut_top в истине).
Режим fit — как до R7: размах уменьшается, пока жест не поместится, место в кадре — случайное.
Жест длится dur_s (по умолчанию 1–7 с: по GIF рука в кадре 3–7 с, короткие сохранены) и кончается
одним из end: fist — 1–3 сжимающихся бокса кулака и обрыв, leave — обрыв сразу, hold — рука стоит до
hold_post_s. Отсчёты — на сетке темпа детекции det_fps (по умолчанию 10 к/с — «как на Jetson», [оценка]
9–16 к/с до замера, R1 §3): подставной pyrealsense2 с тем же темпом даёт ровно один отсчёт на кадр.
Детектор находит руку, если в кадре не меньше половины бокса [оценка].

Случайность — только через random.Random.random() (его последовательность по seed гарантирована между
версиями Python), числа округляются перед записью: один seed даёт одинаковые байты под 3.8 и 3.12.

  python -m neurobot.sim.gestures.gesturegen gen --seed 1 --gestures 200 --negatives 60 --out-dir DIR
  python -m neurobot.sim.gestures.gesturegen info DIR
"""
from __future__ import annotations

import argparse
import hashlib
import math
import os
import random
import sys
from dataclasses import asdict, dataclass

from .shapes import CLASS_NAMES, NEGATIVE_KINDS, PROBE_NOTE, SHAPES, shapes_of, transform
from .timeline import FORMAT_VERSION, dumps, file_sha256, read_jsonl, rnd, write_jsonl

FRAME_W, FRAME_H = 640, 480
TIMELINE_NAME, TRUTH_NAME = "timeline.jsonl", "truth.jsonl"
END_KINDS = ("fist", "leave", "hold")
FIST_SCALE = (0.8, 0.65, 0.5)       # кулак меньше раскрытой ладони и сжимается до ухода руки [оценка]
MIN_VISIBLE = 0.5                   # доля площади бокса в кадре, при которой детектор ещё видит руку [оценка]
# диапазоны (min, max): у первых нужен min > 0 (на них делят), у остальных — min ≥ 0
POSITIVE_RANGES = ("dist_m", "amp_m", "dur_s", "passer_dist_m", "passer_speed")
RANGES = POSITIVE_RANGES + ("hand_h_m", "pause_s", "conf", "false_conf", "still_s", "false_s", "false_rate",
                            "empty_s")
# одиночные числа: границы [min, max]; доли — [0, 1): при aspect ≥ 1 форма вырождается или зеркалится
BOUNDS = {"det_fps": (1.0, 60.0), "hold_pre_s": (0.0, math.inf), "hold_post_s": (0.0, math.inf),
          "lead_in_s": (0.0, math.inf), "rotate_deg": (0.0, 180.0), "jitter": (0.0, 1.0), "size_jitter": (0.0, 1.0),
          "margin_px": (0.0, FRAME_H / 2)}
FRACTIONS = ("aspect", "p_miss", "passer_p_miss")
POSITIVE = ("f_px", "cam_h_m")


class Rng:
    """ГСЧ сегмента: random.Random с зерном из sha256(ключ); распределения — только из random()."""

    def __init__(self, *key):
        digest = hashlib.sha256(":".join(str(k) for k in key).encode("utf-8")).digest()
        self._r = random.Random(int.from_bytes(digest[:8], "big"))

    def random(self) -> float:
        return self._r.random()

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self._r.random()

    def gauss(self, sigma: float) -> float:
        u1 = 1.0 - self._r.random()
        u2 = self._r.random()
        return sigma * math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)

    def chance(self, p: float) -> bool:
        return self._r.random() < p

    def index(self, n: int) -> int:
        return min(int(self._r.random() * n), n - 1)

    def shuffle(self, items: list) -> None:
        for i in range(len(items) - 1, 0, -1):
            j = self.index(i + 1)
            items[i], items[j] = items[j], items[i]


@dataclass
class GenConfig:
    seed: int = 1
    gestures: int = 40
    negatives: int = 12
    classes: tuple = (0, 1, 2, 3)
    shapes: tuple = ()                    # пусто — все формы выбранных классов по очереди
    neg_kinds: tuple = NEGATIVE_KINDS
    det_fps: float = 10.0
    f_px: float = 616.0
    fit: bool = False
    cam_h_m: float = 0.66
    hand_h_m: tuple = (0.8, 1.4)
    dist_m: tuple = (0.5, 2.5)
    amp_m: tuple = (0.3, 0.6)
    hand_m: tuple = (0.13, 0.18)          # ширина и высота бокса кисти (кадр слайда 8: 185×244 px) [оценка]
    dur_s: tuple = (1.0, 7.0)
    end: tuple = ("fist", "leave")
    hold_pre_s: float = 0.3
    hold_post_s: float = 0.3
    rotate_deg: float = 10.0
    aspect: float = 0.15
    jitter: float = 0.03                  # σ дрожания центра, доля размера бокса
    size_jitter: float = 0.05
    p_miss: float = 0.1
    conf: tuple = (0.5, 0.9)
    pause_s: tuple = (12.0, 18.0)
    lead_in_s: float = 5.0
    margin_px: float = 4.0
    still_s: tuple = (1.5, 10.0)
    passer_dist_m: tuple = (2.0, 3.5)
    passer_speed: tuple = (0.8, 1.5)
    passer_p_miss: float = 0.4
    false_s: tuple = (4.0, 12.0)
    false_rate: tuple = (0.5, 3.0)
    false_conf: tuple = (0.25, 0.6)
    empty_s: tuple = (5.0, 15.0)

    def validate(self) -> None:
        """Неверный параметр — ValueError, сообщение начинается с имени параметра; NaN и бесконечность не проходят."""
        if self.gestures < 0 or self.negatives < 0 or self.gestures + self.negatives == 0:
            raise ValueError("gestures, negatives: нужно ≥ 0 и хотя бы один сегмент")
        for name, (lo, hi) in BOUNDS.items():
            v = getattr(self, name)
            if not (math.isfinite(v) and lo <= v <= hi):
                upper = " ≤ %g" % hi if hi < math.inf else ""
                raise ValueError("%s = %r: нужно %g ≤ %s%s" % (name, v, lo, name, upper))
        for name in FRACTIONS:
            v = getattr(self, name)
            if not 0.0 <= v < 1.0:
                raise ValueError("%s = %r: нужно 0 ≤ %s < 1" % (name, v, name))
        for name in POSITIVE:
            v = getattr(self, name)
            if not (math.isfinite(v) and v > 0.0):
                raise ValueError("%s = %r: нужно %s > 0" % (name, v, name))
        if not all(math.isfinite(v) and v > 0.0 for v in self.hand_m):
            raise ValueError("hand_m = %r: ширина и высота кисти > 0" % (self.hand_m,))
        bad = [c for c in self.classes if c not in CLASS_NAMES]
        if bad or not self.classes:
            raise ValueError("classes: %r" % (bad or "пусто",))
        for name in self.shapes:
            if name not in SHAPES or SHAPES[name].cls not in self.classes:
                raise ValueError("shapes: форма %r не из выбранных классов" % name)
        for c in self.classes:
            if self.gestures and not self._shapes_for(c):
                raise ValueError("shapes: у класса %d нет форм" % c)
        bad = [k for k in self.neg_kinds if k not in NEGATIVE_KINDS]
        if bad or (self.negatives and not self.neg_kinds):
            raise ValueError("neg_kinds: %r" % (bad or "пусто",))
        bad = [k for k in self.end if k not in END_KINDS]
        if bad or (self.gestures and not self.end):
            raise ValueError("end: %r, допустимы %s" % (bad or "пусто", ", ".join(END_KINDS)))
        for name in RANGES:
            lo, hi = getattr(self, name)
            positive = name in POSITIVE_RANGES
            if not (lo > 0.0 if positive else lo >= 0.0) or not lo <= hi or not math.isfinite(hi):
                raise ValueError("%s: нужен %s ≤ max" % (name, "0 < min" if positive else "0 ≤ min"))
        if self.conf[1] > 1.0 or self.false_conf[1] > 1.0:
            raise ValueError("conf, false_conf: уверенность не больше 1")

    def _shapes_for(self, cls: int) -> list:
        return [s for s in (self.shapes or shapes_of(cls)) if SHAPES[s].cls == cls]


def _plan(cfg: GenConfig) -> list:
    """Список сегментов (kind, cls, side, shape | negative kind) в перемешанном по seed порядке."""
    plan = []
    per_class = {c: 0 for c in cfg.classes}
    sides = 0
    for i in range(cfg.gestures):
        cls = cfg.classes[i % len(cfg.classes)]
        names = cfg._shapes_for(cls)
        shape = names[per_class[cls] % len(names)]
        per_class[cls] += 1
        side = None
        if cls == 2:
            side = ("left", "right")[sides % 2]
            sides += 1
        plan.append(("gesture", cls, side, shape))
    for i in range(cfg.negatives):
        plan.append(("negative", -1, None, cfg.neg_kinds[i % len(cfg.neg_kinds)]))
    Rng(cfg.seed, "order").shuffle(plan)
    return plan


def _min_jerk(tau: float) -> float:
    return tau * tau * tau * (10.0 + tau * (-15.0 + 6.0 * tau))


class _Path:
    """Траектория по штрихам: время штриха пропорционально его длине, внутри — minimum-jerk по длине дуги."""

    def __init__(self, strokes: list):
        self.strokes = []
        for st in strokes:
            cum = [0.0]
            for (x0, y0), (x1, y1) in zip(st, st[1:]):
                cum.append(cum[-1] + math.hypot(x1 - x0, y1 - y0))
            self.strokes.append((st, cum))
        self.length = sum(c[-1] for _, c in self.strokes)

    def at(self, u: float):
        """Точка в доле времени движения u ∈ [0, 1]."""
        u = min(max(u, 0.0), 1.0)
        acc = 0.0
        last = len(self.strokes) - 1
        for i, (st, cum) in enumerate(self.strokes):
            share = cum[-1] / self.length
            if u <= acc + share or i == last:
                tau = min(max((u - acc) / share, 0.0), 1.0) if share > 0 else 1.0
                return self._on_stroke(st, cum, _min_jerk(tau) * cum[-1])
            acc += share

    @staticmethod
    def _on_stroke(st, cum, s: float):
        for k in range(1, len(cum)):
            if s <= cum[k] or k == len(cum) - 1:
                seg = cum[k] - cum[k - 1]
                a = (s - cum[k - 1]) / seg if seg > 0 else 0.0
                (x0, y0), (x1, y1) = st[k - 1], st[k]
                return x0 + a * (x1 - x0), y0 + a * (y1 - y0)
        return st[-1]

    def extent(self):
        xs = [x for st, _ in self.strokes for x, _ in st]
        ys = [y for st, _ in self.strokes for _, y in st]
        return min(xs), max(xs), min(ys), max(ys)


class _Builder:
    """Сборка таймлайна на сетке k / det_fps: отсчёты сегментов, затем сжатие подряд идущих null."""

    def __init__(self, cfg: GenConfig):
        self.cfg = cfg
        self.fps = cfg.det_fps
        self.records = [{"t": 0.0, "bbox": None, "conf": None, "seg": None}]

    def k_at_or_after(self, t: float) -> int:
        return int(math.ceil(t * self.fps - 1e-9))

    def t(self, k: int) -> float:
        return rnd(k / self.fps, 4)

    def put(self, k: int, seg: int, box, conf) -> None:
        t = self.t(k)
        if box is None:
            self.records.append({"t": t, "bbox": None, "conf": None, "seg": seg})
        else:
            self.records.append({"t": t, "bbox": [rnd(v, 1) for v in box], "conf": rnd(conf, 3), "seg": seg})

    def gap(self, k: int) -> None:
        self.records.append({"t": self.t(k), "bbox": None, "conf": None, "seg": None})

    def compressed(self) -> list:
        """Подряд идущие null сливаются; отсчёт сегмента в момент закрывающего null заменяет этот null."""
        out = []
        for r in self.records:
            if out and r["t"] == out[-1]["t"] and out[-1]["bbox"] is None and out[-1]["seg"] is None:
                out.pop()
            if r["bbox"] is None and out and out[-1]["bbox"] is None:
                continue
            if out and r["t"] <= out[-1]["t"]:
                raise AssertionError("сегменты перекрываются: t=%s" % r["t"])
            out.append(r)
        return out


def _visible_box(cx: float, cy: float, bw: float, bh: float):
    """Бокс, обрезанный кадром, или None, если в кадре меньше MIN_VISIBLE его площади."""
    x1, y1 = max(0.0, cx - bw / 2), max(0.0, cy - bh / 2)
    x2, y2 = min(float(FRAME_W), cx + bw / 2), min(float(FRAME_H), cy + bh / 2)
    if x2 <= x1 or y2 <= y1 or (x2 - x1) * (y2 - y1) < MIN_VISIBLE * bw * bh:
        return None
    return x1, y1, x2, y2


def _hand_px(cfg: GenConfig, dist: float):
    return cfg.f_px * cfg.hand_m[0] / dist, cfg.f_px * cfg.hand_m[1] / dist


def _place(cfg: GenConfig, rng: Rng, path: _Path, amp_px: float, bw: float, bh: float, dist: float):
    """(cx, cy, fit, h_m): начало координат формы в кадре, множитель размаха и высота кисти над полом."""
    x0, x1, y0, y1 = path.extent()
    m = cfg.margin_px
    fit, h = 1.0, None
    if cfg.fit:
        fit = min(1.0, (FRAME_W - 2 * m - bw) / max((x1 - x0) * amp_px, 1e-9),
                  (FRAME_H - 2 * m - bh) / max((y1 - y0) * amp_px, 1e-9))
        amp_px *= fit
    lo, hi = m + bw / 2 - x0 * amp_px, FRAME_W - m - bw / 2 - x1 * amp_px
    cx = rng.uniform(lo, hi) if lo <= hi else (lo + hi) / 2
    if cfg.fit:
        cy = rng.uniform(m + bh / 2 - y0 * amp_px, FRAME_H - m - bh / 2 - y1 * amp_px)
    else:
        band = max(0.0, (FRAME_H / 2 - bh / 2 - m) * dist / cfg.f_px)   # видимый ход центра кисти, м
        h = min(max(rng.uniform(*cfg.hand_h_m), cfg.cam_h_m - band), cfg.cam_h_m + band)
        cy = FRAME_H / 2 - cfg.f_px * (h - cfg.cam_h_m) / dist - (y0 + y1) / 2 * amp_px
    return cx, cy, fit, h


def _gesture(b: _Builder, rng: Rng, seg: int, k0: int, cls: int, side, shape_name: str) -> dict:
    cfg = b.cfg
    shape = SHAPES[shape_name]
    dist = rng.uniform(*cfg.dist_m)
    amp_px = cfg.f_px * rng.uniform(*cfg.amp_m) / dist
    bw, bh = _hand_px(cfg, dist)
    reverse = cls != 2 and rng.chance(0.5)
    path = _Path(transform(shape, side=side, reverse=reverse, rot_deg=rng.uniform(-cfg.rotate_deg, cfg.rotate_deg),
                           aspect=rng.uniform(1.0 - cfg.aspect, 1.0 + cfg.aspect)))
    cx, cy, fit, h = _place(cfg, rng, path, amp_px, bw, bh, dist)
    amp_px *= fit
    end = cfg.end[rng.index(len(cfg.end))]
    dur = rng.uniform(*cfg.dur_s)
    t_start = b.t(k0)
    t_move = t_start + rng.uniform(0.0, cfg.hold_pre_s)
    t0 = t_move + dur
    k_last = b.k_at_or_after(t0)
    if end == "hold":
        k_last = max(k_last, int(math.floor((t0 + rng.uniform(0.0, cfg.hold_post_s)) * b.fps + 1e-9)))
    fist = 1 + rng.index(len(FIST_SCALE)) if end == "fist" else 0
    n_boxes = lost = lost_top = 0
    for k in range(k0, k_last + fist + 1):
        u, v = path.at((k / b.fps - t_move) / dur)
        scale = FIST_SCALE[k - k_last - 1] if k > k_last else 1.0
        jw = scale * bw * (1.0 + rng.gauss(cfg.size_jitter))
        jh = scale * bh * (1.0 + rng.gauss(cfg.size_jitter))
        hx = cx + u * amp_px + rng.gauss(cfg.jitter * bw)
        hy = cy + v * amp_px + rng.gauss(cfg.jitter * bh)
        box = _visible_box(hx, hy, jw, jh)
        conf = rng.uniform(*cfg.conf)
        lost += box is None
        lost_top += hy < 0.0
        if box is None or rng.chance(cfg.p_miss):
            b.put(k, seg, None, None)
        else:
            b.put(k, seg, box, conf)
            n_boxes += 1
    n = k_last + fist + 1 - k0
    return {"kind": "gesture", "cls": cls, "name": CLASS_NAMES[cls], "side": side, "shape": shape_name,
            "reverse": reverse, "end": end, "t_start": t_start, "t0": rnd(t0, 4), "t_leave": b.t(k_last + fist),
            "dist_m": rnd(dist, 3), "h_m": rnd(h, 3), "amp_px": rnd(amp_px, 1), "fit": rnd(fit, 3),
            "dur_s": rnd(dur, 3), "cut": rnd(lost / n, 3), "cut_top": rnd(lost_top / n, 3), "n_boxes": n_boxes,
            "k_last": k_last + fist}


def _negative(b: _Builder, rng: Rng, seg: int, k0: int, kind: str) -> dict:
    cfg = b.cfg
    t_start = b.t(k0)
    n_boxes = 0
    info = {}
    if kind == "hand_still":
        dist = rng.uniform(*cfg.dist_m)
        bw, bh = _hand_px(cfg, dist)
        cx = rng.uniform(bw / 2 + cfg.margin_px, FRAME_W - bw / 2 - cfg.margin_px)
        cy = rng.uniform(bh / 2 + cfg.margin_px, FRAME_H - bh / 2 - cfg.margin_px)
        k_last = k0 + int(round(rng.uniform(*cfg.still_s) * b.fps))
        for k in range(k0, k_last + 1):
            box = _visible_box(cx + rng.gauss(0.02 * bw), cy + rng.gauss(0.02 * bh),
                               bw * (1.0 + rng.gauss(cfg.size_jitter)), bh * (1.0 + rng.gauss(cfg.size_jitter)))
            conf = rng.uniform(*cfg.conf)
            if box is None or rng.chance(cfg.p_miss / 2):
                b.put(k, seg, None, None)
            else:
                b.put(k, seg, box, conf)
                n_boxes += 1
        info = {"dist_m": rnd(dist, 3)}
    elif kind == "passerby":
        dist = rng.uniform(*cfg.passer_dist_m)
        bw, bh = _hand_px(cfg, dist)
        speed_px = cfg.f_px * rng.uniform(*cfg.passer_speed) / dist
        rightward = rng.chance(0.5)
        cy0 = rng.uniform(FRAME_H / 2, FRAME_H - bh / 2 - cfg.margin_px)
        span = FRAME_W + bw
        k_last = k0 + int(math.ceil(span / speed_px * b.fps))
        for k in range(k0, k_last + 1):
            dt = (k - k0) / b.fps
            x = -bw / 2 + speed_px * dt
            cx = x if rightward else FRAME_W - x
            cy = cy0 + 4.0 * math.sin(2.0 * math.pi * 1.8 * dt)
            box = _visible_box(cx, cy, bw, bh)
            conf = rng.uniform(0.36, 0.7)
            if box is None or rng.chance(cfg.passer_p_miss):
                b.put(k, seg, None, None)
            else:
                b.put(k, seg, box, conf)
                n_boxes += 1
        info = {"dist_m": rnd(dist, 3), "direction": "right" if rightward else "left"}
    elif kind == "false_boxes":
        rate = rng.uniform(*cfg.false_rate)
        k_last = k0 + int(round(rng.uniform(*cfg.false_s) * b.fps))
        for k in range(k0, k_last + 1):
            if rng.chance(rate / b.fps):
                size = rng.uniform(20.0, 120.0)
                asp = rng.uniform(0.6, 1.6)
                box = _visible_box(rng.uniform(0.0, FRAME_W), rng.uniform(0.0, FRAME_H), size * asp, size)
                if box is not None:
                    b.put(k, seg, box, rng.uniform(*cfg.false_conf))
                    n_boxes += 1
                    continue
            b.put(k, seg, None, None)
        info = {"rate_hz": rnd(rate, 3)}
    elif kind == "empty":
        k_last = k0 + int(round(rng.uniform(*cfg.empty_s) * b.fps))
        b.put(k0, seg, None, None)
    else:
        raise ValueError("неизвестный отрицательный сегмент %r" % kind)
    t_end = b.t(k_last)
    out = {"kind": "negative", "cls": -1, "name": kind, "side": None, "shape": None, "t_start": t_start,
           "t0": t_end, "t_leave": t_end, "n_boxes": n_boxes, "k_last": k_last}
    out.update(info)
    return out


def generate(cfg: GenConfig):
    """(header, timeline_records, truth_records)."""
    cfg.validate()
    b = _Builder(cfg)
    truth = []
    k = b.k_at_or_after(cfg.lead_in_s)
    for seg, (kind, cls, side, what) in enumerate(_plan(cfg)):
        rng = Rng(cfg.seed, "seg", seg, kind, what)
        if kind == "gesture":
            rec = _gesture(b, rng, seg, k, cls, side, what)
        else:
            rec = _negative(b, rng, seg, k, what)
        k_last = rec.pop("k_last")
        b.gap(k_last + 1)
        rec["seg"] = seg
        truth.append(rec)
        k = b.k_at_or_after((k_last + 1) / b.fps + rng.uniform(*cfg.pause_s))
    params = asdict(cfg)
    header = {"version": FORMAT_VERSION, "generator": "neurobot.sim.gestures.gesturegen", "seed": cfg.seed,
              "det_fps": cfg.det_fps, "frame": [FRAME_W, FRAME_H], "f_px": cfg.f_px, "classes": CLASS_NAMES,
              "shapes": {n: s.cls for n, s in sorted(SHAPES.items())}, "note": PROBE_NOTE,
              "duration_s": b.t(k), "n_segments": len(truth), "params": params}
    return header, b.compressed(), truth


def write_outputs(cfg: GenConfig, out_dir: str, force: bool = False):
    """Записать timeline.jsonl и truth.jsonl; без force существующие файлы не перезаписываются. Неверные параметры —
    ошибка до создания каталога."""
    cfg.validate()
    os.makedirs(out_dir, exist_ok=True)
    paths = (os.path.join(out_dir, TIMELINE_NAME), os.path.join(out_dir, TRUTH_NAME))
    existing = [p for p in paths if os.path.exists(p)]
    if existing and not force:
        raise FileExistsError("уже есть: %s (--force — перезаписать)" % ", ".join(existing))
    header, timeline, truth = generate(cfg)
    write_jsonl(paths[0], header, timeline)
    write_jsonl(paths[1], dict(header, timeline_sha256=file_sha256(paths[0])), truth)
    return paths


def _count(out: dict, key) -> None:
    out[key] = out.get(key, 0) + 1


def summarize(truth_path: str) -> dict:
    """Сводка истины; не истина gesturegen (нет timeline_sha256 в заголовке, например timeline.jsonl) — ValueError."""
    header, segs = read_jsonl(truth_path)
    if "timeline_sha256" not in header:
        raise ValueError("%s: не истина gesturegen — в заголовке нет timeline_sha256" % truth_path)
    out = {"seed": header["seed"], "det_fps": header["det_fps"], "f_px": header["f_px"],
           "duration_s": header["duration_s"], "timeline_sha256": header["timeline_sha256"], "gestures": {},
           "negatives": {}, "sides": {}, "ends": {}}
    gestures = [s for s in segs if s["kind"] == "gesture"]
    for s in segs:
        if s["kind"] == "gesture":
            _count(out["gestures"], "%d:%s" % (s["cls"], s["name"]))
            _count(out["ends"], s["end"])
            if s["side"]:
                _count(out["sides"], s["side"])
        else:
            _count(out["negatives"], s["name"])
    if gestures:
        boxes = sorted(s["n_boxes"] for s in gestures)
        out["boxes_per_gesture"] = {"min": boxes[0], "median": boxes[len(boxes) // 2], "max": boxes[-1],
                                    "below_6": sum(1 for x in boxes if x < 6)}
        out["cut"] = {"gestures_cut": sum(1 for s in gestures if s["cut"] > 0),
                      "gestures_cut_top_over_25pct": sum(1 for s in gestures if s["cut_top"] > 0.25),
                      "mean_cut": rnd(sum(s["cut"] for s in gestures) / len(gestures), 3),
                      "mean_cut_top": rnd(sum(s["cut_top"] for s in gestures) / len(gestures), 3)}
    return out


def _pair(s: str) -> tuple:
    a, b_ = s.split(",")
    return float(a), float(b_)


def _ints(s: str) -> tuple:
    return tuple(int(x) for x in s.split(",") if x)


def _names(s: str) -> tuple:
    return tuple(x for x in s.split(",") if x)


def build_parser() -> argparse.ArgumentParser:
    d = GenConfig()
    p = argparse.ArgumentParser(prog="python -m neurobot.sim.gestures.gesturegen",
                                description="Генератор таймлайна боксов руки и истины жестов (seed)")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen", help="сгенерировать timeline.jsonl и truth.jsonl")
    g.add_argument("--out-dir", required=True, help="каталог для timeline.jsonl и truth.jsonl")
    g.add_argument("--force", action="store_true", help="перезаписать существующие файлы")
    g.add_argument("--seed", type=int, default=d.seed, help="зерно: один seed — одинаковые байты")
    g.add_argument("--gestures", type=int, default=d.gestures, help="число жестов (классы по очереди)")
    g.add_argument("--negatives", type=int, default=d.negatives, help="число отрицательных сегментов")
    g.add_argument("--classes", type=_ints, default=d.classes, help="классы жестов, напр. 0,1,2,3")
    g.add_argument("--shapes", type=_names, default=d.shapes, help="подмножество форм: " + ",".join(sorted(SHAPES)))
    g.add_argument("--neg-kinds", type=_names, default=d.neg_kinds, help=",".join(NEGATIVE_KINDS))
    g.add_argument("--end", type=_names, default=d.end, help="концы жеста на выбор: " + ",".join(END_KINDS))
    g.add_argument("--fit", action="store_true", help="вписывать жест в кадр, место случайное (как до R7)")
    for name, help_ in (("det_fps", "темп детекции, к/с (сетка таймлайна)"),
                        ("f_px", "фокус камеры, px: 616 [оценка RS4] или 462"),
                        ("cam_h_m", "высота камеры над полом, м"),
                        ("hold_pre_s", "рука в кадре до начала движения, до N с"),
                        ("hold_post_s", "рука стоит после жеста (--end hold), до N с"),
                        ("rotate_deg", "поворот формы, ± градусы"), ("aspect", "растяжение формы по x, ± доля"),
                        ("jitter", "σ дрожания центра бокса, доля его размера"),
                        ("size_jitter", "σ дрожания размера бокса, доля"),
                        ("p_miss", "доля пропусков детекции руки"),
                        ("passer_p_miss", "доля пропусков детекции прохожего"),
                        ("lead_in_s", "пусто до первого сегмента, с"),
                        ("margin_px", "отступ от края кадра при размещении, px")):
        g.add_argument("--" + name.replace("_", "-"), dest=name, type=float, default=getattr(d, name), help=help_)
    g.add_argument("--hand-m", type=_pair, default=d.hand_m, help="ширина,высота бокса кисти, м")
    for name, help_ in (("dist_m", "дистанция до руки, м"), ("hand_h_m", "высота кисти над полом, м"),
                        ("amp_m", "размах жеста, м"), ("dur_s", "длительность жеста, с"),
                        ("pause_s", "пауза между сегментами, с"), ("conf", "уверенность боксов руки"),
                        ("still_s", "«рука стоит», с"), ("false_s", "«ложные боксы», с"),
                        ("false_rate", "ложных боксов в секунду"), ("false_conf", "уверенность ложных боксов"),
                        ("empty_s", "«пустой кадр», с"), ("passer_dist_m", "дистанция прохожего, м"),
                        ("passer_speed", "скорость прохожего, м/с")):
        g.add_argument("--" + name.replace("_", "-"), dest=name, type=_pair, default=getattr(d, name),
                       help=help_ + " (min,max)")
    i = sub.add_parser("info", help="сводка по истине (каталог или truth.jsonl)")
    i.add_argument("path")
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "gen":
            fields = {k: v for k, v in vars(a).items() if k in GenConfig.__dataclass_fields__}
            paths = write_outputs(GenConfig(**fields), a.out_dir, a.force)
            print("gesturegen: %s, %s" % paths, file=sys.stderr)
        else:
            path = os.path.join(a.path, TRUTH_NAME) if os.path.isdir(a.path) else a.path
            print(dumps(summarize(path)))
    except (OSError, ValueError) as e:
        print("gesturegen: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
