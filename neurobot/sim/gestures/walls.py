"""Стены и препятствия мира и расстояние от контура робота до них (плоскость XY, только рыскание).

Препятствие — прямоугольник {name, cx, cy, yaw, sx, sy} или круг {name, cx, cy, r} в мировой СК. Источник — JSON-список
таких записей или SDF мира: модели верхнего уровня с именем по регулярному выражению, их коллизии-<box> и
вертикальные <cylinder> (поза модели ∘ звена ∘ коллизии; крен и тангаж не учитываются, <include> не раскрывается).
Касание считается в проекции на пол: высоты препятствий и частей робота не учитываются.

Контур робота — прямоугольник (x_min, x_max, y_min, y_max) или круг радиуса r вокруг начала модели (base_footprint,
его поза — /ground_truth/pose). По умолчанию — TurtleBot3 waffle_pi [код: turtlebot3_gazebo/models/
turtlebot3_waffle_pi/model.sdf]: base_collision 0,265×0,265 м со смещением −0,064 м по x, колёса до |y| = 0,153 м,
камера — до x = 0,08 м. Это контур модели стенда, а не робота лаборатории: у того круглая база Ø ≈ 0,39 м с краем в
≈ 0,21 м от оси лидара против 0,13 м у TB3 (R7b RS2, оценка по фото), поэтому касания на TB3 — оптимистичные. Для
модели стенда neurochip_base (neurobot/sim/stand, --base neurochip) контур — круг r = 0,195 м.
"""
from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

FOOTPRINT_TB3_WAFFLE_PI = (-0.197, 0.08, -0.153, 0.153)
RADIUS_NEUROCHIP_BASE = 0.195


@dataclass(frozen=True)
class Rect:
    name: str
    cx: float
    cy: float
    yaw: float
    sx: float
    sy: float

    def corners(self) -> list:
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        hx, hy = self.sx / 2, self.sy / 2
        return [(self.cx + c * x - s * y, self.cy + s * x + c * y)
                for x, y in ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy))]


@dataclass(frozen=True)
class Circle:
    name: str
    cx: float
    cy: float
    r: float


def footprint(x: float, y: float, yaw: float, fp=FOOTPRINT_TB3_WAFFLE_PI) -> list:
    x0, x1, y0, y1 = fp
    c, s = math.cos(yaw), math.sin(yaw)
    return [(x + c * px - s * py, y + s * px + c * py) for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]


def _project(poly, ax: float, ay: float):
    vals = [px * ax + py * ay for px, py in poly]
    return min(vals), max(vals)


def _separated(a: list, b: list) -> bool:
    for poly in (a, b):
        for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1]):
            ax, ay = y0 - y1, x1 - x0
            amin, amax = _project(a, ax, ay)
            bmin, bmax = _project(b, ax, ay)
            if amax < bmin or bmax < amin:
                return True
    return False


def _point_segment(px, py, x0, y0, x1, y1) -> float:
    dx, dy = x1 - x0, y1 - y0
    L2 = dx * dx + dy * dy
    u = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / L2))
    return math.hypot(px - (x0 + u * dx), py - (y0 + u * dy))


def point_poly_distance(px: float, py: float, poly: list) -> float:
    """Расстояние от точки до выпуклого многоугольника (0 — внутри или на границе)."""
    edges = list(zip(poly, poly[1:] + poly[:1]))
    cross = [(x1 - x0) * (py - y0) - (y1 - y0) * (px - x0) for (x0, y0), (x1, y1) in edges]
    if all(c >= 0 for c in cross) or all(c <= 0 for c in cross):
        return 0.0
    return min(_point_segment(px, py, x0, y0, x1, y1) for (x0, y0), (x1, y1) in edges)


def poly_distance(a: list, b: list) -> float:
    """Расстояние между выпуклыми многоугольниками (0 — пересекаются или касаются)."""
    if not _separated(a, b):
        return 0.0
    best = math.inf
    for p, q in ((a, b), (b, a)):
        for px, py in p:
            for (x0, y0), (x1, y1) in zip(q, q[1:] + q[:1]):
                best = min(best, _point_segment(px, py, x0, y0, x1, y1))
    return best


def _distance(x: float, y: float, yaw: float, fp, obstacle) -> float:
    if isinstance(fp, (int, float)):
        if isinstance(obstacle, Circle):
            return max(0.0, math.hypot(obstacle.cx - x, obstacle.cy - y) - obstacle.r - fp)
        return max(0.0, point_poly_distance(x, y, obstacle.corners()) - fp)
    robot = footprint(x, y, yaw, fp)
    if isinstance(obstacle, Circle):
        return max(0.0, point_poly_distance(obstacle.cx, obstacle.cy, robot) - obstacle.r)
    return poly_distance(robot, obstacle.corners())


def nearest_wall(walls: list, x: float, y: float, yaw: float, fp=FOOTPRINT_TB3_WAFFLE_PI):
    """(расстояние, имя) от контура робота (прямоугольник или радиус) до ближайшего препятствия; (inf, None) без них."""
    best = (math.inf, None)
    for w in walls:
        d = _distance(x, y, yaw, fp, w)
        if d < best[0]:
            best = (d, w.name)
    return best


def _wall(e: dict):
    if "r" in e:
        return Circle(str(e["name"]), float(e["cx"]), float(e["cy"]), float(e["r"]))
    return Rect(str(e["name"]), float(e["cx"]), float(e["cy"]), float(e.get("yaw", 0.0)), float(e["sx"]),
                float(e["sy"]))


def load_walls(path: str) -> list:
    """JSON-список препятствий. Нет файла — OSError; неверное содержимое — ValueError с именем файла."""
    with open(path, encoding="utf-8") as fh:
        try:
            entries = json.load(fh)
            if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
                raise TypeError("нужен JSON-список объектов")
            return [_wall(e) for e in entries]
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError("%s: препятствие — {name, cx, cy, yaw, sx, sy} или {name, cx, cy, r}: %s %s"
                             % (path, type(e).__name__, e)) from None


def _pose(el):
    p = el.find("pose") if el is not None else None
    if p is None or not (p.text or "").strip():
        return 0.0, 0.0, 0.0
    v = [float(x) for x in p.text.split()]
    return v[0], v[1], v[5]


def _compose(a, b):
    ax, ay, ayaw = a
    bx, by, byaw = b
    c, s = math.cos(ayaw), math.sin(ayaw)
    return ax + c * bx - s * by, ay + s * bx + c * by, ayaw + byaw


def walls_from_sdf(path: str, name_regex: str = "wall") -> list:
    """Коллизии-<box> и <cylinder> моделей мира, чьё имя подходит под name_regex. Нет файла — OSError; не SDF,
    неверное выражение, неверные числа или ни одного препятствия — ValueError."""
    try:
        pat = re.compile(name_regex)
    except re.error as e:
        raise ValueError("имена препятствий: неверное регулярное выражение %r: %s" % (name_regex, e)) from None
    try:
        world = ET.parse(path).getroot().find("world")
    except ET.ParseError as e:
        raise ValueError("%s: не XML: %s" % (path, e)) from None
    if world is None:
        raise ValueError("%s: нет <world>" % path)
    out = []
    for model in world.findall("model"):
        name = model.get("name", "")
        if pat.search(name):
            try:
                out += _model_walls(model, name)
            except (AttributeError, IndexError, TypeError, ValueError) as e:
                raise ValueError("%s: модель %s: %s %s" % (path, name, type(e).__name__, e)) from None
    if not out:
        raise ValueError("%s: нет препятствий с именем по %r" % (path, name_regex))
    return out


def _model_walls(model, name: str) -> list:
    out = []
    mp = _pose(model)
    for link in model.findall("link"):
        lp = _compose(mp, _pose(link))
        for col in link.findall("collision"):
            x, y, yaw = _compose(lp, _pose(col))
            full = "%s/%s/%s" % (name, link.get("name"), col.get("name"))
            size, radius = col.find("geometry/box/size"), col.find("geometry/cylinder/radius")
            if size is not None:
                sx, sy, _ = (float(v) for v in size.text.split())
                out.append(Rect(full, x, y, yaw, sx, sy))
            elif radius is not None:
                out.append(Circle(full, x, y, float(radius.text)))
    return out
