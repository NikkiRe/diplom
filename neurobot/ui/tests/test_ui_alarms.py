"""Пульт: философия тревог (alarms.py) — два уровня (PLAN §0а п. 2), норма серым, словарь против контракта README
safety, отслеживание (повтор — счётчик, снятие с задержкой, эскалация). Только stdlib, Python 3.8+."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from neurobot.ui import alarms as A  # noqa: E402

BLIND = "lidar_blind: перед в слепом секторе лидара — только глубина, ≤ 0.15 м/с"
UNSEEN = "lidar_unseen: нет данных лидара 36.6…43.9° у корпуса, вне поля глубины — как слепой сектор, ≤ 0.15 м/с"


class LevelTest(unittest.TestCase):
    def test_normal_robot_state_is_grey(self):
        # на роботе slow с lidar_blind — обычное рабочее состояние: не тревога
        self.assertEqual(A.level("slow", [BLIND]), A.NORM)
        self.assertEqual(A.level("slow", [BLIND, UNSEEN]), A.NORM)
        self.assertEqual(A.level("blocked", ["unarmed: ждёт нулевой запрос"]), A.NORM)
        self.assertEqual(A.level("blocked", ["lidar_mask: учим маску слепых лучей, 1/20 сканов"]), A.NORM)
        self.assertEqual(A.level("ok", []), A.NORM)

    def test_obstacle_codes_alarm_only_when_stopped(self):
        self.assertEqual(A.level("blocked", ["obstacle: глубина — путь 0.06 м", BLIND]), A.P2)
        self.assertEqual(A.level("slow", ["approach: глубина — путь 0.15 м, ≤ 0.24 м/с", BLIND]), A.NORM)
        self.assertEqual(A.level("blocked", ["touch: поворот на месте заденет"]), A.P2)
        self.assertEqual(A.level("slow", ["touch: поворот на месте заденет"]), A.NORM)
        self.assertEqual(A.level("blocked", ["lidar_nodata: нет данных лидара перед роботом"]), A.P2)

    def test_priorities(self):
        self.assertEqual(A.level("estop", ["shock: удар: всплеск ускорения 9.8 м/с² > 6"]), A.P1)
        self.assertEqual(A.level("estop", []), A.P1)                  # защёлка без причины — всё равно P1
        self.assertEqual(A.level("blocked", ["stale_scan: /scan — 0.42 с без данных", "obstacle: …"]), A.P2)
        self.assertEqual(A.level("blocked", ["lidar_fault: пустые сканы"]), A.P1)
        self.assertEqual(A.level("slow", ["reverse_limit: назад уже 0.26 м", BLIND]), A.P2)
        self.assertEqual(A.level("slow", [BLIND], warnings=["override: есть издатель /cmd_vel_joy"]), A.P2)
        self.assertEqual(A.level("slow", ["new_code: неизвестное"]), A.P2)   # новый код без словаря не прячется

    def test_dictionary_covers_safety_contract(self):
        # все коды reasons и warnings из README safety («/neurobot/safety_state») — в словаре пульта
        text = (ROOT / "neurobot" / "safety" / "README.md").read_text(encoding="utf-8")
        block = text[text.index("-  `reasons` — «код: текст»"):text.index("- `estop` — `{engaged, since, reasons}`")]
        codes = set(re.findall(r"`([a-z_]+)`", block))
        for c in ("stale_scan", "stale_odom", "stale_imu", "stale_accel", "stale_depth"):
            codes.add(c)
        codes -= {"reasons", "warnings", "stale_", "cmd_vel_joy", "cmd_vel_tracker", "scan"}
        missing = sorted(c for c in codes if c not in A.CODES)
        self.assertEqual(missing, [])
        self.assertTrue({"estop", "shock", "obstacle", "lidar_blind", "override_active"} <= codes)

    def test_two_levels(self):
        # §0а п. 2: тревоги — два уровня; P1 — защёлка, отказ лидара, safety_state молчит
        self.assertEqual({c.prio for c in A.CODES.values()} | {A.UNKNOWN.prio}, {A.NORM, A.P1, A.P2})
        p1 = sorted(k for k, c in A.CODES.items() if c.prio == A.P1)
        self.assertEqual(p1, ["estop", "foreign_writer", "lidar_fault", "shock", "stall", "tilt", "ui_safety_silent"])

    def test_signs_and_texts(self):
        d = A.dictionary()
        self.assertEqual(d["shock"]["prio"], 1)
        self.assertEqual(d["lidar_blind"]["prio"], 0)
        self.assertTrue(d["obstacle"]["only_stopped"])
        self.assertTrue(all(v["text"] for v in d.values()))


class TrackerTest(unittest.TestCase):
    def test_repeat_is_counter_and_off_delay(self):
        tr = A.AlarmTracker(off_delay_s=3.0)
        ob = A.safety_alarms("blocked", ["obstacle: …"])
        on = tr.update(10.0, "safety", ob)
        self.assertEqual([(m["key"], m["prio"], m["on"], m["n"]) for m in on], [("obstacle", 2, True, 1)])
        self.assertEqual(tr.update(11.0, "safety", []), [])           # мерцание короче задержки — тревога стоит
        self.assertEqual(tr.update(12.0, "safety", ob), [])
        off = tr.update(15.5, "safety", [])
        self.assertEqual([(m["key"], m["on"]) for m in off], [("obstacle", False)])
        again = tr.update(20.0, "safety", ob)
        self.assertEqual([(m["on"], m["n"]) for m in again], [(True, 2)])   # та же строка, счётчик 2

    def test_sources_are_independent_and_escalation(self):
        tr = A.AlarmTracker()
        tr.update(1.0, "unit", [A.Alarm("ui_unit_down:perception", A.P2, "компонент не работает")])
        tr.update(1.0, "safety", A.safety_alarms("blocked", ["obstacle: …"]))
        off = tr.update(10.0, "safety", [])
        self.assertEqual([m["key"] for m in off], ["obstacle"])           # safety снята, юнит стоит
        self.assertEqual(list(tr.active), [("unit", "ui_unit_down:perception")])
        msgs = tr.update(11.0, "x", [A.Alarm("k", A.P2, "a")]) + tr.update(12.0, "x", [A.Alarm("k", A.P1, "b")])
        self.assertEqual([(m["prio"], m["on"]) for m in msgs], [(2, True), (1, True)])
        closed = tr.close(20.0)
        self.assertTrue(closed and all(not m["on"] for m in closed) and not tr.active)

    def test_immediate_off(self):
        tr = A.AlarmTracker()
        tr.update(1.0, "link", [A.Alarm("ui_safety_silent", A.P1, "молчит")])
        self.assertEqual([m["on"] for m in tr.update(1.5, "link", [], off_delay=0.0)], [False])

    def test_estop_without_p1_reason(self):
        al = A.safety_alarms("estop", ["stale_imu: …"])
        self.assertEqual([(a.key, a.prio) for a in al], [("estop", 1), ("stale_imu", 2)])
        self.assertEqual(A.safety_alarms("slow", [BLIND, UNSEEN]), [])


if __name__ == "__main__":
    unittest.main()
