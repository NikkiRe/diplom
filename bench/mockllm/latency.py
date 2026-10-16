"""Модель задержки ответа mock-llm (параметр сервера --latency).

Форматы (миллисекунды):
    0 | fixed:250                фиксированная;
    uniform:100:300              равномерная на [a, b];
    lognormal:800:0.5            логнормальная: медиана 800 мс, σ логарифма 0,5;
    normal:500:100               нормальная, обрезанная снизу нулём.
Генератор детерминирован по seed: одинаковая последовательность задержек при повторном прогоне.
"""
from __future__ import annotations

import math
import random
import threading


class LatencyModel:
    def __init__(self, spec: str = "0", seed: int = 0):
        self.spec = spec
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        parts = str(spec).strip().split(":")
        kind = parts[0].lower()
        try:
            nums = [float(x) for x in parts[1:]]
            if kind.replace(".", "", 1).isdigit():
                kind, nums = "fixed", [float(parts[0])]
        except ValueError as e:
            raise ValueError(f"bad latency spec {spec!r}") from e
        expected = {"fixed": 1, "uniform": 2, "lognormal": 2, "normal": 2}
        if kind not in expected or len(nums) != expected[kind]:
            raise ValueError(f"bad latency spec {spec!r}; use fixed:MS | uniform:A:B | lognormal:MEDIAN:SIGMA | "
                             f"normal:MEAN:SD")
        if any(x < 0 for x in nums):
            raise ValueError("latency parameters must be non-negative")
        self.kind, self.params = kind, nums

    def sample_ms(self) -> float:
        with self._lock:
            k, p = self.kind, self.params
            if k == "fixed":
                return p[0]
            if k == "uniform":
                return self._rng.uniform(min(p), max(p))
            if k == "lognormal":
                return math.exp(math.log(max(p[0], 1e-9)) + self._rng.gauss(0.0, p[1])) if p[0] > 0 else 0.0
            return max(0.0, self._rng.gauss(p[0], p[1]))
