"""Сторожа с защёлкой: наклон и удар по IMU, упор и пробуксовка по одометрии (PLAN §13.3)."""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

from .config import Params
from .geometry import quat_matrix


def tilt_deg(quat, base_in_imu: np.ndarray) -> float:
    """Наклон базы от вертикали по ориентации IMU в мире (REP 145; ENU, NWU или NED — берётся |cos|).

    base_in_imu — поворот базы в кадре IMU (v_imu = base_in_imu · v_база), из TF.
    """
    up = quat_matrix(*quat) @ (base_in_imu @ np.array([0.0, 0.0, 1.0]))
    return math.degrees(math.acos(min(1.0, abs(float(up[2])) / float(np.linalg.norm(up)))))


class Shock:
    """Всплеск ускорения: отклонение вектора от его медленного среднего (τ 0,5 с). Наклон и плавный разгон
    среднее догоняет, удар — нет."""

    TAU_S = 0.5

    def __init__(self):
        self._mean: Optional[np.ndarray] = None
        self._t: Optional[float] = None

    def feed(self, now: float, accel) -> float:
        a = np.asarray(accel, dtype=float)
        if self._mean is None or not np.all(np.isfinite(self._mean)):
            self._mean, self._t = a, now
            return 0.0
        dev = float(np.linalg.norm(a - self._mean))
        k = 1.0 - math.exp(-max(now - self._t, 0.0) / self.TAU_S)
        self._mean, self._t = self._mean + k * (a - self._mean), now
        return dev


class Stall:
    """Упор или пробуксовка: команда есть, она дошла до базы, а по одометрии база стоит дольше stall_s.
    Бережёт моторы и драйвер L298N; при пробуксовке на скользком полу одометрия «едет» — это не ловится."""

    def __init__(self, p: Params):
        self.p = p
        self._since: Optional[float] = None

    def feed(self, now: float, cmd_v: float, cmd_w: float, odom_v: float, odom_w: float, delivered: bool) -> bool:
        p = self.p
        commanded = abs(cmd_v) >= p.stall_cmd or abs(cmd_w) >= p.stall_turn_cmd
        still = abs(odom_v) < p.stall_odom and abs(odom_w) < p.stall_turn_odom
        if not (commanded and still and delivered):
            self._since = None
            return False
        if self._since is None:
            self._since = now
        return now - self._since >= p.stall_s
