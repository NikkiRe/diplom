"""Геометрия safety_gate без tf2: позы кадров из /tf_static, кватернионы, коридор корпуса.

tf2_ros не нужен: датчики стоят на роботе неподвижно, их позы относительно базы — статические преобразования
(robot_state_publisher, realsense2_camera). Своё дерево из /tf_static не тянет в процесс /tf с частотой одометрии.
"""
from __future__ import annotations

import math
from typing import Dict, NamedTuple, Optional, Sequence, Tuple

import numpy as np


def quat_matrix(x: float, y: float, z: float, w: float) -> np.ndarray:
    """Матрица поворота 3×3 по кватерниону (нормируется); ValueError — нулевой или не число."""
    n = x * x + y * y + z * z + w * w
    if not (math.isfinite(n) and n > 1e-12):
        raise ValueError("кватернион {} не годится".format((x, y, z, w)))
    s = 2.0 / n
    return np.array([
        [1 - s * (y * y + z * z), s * (x * y - z * w), s * (x * z + y * w)],
        [s * (x * y + z * w), 1 - s * (x * x + z * z), s * (y * z - x * w)],
        [s * (x * z - y * w), s * (y * z + x * w), 1 - s * (x * x + y * y)],
    ])


def yaw_of(x: float, y: float, z: float, w: float) -> float:
    """Рысканье кватерниона, рад."""
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


class Pose(NamedTuple):
    """Поза кадра в базе: v_база = rot · v_кадр + pos."""
    rot: np.ndarray
    pos: np.ndarray

    def then(self, inner: "Pose") -> "Pose":
        """Поза inner (заданная в этом кадре) в базе."""
        return Pose(self.rot @ inner.rot, self.rot @ inner.pos + self.pos)

    def apply(self, points: np.ndarray) -> np.ndarray:
        """Точки (N×3) кадра → точки базы."""
        return points @ self.rot.T + self.pos


IDENTITY = Pose(np.eye(3), np.zeros(3))


class StaticTf:
    """Дерево статических преобразований: у кадра один родитель (REP 105). Имена — без ведущего «/»."""

    def __init__(self):
        self._parent: Dict[str, Tuple[str, Pose]] = {}

    def add(self, parent: str, child: str, xyz: Sequence[float], quat: Sequence[float]) -> None:
        self._parent[child.lstrip("/")] = (parent.lstrip("/"), Pose(quat_matrix(*quat), np.asarray(xyz, float)))

    def lookup(self, base: str, frame: str) -> Optional[Pose]:
        """Поза frame в base или None, если frame не связан с base статикой."""
        base, frame = base.lstrip("/"), frame.lstrip("/")
        pose = IDENTITY
        seen = set()
        while frame != base:
            if frame in seen or frame not in self._parent:
                return None
            seen.add(frame)
            frame, step = self._parent[frame]
            pose = step.then(pose)
        return pose


def edge_distance(x: np.ndarray, y: np.ndarray, radius: float) -> np.ndarray:
    """Путь прямо вперёд, после которого край круга корпуса коснётся точки (x, y) базы; для |y| ≥ radius — x
    (точка в боковом запасе коридора: её заденет только расширенный корпус)."""
    yy = np.minimum(np.abs(y), radius)
    return x - np.sqrt(radius * radius - yy * yy)
