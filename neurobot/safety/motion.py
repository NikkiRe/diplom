"""Тормозной путь, скорость по свободному пути, разгон и плавная остановка с пределом ускорения и рывка.

Тормозной путь отсчитывается от края корпуса, а не от оси лидара (R7b RS2: у демо запас до края ≈4 см):
d_stop(v) = v·t_lat + v²/(2·a_brake) + margin. Обратная задача — наибольшая скорость, с которой робот успеет
встать на свободном пути d: при приближении к препятствию разрешённая скорость падает плавно (замедление), а не
скачком 0 ↔ max.
"""
from __future__ import annotations

import math

from .config import Params


def stop_distance(p: Params, v: float) -> float:
    """Путь от края корпуса до полной остановки со скорости v, м (не число — бесконечность)."""
    if not math.isfinite(v):
        return math.inf
    v = max(v, 0.0)
    return v * p.t_lat + v * v / (2.0 * p.a_brake) + p.margin


def allowed_speed(p: Params, free_m: float) -> float:
    """Наибольшая v со stop_distance(v) ≤ free_m; inf — путь свободен, 0 — не больше запаса (или не число)."""
    if free_m == math.inf:
        return math.inf
    if not free_m > p.margin:
        return 0.0
    a, t = p.a_brake, p.t_lat
    return a * (math.sqrt(t * t + 2.0 * (free_m - p.margin) / a) - t)


class Ramp:
    """Скорость по одной оси: разгон с ускорением ≤ accel и его ростом ≤ jerk·dt (jerk 0 — без предела рывка),
    торможение — сразу или плавно.

    Плавно (soft, decel > 0) — S-кривая: замедление растёт не быстрее jerk·dt, не больше decel и спадает с тем же
    рывком, как только остатка скорости хватает лишь на спад (d²/(2·jerk)), поэтому мачта не получает ступени
    ускорения ни в начале, ни в конце остановки. Сразу —
    остановку держит L1 (пределы diff_drive_controller); так тормозит всё, что решает безопасность. При смене знака
    выход сначала падает до нуля.
    """

    def __init__(self, accel: float, jerk: float = 0.0, decel: float = 0.0):
        self.accel, self.jerk, self.decel = accel, jerk, decel
        self.v = self.a = self.d = 0.0

    def step(self, target: float, dt: float, soft: bool = False) -> float:
        v = self.v
        if target * v < 0.0 or abs(target) < abs(v):
            goal = 0.0 if target * v < 0.0 else target
            self.a = 0.0
            if not (soft and self.decel > 0.0):
                self.v, self.d = goal, 0.0
                return self.v
            if dt <= 0.0:
                return v
            gap = abs(v - goal)
            if self.jerk <= 0.0:
                d = self.decel
            elif self.d * self.d / (2.0 * self.jerk) >= gap:  # пора спадать: иначе в конце — ступень замедления
                d = max(self.d - self.jerk * dt, self.jerk * dt)
            else:
                d = min(self.decel, self.d + self.jerk * dt)
            self.d = min(d, gap / dt)
            self.v = goal if self.d * dt >= gap else v - math.copysign(self.d * dt, v)
            return self.v
        self.d = 0.0
        if target == v:
            self.a = 0.0
            return v
        if dt <= 0.0:
            return v
        a = min(self.accel, (abs(target) - abs(v)) / dt)
        if self.jerk > 0.0:
            a = min(a, self.a + self.jerk * dt)
        self.a = a
        self.v = math.copysign(min(abs(target), abs(v) + a * dt), target)
        return self.v
