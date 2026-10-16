"""Тормозной путь, скорость по свободному пути и разгон с пределом ускорения и рывка.

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
    """Разгон по одной оси: ускорение ≤ accel, его рост ≤ jerk·dt (jerk 0 — без предела рывка).

    Торможение и смена знака — сразу: остановка не ждёт, её плавность держит L1 (пределы diff_drive_controller);
    при смене знака выход сначала падает до нуля.
    """

    def __init__(self, accel: float, jerk: float = 0.0):
        self.accel, self.jerk = accel, jerk
        self.v = self.a = 0.0

    def step(self, target: float, dt: float) -> float:
        v = self.v
        if target * v < 0.0 or abs(target) <= abs(v):
            self.v, self.a = (0.0 if target * v < 0.0 else target), 0.0
            return self.v
        if dt <= 0.0:
            return v
        a = min(self.accel, (abs(target) - abs(v)) / dt)
        if self.jerk > 0.0:
            a = min(a, self.a + self.jerk * dt)
        self.a = a
        self.v = math.copysign(min(abs(target), abs(v) + a * dt), target)
        return self.v
