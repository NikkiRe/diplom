#!/usr/bin/env python3
"""Набор безопасности (PLAN §13.4) на двойнике с пакетом neurobot целиком (в ВМ sim, root, после setup_twin.sh и
systemctl start neurobot.target): каждый сценарий — скрипт, итог — по истине Gazebo (/twin/truth).

    python3 safety.py --out DIR [--label L] [сценарий …]      по умолчанию — все сценарии мира lab по порядку

Мир lab: start_poses (позы сценариев: стоп на старте — ложный), wall (разгон в стену: манёвр жеста 0,5 м/с),
low_box (низкий ящик, лидар не видит), person (человек выходит на путь), blind_pole (столб в слепом секторе лидара),
low_near (низкий предмет вне поля камеры, вплотную: не видит никто — упор), stall_reverse (задом в стену: сзади
датчиков нет, упор), kill_executor и kill_gate (kill -9 в движении), freeze_gate (SIGSTOP safety_gate),
kill_agent_path (kill -9 rosbridge и MCP посреди толчка агента, PicoClaw в покое), agent_wall (агент толчками MCP в
стену), n1_300s (N1: «300 с» через MCP и в обход L3), joystick (лишний издатель /cmd_vel_joy: ноль и езда в стену),
latch (защёлка: агент снять не может).
Мир step: step_edge. Мир ramp: ramp_drive (въезд на пандус), slope_latch (поставлен на склон 12°).
Движение — с входов исполнителя, как на роботе: «манёвр жеста» — в /cmd_vel_gesture (распознавание на это время
остановлено: в тему пишет его DriveController), «агент» — в /cmd_vel_agent или инструментом MCP robot_nudge.
Итог — DIR/safety.json (--label — метка прогона: before/after); журналы юнитов — DIR/logs/<сценарий>.log. Числа —
ориентир двойника (Gazebo в общей ВМ), не результат робота; в зачёт истины — только образцы с rtf_ok.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import metrics as m  # noqa: E402
from harness import CFG, UNITS, Twin  # noqa: E402
from std_msgs.msg import String  # noqa: E402

log = logging.getLogger("safety")
GESTURE, AGENT, JOY = "/cmd_vel_gesture", CFG.get("NEUROBOT_AGENT_TOPIC"), "/cmd_vel_joy"
DEMO_SPEED = 0.5              # м/с — DriveController демо (self.speed), предел манёвра perception v2
SOUTH = (-1.0, 1.0, 0.01, -math.pi / 2)   # мир lab: на юг 1,9 м свободно, проём северной стены за спиной
EAST_WALL = (1.6, 0.0, 0.01, 0.0)         # восточная стена в 0,9 м (от края корпуса — 0,705 м), проём сзади-слева
# позы сценариев прошлых прогонов safety (W2, neurobot_20261015_safety_fix): 3 из 6 — стоп lidar_nodata на старте
OLD_POSES = {"low_box": (-1.5, 0.0, 0.01, 0.0), "low_box_east": (1.5, 0.0, 0.01, math.pi),
             "wall": (1.0, 0.0, 0.01, 0.0), "person_steps_in": (0.6, 1.0, 0.01, 0.0),
             "kill_gate": (1.0, 0.0, 0.01, 0.0), "joystick": (1.0, 0.0, 0.01, 0.0)}


class Safety:
    def __init__(self, twin: Twin, out: Path):
        self.t, self.out = twin, out
        (out / "logs").mkdir(parents=True, exist_ok=True)
        self.estop = twin.node.create_publisher(String, CFG.get("NEUROBOT_ESTOP_TOPIC"), 10)

    # ---- общее

    def summary(self, t0: float, t1: float, trigger=None) -> dict:
        """Метрики окна [t0, t1] по истине и safety_state; trigger — момент события для тормозного пути."""
        t = self.t
        truth = t.since("truth", t0, t1)
        ok = m.ok_truth(truth)
        states = t.since("state", t0, t1)
        cmds = t.since("cmd", t0, t1)
        zero = m.zero_after(cmds, trigger if trigger is not None else t0)
        doc = {"rtf_ok_share": round(len(ok) / max(len(truth), 1), 3),
               "rtf_min": min((r["rtf"] for r in truth if r.get("rtf") is not None), default=None),
               "min_clearance_m": m.min_clearance(ok), "contacts": m.touched(truth),
               "v_max_truth": round(max((abs(r["v"]) for r in truth), default=0.0), 3),
               "cmd_v_max": round(max((c["v"] for c in cmds), default=0.0), 3),
               "path_m": round(m.path_length(truth), 3),
               "states": sorted({s["state"] for s in states}),
               "stop_reasons": m.stop_reason(states, zero),
               "false_stops": m.false_stops(states, truth)}
        if zero is not None:
            doc.update({"zero_cmd_s": round(zero - (trigger if trigger is not None else t0), 3),
                        "braking": m.braking(truth, zero), "stop_by": self.stop_by(zero)})
        if trigger is not None:
            doc["after_trigger"] = m.braking(truth, trigger)
        if states:
            last = states[-1]
            doc["last_state"] = {k: last.get(k) for k in ("state", "reasons", "warnings", "front_m", "limits")}
        return doc

    def stop_by(self, zero: float) -> str:
        """Кто остановил: исполнитель (его срез вперёд — motion_state cut/front) или safety_gate (blocked, estop)."""
        motion = [s for s in self.t.rec["motion"] if s["t"] <= zero + 0.3]
        state = [s for s in self.t.rec["state"] if s["t"] <= zero + 0.3]
        if state and state[-1]["state"] in ("blocked", "estop"):
            return "safety_gate: " + ", ".join(sorted({r.split(":", 1)[0] for r in state[-1]["reasons"]}))
        if motion and (motion[-1].get("cut") or motion[-1].get("front") not in (None, "clear")):
            return "исполнитель: front={}".format(motion[-1].get("front"))
        return "запрос снят"

    def drive_until_stop(self, topic: str, cmd, limit: float = 25.0, during=None) -> tuple:
        """Пилот публикует cmd во вход topic, пока база по истине не поедет и не встанет (или limit); (t0, t1)."""
        t = self.t
        t0 = time.monotonic()

        def done():
            if during:
                during()
            moved = any(abs(r["v"]) > 0.03 for r in t.since("truth", t0))
            return moved and m.stopped_at(t.since("truth", t0), t0) is not None
        t.spin(limit, until=done, drive={topic: cmd})
        t.spin(1.0, drive={topic: cmd})
        t1 = time.monotonic()
        t.spin(1.0, drive={topic: (0.0, 0.0)})
        return t0, t1

    def gesture_pilot(self, on: bool) -> None:
        """Пилот манёвра жеста: распознавание остановлено, пока в /cmd_vel_gesture пишет сценарий."""
        self.t.systemctl("stop" if on else "start", "neurobot-perception")
        if on:
            self.t.spin(0.5)

    def agent_path_up(self, units, timeout: float = 120.0):
        """Юниты агентского пути снова active и MCP отвечает (tools/list); момент — или None за timeout."""
        def up():
            if not all(self.t.active(u) for u in units):
                return False
            try:
                return bool(self.t.mcp_tools())
            except Exception:  # noqa: BLE001 — сервер ещё поднимается
                return False
        return time.monotonic() if self.t.spin(timeout, until=up) else None

    def restarts(self, units, since: float) -> dict:
        """Сколько раз юниты стартовали с момента since (монотонные секунды) — по строкам systemd «Started …»."""
        text = self.t.journal_text(units, time.time() - (time.monotonic() - since))
        return {u: text.count("Started {}.service".format(u)) for u in units}

    def mcp_async(self, tool: str, args: dict) -> dict:
        """Вызов MCP в потоке (rclpy крутится в главном); итог — в словаре, когда поток кончится."""
        box: dict = {}

        def run():
            t0 = time.monotonic()
            try:
                box["result"] = self.t.mcp(tool, args)
            except Exception as e:  # noqa: BLE001 — при kill сервера ответа нет, это ожидаемо
                box["error"] = repr(e)
            box["took_s"] = round(time.monotonic() - t0, 3)
        box["thread"] = threading.Thread(target=run, daemon=True)
        box["thread"].start()
        return box

    def quiet_after(self, since: float, seconds: float, topic: str, cmd) -> dict:
        """Пилот продолжает просить cmd seconds после события: двигалась ли база (движение само не продолжается)."""
        self.t.spin(seconds, drive={topic: cmd})
        moving = [r for r in self.t.since("truth", since) if abs(r["v"]) > 0.02]
        return {"moved_after": bool(moving), "moved_after_m": round(m.path_length(self.t.since("truth", since)), 3)}

    # ---- мир lab

    def start_poses(self) -> dict:
        """Позы всех сценариев мира lab: стоп вперёд на старте при свободном пути — ложный (решение (А) 16.10)."""
        poses = dict(OLD_POSES, south=SOUTH, east_wall=EAST_WALL)
        out, t = {}, self.t
        for name, pose in poses.items():
            t.relocate(*pose)
            t.spin(1.0)
            t0 = time.monotonic()
            t.spin(1.5, drive={AGENT: (0.1, 0.0)})
            t.spin(0.5, drive={AGENT: (0.0, 0.0)})
            s = t.since("state", t0)
            truth = t.since("truth", t0)
            blocked = [x for x in s if x["state"] == "blocked"]
            first = blocked[0] if blocked else (s[0] if s else {"reasons": [], "warnings": []})
            out[name] = {"pose": list(pose), "moved_m": round(m.path_length(truth), 3), "blocked": bool(blocked),
                         "reasons": first["reasons"], "warnings": first["warnings"],
                         "min_clearance_m": m.min_clearance(truth), "false_stops": m.false_stops(s, truth)}
        false = [n for n, v in out.items() if v["false_stops"] or (v["blocked"] and v["min_clearance_m"] and
                                                                   v["min_clearance_m"] > m.FALSE_CLEAR_M)]
        return {"ok": not false, "false_stop_poses": false, "poses": out}

    def wall(self) -> dict:
        """Разгон в стену на предельной скорости: манёвр жеста 0,5 м/с (DriveController демо) в стену в 1,5 м."""
        t = self.t
        t.relocate(1.0, 0.0, 0.01, 0.0)
        self.gesture_pilot(True)
        t0, t1 = self.drive_until_stop(GESTURE, (DEMO_SPEED, 0.0))
        self.gesture_pilot(False)
        doc = self.summary(t0, t1)
        return dict(doc, ok=not doc["contacts"], requested_v=DEMO_SPEED)

    def low_box(self) -> dict:
        """Низкий ящик 0,15 м по курсу (грань в 1,35 м от центра): лидар над ним, глубина видит с ≈1,3 м — память."""
        t = self.t
        t.relocate(1.5, 0.0, 0.01, math.pi)
        self.gesture_pilot(True)
        t0, t1 = self.drive_until_stop(GESTURE, (DEMO_SPEED, 0.0))
        self.gesture_pilot(False)
        doc = self.summary(t0, t1)
        return dict(doc, ok=not doc["contacts"] and doc["min_clearance_m"] is not None)

    def person(self) -> dict:
        """Человек выходит на путь: робот проехал 0,3 м — человек встаёт в 0,7 м впереди-справа (слепой сектор)."""
        t = self.t
        t.relocate(0.6, 1.0, 0.01, 0.0)
        placed = {}

        def step_in():
            r = t.last("truth")
            if not placed and r and r["x"] >= 0.9:
                placed.update(t=time.monotonic(), x=r["x"])
                t.set_pose("person", r["x"] + 0.7, 0.9, 0.0)
        self.gesture_pilot(True)
        t0, t1 = self.drive_until_stop(GESTURE, (DEMO_SPEED, 0.0), during=step_in)
        self.gesture_pilot(False)
        doc = self.summary(t0, t1, placed.get("t"))
        t.set_pose("person", 1.2, -0.9, 0.0)
        return dict(doc, ok=bool(placed) and not doc["contacts"], person_placed_at_x=placed.get("x"))

    def blind_pole(self) -> dict:
        """Столб Ø5 см, 1 м в слепом секторе лидара (−10° от курса, 0,7 м): видит только глубина."""
        t = self.t
        t.relocate(*SOUTH)
        t.create_pole("pole", SOUTH[0] - 0.12, SOUTH[1] - 0.7, 0.025, 1.0)   # курс на юг: вправо — это −x мира
        t.known("pole")
        self.gesture_pilot(True)
        t0, t1 = self.drive_until_stop(GESTURE, (DEMO_SPEED, 0.0))
        self.gesture_pilot(False)
        doc = self.summary(t0, t1)
        t.remove("pole")
        return dict(doc, ok=not doc["contacts"])

    def low_near(self) -> dict:
        """Низкий предмет 0,10 м в 0,3 м перед корпусом (поставили, пока робот стоял): ниже лидара, ближе поля глубины
        (её пол — с ≈1,2 м, верх 0,10 м — с ≈1 м) — его не видит никто. Робот едет и упирается; дальше — упор."""
        t = self.t
        t.relocate(*SOUTH)
        t.create_box("low_near", SOUTH[0], SOUTH[1] - 0.545, (0.3, 0.1, 0.10))
        t.known("low_near")
        self.gesture_pilot(True)
        t0 = time.monotonic()
        t.spin(8.0, drive={GESTURE: (DEMO_SPEED, 0.0)})
        t1 = time.monotonic()
        t.spin(1.0, drive={GESTURE: (0.0, 0.0)})
        self.gesture_pilot(False)
        doc = self.summary(t0, t1)
        touch = m.first(t.since("truth", t0, t1), t0, lambda r: "low_near" in r.get("contacts", []))
        pushing = [r for r in t.since("truth", touch["t"], t1)] if touch else []
        latch = sorted({r.split(":", 1)[0] for s in t.since("state", t0, t1) if s["state"] == "estop"
                        for r in s["reasons"]})
        doc.update(expected_contact=True, ok=bool(touch), touch_after_s=round(touch["t"] - t0, 3) if touch else None,
                   pushed_m=round(m.path_length(pushing), 3), latch=latch)
        t.remove("low_near")
        if latch:
            doc["human_release_rc"] = t.cli("estop", "release", "--reason", "проверка: ящик убран",
                                            answer="release")["rc"]
        return doc

    def stall_reverse(self) -> dict:
        """Задом в стену: сзади датчиков нет (S7), задний ход ≤ 0,05 м/с и ≤ 0,2 м; стена в 0,155 м за корпусом."""
        t = self.t
        t.relocate(-2.15, 1.0, 0.01, 0.0)
        t0 = time.monotonic()
        t.spin(8.0, drive={AGENT: (-0.05, 0.0)})
        t1 = time.monotonic()
        t.spin(1.0, drive={AGENT: (0.0, 0.0)})
        doc = self.summary(t0, t1)
        odom = t.since("odom", t0, t1)
        reasons = sorted({r.split(":", 1)[0] for s in t.since("state", t0, t1) for r in s["reasons"]})
        return dict(doc, expected_contact=True, ok=True, odom_m=round(m.path_length(odom), 3), reasons_seen=reasons)

    def _kill_in_motion(self, unit: str) -> dict:
        t = self.t
        t.relocate(*SOUTH)
        self.gesture_pilot(True)
        t0 = time.monotonic()
        t.spin(2.5, drive={GESTURE: (DEMO_SPEED, 0.0)})
        pid = t.kill9(unit)
        t_kill = time.monotonic()
        quiet = self.quiet_after(t_kill + 3.0, 5.0, GESTURE, (DEMO_SPEED, 0.0))
        t.wait_active([unit], 60.0)
        t_up = time.monotonic()
        quiet_up = self.quiet_after(t_up, 4.0, GESTURE, (DEMO_SPEED, 0.0))
        t1 = time.monotonic()
        t.spin(1.5, drive={GESTURE: (0.0, 0.0)})
        self.gesture_pilot(False)
        doc = self.summary(t0, t1, t_kill)
        mux = t.since("mux", t_kill, t1)
        last_mux = max((c["t"] for c in mux if c["v"] != 0.0), default=None)
        stopped = doc["after_trigger"]["stop_s"]
        moving = doc["after_trigger"]["v0"] is not None and doc["after_trigger"]["v0"] > 0.1
        doc.update(killed_pid=pid, restart_s=round(t_up - t_kill, 2),
                   last_nonzero_base_cmd_after_kill_s=None if last_mux is None else round(last_mux - t_kill, 3),
                   resumed_after_restart=quiet_up["moved_after"], moved_after_stop=quiet["moved_after"])
        doc["ok"] = (moving and not doc["contacts"] and stopped is not None and stopped <= 1.5
                     and not quiet_up["moved_after"] and not quiet["moved_after"])
        return doc

    def kill_executor(self) -> dict:
        """kill -9 исполнителя в движении: safety_gate по req_timeout (0,5 с) отдаёт ноль; после рестарта манёвр жеста,
        идущий со старта исполнителя, не продолжается (правило 9 исполнителя)."""
        return self._kill_in_motion("neurobot-executor")

    def kill_gate(self) -> dict:
        """kill -9 safety_gate в движении: /cmd_vel замолкает — twist_mux (0,5 с) и diff_drive_controller
        (cmd_vel_timeout 0,5 с) тормозят базу, ExecStopPost шлёт ноль; новый safety_gate не взведён, пока исполнитель
        не пришлёт ноль."""
        return self._kill_in_motion("neurobot-safety")

    def freeze_gate(self) -> dict:
        """Зависание safety_gate в движении (SIGSTOP): /cmd_vel молчит, процесс жив. Базу тормозит diff_drive_controller
        (cmd_vel_timeout 0,5 с); если база держит команду (hold), — только сторож systemd: нет WATCHDOG 3 с → SIGABRT →
        ExecStopPost шлёт ноль. Новый safety_gate не взведён, пока исполнитель не пришлёт ноль."""
        t = self.t
        t.relocate(*SOUTH)
        self.gesture_pilot(True)
        t0 = time.monotonic()
        t.spin(2.5, drive={GESTURE: (DEMO_SPEED, 0.0)})
        pid = t.main_pid("neurobot-safety")
        os.kill(pid, signal.SIGSTOP)
        t_stop = time.monotonic()
        t.spin(8.0, drive={GESTURE: (DEMO_SPEED, 0.0)}, until=lambda: t.main_pid("neurobot-safety") not in (0, pid))
        t_new = time.monotonic()
        t.wait_active(["neurobot-safety"], 60.0)
        quiet = self.quiet_after(time.monotonic(), 4.0, GESTURE, (DEMO_SPEED, 0.0))
        t1 = time.monotonic()
        t.spin(1.0, drive={GESTURE: (0.0, 0.0)})
        self.gesture_pilot(False)
        doc = self.summary(t0, t1, t_stop)
        doc.update(frozen_pid=pid, replaced_after_s=round(t_new - t_stop, 2),
                   resumed_after_restart=quiet["moved_after"])
        doc["ok"] = (doc["after_trigger"]["v0"] > 0.1 and doc["after_trigger"]["stop_s"] is not None
                     and not doc["contacts"] and not quiet["moved_after"])
        return doc

    def kill_agent_path(self) -> dict:
        """kill -9 rosbridge и MCP-сервера посреди толчка агента (robot_nudge 1 с, 0,15 м/с): исполнитель гасит вход
        агента по deadman (0,4 с); kill -9 PicoClaw в покое. Юниты поднимаются сами, doctor — ОК."""
        t, out = self.t, {}
        path = ("neurobot-rosbridge", "neurobot-mcp", "neurobot-picoclaw")
        for unit in path[:2]:
            t.relocate(*SOUTH)
            t0 = time.monotonic()
            call = self.mcp_async("robot_nudge", {"direction": "forward", "duration_ms": 1000, "speed": 0.15})
            t.spin(10.0, until=lambda: any(abs(r["v"]) > 0.05 for r in t.since("truth", t0)))
            t.spin(0.2)
            pid = t.kill9(unit)
            t_kill = time.monotonic()
            t.spin(4.0)
            call["thread"].join(30.0)
            up = self.agent_path_up(path)
            out[unit] = dict(self.summary(t0, time.monotonic(), t_kill), killed_pid=pid, path_up_s=up and
                             round(up - t_kill, 2), mcp_result={k: v for k, v in call.items() if k != "thread"},
                             restarted=self.restarts(path, t0))
        t0 = time.monotonic()
        pid = t.kill9("neurobot-picoclaw")
        t.spin(5.0)
        up = self.agent_path_up(path)
        out["neurobot-picoclaw"] = dict(self.summary(t0, time.monotonic()), killed_pid=pid,
                                        path_up_s=up and round(up - t0, 2), restarted=self.restarts(path, t0))
        t.spin(10.0)
        doctor = t.cli("doctor", timeout=120.0)
        ok = all(v["after_trigger"]["stop_s"] is not None and v["after_trigger"]["stop_s"] <= 1.0 and not v["contacts"]
                 and v["after_trigger"]["v0"] > 0.1 and v["path_up_s"] for k, v in out.items()
                 if k != "neurobot-picoclaw") and out["neurobot-picoclaw"]["path_m"] < 0.01
        return {"ok": ok and doctor["rc"] == 0, "units": out, "doctor_rc": doctor["rc"],
                "doctor_tail": doctor["out"].strip().splitlines()[-1:]}

    def agent_wall(self) -> dict:
        """Агент толчками MCP robot_nudge вперёд (1 с, 0,15 м/с) в стену в 0,7 м от корпуса: безопасность не пускает."""
        t = self.t
        t.relocate(*EAST_WALL)
        t0, calls = time.monotonic(), []
        for _ in range(12):
            c0 = time.monotonic()
            call = self.mcp_async("robot_nudge", {"direction": "forward", "duration_ms": 1000, "speed": 0.15})
            t.spin(15.0, until=lambda: not call["thread"].is_alive())
            t.spin(0.8)
            calls.append({"took_s": call.get("took_s"), "moved_m": round(m.path_length(t.since("truth", c0)), 3),
                          "error": call.get("error")})
            if len(calls) >= 3 and all(c["moved_m"] < 0.005 for c in calls[-2:]):
                break
        doc = self.summary(t0, time.monotonic())
        return dict(doc, ok=not doc["contacts"], nudges=calls)

    def n1_300s(self) -> dict:
        """N1: агент просит 300 с. (а) robot_nudge duration_ms=300000, speed=0,8 — сервер урезает до 1 с и 0,15 м/с
        (L3); (б) L3 обойдён (непатченный прокси, воркер без пределов): 0,8 м/с во вход агента без конца — до 60 с."""
        t = self.t
        t.relocate(*SOUTH)
        t0 = time.monotonic()
        call = self.mcp_async("robot_nudge", {"direction": "forward", "duration_ms": 300000, "speed": 0.8})
        t.spin(20.0, until=lambda: not call["thread"].is_alive())
        t.spin(2.0)
        a = dict(self.summary(t0, time.monotonic()), mcp_result={k: v for k, v in call.items() if k != "thread"})
        t.set_pose("person", 2.0, 2.0, 0.0)
        t.relocate(2.2, -1.0, 0.01, math.pi)
        t1 = time.monotonic()
        t.spin(60.0, drive={AGENT: (0.8, 0.0)},
               until=lambda: time.monotonic() - t1 > 5.0 and m.stopped_at(t.since("truth", t1), t1 + 3.0) is not None)
        t.spin(2.0, drive={AGENT: (0.8, 0.0)})
        b = self.summary(t1, time.monotonic())
        t.spin(1.0, drive={AGENT: (0.0, 0.0)})
        t.set_pose("person", 1.2, -0.9, 0.0)
        ok = a["path_m"] <= 0.2 and not a["contacts"] and not b["contacts"] and b["path_m"] <= 3.3
        return {"ok": ok, "mcp_300000ms": a, "l3_bypassed": b}

    def joystick(self) -> dict:
        """Лишний издатель /cmd_vel_joy (геймпад лаборатории): twist_mux ставит его выше /cmd_vel. (а) нули геймпада
        посреди манёвра — база стоит, safety_gate предупреждает; (б) геймпад 0,1 м/с в стену — L2 до twist_mux, и
        остановить его safety_gate не может (по построению)."""
        t = self.t
        t.relocate(*SOUTH)
        self.gesture_pilot(True)
        t0 = time.monotonic()
        t.spin(2.0, drive={GESTURE: (DEMO_SPEED, 0.0)})
        tj = time.monotonic()
        t.spin(3.0, drive={GESTURE: (DEMO_SPEED, 0.0), JOY: (0.0, 0.0)})
        tj1 = time.monotonic()
        t.unpub(JOY)
        t.spin(2.0, drive={GESTURE: (DEMO_SPEED, 0.0)})
        t.spin(1.0, drive={GESTURE: (0.0, 0.0)})
        self.gesture_pilot(False)
        warn = sorted({w.split(":", 1)[0] for s in t.since("state", tj, tj1) for w in s["warnings"]})
        held = [r for r in t.since("truth", tj + 1.0, tj1) if abs(r["v"]) > 0.02]
        a = dict(self.summary(t0, time.monotonic(), tj), warnings_during_joy=warn, base_moved_during_joy=bool(held))
        t.relocate(*EAST_WALL)
        t1 = time.monotonic()
        t.spin(12.0, drive={JOY: (0.1, 0.0)},
               until=lambda: bool(m.touched(t.since("truth", t1))) and time.monotonic() - t1 > 2.0)
        t.spin(1.0, drive={JOY: (0.0, 0.0)})
        t.unpub(JOY)
        b = dict(self.summary(t1, time.monotonic()), expected_contact=True)
        if (t.last("state") or {}).get("state") == "estop":
            b["human_release_rc"] = t.cli("estop", "release", "--reason", "проверка: геймпад убран",
                                          answer="release")["rc"]
        return {"ok": not a["base_moved_during_joy"] and "override" in warn and not a["contacts"],
                "joy_zero": a, "joy_into_wall": b}

    def latch(self) -> dict:
        """Защёлка посреди езды: ставит человек (CLI); агенту снять нечем — нет инструмента MCP, rosbridge не пускает в
        тему, узел ROS с by=agent — отказ safety_gate, CLI без терминала — отказ; kill -9 safety_gate — защёлка на
        месте; снимает только человек в терминале. Исполнитель всё это время просит вперёд (пилот входа агента): после
        снятия база стоит, пока запрос не станет нулевым, и едет с нового запроса."""
        t = self.t
        t.relocate(*SOUTH)
        stop = t.pilot(AGENT, (0.15, 0.0))
        t.spin(2.0)
        engage = t.cli("estop", "engage", "--reason", "проверка набора безопасности")
        t_engage = time.monotonic()
        t.spin(1.0)
        names = [x["name"] for x in t.mcp_tools()]
        status = t.mcp("robot_get_status", {})
        sent = json.dumps({"cmd": "release", "by": "human", "reason": "агент через rosbridge", "id": "ws1",
                           "t": time.time()})
        t.rosbridge_publish(CFG.get("NEUROBOT_ESTOP_TOPIC"), "std_msgs/msg/String", {"data": sent},
                            CFG.port("NEUROBOT_ROSBRIDGE_PORT"))
        t.spin(1.0)
        ws_arrived = any(e.get("id") == "ws1" for e in t.rec["estop"])
        denied0 = t.last("state")["counters"]["release_denied"]
        self.estop.publish(String(data=json.dumps({"cmd": "release", "by": "agent", "reason": "агент", "id": "ag1",
                                                   "t": time.time()})))
        t.spin(1.5)
        node_try = t.last("state")
        no_tty = t.cli("estop", "release", "--reason", "без терминала")
        pid = t.kill9("neurobot-safety")
        t.wait_active(["neurobot-safety"], 60.0)
        t.spin(3.0)
        after_kill = t.last("state")
        moved_latched = m.path_length(t.since("truth", t_engage + 0.8)) > 0.005
        human = t.cli("estop", "release", "--reason", "проверка: человек в терминале", answer="release")
        t_rel = time.monotonic()
        t.spin(3.0)
        released = t.last("state")
        moved_after_release = m.path_length(t.since("truth", t_rel)) > 0.005
        stop.set()
        t.spin(1.0)
        t_new = time.monotonic()
        t.spin(2.0, drive={AGENT: (0.15, 0.0)})
        moved_new = m.path_length(t.since("truth", t_new)) > 0.02
        t.spin(1.0, drive={AGENT: (0.0, 0.0)})
        rosbridge_log = "No match found" in t.journal_text(["neurobot-rosbridge"], time.time() - 60.0)
        latched = m.first(t.rec["state"], t_engage - 5.0, lambda st: st["state"] == "estop")
        doc = {"engage_rc": engage["rc"],
               "stopped_after_latch": latched and m.braking(t.rec["truth"], latched["t"]),
               "moved_while_latched": moved_latched, "mcp_tools": names, "mcp_status_ok": not status.get("isError"),
               "rosbridge_release": {"arrived": ws_arrived, "refused_in_log": rosbridge_log},
               "agent_node_release": {"state": node_try["state"], "release_denied": node_try["counters"]
                                      ["release_denied"], "denied_before": denied0},
               "cli_without_tty": {"rc": no_tty["rc"], "out": no_tty["out"].strip()[-160:]},
               "after_kill9": {"killed_pid": pid, "state": after_kill["state"], "reasons": after_kill["reasons"][:1]},
               "human_release": {"rc": human["rc"], "state": released["state"], "reasons": released["reasons"]},
               "moved_after_release_with_old_request": moved_after_release, "moved_on_new_request": moved_new}
        doc["ok"] = (engage["rc"] == 0 and not moved_latched and not any("release" in n or "estop" in n for n in names)
                     and not ws_arrived and node_try["state"] == "estop" and node_try["counters"]["release_denied"]
                     > denied0 and no_tty["rc"] != 0 and after_kill["state"] == "estop" and human["rc"] == 0
                     and released["state"] != "estop" and not moved_after_release and moved_new)
        return doc

    # ---- миры step и ramp

    def step_edge(self) -> dict:
        """Мир step: площадка 0,15 м, край в 1,5 м по курсу (x = 0,5): лидар края не видит, глубина — пол ниже."""
        t = self.t
        t.relocate(-1.0, 0.0, 0.16, 0.0)
        self.gesture_pilot(True)
        t0, t1 = self.drive_until_stop(GESTURE, (DEMO_SPEED, 0.0))
        self.gesture_pilot(False)
        doc = self.summary(t0, t1)
        end = t.since("truth", t0, t1)[-1]
        edge = round(0.5 - (end["x"] + 0.195), 3)
        return dict(doc, front_edge_to_cliff_m=edge, fell=end["z"] < 0.1, ok=edge > 0.0 and end["z"] > 0.1)

    def ramp_drive(self) -> dict:
        """Мир ramp: с пола на пандус 12° (начало в x = −0,8, первые 0,2 м — под полом, порога нет)."""
        t = self.t
        t.relocate(-1.5, 0.0, 0.01, 0.0)
        self.gesture_pilot(True)
        t0, t1 = self.drive_until_stop(GESTURE, (DEMO_SPEED, 0.0))
        self.gesture_pilot(False)
        doc = self.summary(t0, t1)
        latch = [s for s in t.since("state", t0, t1) if s["state"] == "estop"]
        pitch = max((abs(r["pitch"]) for r in t.since("truth", t0, t1)), default=0.0)
        if latch:
            t.cli("estop", "release", "--reason", "проверка: человек после пандуса", answer="release")
        return dict(doc, latch=latch[0]["reasons"] if latch else None, max_pitch_deg=round(math.degrees(pitch), 1),
                    ok=not [c for c in doc["contacts"] if not c.startswith("ramp")])

    def slope_latch(self) -> dict:
        """Мир ramp: робот поставлен на склон 12° — защёлка tilt; агент и человек на склоне её не снимут."""
        t = self.t
        slope = math.radians(12.0)
        t.relocate(0.0, 0.0, 0.8 * math.tan(slope) + 0.003, 0.0, -slope, arm=False)
        t.spin(4.0)
        on_slope = t.last("state")
        self.estop.publish(String(data=json.dumps({"cmd": "release", "by": "agent", "id": "ag2", "t": time.time()})))
        t.spin(1.0)
        human_on_slope = t.cli("estop", "release", "--reason", "на склоне", answer="release")
        t.spin(1.0)
        still = t.last("state")
        t.relocate(-1.5, 0.0, 0.01, 0.0, arm=False)
        t.spin(3.0)
        human_flat = t.cli("estop", "release", "--reason", "проверка: на ровном", answer="release")
        t.spin(1.0)
        flat = t.last("state")
        doc = {"on_slope": {k: on_slope[k] for k in ("state", "reasons", "tilt_deg")},
               "agent_release_denied": still["counters"]["release_denied"], "human_on_slope_rc": human_on_slope["rc"],
               "state_after_releases_on_slope": still["state"], "human_flat_rc": human_flat["rc"],
               "state_flat": flat["state"]}
        doc["ok"] = (on_slope["state"] == "estop" and any(r.startswith("tilt") for r in on_slope["reasons"])
                     and still["state"] == "estop" and human_flat["rc"] == 0 and flat["state"] != "estop")
        return doc


LAB = ("start_poses", "wall", "low_box", "person", "blind_pole", "low_near", "stall_reverse", "kill_executor",
       "kill_gate", "freeze_gate", "kill_agent_path", "agent_wall", "n1_300s", "joystick", "latch")
OTHER = ("step_edge", "ramp_drive", "slope_latch")


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, help="каталог результатов (safety.json, logs/)")
    ap.add_argument("--label", default="", help="метка прогона в safety.json (before/after)")
    ap.add_argument("scenarios", nargs="*", choices=LAB + OTHER, metavar="сценарий")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    twin = Twin("neurobot_safety_suite")
    s = Safety(twin, out)
    path = out / "safety.json"
    results = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    try:
        for name in a.scenarios or LAB:
            log.info("сценарий %s", name)
            wall = time.time()
            key = "{}:{}".format(a.label, name) if a.label else name
            try:
                results[key] = dict(getattr(s, name)(), started=round(wall, 3))
            except Exception as e:  # noqa: BLE001 — сценарий не прошёл, остальные идут дальше
                log.exception("сценарий %s упал", name)
                results[key] = {"ok": False, "error": repr(e), "started": round(wall, 3)}
            twin.journal(UNITS, wall, str(out / "logs" / "{}.log".format(key.replace(":", "_"))))
            log.info("%s: %s", key, "ОК" if results[key]["ok"] else "НЕ ПРОШЁЛ")
            path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        twin.close()
    return 0 if all(r["ok"] for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
