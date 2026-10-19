"""Глубина D435i для safety_gate: уменьшенная карта → точки в базе → препятствия, пол, обрыв, покрытие → память клеток.

Без PCL: только numpy. Точки переводятся в систему базы по позе оптического кадра из TF; пол — плоскость z = 0
базы (робот стоит ровно; наклон сторожит IMU). По высоте точки:
- препятствие — obstacle_min_h…obstacle_max_h: всё, во что упрётся корпус, в том числе ниже плоскости лидара
  (низкий ящик, ножки, сиденье) и нависания до верха робота (столешница на уровне URG);
- пол — от −drop_min до obstacle_min_h;
- ниже −drop_min — пола нет. Опасной помечается точка, где луч пересёк плоскость пола: там пол должен был быть
  (край ступеньки), а не далёкий нижний пол.

Камера на 0,66 м смотрит горизонтально (R7b §3.4): пол виден с ≈1,2 м, низкий ящик 0,15 м — с ≈0,93 м. Поэтому
клетки опасности хранятся в системе одометрии и переживают выход из поля зрения: ящик и край, увиденные издалека,
остаются на месте, когда робот к ним подъехал. Клетку очищает только кадр, где в ней или рядом (соседние клетки:
уменьшенная карта на 1–2 м кладёт точки пола через клетку) виден пол, а точек опасности в ней нет.

Свежий кадр ещё не значит, что глубина видит путь: закрытый объектив, засветка ИК, предмет ближе min-z, провал
стерео на чёрном или блестящем дают пиксели без данных — ни точек, ни опасностей. Поэтому у кадра есть покрытие
(Coverage): худшая по полосам поперёк курса доля пикселей с глубиной среди тех, что по геометрии смотрят на пол
коридора вперёд. Кадр с покрытием ниже depth_min_valid safety_gate не принимает — глубина устаревает.

Принятый кадр ещё и отвечает лидару (FreeSpace): точка плоскости скана свободна, если камера смотрит сквозь неё и видит
поверхность дальше. Так «нет данных» лидара (луч без отражения, чёрный предмет) впереди в поле камеры закрывает глубина;
про точку вне поля камеры кадр не говорит ничего (visible).
"""
from __future__ import annotations

import math
from typing import Dict, NamedTuple, Tuple

import numpy as np

from .config import Params
from .geometry import Pose

OBSTACLE, DROP = "obstacle", "drop"
BAND_M = 0.1  # м, ширина полосы коридора для покрытия: провал глубины такой ширины уже не «одиночные пиксели»
FREE_TOL_M = 0.05  # м: камера видит поверхность за точкой дальше на столько — точка свободна [допущение: клетка памяти]
FREE_BELOW_RAD = math.radians(10.0)  # пиксель точки без глубины: лучи ниже в том же столбце не дальше [допущение]

# Кодировки realsense2_camera (16UC1, мм; 0 — нет данных) и gz/ros_gz (32FC1, м).
ENCODINGS = {"16UC1": ("u2", 0.001), "mono16": ("u2", 0.001), "32FC1": ("f4", 1.0)}


class Camera(NamedTuple):
    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int


def image_array(encoding: str, data, height: int, width: int, step: int, bigendian: bool):
    """(массив height×width, множитель в метры); ValueError — кодировка или размер не те."""
    if encoding not in ENCODINGS:
        raise ValueError("кодировка глубины {!r}: ожидается {}".format(encoding, ", ".join(ENCODINGS)))
    kind, scale = ENCODINGS[encoding]
    dtype = np.dtype(kind).newbyteorder(">" if bigendian else "<")
    if step % dtype.itemsize or step < width * dtype.itemsize or len(data) < height * step:
        raise ValueError("размер кадра глубины: {}×{}, step {}, байт {}".format(width, height, step, len(data)))
    rows = np.frombuffer(data, dtype=dtype, count=height * step // dtype.itemsize).reshape(height, -1)
    return rows[:, :width], scale


class Sampler:
    """Пиксели уменьшенной карты (шаг ≈ width / cols по обеим осям) и их лучи в оптическом кадре
    (x вправо, y вниз, z вперёд; у выпрямленной глубины значение — z)."""

    def __init__(self, cam: Camera, cols: int):
        self.stride = max(1, int(round(cam.width / cols)))
        self.camera = cam
        self.rows = np.arange(self.stride // 2, cam.height, self.stride)
        self.cols = np.arange(self.stride // 2, cam.width, self.stride)
        u, v = np.meshgrid(self.cols, self.rows)
        self.dirs = np.stack(((u - cam.cx) / cam.fx, (v - cam.cy) / cam.fy, np.ones(u.shape)), axis=-1).reshape(-1, 3)

    def depths(self, image: np.ndarray, scale: float) -> np.ndarray:
        """Глубина пикселей уменьшенной карты, м; нет данных — 0."""
        d = image[np.ix_(self.rows, self.cols)].astype(float).reshape(-1) * scale
        return np.where(np.isfinite(d) & (d > 0.0), d, 0.0)

    def points(self, depth: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """(точки оптического кадра у пикселей с глубиной, признак «есть глубина» у каждого пикселя карты)."""
        ok = depth > 0.0
        return self.dirs[ok] * depth[ok, None], ok


class FreeSpace:
    """Свободна ли точка базы по кадру глубины: камера смотрит сквозь неё и видит поверхность дальше FREE_TOL_M (по
    оси камеры). В пикселе точки глубины нет (поверхность дальше дальности камеры: зал, проём, — или чёрное и блестящее)
    — точка свободна, если первый луч ниже в том же столбце (не дальше FREE_BELOW_RAD) уходит дальше неё: он проходит
    под точкой, а предмет, стоящий там от пола, его бы остановил; не свободна — предмет без глубины от пола до этой
    высоты. Точка за камерой или вне кадра — не свободна. visible — точка в поле камеры (впереди неё и внутри кадра):
    про точку вне поля кадр не говорит ничего."""

    def __init__(self, sampler: Sampler, pose: Pose, depth: np.ndarray):
        self.sampler, self.pose = sampler, pose
        self.depth = depth.reshape(len(sampler.rows), len(sampler.cols))
        self.below = max(1, int(math.ceil(FREE_BELOW_RAD * sampler.camera.fy / sampler.stride)))

    def _project(self, points: np.ndarray):
        """(в поле камеры, строка и столбец уменьшенной карты, глубина точки по оси камеры)."""
        s, cam = self.sampler, self.sampler.camera
        q = (points - self.pose.pos) @ self.pose.rot  # в оптический кадр: x вправо, y вниз, z вперёд
        z = q[:, 2]
        ahead = z > 0.0
        zz = np.where(ahead, z, 1.0)
        u, v = cam.fx * q[:, 0] / zz + cam.cx, cam.fy * q[:, 1] / zz + cam.cy
        inside = ahead & (u >= 0.0) & (u < cam.width) & (v >= 0.0) & (v < cam.height)
        i = np.clip(np.rint((v - s.rows[0]) / s.stride), 0, len(s.rows) - 1).astype(int)
        j = np.clip(np.rint((u - s.cols[0]) / s.stride), 0, len(s.cols) - 1).astype(int)
        return inside, i, j, z

    def __call__(self, points: np.ndarray) -> np.ndarray:
        inside, i, j, z = self._project(points)
        d = self.depth[i, j]
        free = inside & (d > 0.0) & (d >= z + FREE_TOL_M)
        hole = np.flatnonzero(inside & (d <= 0.0))
        if len(hole):
            rows = np.minimum(i[hole, None] + np.arange(1, self.below + 1), len(self.sampler.rows) - 1)
            col = self.depth[rows, j[hole, None]]
            first = np.argmax(col > 0.0, axis=1)
            below = col[np.arange(len(hole)), first]
            free[hole] = (below > 0.0) & (below >= z[hole] + FREE_TOL_M)
        return free

    def visible(self, points: np.ndarray) -> np.ndarray:
        return self._project(points)[0]


class Coverage:
    """Пиксели карты, которые при этой позе камеры смотрят на пол коридора вперёд: луч идёт вниз и встречает
    плоскость пола при x > 0, |y| ≤ robot_radius + lateral_margin, не дальше depth_max_m от камеры. Коридор поделён
    поперёк курса на полосы ≈BAND_M. Покрытие кадра — худшая по полосам доля таких пикселей с глубиной: на чём бы
    луч ни кончился (пол, препятствие, пол ниже края), этот участок коридора виден. Полоса без своих пикселей (вне
    поля камеры) — покрытие 0."""

    def __init__(self, sampler: Sampler, pose: Pose, p: Params):
        d = sampler.dirs @ pose.rot.T
        cam = pose.pos
        down = d[:, 2] < 0.0
        t = np.where(down, -cam[2] / np.where(down, d[:, 2], -1.0), 0.0)
        x, y = cam[0] + t * d[:, 0], cam[1] + t * d[:, 1]
        half = p.robot_radius + p.lateral_margin
        seen = down & (x > 0.0) & (np.abs(y) <= half) & (t * np.hypot(d[:, 0], d[:, 1]) <= p.depth_max_m)
        n = max(1, int(round(2 * half / BAND_M)))
        band = np.clip(((y + half) / (2 * half) * n).astype(int), 0, n - 1)
        self.bands = [np.flatnonzero(seen & (band == k)) for k in range(n)]

    def __call__(self, valid: np.ndarray) -> float:
        return min(float(np.mean(valid[i])) if len(i) else 0.0 for i in self.bands)


class Hazards(NamedTuple):
    obstacle: np.ndarray  # N×2, xy базы
    drop: np.ndarray      # M×2: где луч ушёл ниже пола
    floor: np.ndarray     # K×2: виден пол
    coverage: float       # покрытие коридора вперёд (Coverage), 0…1


def classify(points: np.ndarray, cam: np.ndarray, p: Params, coverage: float) -> Hazards:
    """Точки базы (N×3), положение камеры в базе и покрытие кадра → препятствия, обрыв, пол в пределах depth_max_m."""
    z = points[:, 2]
    rel = points[:, :2] - cam[:2]
    near = np.hypot(rel[:, 0], rel[:, 1]) <= p.depth_max_m
    obstacle = near & (z >= p.obstacle_min_h) & (z <= p.obstacle_max_h)
    floor = near & (z > -p.drop_min) & (z < p.obstacle_min_h)
    below = z < -p.drop_min
    t = cam[2] / (cam[2] - z[below])  # доля пути луча до плоскости пола
    cross = cam[:2] + rel[below] * t[:, None]
    cross = cross[np.hypot(*(cross - cam[:2]).T) <= p.depth_max_m]
    return Hazards(points[obstacle, :2], cross, points[floor, :2], coverage)


def _to_odom(xy: np.ndarray, pose: Tuple[float, float, float]) -> np.ndarray:
    x, y, yaw = pose
    c, s = math.cos(yaw), math.sin(yaw)
    return np.stack((x + c * xy[:, 0] - s * xy[:, 1], y + s * xy[:, 0] + c * xy[:, 1]), axis=1)


class Memory:
    """Клетки опасности в системе одометрии: (i, j) → OBSTACLE | DROP."""

    def __init__(self, p: Params):
        self.p = p
        self.cells: Dict[Tuple[int, int], str] = {}
        self._xy = np.zeros((0, 2))
        self._drop = np.zeros(0, dtype=bool)

    def _cells(self, xy: np.ndarray, pose, min_count: int) -> set:
        if not len(xy):
            return set()
        keys = np.floor(_to_odom(xy, pose) / self.p.cell_m).astype(np.int64)
        uniq, counts = np.unique(keys, axis=0, return_counts=True)
        return {(int(i), int(j)) for (i, j), c in zip(uniq, counts) if c >= min_count}

    def update(self, pose: Tuple[float, float, float], h: Hazards) -> None:
        obstacle = self._cells(h.obstacle, pose, self.p.min_pixels)
        drop = self._cells(h.drop, pose, self.p.min_pixels)
        touched = self._cells(h.obstacle, pose, 1) | self._cells(h.drop, pose, 1)
        floor = {(i + di, j + dj) for i, j in self._cells(h.floor, pose, 1) for di in (-1, 0, 1) for dj in (-1, 0, 1)}
        for key in floor - touched:
            self.cells.pop(key, None)
        self.cells.update(dict.fromkeys(obstacle, OBSTACLE))
        self.cells.update(dict.fromkeys(drop, DROP))
        keep = (self.p.memory_m / self.p.cell_m) ** 2
        cx, cy = pose[0] / self.p.cell_m, pose[1] / self.p.cell_m
        self.cells = {k: v for k, v in self.cells.items() if (k[0] + 0.5 - cx) ** 2 + (k[1] + 0.5 - cy) ** 2 <= keep}
        keys = list(self.cells)
        self._xy = (np.array(keys, dtype=float).reshape(-1, 2) + 0.5) * self.p.cell_m
        self._drop = np.array([self.cells[k] == DROP for k in keys], dtype=bool)

    def in_base(self, pose: Tuple[float, float, float]):
        """Центры клеток опасности в базе (N×2) и признак «обрыв» для каждой."""
        x, y, yaw = pose
        c, s = math.cos(yaw), math.sin(yaw)
        dx, dy = self._xy[:, 0] - x, self._xy[:, 1] - y
        return np.stack((c * dx + s * dy, -s * dx + c * dy), axis=1), self._drop
