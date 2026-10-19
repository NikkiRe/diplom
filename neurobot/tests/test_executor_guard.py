"""Страж /cmd_vel (neurobot/executor/guard.py): таблица переходов, тормозная дальность, слепой сектор и разбор
скана. Без rclpy, Python ≥ 3.8.

Сканы двух видов: TurtleBot3 стенда (360 лучей 0…2π) и URG-04LX робота с геометрией скана лаборатории
(683 луча ±2,0923497677 рад, range_min 0,02 — R7, RS15), в том числе с блоком NaN −60,8…+4,2° (RS3) и кодами
ошибок ниже range_min (N14).
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from neurobot.executor.guard import (FRONT_BLIND, FRONT_CLEAR, FRONT_OBSTACLE, FRONT_STALE,  # noqa: E402
                                     Command, Guard, Params, front_scan, stop_distance)

N = 360
FREE = [3.0] * N  # TurtleBot3: 360 лучей, 0…2π, луч 0 — вперёд
TB3 = (0.0, 2 * math.pi / N, 0.12, 3.5)  # angle_min, angle_increment, range_min, range_max

URG_N = 683
URG_MIN = -2.0923497677
URG_INC = 2 * -URG_MIN / (URG_N - 1)
URG = (URG_MIN, URG_INC, 0.02, 5.6)
URG_FRONT = 341  # луч 0°
HALF = math.radians(30)


def tb3_scan(**hits: float) -> list:
    """Скан TB3 со стенкой 3 м и отдельными лучами {"deg<угол>": дальность}."""
    ranges = list(FREE)
    for key, r in hits.items():
        ranges[int(key[3:]) % N] = r
    return ranges


def urg_scan(nan_from: int = 0, nan_to: int = -1, **rays: float) -> list:
    """Скан URG-04LX: стена 3 м, NaN на лучах nan_from…nan_to включительно, отдельные лучи {"i<индекс>": r}."""
    ranges = [3.0] * URG_N
    for i in range(nan_from, nan_to + 1):
        ranges[i] = math.nan
    for key, r in rays.items():
        ranges[int(key[1:])] = r
    return ranges


RS3 = dict(nan_from=168, nan_to=353)  # скан с робота 02.06: блок NaN −60,8…+4,2° (R7, RS3)


def feed(g: Guard, now: float, ranges=FREE, geometry=TB3) -> None:
    g.scan(now, ranges, *geometry)


def C(linear, angular, by, front=FRONT_CLEAR, cut=False) -> Command:
    return Command(linear, angular, by, front, cut)


def idle(**params) -> Guard:
    """Страж, у которого входы до таблицы уже в покое: демо прислало нули, агент молчит — удержание старта снято
    (оно — test_safety_executor.py)."""
    g = Guard(Params(**params))
    g.gesture(-1.0, 0.0, 0.0)
    g.agent(-1.0, 0.0, 0.0)
    return g


class TransitionTable(unittest.TestCase):
    """Каждая строка: (время, событие, аргументы) → ожидаемая команда после события."""

    def run_table(self, rows, guard=None):
        g = guard or idle()
        for t, event, args, want in rows:
            if event == "scan":
                feed(g, t, *args)
            elif event in ("gesture", "agent"):
                getattr(g, event)(t, *args)
            got = g.command(t)
            self.assertEqual(got, want, "t={} {}{}".format(t, event, args))
        return g

    def test_start_and_idle_zeros(self):
        self.run_table([
            (0.0, "tick", (), C(0.0, 0.0, "none", FRONT_STALE)),            # скана ещё нет — вперёд нельзя
            (0.0, "scan", (), C(0.0, 0.0, "none")),
            (0.1, "gesture", (0.0, 0.0), C(0.0, 0.0, "none")),              # нули демо — не заявка
        ])

    def test_gesture_drives_until_input_stale(self):
        g = self.run_table([
            (0.0, "scan", (), C(0.0, 0.0, "none")),
            (0.1, "gesture", (0.5, 0.0), C(0.5, 0.0, "gesture")),
            (0.3, "scan", (), C(0.5, 0.0, "gesture")),
            (0.49, "tick", (), C(0.5, 0.0, "gesture")),                     # возраст 0,39 — ещё свежий
            (0.51, "tick", (), C(0.0, 0.0, "none")),                        # старше 0,4 — явный ноль
            (0.81, "tick", (), C(0.0, 0.0, "none", FRONT_STALE)),           # и скан старше 0,5
        ])
        self.assertEqual(g.counters["stale"], 1)

    def test_agent_nonzero_beats_gesture(self):
        g = self.run_table([
            (0.0, "scan", (), C(0.0, 0.0, "none")),
            (0.0, "gesture", (0.5, 0.0), C(0.5, 0.0, "gesture")),
            (0.1, "agent", (0.08, 0.0), C(0.08, 0.0, "agent")),
            (0.2, "gesture", (0.5, 0.0), C(0.08, 0.0, "agent")),
            (0.3, "scan", (), C(0.08, 0.0, "agent")),
        ])
        self.assertEqual(g.counters["preempted"], 1)

    def test_agent_stop_cancels_gesture_maneuver_until_its_end(self):
        self.run_table([
            (0.0, "scan", (), C(0.0, 0.0, "none")),
            (0.0, "gesture", (0.0, -1.0), C(0.0, -1.0, "gesture")),
            (0.1, "agent", (0.0, 0.0), C(0.0, 0.0, "agent")),               # стоп агента — явный ноль
            (0.3, "gesture", (0.0, -1.0), C(0.0, 0.0, "agent")),
            (0.4, "scan", (), C(0.0, 0.0, "agent")),
            (0.6, "gesture", (0.0, -1.0), C(0.0, 0.0, "none")),             # агент устарел, манёвр погашен
            (0.7, "gesture", (0.0, 0.0), C(0.0, 0.0, "none")),              # манёвр демо кончился
            (0.75, "scan", (), C(0.0, 0.0, "none")),
            (0.8, "gesture", (0.5, 0.0), C(0.5, 0.0, "gesture")),           # новый жест снова едет
        ])

    def test_agent_silence_after_nonzero_does_not_resume_preempted_maneuver(self):
        g = self.run_table([
            (0.0, "scan", (), C(0.0, 0.0, "none")),
            (0.0, "gesture", (0.5, 0.0), C(0.5, 0.0, "gesture")),
            (0.1, "agent", (0.0, 0.5), C(0.0, 0.5, "agent")),
            (0.3, "scan", (), C(0.0, 0.5, "agent")),
            (0.4, "gesture", (0.5, 0.0), C(0.0, 0.5, "agent")),
            (0.55, "gesture", (0.5, 0.0), C(0.0, 0.0, "none")),             # агент замолчал: ноль, не манёвр
        ])
        self.assertEqual(g.counters["stale"], 1)

    def test_obstacle_in_sector_cuts_forward_keeps_turns_and_reverse(self):
        wall = tb3_scan(deg0=0.3)
        g = self.run_table([
            (0.0, "scan", (wall,), C(0.0, 0.0, "none", FRONT_OBSTACLE)),
            (0.1, "gesture", (0.5, 0.0), C(0.0, 0.0, "gesture", FRONT_OBSTACLE, True)),
            (0.2, "gesture", (0.5, 0.3), C(0.0, 0.3, "gesture", FRONT_OBSTACLE, True)),
            (0.3, "gesture", (-0.05, 0.0), C(-0.05, 0.0, "gesture", FRONT_OBSTACLE)),
            (0.4, "gesture", (0.5, 0.0), C(0.0, 0.0, "gesture", FRONT_OBSTACLE, True)),
            (0.45, "scan", (), C(0.5, 0.0, "gesture")),                     # препятствие ушло
        ])
        self.assertEqual(g.counters["cut_forward"], 2)

    def test_stop_distance_grows_with_speed(self):
        self.assertAlmostEqual(stop_distance(Params(), 0.0), 0.35)
        self.assertAlmostEqual(stop_distance(Params(), 0.5), 0.35 + 0.5 * 0.2 + 0.25 / 2)  # 0,575 м
        self.assertAlmostEqual(stop_distance(Params(), -0.05), 0.35)
        self.run_table([
            (0.0, "scan", (tb3_scan(deg0=0.45),), C(0.0, 0.0, "none")),     # стоя — свободно
            (0.1, "gesture", (0.5, 0.0), C(0.0, 0.0, "gesture", FRONT_OBSTACLE, True)),  # 0,45 < 0,575
            (0.2, "agent", (0.08, 0.0), C(0.08, 0.0, "agent")),             # 0,45 ≥ 0,369 — толчок проходит
            (0.3, "scan", (tb3_scan(deg0=0.36),), C(0.0, 0.0, "agent", FRONT_OBSTACLE, True)),
        ])

    def test_sector_bounds(self):
        for key, blocked in (("deg29", True), ("deg-29", True), ("deg31", False), ("deg-31", False),
                             ("deg180", False)):
            g = idle()
            feed(g, 0.0, tb3_scan(**{key: 0.2}))
            g.gesture(0.0, 0.5, 0.0)
            self.assertEqual(g.command(0.0).blocked_front, blocked, key)

    def test_d_stop_is_strict(self):
        g = idle(d_stop=0.35)
        feed(g, 0.0, tb3_scan(deg0=0.35))
        self.assertFalse(g.command(0.0).blocked_front)
        feed(g, 0.0, tb3_scan(deg0=0.349))
        self.assertTrue(g.command(0.0).blocked_front)

    def test_scan_stale_blocks_forward_only(self):
        g = idle()
        feed(g, 0.0)
        g.agent(0.4, 0.1, 0.2)
        self.assertEqual(g.command(0.5), C(0.1, 0.2, "agent"))
        g.agent(0.55, 0.1, 0.2)
        self.assertEqual(g.command(0.55), C(0.0, 0.2, "agent", FRONT_STALE, True))


class BlindSector(unittest.TestCase):
    """RS3: провал данных в переднем секторе — «нет данных, вперёд нельзя», повороты можно."""

    def guard(self, ranges, **params) -> Guard:
        g = idle(**params)
        feed(g, 0.0, ranges, URG)
        return g

    def test_robot_scan_with_camera_shadow_blocks_forward(self):
        g = self.guard(urg_scan(**RS3))
        self.assertAlmostEqual(g.front.gap_deg, (353 - 256 + 1) * math.degrees(URG_INC), places=6)  # ≈34°
        self.assertEqual(g.front.nearest, 3.0)  # остаток сектора +4…+30° свободен
        g.gesture(0.0, 0.5, 0.0)
        self.assertEqual(g.command(0.0), C(0.0, 0.0, "gesture", FRONT_BLIND, True))
        g.gesture(0.1, 0.0, -1.0)
        self.assertEqual(g.command(0.1), C(0.0, -1.0, "gesture", FRONT_BLIND))
        self.assertEqual(g.counters["cut_forward"], 1)

    def test_allow_blind_under_supervision(self):
        g = self.guard(urg_scan(**RS3), allow_blind=True)
        g.gesture(0.0, 0.5, 0.0)
        self.assertEqual(g.command(0.0), C(0.5, 0.0, "gesture"))
        feed(g, 0.1, urg_scan(i300=0.3, **RS3), URG)  # видимое препятствие по-прежнему останавливает
        self.assertEqual(g.command(0.1), C(0.0, 0.0, "gesture", FRONT_OBSTACLE, True))

    def test_isolated_error_codes_and_dropouts_are_tolerated(self):
        # N14: одиночные коды ошибок URG (0,006–0,019 м) и NaN — не препятствие и не слепота
        g = self.guard(urg_scan(i341=0.007, i330=math.nan, i300=0.019, i420=0.006))
        self.assertEqual(g.front, (3.0, math.degrees(URG_INC)))
        g.gesture(0.0, 0.5, 0.0)
        self.assertEqual(g.command(0.0), C(0.5, 0.0, "gesture"))

    def test_gap_threshold(self):
        # 8 лучей подряд — 2,81°, 9 — 3,16° при max_gap_deg 3
        for n, front in ((8, FRONT_CLEAR), (9, FRONT_BLIND)):
            g = self.guard(urg_scan(nan_from=URG_FRONT, nan_to=URG_FRONT + n - 1))
            self.assertEqual(g.front_state(0.0, 0.0), front, n)
        g = self.guard(urg_scan(nan_from=URG_FRONT, nan_to=URG_FRONT + 8), max_gap_deg=4.0)
        self.assertEqual(g.front_state(0.0, 0.0), FRONT_CLEAR)


class FrontScan(unittest.TestCase):
    def test_tb3_full_circle(self):
        self.assertEqual(front_scan(tb3_scan(deg359=1.0, deg1=2.0), 0.0, 2 * math.pi / N, HALF, 0.12, 3.5),
                         (1.0, 0.0))

    def test_urg_robot_geometry(self):
        ranges = [4.0] * URG_N
        ranges[0] = ranges[-1] = 0.1   # ±120° — вне сектора
        ranges[URG_FRONT] = 0.8        # прямо вперёд
        ranges[URG_FRONT - 85] = 0.7   # −29,9° — в секторе
        ranges[URG_FRONT + 86] = 0.6   # +30,2° — вне
        self.assertEqual(front_scan(ranges, *URG[:2], HALF, *URG[2:]).nearest, 0.7)

    def test_invalid_values(self):
        inc = 2 * math.pi / N
        got = front_scan(tb3_scan(deg0=math.nan, deg1=math.inf, deg2=0.05, deg3=9.0), 0.0, inc, HALF, 0.12, 3.5)
        self.assertEqual(got.nearest, 3.0)                     # NaN, +inf, ниже минимума, дальше максимума
        # провалы — NaN и 0,05 по одному лучу: +inf между ними их разделяет
        self.assertAlmostEqual(got.gap_deg, 1.0)
        self.assertEqual(front_scan(tb3_scan(deg0=-math.inf), 0.0, inc, HALF, 0.12, 3.5), (0.0, 0.0))
        self.assertEqual(front_scan([math.inf] * N, 0.0, inc, HALF, 0.12, 3.5), (math.inf, 0.0))

    def test_gap_across_array_end(self):
        # у скана 0…2π передний сектор — конец и начало массива: провал 358…2 сплошной
        ranges = tb3_scan(deg358=math.nan, deg359=math.nan, deg0=math.nan, deg1=0.01, deg2=math.nan)
        got = front_scan(ranges, 0.0, 2 * math.pi / N, HALF, 0.12, 3.5)
        self.assertAlmostEqual(got.gap_deg, 5.0)


if __name__ == "__main__":
    unittest.main()
