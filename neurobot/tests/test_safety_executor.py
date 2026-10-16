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
