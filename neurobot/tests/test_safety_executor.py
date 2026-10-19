"""Исполнитель v1 под контрактом safety_gate (neurobot/executor/guard.py, PLAN §13.3): отмена задачи и task_end,
жест не исполняется при защёлке и блоке, слепой сектор решает safety_gate, нечисловые входы, вырожденный скан,
удержание старта (манёвр, начатый до рестарта исполнителя, не продолжается). Без rclpy, Python ≥ 3.8. Правила v1
без safety_gate — test_executor_guard.py.
"""
from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from neurobot.executor.guard import (FRONT_BLIND, FRONT_CLEAR, Guard, Params, Safety, parse_safety,  # noqa: E402
                                     stop_distance)

N = 360
TB3 = (0.0, 2 * math.pi / N, 0.12, 3.5)
OK = Safety("ok", 0.25, 0.05, 0.8, ())
BLOCKED_FRONT = Safety("blocked", 0.0, 0.05, 0.8, ("obstacle: глубина — путь 0.05 м",))
ESTOP = Safety("estop", 0.0, 0.0, 0.0, ("estop: human: кнопка",))


def at_rest(g: Guard) -> Guard:
    """Входы до теста уже в покое: демо прислало нули, агент молчит — удержание старта снято."""
    g.gesture(-1.0, 0.0, 0.0)
    g.agent(-1.0, 0.0, 0.0)
    return g


def guard(**params) -> Guard:
    g = at_rest(Guard(Params(require_safety=True, **params)))
    g.scan(0.0, [3.0] * N, *TB3)
    return g


class ParseSafety(unittest.TestCase):
    def test_parse(self):
        doc = {"state": "slow", "reasons": ["lidar_blind: …"], "limits": {"v_max": 0.15, "v_rev": 0.05, "w_max": 0.8}}
        self.assertEqual(parse_safety(json.dumps(doc)), Safety("slow", 0.15, 0.05, 0.8, ("lidar_blind: …",)))
        doc["limits"] = {"v_max": True, "w_max": None}  # не число — 0, строже
        self.assertEqual(parse_safety(json.dumps(doc))[1:4], (0.0, 0.0, 0.0))
        for bad in ("{", '{"state": "fine"}', "[]", '{"reasons": []}'):
            with self.assertRaises(ValueError):
                parse_safety(bad)


class Cancel(unittest.TestCase):
    def test_without_safety_gate_nothing_moves(self):
        g = guard()
        g.gesture(0.0, 0.5, 0.0)
        cmd = g.command(0.0)
        self.assertEqual((cmd.linear, cmd.angular, cmd.by), (0.0, 0.0, "none"))
        self.assertEqual(g.events, [{"type": "task_end", "task": "gesture", "status": "safety_lost", "reasons": []}])

    def test_estop_cancels_maneuver_until_its_end(self):
        g = guard()
        g.safety(0.0, OK)
        g.gesture(0.0, 0.5, 0.0)
        self.assertEqual(g.command(0.0).linear, 0.5)
        g.safety(0.1, ESTOP)
        g.gesture(0.1, 0.5, 0.0)
        self.assertEqual(g.command(0.1).by, "none")
        self.assertEqual(g.events[-1]["status"], "estop")
        self.assertEqual(g.events[-1]["reasons"], ["estop: human: кнопка"])
        g.safety(0.2, OK)  # защёлку сняли, а манёвр демо ещё идёт по своим часам — не продолжаем
        g.gesture(0.2, 0.5, 0.0)
        self.assertEqual(g.command(0.2).by, "none")
        g.gesture(0.3, 0.0, 0.0)  # манёвр кончился
        g.gesture(0.4, 0.5, 0.0)  # новый жест
        self.assertEqual(g.command(0.4).linear, 0.5)
        self.assertEqual(g.counters["safety_cancel"], 1)

    def test_blocked_forward_cancels_forward_gesture_turn_runs(self):
        g = guard()
        g.safety(0.0, BLOCKED_FRONT)
        g.gesture(0.0, 0.5, 0.3)  # жест «вперёд» при блоке не исполняется
        self.assertEqual(g.command(0.0).by, "none")
        self.assertEqual(g.events[-1]["status"], "blocked")
        g.gesture(0.1, 0.0, 0.0)
        g.gesture(0.2, 0.0, -1.0)  # поворот на месте safety_gate пропускает
        cmd = g.command(0.2)
        self.assertEqual((cmd.angular, cmd.by), (-1.0, "gesture"))

    def test_agent_task_cancelled_until_agent_stops(self):
        g = guard()
        g.safety(0.0, OK)
        g.agent(0.0, 0.1, 0.0)
        self.assertEqual(g.command(0.0).by, "agent")
        g.safety(0.1, BLOCKED_FRONT)
        g.agent(0.1, 0.1, 0.0)
        self.assertEqual(g.command(0.1).by, "none")
        self.assertEqual(g.events[-1]["task"], "agent")
        g.safety(0.2, OK)
        g.agent(0.2, 0.1, 0.0)
        self.assertEqual(g.command(0.2).by, "none")  # та же задача агента не продолжается
        g.agent(0.3, 0.0, 0.0)
        g.agent(0.35, 0.1, 0.0)
        self.assertEqual(g.command(0.35).by, "agent")

    def test_safety_state_goes_stale(self):
        g = guard()
        g.safety(0.0, OK)
        g.scan(0.9, [3.0] * N, *TB3)
        g.gesture(0.9, 0.5, 0.0)
        self.assertEqual(g.command(0.9).linear, 0.5)
        g.scan(1.1, [3.0] * N, *TB3)
        g.gesture(1.1, 0.5, 0.0)
        self.assertEqual(g.command(1.1).by, "none")
        self.assertEqual(g.events[-1]["status"], "safety_lost")

    def test_zero_command_is_not_a_task(self):
        g = guard()
        g.safety(0.0, ESTOP)
        g.agent(0.0, 0.0, 0.0)  # явный стоп агента — не задача, отменять нечего
        self.assertEqual(g.command(0.0).by, "agent")
        self.assertEqual(g.events, [])


class BlindAndInputs(unittest.TestCase):
    URG = (-2.0923497677, 2 * 2.0923497677 / 682, 0.02, 5.6)

    def test_blind_sector_decided_by_safety_gate(self):
        ranges = [3.0] * 683
        ranges[168:354] = [math.nan] * 186  # RS3
        g = Guard(Params(require_safety=True))
        g.scan(0.0, ranges, *self.URG)
        g.safety(0.0, Safety("slow", 0.15, 0.05, 0.8, ("lidar_blind: …",)))
        self.assertEqual(g.front_state(0.0, 0.15), FRONT_CLEAR)  # глубина у safety_gate видит перед
        g.scan(2.0, ranges, *self.URG)
        self.assertEqual(g.front_state(2.0, 0.15), FRONT_BLIND)  # safety_gate потерян — правило v1

    def test_non_finite_inputs_are_zero(self):
        for bad in ((math.nan, 0.0), (math.inf, 0.0), (-math.inf, 0.0), (0.0, math.inf), (0.0, math.nan)):
            g = guard()
            g.safety(0.0, OK)
            g.scan(0.0, [0.3] * N, *TB3)  # препятствие: NaN не должен обойти срез
            g.agent(0.0, *bad)
            g.gesture(0.0, *bad)
            cmd = g.command(0.0)
            self.assertEqual((cmd.linear, cmd.angular), (0.0, 0.0), bad)
            self.assertEqual(g.counters["invalid"], 2, bad)
        self.assertEqual(stop_distance(Params(), math.nan), math.inf)

    def test_degenerate_scan_metadata(self):
        for args in (([], *TB3), ([1.0] * N, 0.0, math.nan, 0.12, 3.5), ([1.0] * N, 0.0, 0.0, 0.12, 3.5),
                     ([1.0] * N, 0.0, TB3[1], 0.12, 0.0), ([1.0] * N, 0.0, TB3[1], 0.12, math.nan),
                     ([1.0] * N, 0.0, TB3[1], -0.1, 3.5)):
            g = at_rest(Guard())
            with self.assertRaises(ValueError):
                g.scan(0.0, *args)
            g.gesture(0.0, 0.5, 0.0)
            self.assertEqual(g.command(0.0).front, "stale")  # скан не принят — вперёд нельзя


FRONT = "obstacle: глубина — путь 0.00 м от края корпуса, вперёд нельзя"
TOUCH = "touch: препятствие у корпуса (глубина, с ошибкой клетки 0.04 м) — поворот на месте заденет"
DEAD_END = Safety("blocked", 0.0, 0.05, 0.0, (FRONT, TOUCH))  # S5 повторной приёмки: стена под углом
TURN_OK = Safety("blocked", 0.0, 0.05, 0.8, (FRONT,))           # отъехал на 0,046 м — поворот снова можно
# lab_v4 (1139 с): поворот заденет, вперёд — только подползти к препятствию 0,06 м — жесты с поворотом отменяются
NEAR = "approach: глубина — путь 0.06 м, ≤ 0.03 м/с"
CREEP = Safety("slow", 0.03, 0.05, 0.0, (NEAR, TOUCH))


class Escape(unittest.TestCase):
    """Выход из тупика (повторная приёмка 18.10): вперёд нельзя, поворот заденет, назад можно — без заявок исполнитель
    через escape_after_s сам отъезжает назад по своему следу (/odom), не дальше escape_dist."""

    @staticmethod
    def trail(g: Guard, dist: float, yaw: float = 0.0) -> Guard:
        """Робот проехал вперёд dist по курсу yaw (одометрия по 1 см)."""
        for k in range(int(round(dist / 0.01)) + 1):
            g.odom(0.01 * k * math.cos(yaw), 0.01 * k * math.sin(yaw), yaw)
        return g

    @staticmethod
    def steps(g: Guard, k0: int, k1: int, s: Safety, agent=None) -> list:
        """Шаги по 0,05 с (k0…k1 − 1): состояние safety_gate s, агент — как вход 20 Гц; [(t, Command)]."""
        out = []
        for k in range(k0, k1):
            t = k / 20
            g.safety(t, s)
            if agent is not None:
                g.agent(t, *agent)
            out.append((t, g.command(t)))
        return out

    def test_dead_end_backs_out_along_own_trail(self):
        g = self.trail(guard(), 0.5)  # подъехал к стене
        idle = self.steps(g, 0, 60, DEAD_END)
        self.assertEqual({(c.linear, c.angular, c.by) for _, c in idle}, {(0.0, 0.0, "none")})
        back = self.steps(g, 60, 100, DEAD_END)  # 3 с тупика — 2 с назад со скоростью 0,05: 0,1 м
        self.assertEqual({(c.linear, c.angular, c.by) for _, c in back}, {(-0.05, 0.0, "escape")})
        self.assertEqual(g.events, [{"type": "escape", "status": "start", "dist_m": 0.1, "reasons": [FRONT, TOUCH]}])
        self.assertFalse(back[0][1].cut)
        end = self.steps(g, 100, 110, TURN_OK)
        self.assertEqual({(c.linear, c.by) for _, c in end}, {(0.0, "none")})
        self.assertEqual(g.events[-1], {"type": "task_end", "task": "escape", "status": "done", "reasons": []})
        self.assertEqual(g.counters["escape"], 1)
        self.steps(g, 110, 300, TURN_OK)  # поворот можно — не тупик, второго отъезда нет
        self.assertEqual(g.counters["escape"], 1)

    def test_turn_ban_with_obstacle_just_ahead_is_a_dead_end(self):
        # вперёд можно только подползти к препятствию (approach), поворот заденет — тоже тупик; поворот нельзя, а
        # впереди свободно (только слепой сектор лидара) — не тупик: «вперёд» жеста уводит робота
        g = self.trail(guard(), 0.5)
        self.steps(g, 0, 100, CREEP)
        self.assertEqual(g.events[0]["type"], "escape")
        open_ahead = Safety("slow", 0.15, 0.05, 0.0, ("lidar_blind: перед в слепом секторе лидара", TOUCH))
        g = self.trail(guard(), 0.5)
        self.assertEqual({c.by for _, c in self.steps(g, 0, 200, open_ahead)}, {"none"})
        self.assertEqual(g.counters["escape"], 0)

    def test_escape_only_as_far_as_the_trail(self):
        # след 0,04 м — отъезд 0,8 с; следа нет (поставлен руками), он короче 3 см, после поворота на месте его нет,
        # скачок одометрии (рестарт базы), защёлка и потеря одометрии след обнуляют — отъезда нет
        g = self.trail(guard(), 0.04)
        back = [c for _, c in self.steps(g, 0, 120, DEAD_END) if c.by == "escape"]
        self.assertEqual(len(back), 16)
        self.assertEqual(g.events[0]["dist_m"], 0.04)
        no_odom = Safety("blocked", 0.0, 0.0, 0.0, ("stale_odom: /odom — 0.60 с без данных",))
        for name, prepare in (("руками", lambda g: g), ("2 см", lambda g: self.trail(g, 0.02)),
                              ("поворот", lambda g: g.odom(0.5, 0.0, 0.3)),
                              ("скачок", lambda g: g.odom(2.0, 0.0, 0.0)),
                              ("защёлка", lambda g: g.safety(0.0, ESTOP)),
                              ("нет одометрии", lambda g: g.safety(0.0, no_odom))):
            g = guard()
            if name not in ("руками", "2 см"):
                self.trail(g, 0.5)
            prepare(g)
            cmds = self.steps(g, 0, 200, DEAD_END)
            self.assertEqual({c.by for _, c in cmds}, {"none"}, name)
            self.assertEqual(g.counters["escape"], 0, name)
        g = self.trail(guard(), 0.5)  # полный стоп по лидару (отказ драйвера) робота не двигает — след остаётся
        g.safety(0.0, Safety("blocked", 0.0, 0.0, 0.0, ("stale_scan: /scan — 0.50 с без данных",)))
        self.steps(g, 0, 70, DEAD_END)
        self.assertEqual(g.events[0]["dist_m"], 0.1)
        g = self.trail(guard(), 0.5)
        g.odom(0.5, 0.0, 0.3)  # развернулся у стены — след считается заново
        for k in range(1, 7):  # и подъехал 6 см
            g.odom(0.5 + 0.01 * k * math.cos(0.3), 0.01 * k * math.sin(0.3), 0.3)
        self.steps(g, 0, 70, DEAD_END)
        self.assertEqual(g.events[0]["dist_m"], 0.06)

    def test_no_escape_without_touch_reverse_or_safety(self):
        # поворот можно (не тупик), задний ход исчерпан или запрещён (reverse_limit), защёлка, safety_gate потерян,
        # отъезд выключен — исполнитель стоит
        no_rev = Safety("blocked", 0.0, 0.0, 0.0, (FRONT, TOUCH, "reverse_limit: назад уже 0.20 м"))
        for s, params in ((TURN_OK, {}), (no_rev, {}), (ESTOP, {}), (DEAD_END, {"escape_after_s": 0.0})):
            g = self.trail(guard(**params), 0.5)
            cmds = self.steps(g, 0, 200, s)
            self.assertEqual({c.by for _, c in cmds}, {"none"}, (s.state, params))
            self.assertEqual(g.counters["escape"], 0)
        g = self.trail(guard(), 0.5)
        g.safety(0.0, DEAD_END)
        for k in range(200):
            g.command(k / 20)  # состояние safety_gate не обновляется — потерян через safety_timeout
        self.assertEqual(g.counters["escape"], 0)

    def test_agent_preempts_escape_and_safety_cuts_it(self):
        g = self.trail(guard(), 0.5)
        self.steps(g, 0, 70, DEAD_END)
        self.assertEqual(g.counters["escape"], 1)
        cmd = self.steps(g, 70, 71, DEAD_END, agent=(0.0, 0.0))[0][1]  # явный стоп агента главнее отъезда
        self.assertEqual((cmd.linear, cmd.by), (0.0, "agent"))
        self.assertEqual(g.events[-1]["status"], "preempted")
        self.steps(g, 71, 80, DEAD_END)  # агент замолчал — тупик отсчитывается заново
        self.assertEqual(g.counters["escape"], 1)
        self.steps(g, 80, 140, DEAD_END)
        self.assertEqual(g.counters["escape"], 2)
        no_rev = Safety("blocked", 0.0, 0.0, 0.0, (FRONT, TOUCH, "reverse_limit: назад уже 0.20 м"))
        cmd = self.steps(g, 140, 141, no_rev)[0][1]  # safety_gate не пускает назад — отъезд отменён
        self.assertEqual((cmd.linear, cmd.by), (0.0, "none"))
        self.assertEqual(g.events[-1], {"type": "task_end", "task": "escape", "status": "blocked",
                                        "reasons": list(no_rev.reasons)})
        g.gesture(7.1, 0.5, 0.0)  # жест после отменённого отъезда не погашен: отмена отъезда его не касается
        g.safety(7.1, Safety("slow", 0.15, 0.05, 0.8, ()))
        self.assertEqual(g.command(7.1).by, "gesture")


class StartHold(unittest.TestCase):
    """Рестарт исполнителя посреди манёвра (ревью W2): новый процесс не продолжает вход, ненулевой с самого старта."""

    def run_inputs(self, g: Guard, t0: float, t1: float, gesture=None, agent=None) -> set:
        """Шаги по 0,1 с: safety_gate на связи, скан свежий, входы — как у демо и агента (10 Гц); что исполнялось."""
        seen = set()
        for k in range(int(round((t1 - t0) / 0.1))):
            t = t0 + 0.1 * k
            g.safety(t, OK)
            g.scan(t, [3.0] * N, *TB3)
            if gesture is not None:
                g.gesture(t, *gesture)
            if agent is not None:
                g.agent(t, *agent)
            cmd = g.command(t)
            seen.add((cmd.linear, cmd.angular, cmd.by))
        return seen

    def test_gesture_maneuver_in_flight_is_not_resumed(self):
        g = Guard(Params(require_safety=True))
        self.assertEqual(self.run_inputs(g, 0.0, 0.3), {(0.0, 0.0, "none")})  # первые шаги — до первого сообщения
        # демо продолжает манёвр, начатый до рестарта, и дольше start_quiet_s — он не исполняется
        self.assertEqual(self.run_inputs(g, 0.3, 4.0, gesture=(0.5, 0.0)), {(0.0, 0.0, "none")})
        self.run_inputs(g, 4.0, 4.2, gesture=(0.0, 0.0))  # манёвр кончился: демо в покое шлёт нули
        self.assertIn((0.5, 0.0, "gesture"), self.run_inputs(g, 4.2, 4.5, gesture=(0.5, 0.0)))  # новый жест
        self.assertEqual(g.events, [])  # это не отмена задачи safety_gate

    def test_agent_command_in_flight_waits_for_pause(self):
        g = Guard(Params(require_safety=True))
        self.assertEqual(self.run_inputs(g, 0.0, 1.0, agent=(0.1, 0.0)), {(0.0, 0.0, "none")})
        self.run_inputs(g, 1.0, 1.6)  # агент замолчал дольше input_timeout
        self.assertIn((0.1, 0.0, "agent"), self.run_inputs(g, 1.6, 2.0, agent=(0.1, 0.0)))

    def test_inputs_quiet_since_start_are_armed(self):
        g = Guard(Params(require_safety=True))
        self.run_inputs(g, 0.0, 2.1)  # ни демо, ни агент не писали start_quiet_s
        self.assertIn((0.1, 0.0, "agent"), self.run_inputs(g, 2.1, 2.3, agent=(0.1, 0.0)))
        g = Guard(Params(require_safety=True))
        self.run_inputs(g, 0.0, 2.1)
        self.assertIn((0.5, 0.0, "gesture"), self.run_inputs(g, 2.1, 2.3, gesture=(0.5, 0.0)))


if __name__ == "__main__":
    unittest.main()
