"""safety_gate (neurobot/safety/gate.py) — таблица сценариев без rclpy, Python ≥ 3.8.

Подставной мир: скан URG робота, одометрия, IMU, опасности глубины в базе. Геометрия — как у двойника и робота
(R7b §2.3, RS15): URG-04LX 683 луча ±2,0923497677 рад, 0,02–5,6 м, ось лидара в (−0,015; 0) базы; блок NaN
−60,8…+4,2° — корпус D435i (RS3). Сценарии: блокировка старта и взвод, стена на разных скоростях и разгон в стену,
коридор корпуса вблизи, неизвестные лучи (REP 117: NaN, −inf, коды ошибок, пропуски) — запрет только по участку в
коридоре за корпусом, позы мира lab двойника без ложного стопа, неизвестное, видимое глубиной, участок вне поля
глубины — как слепой сектор (nodata_unseen), вырожденный скан, маска — только корпус камеры, низкий ящик в памяти
глубины, обрыв, кадр глубины без данных, наклон, удар, упор и пробуксовка, устаревшие данные, джойстик, защёлка
(снятие — только файлом каталога root), чужой писатель, второй и чужой издатели /scan, нечисловой запрос, пределы,
задний ход по одометрии, разгон и рывок, плавная остановка по запросу и резкая по причине безопасности и по стопу
агента (без защёлки), дребезг у порога, время движения, геозабор, касание (лидар; клетка глубины — с ошибкой
depth_err_m, она же в пути вперёд).
Выход процесса — test_safety_node.py.
"""
from __future__ import annotations

import collections
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from neurobot.safety.config import Params  # noqa: E402
from neurobot.safety.depth import Hazards  # noqa: E402
from neurobot.safety.gate import BLOCKED, ESTOP, OK, SLOW, TILT_AGREE_S, Gate  # noqa: E402
from neurobot.safety.geometry import Pose  # noqa: E402
from neurobot.safety.latch import EstopCmd, Latch, ReleaseReq, StopCmd  # noqa: E402
from neurobot.safety.lidar import ScanMeta  # noqa: E402
from neurobot.safety.motion import allowed_speed, stop_distance  # noqa: E402

N = 683
AMIN = -2.0923497677
INC = 2 * -AMIN / (N - 1)
META = ScanMeta(AMIN, INC, N, 0.02, 5.6)
LASER = Pose(np.eye(3), np.array([-0.015, 0.0, 0.67]))
RS3 = range(168, 354)  # лучи блока NaN в скане с робота 02.06
NONE = np.zeros((0, 2))


def ray(deg: float) -> int:
    """Индекс луча, ближайшего к углу (градусы)."""
    return int(round((math.radians(deg) - AMIN) / INC))


def seen(obstacle=NONE, drop=NONE, floor=NONE, coverage: float = 1.0) -> Hazards:
    """Опасности кадра глубины; по умолчанию кадр видит коридор вперёд целиком (покрытие 1)."""
    return Hazards(obstacle, drop, floor, coverage)


def room(rs3: bool = True, wall_x: float = math.inf, points=(), nan=()) -> list:
    """Скан в комнате 3 м: стена поперёк курса на x = wall_x базы, точки (x, y) базы — препятствия, nan — лучи
    «нет данных», rs3 — корпус камеры."""
    ox, oy = float(LASER.pos[0]), float(LASER.pos[1])
    ranges = []
    for i in range(N):
        b = AMIN + i * INC
        r = 3.0
        if math.cos(b) > 1e-6 and math.isfinite(wall_x):
            r = min(r, (wall_x - ox) / math.cos(b))
        ranges.append(r)
    for x, y in points:
        d = math.hypot(x - ox, y - oy)
        i = ray(math.degrees(math.atan2(y - oy, x - ox)))
        ranges[i] = min(ranges[i], d)
    for i in list(RS3 if rs3 else ()) + list(nan):
        ranges[i] = math.nan
    return ranges


class World:
    """Gate и его датчики. t — монотонное время теста; step() — подать датчики и сделать шаг решения."""

    def __init__(self, rs3: bool = True, latch_path: str = "", **params):
        self.p = Params(**params)
        self.dir = tempfile.TemporaryDirectory()
        self.latch_path = latch_path or str(Path(self.dir.name) / "estop.json")
        self.latch = Latch(self.latch_path)
        self.gate = Gate(self.p, self.latch)
        self.t = 0.0
        self.rs3 = rs3
        self.ranges = room(rs3)
        self.meta = META
        self.pose = (0.0, 0.0, 0.0)
        self.vw = (0.0, 0.0)
        self.tilt = 0.3
        self.tilt_acc = 0.3    # наклон по ускорению /camera/imu (в покое — верный)
        self.accel = (0.0, 0.0, 9.81)
        self.hazards = seen()
        self.free = None       # FreeSpace кадра глубины для неизвестных лучей лидара (None — кадр её не даёт)
        self.mux = True        # выход twist_mux повторяет нашу команду
        self.follow = True     # одометрия повторяет нашу команду (база без инерции); False — vw задаёт тест
        self.last = None

    def feed(self, scan=True, odom=True, imu=True, depth=True) -> None:
        g, t = self.gate, self.t
        if scan:
            g.scan(t, self.ranges, self.meta, LASER)
        if self.follow and self.last is not None:
            self.vw = (self.last.v, self.last.w)
        if odom:
            g.odom(t, *self.pose, *self.vw)
        if imu:
            g.imu_accel(t, self.accel, self.tilt_acc)
            g.imu_tilt(t, self.tilt)
        if depth:
            g.depth(t, self.hazards, self.free)
        if self.mux and self.last is not None:
            g.mux(t, self.last.v, self.last.w)

    def step(self, req=None, dt: float = 0.05, **feed):
        self.t += dt
        self.feed(**feed)
        if req is not None:
            self.gate.request(self.t, *req)
        self.last = self.gate.tick(self.t)
        return self.last

    def run(self, seconds: float, req=None, **feed):
        d = None
        for _ in range(int(round(seconds / 0.05))):
            d = self.step(req, **feed)
        return d

    def start(self):
        """Маска выучена (≥ mask_scans сканов), исполнитель шлёт ноль — взвод."""
        return self.run(0.05 * (self.p.mask_scans + 2), (0.0, 0.0))

    def close(self) -> None:
        self.dir.cleanup()


class Frame:
    """Ответ кадра глубины неизвестным лучам лидара, как у FreeSpace: в поле ли точки камеры (visible: bool или
    функция точек базы → bool) и свободны ли (free — только точки в поле); asked — точки, о которых спрашивали."""

    def __init__(self, free: bool, visible=True):
        self.free, self.vis, self.asked = free, visible, []

    def __call__(self, points):
        self.asked.append(points)
        return np.full(len(points), self.free) & self.visible(points)

    def visible(self, points):
        return np.asarray(self.vis(points), bool) if callable(self.vis) else np.full(len(points), self.vis)


def human(latch: Latch, t: float, id_: str = "") -> ReleaseReq:
    """Снятие человеком (файл root от sudo neurobot estop release): защёлка, которую он видел."""
    return ReleaseReq(id_ or "release-{}".format(t), t, latch.since, tuple(latch.reasons), "проверка")


def face(x: float, y0: float = -0.15, y1: float = 0.15) -> np.ndarray:
    """Передняя грань предмета на x базы: по три точки на каждые 2 см (клетке нужно ≥ min_pixels)."""
    ys = np.arange(y0, y1 + 1e-9, 0.02)
    return np.array([(x + dx, y) for y in ys for dx in (0.0, 0.005, 0.01)])


class Base:
    """База за safety_gate: команда доходит с задержкой delay_s, разгон ≤ accel, торможение ≤ decel (L1)."""

    def __init__(self, delay_s=0.1, accel=1.0, decel=0.8):
        self.queue = collections.deque([0.0] * int(round(delay_s / 0.05)))
        self.accel, self.decel = accel, decel
        self.v = self.x = 0.0

    def step(self, cmd: float, dt: float = 0.05) -> None:
        self.queue.append(cmd)
        target = self.queue.popleft()
        dv = target - self.v
        self.v += max(-self.decel * dt, min(self.accel * dt, dv))
        self.x += self.v * dt


class Case(unittest.TestCase):
    def world(self, **kw) -> World:
        w = World(**kw)
        self.addCleanup(w.close)
        return w


class StartAndArming(Case):
    def test_no_sensors_no_motion(self):
        w = self.world()
        d = w.step((0.2, 0.3), scan=False, odom=False, imu=False, depth=False)
        self.assertEqual((d.v, d.w, d.state), (0.0, 0.0, BLOCKED))
        for code in ("stale_scan", "stale_odom", "stale_imu", "stale_accel", "stale_depth"):
            self.assertIn(code, d.codes)
        self.assertEqual((d.v_max, d.v_rev, d.w_max), (0.0, 0.0, 0.0))

    def test_each_missing_sensor_blocks_start(self):
        for missing, code in (("scan", "stale_scan"), ("odom", "stale_odom"), ("imu", "stale_imu"),
                              ("depth", "stale_depth")):
            w = self.world()
            d = w.run(1.5, (0.0, 0.0), **{missing: False})
            self.assertEqual(d.state, BLOCKED, missing)
            self.assertIn(code, d.codes, missing)

    def test_mask_learning_then_armed_by_zero_request(self):
        w = self.world()
        d = w.step((0.0, 0.0))
        self.assertIn("lidar_mask", d.codes)
        d = w.start()
        self.assertEqual(d.state, SLOW)  # перед — в слепом секторе: только глубина, не быстрее blind_speed
        self.assertEqual(d.codes, ("lidar_blind",))
        self.assertEqual(w.gate.mask_deg, [[-61.5, 4.9]])  # блок RS3 −60,8…+4,2° и расширение на 2 луча
        d = w.run(1.0, (0.3, 0.0))
        self.assertAlmostEqual(d.v, w.p.blind_speed)

    def test_motion_does_not_resume_after_start(self):
        w = self.world()
        d = w.run(2.0, (0.1, 0.2))  # исполнитель «продолжает» манёвр, начатый до рестарта safety_gate
        self.assertEqual((d.v, d.w, d.state), (0.0, 0.0, BLOCKED))
        self.assertIn("unarmed", d.codes)
        self.assertEqual((d.v_max, d.w_max), (0.0, 0.0))
        w.step((0.0, 0.0))
        d = w.run(0.5, (0.1, 0.2))
        self.assertGreater(d.v, 0.0)
        self.assertGreater(d.w, 0.0)


class Wall(Case):
    """Стена поперёк курса, лидар без слепого сектора: предел вперёд — по тормозному пути от края корпуса."""

    def test_limit_by_distance_table(self):
        p = Params()
        block = stop_distance(p, p.min_speed)
        for edge in (1.5, 0.30, 0.20, 0.152, 0.12, 0.08, 0.06, 0.057, 0.04, 0.0):
            w = self.world(rs3=False)
            w.ranges = room(False, wall_x=p.robot_radius + edge)
            w.start()
            d = w.step((0.25, 0.0))
            want = 0.0 if edge < block else min(p.max_speed, allowed_speed(p, edge))
            self.assertAlmostEqual(d.v_max, want, places=2, msg=edge)
            self.assertEqual(d.state, BLOCKED if want == 0.0 else SLOW if want < p.max_speed else OK, edge)
            self.assertAlmostEqual(d.front_m, edge, places=2)
            self.assertLessEqual(d.v, d.v_max)
            # повороты на месте разрешены (база круглая), пока стена не ближе turn_margin
            self.assertEqual(d.w_max, p.max_turn if edge >= p.turn_margin else 0.0, edge)

    def test_drive_into_wall_at_max_speed_stops_with_margin(self):
        p = Params()
        for delay in (0.05, 0.1, 0.15):  # задержка до колёс: доставка, ПИД STM32 (R7b §5.2) — меньше t_lat
            w = self.world(rs3=False)
            w.follow = False
            wall = 1.2
            w.ranges = room(False, wall_x=wall)
            w.start()
            base, top = Base(delay), 0.0
            for _ in range(300):
                w.ranges, w.pose, w.vw = room(False, wall_x=wall - base.x), (base.x, 0.0, 0.0), (base.v, 0.0)
                base.step(w.step((0.25, 0.0)).v)
                top = max(top, base.v)
            gap = wall - base.x - p.robot_radius
            self.assertGreater(top, 0.24, delay)
            self.assertLess(abs(base.v), 1e-6, delay)
            self.assertGreater(gap, 0.03, delay)
            self.assertLess(gap, 0.12, delay)  # не встаёт сильно раньше: замедление плавное, без запаса ×3

    def test_debounce_near_threshold(self):
        p = Params()
        block = stop_distance(p, p.min_speed)
        w = self.world(rs3=False)
        w.start()
        states = []
        for k in range(60):  # шум лидара ±1 см у порога
            w.ranges = room(False, wall_x=p.robot_radius + block + (0.01 if k % 2 else -0.01))
            states.append(w.step((0.25, 0.0)).v_max == 0.0)
        self.assertLessEqual(sum(a != b for a, b in zip(states, states[1:])), 1)
        w.ranges = room(False, wall_x=p.robot_radius + block + p.release_hyst + 0.02)
        d = w.step((0.25, 0.0))
        self.assertEqual(d.v_max, 0.0)  # путь освободился, но ещё не release_s
        d = w.run(p.release_s + 0.05, (0.25, 0.0))
        self.assertGreater(d.v_max, 0.0)


class Corridor(Case):
    """Коридор корпуса вблизи, а не угловой сектор: точки на пеленге 31–50° в 0,2–0,35 м (ревью пакета 1)."""

    def test_points_beside_the_front_edge(self):
        p = Params()
        # (точка, путь вперёд от края корпуса, состояние при запросе 0,25 м/с); сектор ±30° v1 не видел первые три
        for point, front, state in (((0.15, 0.17), 0.055, BLOCKED), ((0.20, 0.17), 0.105, SLOW),
                                    ((0.25, 0.18), 0.175, OK), ((0.20, -0.24), 0.20, OK),
                                    ((0.5, 0.40), 2.79, OK)):  # 2,79 — стена комнаты
            w = self.world(rs3=False)
            w.ranges = room(False, points=[point])
            w.start()
            d = w.step((0.25, 0.0))
            self.assertEqual(d.state, state, point)
            self.assertAlmostEqual(d.front_m, front, delta=0.01, msg=point)
            if state == SLOW:
                self.assertAlmostEqual(d.v_max, allowed_speed(p, d.front_m), msg=point)
                self.assertIn("approach", d.codes, point)


class LidarData(Case):
    def test_nan_sector_blocks_forward_turns_allowed(self):
        w = self.world()
        w.start()
        w.ranges = room(True, nan=range(ray(10), ray(20)))
        d = w.run(0.5, (0.1, 0.3))
        self.assertIn("lidar_nodata", d.codes)
        self.assertEqual((d.v, d.v_max, d.state), (0.0, 0.0, BLOCKED))
        self.assertGreater(d.w, 0.0)
        self.assertTrue(d.cut)

    def test_isolated_dropouts_and_error_codes(self):
        # серия неизвестных лучей запрещает, если на дальнем конце её участка в коридоре за корпусом поместится 3 см:
        # прямо по курсу (10°) участок 0,26…0,41 м от лидара — терпим 10 лучей, 13 — нет (12 — на границе);
        # на 50° участок до 0,32 м — терпим 14, 17 — нет; за ≈60° участок короче 3 см: 55–75° и у плеча (−90…−70°)
        # не запрещает никакая ширина
        for bad, blocked in (([ray(10)], False), (range(ray(10), ray(10) + 10), False),
                             (range(ray(10), ray(10) + 13), True), (range(ray(50), ray(50) + 14), False),
                             (range(ray(50), ray(50) + 17), True), (range(ray(55), ray(75)), False),
                             (range(ray(-90), ray(-70)), False)):
            for value in (math.nan, 0.007, -math.inf):  # NaN urg_node2, код ошибки URG ниже range_min (N14), −inf
                w = self.world()
                w.start()
                w.ranges = room(True)
                for i in bad:
                    w.ranges[i] = value
                d = w.step((0.1, 0.0))
                self.assertEqual("lidar_nodata" in d.codes, blocked, (len(bad), value))
                self.assertEqual(any(x.startswith("lidar_unknown") for x in d.warnings), not blocked)

    def test_rep117_plus_inf_free_minus_inf_unknown(self):
        # REP 117: +inf и дальше range_max — «до range_max свободно»; −inf — неизвестно (не попадание: касания нет).
        # Впереди ±60° свободно, по бокам стены в 3 м (скан без попаданий вовсе — lidar_fault, LidarFault)
        front = range(ray(-60), ray(60) + 1)
        w = self.world(rs3=False)
        w.start()
        for value in (math.inf, 9.0):
            w.ranges = room(False)
            for i in front:
                w.ranges[i] = value
            self.assertEqual(w.run(0.5, (0.25, 0.0)).state, OK, value)
        w.ranges[ray(0)] = -math.inf
        d = w.step((0.25, 0.0))
        self.assertEqual(d.state, OK)
        self.assertNotIn("touch", d.codes)
        for i in range(ray(-5), ray(5)):
            w.ranges[i] = -math.inf
        d = w.step((0.25, 0.0))
        self.assertIn("lidar_nodata", d.codes)
        self.assertGreater(d.w_max, 0.0)

    def test_lab_world_start_poses_have_no_false_stop(self):
        # решение 16.10 (находка W2): «нет отражения» через дверной проём сбоку в мире lab двойника; серии NaN /scan в
        # позах сценариев [замер на двойнике 16.10, вне блока корпуса камеры]: участок у плеча не заходит в коридор
        # за корпус, серия на 40–47° узкая — вперёд можно, в safety_state — предупреждение
        poses = {"low_box": ((40.1, 43.9), (45.7, 46.8)), "low_box_east": ((-91.8, -74.9),),
                 "wall": ((77.0, 93.9),), "person_steps_in": ((58.0, 81.9),)}
        for name, spans in poses.items():
            w = self.world()
            w.ranges = room(True, nan=[i for lo, hi in spans for i in range(ray(lo), ray(hi) + 1)])
            w.start()
            d = w.step((0.1, 0.0))
            self.assertNotIn("lidar_nodata", d.codes, name)
            self.assertEqual((d.state, d.v_max), (SLOW, w.p.blind_speed), name)  # перед — в слепом секторе (RS3)
            self.assertIn("lidar_unknown", [x.split(":")[0] for x in d.warnings], name)
            self.assertGreater(d.v, 0.0, name)
        w = self.world()  # тот же проём ближе к курсу (35–60°): в коридоре за корпусом — запрет
        w.ranges = room(True, nan=range(ray(35), ray(60)))
        w.start()
        self.assertIn("lidar_nodata", w.step((0.1, 0.0)).codes)

    def test_unknown_seen_free_by_depth_does_not_block(self):
        # неизвестный участок, который свежий кадр глубины видит свободным, не запрещает; кадр устарел — снова запрет
        w = self.world()
        w.start()
        w.ranges = room(True, nan=range(ray(10), ray(20)))
        self.assertIn("lidar_nodata", w.step((0.1, 0.0)).codes)
        w.free = Frame(False)
        self.assertIn("lidar_nodata", w.run(0.2, (0.1, 0.0)).codes)
        w.free = Frame(True)
        d = w.run(0.2, (0.1, 0.0))
        self.assertNotIn("lidar_nodata", d.codes)
        self.assertIn("lidar_unknown", [x.split(":")[0] for x in d.warnings])
        np.testing.assert_allclose(w.free.asked[-1][:, 2], LASER.pos[2])  # проверяются точки плоскости скана
        d = w.run(0.4 + w.p.depth_timeout, (0.1, 0.0), depth=False)
        self.assertIn("lidar_nodata", d.codes)

    def test_unknown_out_of_depth_view_is_like_blind_sector(self):
        # приёмка 17.10 (зал прогонов A1: lidar_nodata 47–75 % времени): «нет отражения» слева-спереди на 35–60°,
        # участок у корпуса — вне поля камеры глубины. nodata_unseen=slow — как слепой сектор корпуса камеры справа:
        # не быстрее blind_speed по глубине и её памяти; block — прежний запрет
        nan = range(ray(35), ray(60))
        w = self.world(rs3=False)
        w.ranges = room(False, nan=nan)
        w.start()
        self.assertIn("lidar_nodata", w.step((0.2, 0.0)).codes)  # кадра глубины нет — запрет
        w.free = Frame(False, visible=False)
        d = w.run(1.5, (0.2, 0.0))
        self.assertEqual(d.codes, ("lidar_unseen",))
        self.assertEqual((d.state, d.v_max), (SLOW, w.p.blind_speed))
        self.assertAlmostEqual(d.v, w.p.blind_speed)
        w.free = Frame(False, visible=lambda pts: pts[:, 0] > 0.2)  # дальний конец участка в поле и не свободен
        d = w.run(0.1, (0.2, 0.0))  # кадр глубины — после скана: новый ответ — со следующего скана
        self.assertIn("lidar_nodata", d.codes)
        self.assertEqual(d.v_max, 0.0)
        w.free = Frame(True, visible=lambda pts: pts[:, 0] > 0.2)  # в поле — свободен, остальное вне поля
        self.assertEqual(w.run(0.5, (0.2, 0.0)).codes, ("lidar_unseen",))
        d = w.run(0.4 + w.p.depth_timeout, (0.2, 0.0), depth=False)  # глубина устарела — вперёд нельзя
        self.assertEqual(d.v_max, 0.0)
        self.assertIn("stale_depth", d.codes)

        w = self.world(rs3=False, nodata_unseen="block")
        w.ranges = room(False, nan=nan)
        w.free = Frame(False, visible=False)
        w.start()
        d = w.step((0.2, 0.0))
        self.assertIn("lidar_nodata", d.codes)
        self.assertEqual(d.v_max, 0.0)

        w = self.world()  # с корпусом камеры справа: обе причины, предел тот же
        w.ranges = room(True, nan=nan)
        w.free = Frame(False, visible=False)
        w.start()
        self.assertEqual(w.step((0.2, 0.0)).codes, ("lidar_blind", "lidar_unseen"))
        w.ranges = room(True, wall_x=0.28, nan=nan)  # стена ограничила сильнее — участок в предупреждениях
        d = w.run(0.1, (0.2, 0.0))
        self.assertIn("approach", d.codes)
        self.assertNotIn("lidar_unseen", d.codes)
        self.assertIn("lidar_unseen", [x.split(":")[0] for x in d.warnings])

    def test_degenerate_scan_is_rejected_and_goes_stale(self):
        cases = (META._replace(angle_increment=math.nan), META._replace(angle_increment=0.0),
                 META._replace(range_max=0.0), META._replace(range_max=math.nan), META._replace(range_min=-1.0))
        for meta in cases:
            w = self.world()
            w.start()
            why = w.gate.scan(w.t, w.ranges, meta, LASER)
            self.assertTrue(why, meta)
            d = w.run(0.5, (0.1, 0.0), scan=False)
            self.assertIn("stale_scan", d.codes, meta)
            self.assertIn(why, " ".join(d.reasons), meta)
            self.assertEqual((d.v, d.w), (0.0, 0.0))
        w = self.world()
        w.start()
        self.assertTrue(w.gate.scan(w.t, [], META._replace(n=0), LASER))

    def test_mask_is_camera_body_only(self):
        # ревью W2: «нет данных» обстановки на старте (дверной проём 35–60°) — не корпус камеры: в маску не идёт,
        # вперёд нельзя, как при любом неизвестном участке в коридоре; в safety_state — предупреждение
        w = self.world()
        w.ranges = room(True, nan=range(ray(35), ray(60)))
        d = w.start()
        self.assertEqual(w.gate.mask_deg, [[-61.5, 4.9]])
        self.assertEqual(w.gate.outside_deg, [[35.2, 59.8]])
        self.assertIn("mask_outside", [x.split(":")[0] for x in d.warnings])
        self.assertIn("lidar_nodata", d.codes)
        self.assertEqual(d.v_max, 0.0)
        w.ranges = room(True)  # дверь закрыли
        d = w.run(0.5, (0.1, 0.0))
        self.assertNotIn("lidar_nodata", d.codes)
        self.assertGreater(d.v, 0.0)

    def test_mask_drops_rays_that_see_past_the_body(self):
        # на старте впереди-справа «нет отражения» (чёрный предмет в секторе корпуса) — в маске, пока лучи не покажут
        # mask_scans сканов подряд попадание дальше корпуса; потом «нет данных» там — провал
        p = Params()
        w = self.world(rs3=False)
        dark = range(ray(-20), ray(-10))
        w.ranges = room(False, nan=dark)
        self.assertIn("lidar_blind", w.start().codes)
        w.ranges = room(False)
        d = w.run(0.05 * (p.mask_scans - 1), (0.0, 0.0))
        self.assertIn("lidar_blind", d.codes)
        d = w.step((0.0, 0.0))
        self.assertEqual(w.gate.mask_deg, [])
        self.assertNotIn("lidar_blind", d.codes)
        w.ranges = room(False, nan=dark)
        self.assertIn("lidar_nodata", w.step((0.1, 0.0)).codes)

    def test_scan_not_covering_front_is_unknown(self):
        # направления без лучей (скан обрезан параметрами драйвера) — неизвестно, по тому же правилу участка
        w = self.world()
        w.meta = META._replace(n=ray(-31))  # −120…−31°: впереди лучей нет
        w.ranges = room(False)[:w.meta.n]
        d = w.start()
        self.assertIn("lidar_nodata", d.codes)
        self.assertEqual(d.v_max, 0.0)
        w = self.world(rs3=False)
        w.meta = META._replace(n=ray(65))  # −120…+65°: нет только лучей у левого плеча
        w.ranges = room(False)[:w.meta.n]
        d = w.start()
        self.assertNotIn("lidar_nodata", d.codes)
        self.assertEqual(d.state, OK)
        self.assertIn("lidar_unknown", [x.split(":")[0] for x in d.warnings])


class LidarFault(Case):
    def test_scan_seeing_nothing_stops_like_no_scan(self):
        # повторная приёмка 18.10 (S3b): скан целиком NaN шёл как «нет отражения вне поля глубины» — slow, и робот ехал
        # на одной глубине. Скан без попаданий дальше корпуса (NaN, коды ошибок URG, +inf, попаданий меньше
        # fault_min_hits) вид лидара не меняет; fault_scans таких подряд — lidar_fault: полный стоп, сканы при этом
        # идут, поэтому не stale_scan. Лидар снова видит — движение только с нулевого запроса
        few = [math.nan] * N
        for i in range(ray(-30), ray(-30) + 9):
            few[i] = 1.0
        for ranges in ([math.nan] * N, [0.007] * N, [math.inf] * N, few):
            w = self.world()
            w.free = Frame(True, visible=False)
            w.start()
            w.run(0.5, (0.1, 0.0))
            w.ranges = ranges
            d = w.run(0.05 * (w.p.fault_scans - 1), (0.1, 0.0))
            self.assertGreater(d.v, 0.0, ranges[0])  # пустых меньше fault_scans — вид лидара прежний
            d = w.step((0.1, 0.0))
            self.assertEqual((d.v, d.w, d.state), (0.0, 0.0, BLOCKED), ranges[0])
            self.assertEqual(d.codes, ("lidar_fault",))
            self.assertEqual(w.run(2.0, (0.1, 0.3)).codes, ("lidar_fault",))
            w.ranges = room(True)
            self.assertIn("unarmed", w.step((0.1, 0.0)).codes)
            w.step((0.0, 0.0))
            self.assertGreater(w.run(0.5, (0.1, 0.0)).v, 0.0)

    def test_interleaved_empty_scans_keep_the_real_view(self):
        # подменный издатель шлёт по два пустых скана на каждый настоящий (S3b): подряд их меньше fault_scans — стопа
        # нет, а вид лидара — по настоящим: стена в 0,3 м держит предел и на пустых сканах
        w = self.world(rs3=False)
        w.start()
        real = room(False, wall_x=0.3)
        limits = set()
        for k in range(30):
            w.ranges = real if k % 3 == 0 else [math.nan] * N
            d = w.step((0.25, 0.0))
            self.assertNotIn("lidar_fault", d.codes)
            self.assertIn("approach", d.codes)
            limits.add(round(d.v_max, 6))
        self.assertEqual(len(limits), 1)

    def test_start_with_blind_lidar_is_lidar_fault_not_mask(self):
        # пустой скан маску не учит: на старте с ослепшим лидаром причина — lidar_fault, а не «учим маску»
        w = self.world()
        w.ranges = [math.nan] * N
        d = w.run(0.05 * (w.p.mask_scans + 2), (0.0, 0.0))
        self.assertEqual(d.codes, ("lidar_fault",))
        self.assertIsNone(w.gate.lidar)
        w.ranges = room(True)
        w.start()
        self.assertGreater(w.run(0.5, (0.1, 0.0)).v, 0.0)
        ref = self.world()
        ref.start()
        self.assertEqual(w.gate.mask_deg, ref.gate.mask_deg)  # маска — по настоящим сканам, как без пустых


def approach(w: World, near_x: float, visible_from: float, kind: str):
    """Робот едет на предмет с ближней гранью near_x (база в начале); глубина видит его, только пока камера
    (x = 0,058) не ближе visible_from. Итог — зазор от края корпуса и последнее решение."""
    w.follow = False
    base = Base(0.1)
    d = None
    for _ in range(400):
        ahead = near_x - base.x
        pts = face(ahead) if ahead - 0.058 >= visible_from else NONE
        w.hazards = seen(pts if kind == "obstacle" else NONE, pts if kind == "drop" else NONE)
        w.pose, w.vw = (base.x, 0.0, 0.0), (base.v, 0.0)
        d = w.step((0.25, 0.0))
        base.step(d.v)
    return near_x - base.x - w.p.robot_radius, d


class Depth(Case):

    def test_low_box_seen_from_afar_is_remembered(self):
        w = self.world()
        w.hazards = seen(face(1.35))  # мир lab двойника: ящик 0,15 м, грань в 1,35 м
        w.start()
        gap, d = approach(w, 1.35, 0.93, "obstacle")  # верх ящика 0,15 м уходит из поля с ≈0,93 м
        self.assertGreater(gap, 0.03)
        self.assertLess(gap, 0.15)
        self.assertEqual(d.state, BLOCKED)
        self.assertIn("obstacle", d.codes)
        self.assertIn("глубина", " ".join(d.reasons))

    def test_cliff_edge_stops_before_edge(self):
        w = self.world()
        w.start()
        gap, d = approach(w, 1.5, 1.2, "drop")  # мир step: край в 1,5 м, пол виден с ≈1,2 м
        self.assertGreater(gap, 0.03)
        self.assertIn("cliff", d.codes)

    def test_floor_seen_clears_memory(self):
        w = self.world()
        w.hazards = seen(face(0.3))
        w.start()
        self.assertEqual(w.step((0.1, 0.0)).state, BLOCKED)
        w.hazards = seen(floor=face(0.3))  # человек ушёл: там виден пол
        d = w.run(1.0, (0.1, 0.0))
        self.assertNotEqual(d.state, BLOCKED)

    def test_frame_without_depth_is_not_fresh(self):
        # ревью W2: кадр без данных (закрыт объектив, засветка ИК, предмет ближе min-z) — не «свежая глубина, всё
        # свободно»; перед — в слепом секторе RS3, ехать вперёд разрешает только глубина
        for coverage in (0.0, 0.3):
            w = self.world()
            w.start()
            self.assertEqual(w.run(0.5, (0.25, 0.0)).state, SLOW)
            w.hazards = seen(coverage=coverage)
            d = w.run(0.6, (0.25, 0.0))
            self.assertEqual((d.v, d.v_max, d.state), (0.0, 0.0, BLOCKED), coverage)
            self.assertIn("stale_depth", d.codes)
            self.assertIn("видит коридор вперёд на", " ".join(d.reasons))
            self.assertEqual(d.w_max, w.p.max_turn)  # поворот на месте — по лидару
            w.hazards = seen()
            self.assertEqual(w.run(0.5, (0.25, 0.0)).state, SLOW)

    def test_start_needs_depth_that_sees_the_corridor(self):
        w = self.world()
        w.hazards = seen(coverage=0.0)
        d = w.start()
        self.assertEqual((d.v_max, d.w_max, d.state), (0.0, 0.0, BLOCKED))
        self.assertIn("stale_depth", d.codes)
        self.assertIn("видит коридор вперёд на 0%", " ".join(d.reasons))

    def test_stale_depth(self):
        for rs3, policy, v_max in ((True, "block", 0.0), (False, "block", 0.0), (False, "slow", 0.15)):
            w = self.world(rs3=rs3, depth_stale=policy)
            w.start()
            d = w.run(0.6, (0.25, 0.0), depth=False)
            self.assertEqual(d.v_max, v_max, (rs3, policy))
            self.assertIn("stale_depth", d.codes)
            self.assertEqual(d.w_max, w.p.max_turn)


class Latches(Case):
    def test_tilt_latches_and_release_waits_for_level(self):
        w = self.world()
        w.start()
        w.tilt = 12.0  # пандус 12° мира ramp
        d = w.step((0.1, 0.0))
        self.assertEqual((d.state, d.v, d.w), (ESTOP, 0.0, 0.0))
        self.assertIn("tilt", d.codes)
        self.assertIn("наклон", w.gate.release(100.0, human(w.latch, 100.0)))
        w.tilt = 0.3
        w.step((0.0, 0.0))
        self.assertEqual(w.gate.release(100.0, human(w.latch, 100.0, "r2")), "снята")
        d = w.step((0.1, 0.0))
        self.assertNotEqual(d.state, ESTOP)

    def test_filter_settles_after_imu_gap(self):
        # прогоны af 17.10: после перезапуска узла камеры madgwick выдал 48° и сходился ≈0,1 рад/с — ложная защёлка
        # tilt; теперь робот стоит, пока фильтр не сойдётся с наклоном по ускорению, а судит наклон ускорение
        w = self.world()
        w.start()
        w.run(1.0, (0.0, 0.0), imu=False)          # поток IMU молчит 1 с (перезапуск драйвера камеры)
        w.tilt, w.accel = 48.3, (3.0, 0.0, 9.81)   # после перерыва: ложный наклон фильтра, иное ускорение
        d = w.step((0.1, 0.0))
        self.assertNotEqual(d.state, ESTOP)
        self.assertEqual((d.v, d.w), (0.0, 0.0))
        self.assertIn("фильтр ориентации сходится: 48.3° против 0.3° по ускорению", " ".join(d.reasons))
        for k in range(180):                       # сходится ≈6° в секунду, как madgwick с beta 0,1
            w.tilt = max(0.3, 48.3 - 6.0 * 0.05 * k)
            d = w.step((0.0, 0.0))
            if w.tilt > 3.5:
                self.assertIn("сходится", " ".join(d.reasons))
        self.assertNotEqual(d.state, ESTOP)
        self.assertNotIn("сходится", " ".join(d.reasons))
        self.assertGreater(w.run(0.5, (0.1, 0.0)).v, 0.0)

    def test_real_slope_after_imu_gap_latches_by_accel(self):
        w = self.world()
        w.start()
        w.run(1.0, (0.0, 0.0), imu=False)
        w.tilt, w.tilt_acc = 40.0, 12.0            # фильтр ещё врёт, а ускорение показывает склон 12°
        d = w.step((0.0, 0.0))
        self.assertEqual(d.state, ESTOP)
        self.assertIn("по ускорению 12.0°", " ".join(d.reasons))

    def test_settle_without_accel_tilt_waits_the_limit(self):
        p = Params()
        w = self.world()
        w.tilt_acc = None                          # нет TF кадра IMU: наклона по ускорению нет
        w.start()
        w.run(1.0, (0.0, 0.0), imu=False)
        self.assertIn("сходится", " ".join(w.run(p.tilt_settle_s - 1.0, (0.0, 0.0)).reasons))
        self.assertNotIn("сходится", " ".join(w.run(1.5, (0.0, 0.0)).reasons))

    def test_shock_latches(self):
        w = self.world()
        w.start()
        w.accel = (8.0, 0.0, 9.81)
        d = w.step((0.1, 0.0))
        self.assertEqual(d.state, ESTOP)
        self.assertIn("shock", d.codes)

    def test_slow_accel_change_is_not_a_shock(self):
        w = self.world()
        w.start()
        for k in range(40):  # разгон 1 м/с² за 0,2 с и наклон корпуса — не удар
            w.accel = (min(k, 4) * 0.25, 0.0, 9.81)
            self.assertNotEqual(w.step((0.0, 0.0)).state, ESTOP)

    def test_stall_when_command_reaches_base(self):
        w = self.world(rs3=False)
        w.follow = False
        w.start()
        d = w.run(1.0, (0.1, 0.0))  # одометрия стоит: робот упёрся
        self.assertNotEqual(d.state, ESTOP)
        d = w.run(0.6, (0.1, 0.0))
        self.assertEqual(d.state, ESTOP)
        self.assertIn("stall", d.codes)
        self.assertEqual((d.v, d.w), (0.0, 0.0))

    def test_no_stall_when_twist_mux_overrides(self):
        w = self.world(rs3=False)
        w.follow = False
        w.start()
        w.mux = False
        d = None
        for _ in range(60):  # геймпад держит базу нулём: команда не доходит
            w.gate.mux(w.t, 0.0, 0.0)
            d = w.step((0.1, 0.0))
        self.assertNotEqual(d.state, ESTOP)
        self.assertIn("override_active", [x.split(":")[0] for x in d.warnings])

    def test_moving_odometry_is_not_a_stall(self):
        w = self.world(rs3=False)
        w.start()
        d = w.run(3.0, (0.1, 0.0))  # одометрия повторяет команду
        self.assertNotEqual(d.state, ESTOP)

    def test_estop_protocol(self):
        w = self.world()
        w.start()
        g = w.gate
        self.assertEqual(g.estop(10.0, EstopCmd("engage", "agent", "LLM", "a1", 10.0)), "поставлена")
        self.assertEqual(w.step((0.1, 0.0)).state, ESTOP)
        with open(w.latch_path, encoding="utf-8") as f:  # записано до публикации состояния estop
            self.assertEqual(json.load(f)["engaged"], True)
        # по теме не снимает никто: by самозаявлено — узел ROS подписался бы «human» (ревью: находка о защёлке)
        self.assertIn("по теме", g.estop(10.0, EstopCmd("release", "agent", "", "a2", 10.0)))
        self.assertIn("по теме", g.estop(10.0, EstopCmd("release", "human", "", "a3", 10.0)))
        self.assertIn("старше", g.release(100.0, human(w.latch, 10.0)))
        other = ReleaseReq("r0", 10.0, w.latch.since, ("estop: другая причина",), "")
        self.assertIn("другую защёлку", g.release(10.0, other))
        self.assertEqual(g.counters["release_denied"], 4)
        self.assertEqual(w.step((0.1, 0.0)).state, ESTOP)
        self.assertEqual(g.release(10.0, human(w.latch, 10.0, "r1")), "снята")
        self.assertTrue(g.release(10.0, human(w.latch, 10.0, "r1")).startswith("повтор"))
        self.assertEqual(g.release(10.0, human(w.latch, 10.0, "r9")), "защёлки нет")
        self.assertTrue(g.estop(10.0, EstopCmd("engage", "agent", "LLM", "a1", 10.0)).startswith("повтор"))
        d = w.step((0.1, 0.0))
        self.assertIn("unarmed", d.codes)  # после снятия — только с нулевого запроса
        with open(w.latch_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["engaged"], False)

    def test_latch_survives_restart_and_unreadable_file_latches(self):
        w = self.world()
        w.start()
        w.gate.estop(10.0, EstopCmd("engage", "human", "кнопка", "e1", 10.0))
        again = Latch(w.latch_path)
        self.assertTrue(again.engaged)
        self.assertIn("кнопка", again.reasons[0])
        with open(w.latch_path, "w", encoding="utf-8") as f:
            f.write("{oops")
        broken = Latch(w.latch_path)
        self.assertTrue(broken.engaged)
        self.assertIn("не читается", broken.reasons[0])
        w2 = self.world(latch_path=w.latch_path)
        self.assertEqual(w2.start().state, ESTOP)

    def test_unwritable_latch_file_keeps_latch_in_memory(self):
        latch = Latch(os.path.join(tempfile.gettempdir(), "нет-такого-каталога-safety", "estop.json"))
        with self.assertLogs("safety_gate", "ERROR"):
            self.assertTrue(latch.engage("estop", "проверка"))
        self.assertTrue(latch.engaged)

    def test_foreign_writer_latches_and_blocks_release(self):
        w = self.world()
        w.start()
        w.gate.graph(["/rogue_teleop"], [])
        d = w.step((0.1, 0.0))
        self.assertEqual(d.state, ESTOP)
        self.assertIn("foreign_writer", d.codes)
        self.assertIn("/rogue_teleop", w.gate.release(10.0, human(w.latch, 10.0)))
        w.gate.graph([], [])
        self.assertEqual(w.gate.release(10.0, human(w.latch, 10.0, "r2")), "снята")

    def test_second_scan_publisher_warns_then_blocks(self):
        for policy in ("warn", "block"):
            w = self.world(rs3=False, scan_dup_policy=policy, scan_dup_s=2.0)
            w.start()
            w.gate.scan_publishers(w.t, ["/urg_node2", "/urg_node2"])
            d = w.run(1.0, (0.1, 0.0))
            self.assertGreater(d.v, 0.0, policy)  # убитый драйвер ещё в графе — сразу не стоим
            self.assertIn("scan_publishers", [x.split(":")[0] for x in d.warnings])
            d = w.run(1.5, (0.1, 0.0))
            if policy == "warn":
                self.assertGreater(d.v, 0.0)
                continue
            self.assertEqual((d.v, d.w, d.state), (0.0, 0.0, BLOCKED))
            self.assertIn("scan_publishers", d.codes)
            w.gate.scan_publishers(w.t, ["/urg_node2"])
            self.assertIn("unarmed", w.step((0.1, 0.0)).codes)  # снова только с нулевого запроса
            w.step((0.0, 0.0))
            self.assertNotIn("scan_publishers", [x.split(":")[0] for x in w.run(0.5, (0.1, 0.0)).warnings])

    def test_second_scan_publisher_with_empty_scans_stops_at_once(self):
        # повторная приёмка 18.10 (S3b): узел с именем драйвера шлёт NaN-сканы между настоящими — это не след убитого
        # драйвера (след молчит): стоп сразу, а не через scan_dup_s; пустой скан до второго издателя не в счёт
        for policy in ("warn", "block"):
            w = self.world(rs3=False, scan_dup_policy=policy)
            w.start()
            w.ranges = [math.nan] * N
            w.step((0.0, 0.0))
            w.ranges = room(False)
            w.gate.scan_publishers(w.t, ["/urg_node2", "/urg_node2"])
            d = w.run(1.0, (0.1, 0.0))
            self.assertGreater(d.v, 0.0, policy)
            w.ranges = [math.nan] * N
            w.step((0.1, 0.0))
            w.ranges = room(False)
            d = w.step((0.1, 0.0))
            self.assertIn("scan_publishers", [x.split(":")[0] for x in d.warnings])
            if policy == "warn":
                self.assertGreater(d.v, 0.0)
                continue
            self.assertEqual((d.v, d.w, d.state), (0.0, 0.0, BLOCKED))
            self.assertIn("scan_publishers", d.codes)
            self.assertIn("пустые сканы", " ".join(d.reasons))
            w.gate.scan_publishers(w.t, ["/urg_node2"])
            self.assertIn("unarmed", w.step((0.1, 0.0)).codes)
            w.step((0.0, 0.0))
            self.assertGreater(w.run(0.5, (0.1, 0.0)).v, 0.0)

    def test_foreign_scan_publisher_stops_at_once(self):
        # приёмка 17.10 (P3): подмена /scan чужим узлом. Драйвер — первый одиночный издатель; издатель с другим именем
        # (подставной «всё свободно», запись) — сразу стоп при block, и когда настоящий драйвер умер, тоже
        for policy in ("warn", "block"):
            w = self.world(rs3=False, scan_dup_policy=policy)
            w.gate.scan_publishers(w.t, [])
            w.gate.scan_publishers(w.t, ["/urg_node2"])
            w.start()
            for names in (["/urg_node2", "/scan_ghost"], ["/scan_ghost"]):
                w.gate.scan_publishers(w.t, names)
                d = w.step((0.1, 0.0))
                self.assertIn("scan_foreign", [x.split(":")[0] for x in d.warnings], (policy, names))
                if policy == "warn":
                    self.assertGreater(d.v, 0.0, names)
                else:
                    self.assertEqual((d.v, d.w, d.state), (0.0, 0.0, BLOCKED), names)
                    self.assertIn("scan_foreign", d.codes)
            w.gate.scan_publishers(w.t, ["/urg_node2", "/urg_node2"])  # рестарт драйвера: след убитого — не чужой
            d = w.step((0.0, 0.0))
            self.assertNotIn("scan_foreign", d.codes + tuple(x.split(":")[0] for x in d.warnings))
            self.assertGreater(w.run(0.5, (0.1, 0.0)).v, 0.0)

    def test_override_publishers_by_policy(self):
        for policy, state in (("warn", SLOW), ("block", BLOCKED), ("estop", ESTOP)):
            w = self.world(override_policy=policy)
            w.start()
            w.gate.graph([], ["/cmd_vel_joy"])
            d = w.step((0.1, 0.0))
            self.assertEqual(d.state, state, policy)
            if policy == "warn":
                self.assertTrue(any(x.startswith("override") for x in d.warnings))
                self.assertGreater(d.v_max, 0.0)


class Inputs(Case):
    def test_stale_scan_and_imu_stop_everything(self):
        for sensor, seconds in (("scan", 0.45), ("imu", 0.55), ("odom", 0.55)):
            w = self.world()
            w.start()
            w.run(0.5, (0.1, 0.3))
            d = w.run(seconds, (0.1, 0.3), **{sensor: False})
            self.assertEqual((d.v, d.w, d.state), (0.0, 0.0, BLOCKED), sensor)
            self.assertIn("stale_" + sensor, d.codes)
            # датчик вернулся — без нулевого запроса не поедем (ориентация после перерыва ещё и сходится)
            d = w.run(0.5 + (TILT_AGREE_S if sensor == "imu" else 0.0), (0.1, 0.3))
            self.assertEqual((d.v, d.w), (0.0, 0.0), sensor)
            self.assertIn("unarmed", d.codes)

    def test_request_timeout_is_zero(self):
        w = self.world(rs3=False)
        w.start()
        w.run(0.5, (0.1, 0.0))
        d = w.run(0.55)
        self.assertEqual((d.v, d.w, d.req), (0.0, 0.0, (0.0, 0.0)))

    def test_non_finite_request(self):
        for bad in ((math.nan, 0.0), (math.inf, 0.0), (-math.inf, 0.0), (0.0, math.inf), (0.0, math.nan)):
            w = self.world(rs3=False)
            w.ranges = room(False, wall_x=0.5)
            w.start()
            w.run(0.5, (0.1, 0.1))
            d = w.step(bad)
            self.assertEqual((d.v, d.w, d.req), (0.0, 0.0, (0.0, 0.0)), bad)  # явный ноль, а не NaN и не прошлое
            self.assertEqual(w.gate.counters["invalid"], 1)


class Limits(Case):
    def test_speed_turn_and_reverse_caps(self):
        p = Params()
        w = self.world(rs3=False)
        w.start()
        d = w.run(2.0, (1.0, 5.0))
        self.assertAlmostEqual(d.v, p.max_speed)
        self.assertAlmostEqual(d.w, p.max_turn)
        d = w.run(1.0, (-1.0, -5.0))
        self.assertAlmostEqual(d.v, -p.reverse_speed)
        self.assertAlmostEqual(d.w, -p.max_turn)

    def test_reverse_budget_follows_odometry(self):
        # назад — не дальше reverse_dist за самую дальнюю точку пути: задний ход тратит бюджет, ход вперёд возвращает
        p = Params()
        w = self.world(rs3=False)
        w.start()
        path = [0.0]

        def drive(req, seconds):
            d = None
            for _ in range(int(round(seconds / 0.05))):
                d = w.step(req)
                path.append(path[-1] + d.v * 0.05)
                w.pose = (path[-1], 0.0, 0.0)
            return d

        d = drive((-0.05, 0.0), 6.0)
        self.assertEqual(d.v, 0.0)
        self.assertIn("reverse_limit", d.codes)
        self.assertAlmostEqual(path[-1], -p.reverse_dist, delta=0.01)
        for _ in range(10):  # ревью W2: «вперёд на один тик, затем назад» — запрос вперёд без хода бюджет не вернул
            drive((0.1, 0.0), 0.05)
            d = drive((-0.05, 0.0), 1.0)
        self.assertIn("reverse_limit", d.codes)
        drive((0.1, 0.0), 1.5)  # проехали вперёд — столько же можно назад
        self.assertGreater(path[-1], -p.reverse_dist + 0.05)
        d = drive((-0.05, 0.0), 8.0)
        self.assertIn("reverse_limit", d.codes)
        self.assertAlmostEqual(min(path), -p.reverse_dist, delta=0.01)

    def test_reverse_forbidden(self):
        w = self.world(rs3=False, reverse_speed=0.0)
        w.start()
        d = w.run(0.5, (-0.05, 0.0))
        self.assertEqual((d.v, d.v_rev), (0.0, 0.0))

    def test_accel_and_jerk_on_start(self):
        p = Params()
        w = self.world(rs3=False)
        w.start()
        vs = [w.step((0.25, 0.0)).v for _ in range(30)]
        acc = [(b - a) / 0.05 for a, b in zip([0.0] + vs, vs)]
        self.assertTrue(all(a <= p.max_accel + 1e-9 for a in acc))
        self.assertTrue(all(b - a <= p.max_jerk * 0.05 + 1e-9 for a, b in zip([0.0] + acc, acc)))
        self.assertAlmostEqual(vs[-1], 0.25)

    def test_requested_stop_is_smooth(self):
        # находка 3: свой стоп на 0,15 м/с дал всплеск /camera/imu 6,1 м/с² и защёлку shock — по запросу тормозим
        # S-кривой: замедление ≤ stop_decel, его рост и спад ≤ max_jerk·dt, в конце — ноль
        p = Params()
        w = self.world(rs3=False)
        w.start()
        w.run(2.0, (0.15, 0.0))
        vs = [w.step((0.0, 0.0)).v for _ in range(30)]
        dec = [(a - b) / 0.05 for a, b in zip([0.15] + vs, vs)]
        self.assertGreater(vs[0], 0.14)
        self.assertTrue(all(d <= p.stop_decel + 1e-9 for d in dec))
        self.assertTrue(all(abs(b - a) <= p.max_jerk * 0.05 + 1e-9 for a, b in zip([0.0] + dec, dec + [0.0])))
        self.assertEqual(vs[-1], 0.0)
        self.assertLess(vs.index(0.0) * 0.05, 0.7)
        hard = self.world(rs3=False, stop_decel=0.0)
        hard.start()
        hard.run(2.0, (0.15, 0.0))
        self.assertEqual(hard.step((0.0, 0.0)).v, 0.0)  # stop_decel 0 — как раньше: тормозит L1

    def test_safety_stop_is_immediate(self):
        def moving(**kw):
            w = self.world(rs3=False, **kw)
            w.start()
            w.run(2.0, (0.15, 0.3))
            return w
        w = moving()
        w.ranges = room(False, wall_x=0.24)  # стена в 4,5 см от края корпуса — предел срезал запрос
        d = w.step((0.15, 0.3))
        self.assertEqual(d.v, 0.0)
        self.assertGreater(d.w, 0.0)  # поворот по-прежнему разрешён и тормозит плавно только по запросу
        w = moving()
        w.ranges = room(False, wall_x=0.24)  # стена — в тот же тик, когда исполнитель прислал ноль: тоже сразу
        self.assertEqual(w.step((0.0, 0.3)).v, 0.0)
        w = moving()
        w.gate.estop(w.t, EstopCmd("engage", "agent", "", "e1", w.t))
        self.assertEqual((w.step((0.15, 0.3)).v, w.last.w), (0.0, 0.0))
        w = moving()
        self.assertEqual((w.run(0.55).v, w.last.w), (0.0, 0.0))  # запрос устарел (исполнитель умер) — сразу
        w = moving()
        d = w.run(0.45, (0.15, 0.3), scan=False)
        self.assertEqual((d.v, d.w), (0.0, 0.0))

    def test_agent_stop_is_immediate_without_latch(self):
        # решение 1а 19.10: robot_stop агента через MCP — стоп по безопасности, ноль сразу; защёлки нет
        w = self.world(rs3=False)
        w.start()
        w.run(2.0, (0.15, 0.3))
        g = w.gate
        self.assertEqual(g.stop_now(StopCmd("agent", "k6", "s1", w.t)), "ноль сразу")
        d = w.step((0.15, 0.3))  # ноль агента до исполнителя ещё не дошёл — запрос прежний
        self.assertEqual((d.v, d.w), (0.0, 0.0))
        self.assertEqual(d.state, BLOCKED)
        self.assertIn("unarmed", d.codes)
        self.assertFalse(w.latch.engaged)
        self.assertFalse(os.path.exists(w.latch_path))  # в файл защёлки ничего не писалось
        self.assertEqual(w.run(1.0, (0.15, 0.3)).v, 0.0)  # прежний манёвр сам не продолжается
        w.step((0.0, 0.0))  # исполнитель погасил задачу
        self.assertTrue(g.stop_now(StopCmd("agent", "k6", "s1", w.t)).startswith("повтор"))  # копия того же стопа
        vs = [w.step((0.15, 0.0)).v for _ in range(20)]  # новая команда — как обычно, с разгоном
        self.assertTrue(0.0 < vs[0] < 0.15)
        self.assertAlmostEqual(vs[-1], 0.15)
        self.assertEqual(g.counters["stop_now"], 1)
        self.assertEqual(g.counters["estop_engaged"], 0)
        # стоп, пришедший после нуля исполнителя (плавное торможение уже идёт), — тоже сразу
        w.step((0.0, 0.0))
        self.assertGreater(w.last.v, 0.1)
        g.stop_now(StopCmd("agent", "", "s2", w.t))
        self.assertEqual(w.step((0.0, 0.0)).v, 0.0)
        self.assertGreater(w.step((0.1, 0.0)).v, 0.0)  # взведён тем же нулём

    def test_max_motion_time(self):
        w = self.world(max_motion_s=1.0)
        w.start()
        d = w.run(1.2, (0.0, 0.5))
        self.assertEqual(d.w, 0.0)
        self.assertIn("motion_time", d.codes)
        self.assertIn("motion_time", w.run(1.0, (0.0, 0.5)).codes)
        w.step((0.0, 0.0))
        self.assertGreater(w.run(0.3, (0.0, 0.5)).w, 0.0)

    def test_geofence(self):
        w = self.world(rs3=False, geofence_m=1.0)
        w.start()
        for k in range(61):
            w.pose = (0.02 * k, 0.0, 0.0)
            d = w.step((0.0, 0.0))
        self.assertIn("geofence", d.codes)
        self.assertEqual(d.v_max, 0.0)
        self.assertGreater(d.v_rev, 0.0)  # назад — к точке старта
        w.pose = (1.2, 0.0, math.pi)
        d = w.step((0.1, 0.0))
        self.assertGreater(d.v_max, 0.0)
        self.assertEqual(d.v_rev, 0.0)

    def test_depth_cell_error_near_thin_leg(self):
        # находка 2 (main_a1, 1609–1629 с): у ножки стула зазор по клетке глубины 0,044 м, по истине — 0,009; поворот
        # на месте с turn_margin 0,03 задел ножку. Клетка глубины считается ближе на depth_err_m — для поворота и пути
        # вперёд
        p = Params()
        leg = np.array([[0.0, p.robot_radius + 0.079]] * 3)  # центр клетки, как в инциденте: угол — в 0,044 м от круга
        for err, turns in ((0.0, True), (p.depth_err_m, False)):
            w = self.world(rs3=False, depth_err_m=err)
            w.hazards = seen(obstacle=leg)
            w.start()
            d = w.run(0.5, (0.0, 0.5))
            self.assertEqual(d.w > 0.0, turns, err)
            self.assertEqual("touch" in d.codes, not turns, err)
        self.assertIn("глубина", " ".join(d.reasons))
        w = self.world(rs3=False)
        w.ranges = room(False, points=[(0.0, p.robot_radius + 0.05)])
        w.start()
        self.assertGreater(w.run(0.5, (0.0, 0.5)).w, 0.0)  # лидар меряет точно: 5 см от круга — поворот можно
        gaps = []
        for err in (0.0, p.depth_err_m):  # и вперёд глубина останавливает раньше на ту же ошибку
            w = self.world(depth_err_m=err)
            w.hazards = seen(face(1.35))
            w.start()
            gaps.append(approach(w, 1.35, 0.93, "obstacle")[0])
        self.assertAlmostEqual(gaps[1] - gaps[0], p.depth_err_m, delta=0.012)

    def test_touch_forbids_turning_in_place(self):
        for point, forward in (((-0.05, 0.21), True), ((0.01, 0.21), False)):  # стойка у борта, 1,5 см от круга
            w = self.world(rs3=False)
            w.ranges = room(False, points=[point])
            w.start()
            d = w.run(0.5, (0.0, 0.5))
            self.assertEqual((d.w, d.w_max), (0.0, 0.0), point)
            self.assertIn("touch", d.codes)
            # сзади от центра — вперёд можно; рядом с передним краем — в боковом запасе коридора, нельзя
            self.assertEqual(d.v_max > 0.0, forward, point)


if __name__ == "__main__":
    unittest.main()
