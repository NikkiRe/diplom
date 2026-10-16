"""Формы жестов 4 классов SNN лаборатории — ПО ЗОНДИРОВАНИЮ ВЕСОВ И СЛАЙДУ 12, ДО ТАБЛИЦЫ «ФОРМА → КЛАСС».

Какие 4 класса выучила snn_emnist_small.pth, лаборатория не сообщила (ANALYSIS G6, R7b RS5). Два источника:
- зондирование весов (R1 §6.2, R6 §3) [замер на numpy-порте; настоящая сеть подтверждает, см. ниже]: 0 — круглые
  (O, C, 0), 1 — угловатые (V, N, 2), 2 — горизонтальный штрих / мах, 3 — вертикаль (1, |, /);
- словарь лаборатории (слайд 12 и GIF «Робот-сканер», R7b §4.1, §4.3, §4.5): круг → «Полный оборот» (0),
  дуга «вверх, через верх вправо и вниз» → «Вперёд» (1 по смыслу: come), горизонталь → направо/налево (2);
  класс 3 на слайде не заявлен. По GIF круг рисуют незамкнутым (¾), мах — с провалом и подъёмом в конце,
  встречается S-образная «зеркальная 2» (замысел по видео не определить).
Класс формы слайда 12 — замысел оператора, а не ответ сети. Что отвечает настоящая сеть, показывает shapeclass.py
(experiments/results/neurobot_20261014_gestures_fix/shapeclass, [замер, ориентир]): на полилинии формы R1 и мах
с подъёмом попадают в свой класс, S и «зеркальная 2» — в 1 (поэтому здесь 1), а крючок «Вперёд» — в 0 (реже
в 2 или 3), и ¾ круга — в 1: замысел и сеть расходятся, это вопрос лаборатории.
Это параметры стенда, а не истина: после ответа лаборатории таблица меняется здесь же.

Координаты нормированы: единичный квадрат [−0,5; 0,5]², x — вправо, y — вниз (как в кадре). Форма — кортеж
штрихов; на стыке штрихов рука замедляется до нуля (профиль minimum-jerk на каждом штрихе), внутри
штриха — плавно. Горизонтальные формы заданы для стороны left: в кадре детектора (после зеркала
cv2.flip в predict_live.py) рука уходит в +x, демо по первому и последнему боксу выбирает go_left
(predict_live.py:322–328); для right форма отражается по x.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

CLASS_NAMES = {0: "round", 1: "angular", 2: "horizontal", 3: "vertical"}
NEGATIVE_KINDS = ("hand_still", "passerby", "false_boxes", "empty")
PROBE_NOTE = ("формы классов — по зондированию весов SNN (R1 §6.2) и слайду 12 / GIF лаборатории (R7b §4), "
              "до таблицы «форма → класс» настоящей сетью")


@dataclass(frozen=True)
class Shape:
    name: str
    cls: int
    strokes: tuple


def _arc(cx: float, cy: float, rx: float, ry: float, a0_deg: float, a1_deg: float, n: int = 48) -> tuple:
    pts = []
    for i in range(n + 1):
        a = math.radians(a0_deg + (a1_deg - a0_deg) * i / n)
        pts.append((cx + rx * math.cos(a), cy + ry * math.sin(a)))
    return tuple(pts)


def _mirror(strokes: tuple) -> tuple:
    return tuple(tuple((-x, y) for x, y in st) for st in strokes)


_HOOK = (((-0.5, 0.5),) + _arc(0.0, 0.0, 0.5, 0.5, 180.0, 360.0, 32) + ((0.5, 0.2),),)
_TWO = (_arc(0.0, -0.15, 0.45, 0.35, 180.0, 360.0, 24), ((0.45, -0.15), (-0.5, 0.5)), ((-0.5, 0.5), (0.5, 0.5)))

SHAPES = {s.name: s for s in (
    # зондирование весов (R1 §6.2)
    Shape("circle", 0, (_arc(0.0, 0.0, 0.5, 0.5, -90.0, 288.0, 64),)),
    Shape("c_arc", 0, (_arc(0.0, 0.0, 0.5, 0.5, -45.0, -315.0),)),
    Shape("oval", 0, (_arc(0.0, 0.0, 0.32, 0.5, -90.0, 288.0, 64),)),
    Shape("v", 1, (((-0.5, -0.5), (0.0, 0.5)), ((0.0, 0.5), (0.5, -0.5)))),
    Shape("n", 1, (((-0.5, 0.5), (-0.5, -0.5)), ((-0.5, -0.5), (0.5, 0.5)), ((0.5, 0.5), (0.5, -0.5)))),
    Shape("two", 1, _TWO),
    Shape("swipe", 2, (((-0.5, 0.0), (0.5, 0.0)),)),
    Shape("wave", 2, (((-0.5, 0.0), (0.5, 0.0)), ((0.5, 0.0), (-0.5, 0.0)), ((-0.5, 0.0), (0.5, 0.0)))),
    Shape("down", 3, (((0.0, -0.5), (0.0, 0.5)),)),
    Shape("slash", 3, (((0.15, -0.5), (-0.15, 0.5)),)),
    Shape("beckon", 3, (((0.0, 0.5), (0.0, -0.5)), ((0.0, -0.5), (0.0, 0.5)), ((0.0, 0.5), (0.0, -0.5)))),
    # слайд 12 и GIF (R7b §4.3, §4.5): ¾ круга (GIF-1), крючок «Вперёд» в обоих зеркалах, мах с провалом и
    # подъёмом (GIF-3), «зеркальная 2» и S (GIF-4)
    Shape("arc34", 0, (_arc(0.0, 0.0, 0.5, 0.5, -135.0, 135.0),)),
    Shape("hook", 1, _HOOK),
    Shape("hook_m", 1, _mirror(_HOOK)),
    Shape("swipe_rise", 2, (((-0.5, 0.0), (-0.25, 0.06), (0.0, 0.08), (0.25, 0.04), (0.4, -0.06), (0.5, -0.3)),)),
    Shape("two_m", 1, _mirror(_TWO)),
    Shape("s", 1, (_arc(0.0, -0.25, 0.4, 0.25, -30.0, -270.0, 32) + _arc(0.0, 0.25, 0.4, 0.25, -90.0, 150.0, 32)[1:],)),
)}


def shapes_of(cls: int) -> list:
    return sorted(name for name, s in SHAPES.items() if s.cls == cls)


def transform(shape: Shape, side=None, reverse: bool = False, rot_deg: float = 0.0, aspect: float = 1.0) -> list:
    """Штрихи формы после отражения (side=right), обращения хода, поворота и растяжения по x."""
    strokes = [list(s) for s in shape.strokes]
    if side == "right":
        strokes = [[(-x, y) for x, y in s] for s in strokes]
    if reverse:
        strokes = [list(reversed(s)) for s in reversed(strokes)]
    c, s_ = math.cos(math.radians(rot_deg)), math.sin(math.radians(rot_deg))
    return [[(aspect * (c * x - s_ * y), s_ * x + c * y) for x, y in st] for st in strokes]
