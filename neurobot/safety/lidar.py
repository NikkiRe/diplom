"""Лидар для safety_gate: лучи в системе базы, маска корпуса камеры, коридор вперёд, лучи «неизвестно».

Значения — по REP 117: +inf и конечные дальше range_max — «до range_max свободно»; NaN, −inf, конечные ниже range_min
(коды ошибок URG, R7a N14) и направления передней полуплоскости без лучей (скан обрезан или повёрнут) — «неизвестно».
urg_node2 отдаёт NaN и на «нет отражения» (открытая дверь, стена дальше range_max), и на ошибку: их не различить.

Неизвестный луч запрещает движение вперёд, только если его участок в коридоре движения выходит за корпус (решение
главного агента 16.10, находка W2 «ложные стопы у проёма»). Корпус здесь — круг R + lateral_margin (палубы с запасом
вбок), коридор — его след вперёд на d_stop(max_speed). Участок луча — от выхода из этого круга до выхода из полосы
|y| ≤ R + lateral_margin (у оси базы — (R + запас)/|sin θ|) или до переднего края следа. Луч у «плеча» (θ → 90°)
входит в коридор только внутри круга: его участок короче max_gap_m, и он не запрещает, а даёт предупреждение. Участок,
который свежий кадр глубины видит свободным (камера смотрит сквозь эту точку плоскости скана дальше), тоже не
запрещает. Остальные неизвестные лучи запрещают, если в их серии на дальнем конце участка поместится предмет шире
max_gap_m: одиночные коды ошибок и мерцающие лучи не останавливают робота, сектор без данных перед корпусом —
останавливает. Исключение (приёмка 17.10, nodata_unseen=slow): участок вне поля камеры глубины (у корпуса
слева-спереди) — как слепой сектор корпуса камеры справа: вперёд не быстрее blind_speed по глубине и её памяти; запрет
остаётся там, где камера участок видит, но свободным его не видит.
Лучи маски (корпус D435i перед URG, RS3) — «слепые»: перед в их секторе видит только глубина. Маска учится только в
секторе корпуса: «нет данных» обстановки на старте в неё не попадает.
"""
from __future__ import annotations

import math
from typing import NamedTuple, Optional, Sequence, Tuple

import numpy as np

from .config import Params
from .geometry import Pose, edge_distance
from .motion import stop_distance

# Точки участка неизвестного луча, которые проверяет глубина: доли от начала до конца участка.
DEPTH_PROBES = (0.0, 0.5, 1.0)


class ScanMeta(NamedTuple):
    angle_min: float
    angle_increment: float
    n: int
    range_min: float
    range_max: float


def check_meta(m: ScanMeta) -> Optional[str]:
    """Причина, по которой скан негоден, или None."""
    if m.n == 0:
        return "пустой скан"
    if not all(math.isfinite(v) for v in (m.angle_min, m.angle_increment, m.range_min, m.range_max)):
        return "метаданные — не числа: {}".format(tuple(m))
    if m.angle_increment == 0.0:
        return "angle_increment = 0"
    if m.range_min < 0.0 or m.range_max <= m.range_min:
        return "диапазон дальностей {}…{}".format(m.range_min, m.range_max)
    return None


class Rays:
    """Геометрия лучей скана в базе по позе лидара из TF (поворот учитывает и лидар «вверх ногами»).

    Направления передней полуплоскости по возрастанию пеленга (dir_bearing) — лучи скана (dir_ray — индекс) и
    пропуски: где соседние лучи (или луч и ±90°) дальше 1,5 шага, через шаг добавлены направления без луча
    (dir_ray = −1), они «неизвестно».
    """

    def __init__(self, meta: ScanMeta, laser: Pose):
        local = meta.angle_min + np.arange(meta.n) * meta.angle_increment
        dirs = np.stack((np.cos(local), np.sin(local), np.zeros(meta.n)), axis=1) @ laser.rot.T
        self.meta = meta
        self.ox, self.oy, self.oz = (float(v) for v in laser.pos)
        self.bearing = np.arctan2(dirs[:, 1], dirs[:, 0])
        self.cos, self.sin = np.cos(self.bearing), np.sin(self.bearing)
        front = np.flatnonzero(np.abs(self.bearing) < math.pi / 2)
        self.front = front[np.argsort(self.bearing[front])]
        step = abs(meta.angle_increment)
        bearing, ray = [], []
        edges = [(-math.pi / 2, -1)] + [(float(self.bearing[i]), int(i)) for i in self.front] + [(math.pi / 2, -1)]
        for (lo, i), (hi, _) in zip(edges[:-1], edges[1:]):
            if i >= 0:
                bearing.append(lo)
                ray.append(i)
            if hi - lo > 1.5 * step:
                fill = lo + step * np.arange(1, int(round((hi - lo) / step)))
                bearing.extend(float(b) for b in fill)
                ray.extend([-1] * len(fill))
        self.dir_bearing, self.dir_ray = np.array(bearing), np.array(ray, dtype=int)
        self._corridor = {}

    def sector(self, lo_deg: float, hi_deg: float) -> np.ndarray:
        """Лучи с пеленгом в базе lo_deg…hi_deg."""
        b = np.degrees(self.bearing)
        return (b >= lo_deg) & (b <= hi_deg)

    def corridor(self, p: Params) -> Tuple[np.ndarray, np.ndarray]:
        """Участок каждого направления в коридоре за корпусом: (от, до) по лучу от лидара, м; до ≤ от — участка нет.
        Корпус — круг w = robot_radius + lateral_margin; участок начинается на выходе из него и кончается на выходе
        из полосы |y| ≤ w или на переднем крае того же круга, сдвинутого вперёд на d_stop(max_speed)."""
        if p not in self._corridor:
            w = p.robot_radius + p.lateral_margin
            c, s = np.cos(self.dir_bearing), np.sin(self.dir_bearing)

            def leave(cx: float) -> np.ndarray:
                """Выход луча через передний край окружности w вокруг (cx, 0); не выходит через него — inf."""
                qx = self.ox - cx
                b = qx * c + self.oy * s
                disc = b * b - (qx * qx + self.oy * self.oy - w * w)
                r = -b + np.sqrt(np.maximum(disc, 0.0))
                return np.where((disc >= 0.0) & (qx + r * c >= 0.0), r, np.inf)
            start = leave(0.0)
            side = np.abs(s) > 1e-12
            strip = np.full(len(s), np.inf)
            strip[side] = (w - self.oy * np.sign(s[side])) / np.abs(s[side])
            end = np.where(c > 0.0, np.minimum(strip, leave(stop_distance(p, p.max_speed))), start)
            self._corridor[p] = (start, end)
        return self._corridor[p]


class MaskLearner:
    """Маска слепых лучей — корпуса камеры перед лидаром (RS3).

    Учится по первым mask_scans сканам: луч в секторе корпуса (body), давший «нет данных» или попадание в свой корпус
    не реже mask_ratio, плюс mask_dilate лучей по краям (край корпуса мерцает), но не за сектором. Такие же лучи вне
    сектора — обстановка (дверной проём, стена дальше range_max, чёрный предмет) или сдвинутая камера: в маску не
    идут, они — в outside. Выученная маска только сужается: луч, давший mask_scans сканов подряд попадание дальше
    корпуса, корпусом не закрыт. Меняется число лучей — учится заново.
    """

    def __init__(self, p: Params):
        self.p = p
        self.mask: Optional[np.ndarray] = None
        self.outside: Optional[np.ndarray] = None
        self._count: Optional[np.ndarray] = None
        self._run: Optional[np.ndarray] = None
        self.seen = 0

    def feed(self, blind: np.ndarray, body: np.ndarray) -> Optional[np.ndarray]:
        """Скан, пока маска не выучена; маска или None."""
        if self._count is None or len(self._count) != len(blind):
            self._count, self.seen, self.mask = np.zeros(len(blind)), 0, None
        if self.mask is not None:
            return self.mask
        self._count += blind
        self.seen += 1
        if self.seen >= self.p.mask_scans:
            core = self._count >= self.p.mask_ratio * self.seen
            mask = core & body
            grown = mask.copy()
            for s in range(1, self.p.mask_dilate + 1):
                grown[s:] |= mask[:-s]
                grown[:-s] |= mask[s:]
            self.mask, self.outside = grown & body, core & ~body
            self._run = np.zeros(len(blind), dtype=int)
        return self.mask

    def watch(self, beyond: np.ndarray) -> np.ndarray:
        """Скан после обучения: лучи с попаданием дальше корпуса (beyond); снятые с маски лучи."""
        self._run = np.where(beyond, self._run + 1, 0)
        freed = self.mask & (self._run >= self.p.mask_scans)
        self.mask = self.mask & ~freed
        return freed


class LidarView(NamedTuple):
    front_m: float                           # путь вперёд до ближайшей точки в коридоре, от края корпуса; inf — нет
    clearance_m: float                       # от края корпуса до ближайшей точки в любом направлении; inf — нет
    touch: bool                              # точка ближе radius + turn_margin: поворот на месте заденет
    nodata: Optional[Tuple[float, float]]    # серия неизвестных лучей, запрещающая вперёд (градусы), или None
    unseen: Optional[Tuple[float, float]]    # серия неизвестных лучей вне поля глубины: как слепой сектор, или None
    unknown: list                            # неизвестные лучи впереди, которые не ограничивают: [[от, до], …] градусов
    blind: bool                              # лучи маски смотрят вперёд: перед в их секторе — только глубина


def classify(ranges: Sequence[float], meta: ScanMeta):
    """(дальности, попадание, «неизвестно») по каждому лучу, REP 117: попадание — range_min…range_max; +inf и
    дальше range_max — свободно; NaN, −inf и ниже range_min — неизвестно."""
    r = np.asarray(ranges, dtype=float)
    finite = np.isfinite(r)
    hit = finite & (r >= meta.range_min) & (r <= meta.range_max)
    unknown = np.isnan(r) | (r == -np.inf) | (finite & (r < meta.range_min))
    return r, hit, unknown


def self_hits(ranges: Sequence[float], rays: Rays, radius: float) -> Tuple[np.ndarray, np.ndarray]:
    """(лучи «неизвестно» или с попаданием внутрь круга корпуса — кандидаты в маску; лучи с попаданием дальше)."""
    r, hit, unknown = classify(ranges, rays.meta)
    rr = np.where(hit, r, 0.0)
    inside = np.hypot(rays.ox + rr * rays.cos, rays.oy + rr * rays.sin) < radius
    return unknown | (hit & inside), hit & ~inside


def _runs(flags: np.ndarray):
    """Серии True: массивы начал и концов (конец — за последним элементом)."""
    d = np.diff(np.concatenate(([0], flags.astype(np.int8), [0])))
    return np.flatnonzero(d == 1), np.flatnonzero(d == -1)


class Unknown(NamedTuple):
    nodata: Optional[Tuple[float, float]]    # серия, запрещающая вперёд (градусы), или None
    unseen: Optional[Tuple[float, float]]    # серия вне поля глубины — как слепой сектор (nodata_unseen=slow) или None
    other: list                              # неизвестные лучи впереди, которые не ограничивают: [[от, до], …]


def unknown_front(unknown: np.ndarray, mask: np.ndarray, rays: Rays, p: Params, free=None) -> Unknown:
    """Неизвестные направления перед роботом. Запрещает вперёд серия, в которой участок в коридоре за корпусом не
    короче max_gap_m, на дальнем конце участка поместится предмет шире max_gap_m, а кадр глубины (free: точки базы →
    свободны ли, free.visible — в поле ли камеры) участок свободным не видит: точка в поле камеры, но не свободна, или
    кадра нет. Участок, точки которого камера видит свободными или не видит вовсе, при nodata_unseen=slow — как слепой
    сектор корпуса камеры: только глубина и её память, не быстрее blind_speed. Вне поля камеры — начало участка у
    корпуса на пеленгах дальше ≈25° от курса: камера стоит всего на 7 см впереди лидара (поле 87°) [расчёт по
    геометрии двойника, test_safety_parts.py]."""
    real = rays.dir_ray >= 0
    flags = np.ones(len(rays.dir_ray), dtype=bool)
    flags[real] = unknown[rays.dir_ray[real]] & ~mask[rays.dir_ray[real]]
    start, end = rays.corridor(p)
    deep = flags & (end - start >= p.max_gap_m)
    hidden = np.zeros(len(deep), dtype=bool)
    if free is not None and deep.any():
        k = np.flatnonzero(deep)
        r = start[k, None] + (end - start)[k, None] * np.asarray(DEPTH_PROBES)
        b = rays.dir_bearing[k, None]
        pts = np.stack((rays.ox + r * np.cos(b), rays.oy + r * np.sin(b), np.full(r.shape, rays.oz)), axis=-1)
        pts = pts.reshape(-1, 3)
        ok, seen = free(pts).reshape(r.shape), free.visible(pts).reshape(r.shape)
        clear = ok.all(axis=1)
        deep[k[clear]] = False
        if p.nodata_unseen == "slow":
            out = ~clear & (ok | ~seen).all(axis=1)  # ни одной точки «в поле, но не свободна»
            hidden[k[out]], deep[k[out]] = True, False
    deg = np.degrees(rays.dir_bearing)
    step = abs(rays.meta.angle_increment)

    def wide(flagged: np.ndarray) -> Optional[Tuple[float, float]]:
        for lo, hi in zip(*_runs(flagged)):
            if (hi - lo) * step * float(np.max(end[lo:hi])) > p.max_gap_m:
                return round(float(deg[lo]), 1), round(float(deg[hi - 1]), 1)
        return None
    nodata, unseen = wide(deep), wide(hidden)
    rest = flags & ~hidden if unseen else flags
    other = [] if nodata else [[round(float(deg[lo]), 1), round(float(deg[hi - 1]), 1)] for lo, hi in zip(*_runs(rest))]
    return Unknown(nodata, unseen, other)


def view(ranges: Sequence[float], rays: Rays, mask: np.ndarray, p: Params, free=None) -> LidarView:
    r, hit, unknown = classify(ranges, rays.meta)
    rr = np.where(hit, r, 0.0)
    x, y = rays.ox + rr * rays.cos, rays.oy + rr * rays.sin
    dist = np.hypot(x, y)
    real = hit & ~(mask & (dist < p.robot_radius))  # попадания маски в свой корпус — не препятствие
    radius = p.robot_radius
    clearance = float(np.min(dist[real]) - radius) if real.any() else math.inf
    corridor = real & (x > 0.0) & (np.abs(y) <= radius + p.lateral_margin)
    front = float(np.min(edge_distance(x[corridor], y[corridor], radius))) if corridor.any() else math.inf
    u = unknown_front(unknown, mask, rays, p, free)
    return LidarView(max(front, 0.0), max(clearance, 0.0), clearance < p.turn_margin, u.nodata, u.unseen, u.other,
                     bool(mask[rays.front].any()))


def mask_spans(mask: np.ndarray, rays: Rays) -> list:
    """Сектора маски в градусах [[от, до], …] — для журнала и safety_state."""
    order = np.argsort(rays.bearing)
    flags = mask[order]
    out = []
    i = 0
    while i < len(flags):
        if flags[i]:
            j = i
            while j + 1 < len(flags) and flags[j + 1]:
                j += 1
            out.append([round(math.degrees(rays.bearing[order[i]]), 1), round(math.degrees(rays.bearing[order[j]]), 1)])
            i = j + 1
        else:
            i += 1
    return out
