#!/usr/bin/env python3
"""Набор безопасности (PLAN §13.4) на двойнике с пакетом neurobot целиком (в ВМ sim, root, после setup_twin.sh и
systemctl start neurobot.target): каждый сценарий — скрипт, итог — по истине Gazebo (/twin/truth).

    python3 safety.py --out DIR [--label L] [сценарий …]      по умолчанию — все сценарии мира lab по порядку

Мир lab: start_poses (позы сценариев: стоп на старте — ложный), wall (разгон в стену: манёвр жеста 0,5 м/с),
low_box (низкий ящик, лидар не видит), person (человек выходит на путь), blind_pole (столб в слепом секторе лидара),
low_near (низкий предмет вне поля камеры, вплотную: не видит никто — упор), stall_reverse (задом в стену: сзади
датчиков нет, упор), kill_executor и kill_gate (kill -9 в движении), freeze_gate (SIGSTOP safety_gate),
kill_agent_path (kill -9 rosbridge и MCP посреди толчка агента, PicoClaw в покое), agent_wall (агент толчками MCP в
стену: отказ blocked с причиной), n1_300s (N1: «300 с» через MCP и в обход L3), joystick (лишний издатель
/cmd_vel_joy: ноль и езда в стену), latch (защёлка: агент и узел ROS с by=human снять не могут, CLI без sudo — тоже).
Приёмка 17.10 (мир lab): driver_faults (kill -9 и SIGSTOP узлов /urg_node2, /camera/camera и rosbridge — юнит
поднимается сам), scan_dup (второй издатель /scan во время работы), stop_shock (всплеск /camera/imu на штатной
остановке: плавная против резкой; стоп агента), k6_latency (K6: stop через MCP → ноль, препятствие → ноль после
скана).
Повторная приёмка 18.10 (мир lab): scan_blind (NaN-сканы под именем драйвера, ослепший лидар — S3b).
Мир step: step_edge. Мир ramp: ramp_drive (въезд на пандус), slope_latch (поставлен на склон 12°). Мир hall:
chair_turn (подъезд к стулу и поворот на месте у ножки, находка 2: no_err — клетка глубины без поправки на
ошибку, как до правки, и after); dead_end (тупик у стены под углом и у стула: выход отъездом исполнителя).
Движение — с входов исполнителя, как на роботе: «манёвр жеста» — в /cmd_vel_gesture (распознавание на это время
остановлено: в тему пишет его DriveController), «агент» — в /cmd_vel_agent или инструментом MCP robot_nudge.
Итог — DIR/safety.json (--label — метка прогона: before/after); журналы юнитов — DIR/logs/<сценарий>.log. Числа —
ориентир двойника (Gazebo в общей ВМ), не результат робота; в зачёт истины — только образцы с rtf_ok.
"""
from __future__ import annotations

import argparse
import inspect
import json
import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, "/opt/neurobot/src")  # neurobot.safety пакета (Params, Shock) — тот же код, что у safety_gate

import metrics as m  # noqa: E402
from harness import CFG, UNITS, Twin  # noqa: E402
from rclpy.qos import qos_profile_sensor_data  # noqa: E402
from sensor_msgs.msg import Image, Imu, LaserScan  # noqa: E402
from std_msgs.msg import String  # noqa: E402

from neurobot.safety.config import Params  # noqa: E402
from neurobot.safety.monitors import Shock  # noqa: E402


def rclpy_node(name: str):
    """Ещё один узел ROS в процессе набора (подставной издатель со своим именем в графе)."""
    import rclpy
    return rclpy.create_node(name)

log = logging.getLogger("safety")
GESTURE, AGENT, JOY = "/cmd_vel_gesture", CFG.get("NEUROBOT_AGENT_TOPIC"), "/cmd_vel_joy"
DEMO_SPEED = 0.5              # м/с — DriveController демо (self.speed), предел манёвра perception v2
SOUTH = (-1.0, 1.0, 0.01, -math.pi / 2)   # мир lab: на юг 1,9 м свободно, проём северной стены за спиной
EAST_WALL = (1.6, 0.0, 0.01, 0.0)         # восточная стена в 0,9 м (от края корпуса — 0,705 м), проём сзади-слева
# позы сценариев прошлых прогонов safety (W2, neurobot_20261015_safety_fix): 3 из 6 — стоп lidar_nodata на старте
# мир hall, инцидент main_a1 (1605–1629 с): робот стоял у стула в (1,207; −1,315) с курсом 0,543, повернул направо
# до −0,922, проехал вперёд и встал в 9 мм от ножки (leg_1 в (1,39; −1,61)), затем повернул на месте влево и задел её
CHAIR_STOP = (1.284, -1.416, -0.922)
CHAIR_REPLAY = (1.207, -1.315, 0.01, 0.543)
# мир hall, повторная приёмка 18.10: к восточной стене (x ≈ 4,5) под углом ≈34° (S1) и в 8,5 см лицом к ней (S5)
WALL_OBLIQUE = (3.3, -0.6, 0.01, 0.6)
WALL_PLACED = (4.22, 1.0, 0.01, 0.0)
# мир hall, тупик A1 18.10 (a1v2, 1077–1079 с): робот стоял в (0,714; −1,298) курсом −0,11 в 0,22 м от ножки стула
# leg_2, проехал вперёд 0,15 м и встал в 0,117 м от неё — вперёд нельзя, поворот заденет
CHAIR_DEAD_END = (0.714, -1.298, -0.11)
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

    @staticmethod
    def tool_json(result) -> dict:
        """JSON-ответ инструмента MCP (text блоков content) или {}."""
        try:
            return json.loads("".join(c.get("text", "") for c in (result or {}).get("content", [])))
        except (ValueError, AttributeError, TypeError):
            return {}

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
            res = self.tool_json(call.get("result"))
            calls.append({"took_s": call.get("took_s"), "moved_m": round(m.path_length(t.since("truth", c0)), 3),
                          "error": call.get("error"), "ok": res.get("ok"), "status": res.get("status"),
                          "reasons": (res.get("reasons") or [])[:2]})
            if len(calls) >= 3 and all(c["moved_m"] < 0.005 for c in calls[-2:]):
                break
        doc = self.summary(t0, time.monotonic())
        # приёмка: где safety_gate не пустил, инструмент отвечает отказом с причиной, а не ok
        silent = [c for c in calls if c["moved_m"] < 0.005 and c["ok"] is not False]
        return dict(doc, ok=not doc["contacts"] and not silent, nudges=calls, ok_without_motion=len(silent))

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
        тему, узел ROS — отказ safety_gate и с by=agent, и с by=human (по теме не снимает никто), CLI без терминала и
        без sudo — отказ; robot_get_status показывает защёлку, robot_nudge отвечает estop; kill -9 safety_gate —
        защёлка на месте; снимает только человек в терминале через sudo (файл root). Исполнитель всё это время
        просит вперёд (пилот входа агента): после снятия база стоит, пока запрос не станет нулевым, и едет с нового
        запроса."""
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
        denied1 = node_try["counters"]["release_denied"]
        # узел ROS, подписавшийся «human», — тоже отказ: по теме не снимает никто (by самозаявлено)
        self.estop.publish(String(data=json.dumps({"cmd": "release", "by": "human", "reason": "узел ROS", "id": "hu1",
                                                   "t": time.time()})))
        t.spin(1.5)
        human_node = t.last("state")
        status = self.tool_json(t.mcp("robot_get_status", {}))
        nudge = self.tool_json(t.mcp("robot_nudge", {"direction": "forward", "duration_ms": 300, "speed": 0.1}))
        no_tty = t.cli("estop", "release", "--reason", "без терминала")
        not_root = t.cli_as(CFG.get("NEUROBOT_USER"), "estop", "release", "--reason", "без sudo", answer="release")
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
               "human_signed_node_release": {"state": human_node["state"], "release_denied":
                                             human_node["counters"]["release_denied"], "denied_before": denied1},
               "mcp_status_safety": status.get("safety"), "mcp_nudge": {k: nudge.get(k) for k in ("ok", "status")},
               "cli_without_tty": {"rc": no_tty["rc"], "out": no_tty["out"].strip()[-160:]},
               "cli_without_sudo": {"rc": not_root["rc"], "out": not_root["out"].strip()[-160:]},
               "after_kill9": {"killed_pid": pid, "state": after_kill["state"], "reasons": after_kill["reasons"][:1]},
               "human_release": {"rc": human["rc"], "state": released["state"], "reasons": released["reasons"]},
               "moved_after_release_with_old_request": moved_after_release, "moved_on_new_request": moved_new}
        doc["ok"] = (engage["rc"] == 0 and not moved_latched and not any("release" in n or "estop" in n for n in names)
                     and not ws_arrived and node_try["state"] == "estop" and node_try["counters"]["release_denied"]
                     > denied0 and human_node["state"] == "estop" and human_node["counters"]["release_denied"] > denied1
                     and (status.get("safety") or {}).get("estop") is True and nudge.get("status") == "estop"
                     and no_tty["rc"] != 0 and not_root["rc"] != 0 and after_kill["state"] == "estop"
                     and human["rc"] == 0
                     and released["state"] != "estop" and not moved_after_release and moved_new)
        return doc

    # ---- приёмка 17.10: драйверы, второй издатель /scan, плавная остановка, K6, поворот у ножки

    def _topic_clock(self, topics):
        """Подписки raw на темы: {тема: [моменты сообщений]} (кадры не разбираются)."""
        got = {tp: [] for tp in topics}
        types = {"/scan": LaserScan, "/camera/color/image_raw": Image}
        subs = [self.t.node.create_subscription(types[tp], tp, lambda _m, tp=tp: got[tp].append(time.monotonic()),
                                                qos_profile_sensor_data, raw=True) for tp in topics]
        return got, subs

    def driver_faults(self) -> dict:
        """kill -9 и SIGSTOP узлов драйверов /urg_node2 и /camera/camera и узла rosbridge_websocket: смерть узла гасит
        launch юнита (обёртка supervised.launch.py), зависание — ловит сторож (свежесть темы, ping rosbridge); юнит
        поднимается сам. TTR — до нового процесса и первого сообщения его темы (у rosbridge — до ответа WebSocket)."""
        from neurobot_deploy.probes import ProbeError, ws_handshake
        t, out = self.t, {}
        got, subs = self._topic_clock(["/scan", "/camera/color/image_raw"])
        cases = (("lidar", "neurobot-driver@lidar", "sensor_model.py", "/scan"),
                 ("camera", "neurobot-driver@camera", "sensor_model.py", "/camera/color/image_raw"),
                 ("rosbridge", "neurobot-rosbridge", "lib/rosbridge_server/rosbridge_websocket", None))
        for name, unit, pattern, topic in cases:
            for sig in (signal.SIGKILL, signal.SIGSTOP):
                t.wait_active([unit], 60.0)
                t.spin(3.0)
                pid = t.unit_pid(unit, pattern)
                t0 = time.monotonic()
                os.kill(pid, sig)

                def back():
                    try:
                        new = t.unit_pid(unit, pattern)
                    except (RuntimeError, OSError):
                        return False
                    if new == pid or not t.active(unit):
                        return False
                    if topic:
                        return any(x > t0 + 0.3 for x in got[topic])
                    try:
                        ws_handshake("127.0.0.1", CFG.port("NEUROBOT_ROSBRIDGE_PORT"), 1.0)
                        return True
                    except ProbeError:
                        return False
                ok = t.spin(40.0, until=back)
                if sig == signal.SIGSTOP:
                    try:
                        os.kill(pid, signal.SIGCONT)
                    except ProcessLookupError:
                        pass
                out["{}_{}".format("kill" if sig == signal.SIGKILL else "stop", name)] = {
                    "pid": pid, "recovered": ok, "ttr_s": round(time.monotonic() - t0, 2) if ok else None,
                    "restarts": self.restarts([unit], t0)}
        for sub in subs:
            t.node.destroy_subscription(sub)
        t.spin(15.0)
        doctor = t.cli("doctor", timeout=120.0)
        watchdog = [ln.split("neurobot-watchdog: ", 1)[-1][:160] for ln in t.journal_text(
            ["neurobot-watchdog"], time.time() - 600).splitlines() if "restart " in ln]
        return {"ok": all(v["recovered"] for v in out.values()) and doctor["rc"] == 0, "cases": out,
                "doctor_rc": doctor["rc"], "watchdog_restarts": watchdog[-8:]}

    def _scan_until(self, pred, seconds: float, drive) -> Optional[float]:
        """Ехать drive, пока pred(safety_state) не станет истинным; момент или None."""
        t = self.t
        ok = t.spin(seconds, drive=drive, until=lambda: pred(t.last("state") or {}))
        return time.monotonic() if ok else None

    def scan_dup(self) -> dict:
        """Второй издатель /scan во время работы (приёмка 17.10, P3: подмена «всё свободно» не проверялась). (а) Чужой
        узел (не драйвер) публикует скан «всё свободно» (+inf) — стоп сразу (scan_foreign); (б) он же, пока узел
        /urg_node2 завис (SIGSTOP): «свежий» скан идёт только от подмены — стоп; (в) второй узел с именем драйвера
        (рестарт, след убитого в графе DDS) — предупреждение, робот едет, стоп через scan_dup_s (30 с); издатель ушёл —
        движение только с нулевого запроса. Пилот просит вперёд 0,04 м/с: за ≈40 с — не дальше 1,6 м из 1,9 м
        свободных. Касаний нет."""
        t = self.t
        t.relocate(*SOUTH)
        real = []
        sub = t.node.create_subscription(LaserScan, "/scan", lambda msg: real.append(msg), qos_profile_sensor_data)
        t.spin(1.0)
        t.node.destroy_subscription(sub)
        ghost_node = rclpy_node("scan_ghost")
        ghost = ghost_node.create_publisher(LaserScan, "/scan", qos_profile_sensor_data)
        fake = real[-1]
        fake.ranges = [math.inf] * len(fake.ranges)
        stop_ghost = threading.Event()

        def publish():
            while not stop_ghost.wait(0.1):
                fake.header.stamp = t.node.get_clock().now().to_msg()
                ghost.publish(fake)
        threading.Thread(target=publish, daemon=True).start()
        drive = {AGENT: (0.04, 0.0)}
        t0 = time.monotonic()
        foreign = self._scan_until(lambda s: any(r.startswith("scan_foreign") for r in s.get("reasons", [])), 5.0,
                                   drive)
        pid = t.unit_pid("neurobot-driver@lidar", "sensor_model.py")
        os.kill(pid, signal.SIGSTOP)
        t1 = time.monotonic()
        t.spin(3.0, drive=drive)
        frozen = t.since("state", t1 + 1.0)
        os.kill(pid, signal.SIGCONT)
        stop_ghost.set()
        ghost_node.destroy_node()
        t.spin(3.0, drive={AGENT: (0.0, 0.0)})
        twin_node = rclpy_node("urg_node2")
        twin_node.create_publisher(LaserScan, "/scan", qos_profile_sensor_data)
        t2 = time.monotonic()
        warned = self._scan_until(lambda s: any(w.startswith("scan_publishers") for w in s.get("warnings", [])), 5.0,
                                  drive)
        t.spin(4.0, drive=drive)
        moved = m.path_length(t.since("truth", t2, t2 + 5.0)) if warned else 0.0
        blocked = self._scan_until(lambda s: any(r.startswith("scan_publishers") for r in s.get("reasons", [])), 40.0,
                                   drive)
        twin_node.destroy_node()
        cleared = self._scan_until(lambda s: not any(r.startswith("scan_") for r in s.get("reasons", [])), 10.0, drive)
        t.spin(1.5, drive=drive)
        resumed_alone = cleared is not None and m.path_length(t.since("truth", cleared + 0.5)) > 0.005
        t.spin(1.0, drive={AGENT: (0.0, 0.0)})
        t3 = time.monotonic()
        t.spin(2.0, drive=drive)
        moves_again = m.path_length(t.since("truth", t3 + 0.5)) > 0.005
        t.spin(1.0, drive={AGENT: (0.0, 0.0)})
        doc = dict(self.summary(t0, time.monotonic()),
                   foreign_stop_s=foreign and round(foreign - t0, 2),
                   frozen_driver={"states": sorted({s["state"] for s in frozen}),
                                  "moved_m": round(m.path_length(t.since("truth", t1 + 1.0, t1 + 3.0)), 3)},
                   same_name={"warning_s": warned and round(warned - t2, 2), "moved_while_warning_m": round(moved, 3),
                              "stop_s": blocked and round(blocked - t2, 2)},
                   resumed_without_zero=resumed_alone, moves_after_zero_request=moves_again)
        doc["ok"] = bool(foreign is not None and frozen and all(s["state"] == "blocked" for s in frozen)
                         and doc["frozen_driver"]["moved_m"] < 0.005 and warned is not None and moved > 0.005
                         and blocked is not None and blocked - t2 >= 20.0 and not resumed_alone and moves_again
                         and not doc["contacts"])
        return doc

    def scan_blind(self) -> dict:
        """Повторная приёмка 18.10, S3b: правило «вне поля глубины — как слепой сектор» пропускало скан целиком из NaN
        как slow, и робот ехал на одной глубине. (а) Узел с именем драйвера urg_node2 шлёт NaN-сканы 20 Гц (между
        настоящими 10 Гц), пока робот едет 0,1 м/с: стоп сразу (scan_publishers — при двух издателях идут пустые
        сканы, это не след убитого драйвера), а не через scan_dup_s; издатель ушёл — движение только с нулевого
        запроса. (б) Ослепший лидар: подмена 10 Гц запущена, затем юнит драйвера остановлен — NaN-сканы шлёт
        единственный издатель с его именем: lidar_fault (сканы идут — не stale_scan), робот не едет; драйвер вернулся —
        едет с нового запроса. Сторож на время (б) остановлен: юнит драйвера стоит намеренно, а свежий подменный /scan
        скрыл бы от сторожа и зависший драйвер (README deploy). Подмена — scan_spoof.py отдельным процессом."""
        t = self.t
        t.relocate(*SOUTH)

        def spoof(hz: float) -> subprocess.Popen:
            """Подмена отдельным процессом (свой участник DDS, как чужая программа на роботе) с именем драйвера."""
            with open(self.out / "logs" / "scan_spoof.log", "a", encoding="utf-8") as log_f:
                return subprocess.Popen([sys.executable, os.path.join(HERE, "scan_spoof.py"), "urg_node2", str(hz)],
                                        stdout=log_f, stderr=log_f)

        def unspoof(proc: subprocess.Popen) -> None:
            proc.send_signal(signal.SIGINT)
            proc.wait(10)

        def codes(t0: float) -> list:
            return sorted({r.split(":", 1)[0] for s in t.since("state", t0) for r in s["reasons"]})
        drive, zero = {AGENT: (0.1, 0.0)}, {AGENT: (0.0, 0.0)}
        t.spin(2.0, drive=drive)
        proc = spoof(20.0)
        t0 = time.monotonic()
        blocked = self._scan_until(lambda s: any(r.startswith("scan_publishers") for r in s.get("reasons", [])), 10.0,
                                   drive)
        t.spin(2.0, drive=drive)
        a = dict(self.summary(t0, time.monotonic(), t0), stop_s=blocked and round(blocked - t0, 2), reasons=codes(t0),
                 moved_after_stop_m=blocked and round(m.path_length(t.since("truth", blocked + 1.0)), 3))
        unspoof(proc)
        cleared = self._scan_until(lambda s: not any(r.startswith("scan_") for r in s.get("reasons", [])), 15.0, drive)
        t.spin(1.5, drive=drive)
        a["resumed_without_zero"] = cleared is not None and m.path_length(t.since("truth", cleared + 0.5)) > 0.005
        t.spin(1.0, drive=zero)
        t1 = time.monotonic()
        t.spin(1.5, drive=drive)
        a["moves_after_zero_request"] = m.path_length(t.since("truth", t1 + 0.5)) > 0.005
        t.spin(1.0, drive=zero)

        watchdog = t.active("neurobot-watchdog")
        if watchdog:
            t.systemctl("stop", "neurobot-watchdog")
        proc = spoof(10.0)
        t.spin(2.0, drive=zero)  # подмена взяла метаданные настоящего скана; при двух издателях — стоп сразу
        t.systemctl("stop", "neurobot-driver@lidar")
        t.spin(3.0, drive=zero)
        t2 = time.monotonic()
        t.spin(3.0, drive=drive)
        b = {"reasons": codes(t2), "states": sorted({s["state"] for s in t.since("state", t2)}),
             "moved_m": round(m.path_length(t.since("truth", t2)), 3)}
        unspoof(proc)
        t.systemctl("start", "neurobot-driver@lidar")
        t.wait_active(["neurobot-driver@lidar"], 60.0)
        if watchdog:
            t.systemctl("start", "neurobot-watchdog")
        back = self._scan_until(lambda s: s.get("state") in ("ok", "slow"), 30.0, zero)
        t3 = time.monotonic()
        t.spin(1.5, drive=drive)
        b.update(back_s=back and round(back - t2, 2), moves_after=m.path_length(t.since("truth", t3 + 0.5)) > 0.005)
        t.spin(1.0, drive=zero)
        doc = {"same_name_nan": a, "blind_lidar": b, "contacts": a["contacts"] + m.touched(t.since("truth", t2))}
        doc["ok"] = bool(blocked is not None and blocked - t0 <= 3.0 and a["moved_after_stop_m"] < 0.005
                         and not a["resumed_without_zero"] and a["moves_after_zero_request"]
                         and "lidar_fault" in b["reasons"] and "stale_scan" not in b["reasons"]
                         and b["moved_m"] < 0.005 and back is not None and b["moves_after"] and not doc["contacts"])
        return doc

    def stop_shock(self, runs: int = 4, modes: str = "hard,soft,agent") -> dict:
        """Находка 3: штатная остановка (конец манёвра жеста, 0,15 м/с) давала всплеск /camera/imu 6,1 м/с² > shock 6 и
        ложную защёлку. Плавная остановка (S-кривая stop_decel 0,5 м/с², рывок 2 м/с³) против прежней резкой
        (stop_decel:=0.0) и стоп агента (robot_stop через MCP — ноль сразу, решение 1а; манёвр жеста при этом ещё
        просит ехать): наибольшее отклонение ускорения от медленного среднего — так его считает safety_gate
        (monitors.Shock), за окно остановки; защёлки shock. Ждём: ни одной защёлки, все всплески ниже shock_mps2,
        медиана у плавной не выше, чем у резкой (всплеск двойника двухуровневый — ≈2,2 и ≈3,2 м/с²)."""
        t = self.t
        imu = []
        sub = t.node.create_subscription(Imu, "/camera/imu", lambda msg: imu.append((
            time.monotonic(), (msg.linear_acceleration.x, msg.linear_acceleration.y, msg.linear_acceleration.z))),
            qos_profile_sensor_data)
        out = {}
        profiles = {"hard": "-p stop_decel:=0.0", "soft": "", "agent": ""}
        for mode in modes.split(","):
            t.safety_args(profiles[mode], arm=False)
            rows = []
            for _ in range(runs):
                t.relocate(*SOUTH)
                self.gesture_pilot(True)
                t.spin(3.0, drive={GESTURE: (DEMO_SPEED, 0.0)})
                v0 = (t.last("truth") or {}).get("v")
                t_zero = time.monotonic()
                if mode == "agent":
                    call = self.mcp_async("robot_stop", {"reason": "stop_shock"})
                    t.spin(2.5, drive={GESTURE: (DEMO_SPEED, 0.0)})
                    call["thread"].join(10.0)
                else:
                    t.spin(2.5, drive={GESTURE: (0.0, 0.0)})
                self.gesture_pilot(False)
                shock, peak = Shock(), 0.0
                for ts, a in imu:
                    dev = shock.feed(ts, a)
                    if t_zero - 0.2 <= ts <= t_zero + 2.0:
                        peak = max(peak, dev)
                stop = m.braking(t.since("truth", t_zero - 0.1), t_zero)
                latched = [r for s_ in t.since("state", t_zero) if s_["state"] == "estop" for r in s_["reasons"]]
                cmd_zero = next((c["t"] for c in t.since("cmd", t_zero) if c["v"] == 0.0 and c["w"] == 0.0), None)
                rows.append({"v0": None if v0 is None else round(v0, 3), "peak_mps2": round(peak, 2),
                             "stop_s": stop["stop_s"], "coast_m": stop["coast_m"], "latch": latched[:1],
                             "cmd_zero_s": cmd_zero and round(cmd_zero - t_zero, 3),
                             "moved_after_m": round(m.path_length(t.since("truth", t_zero + 1.5)), 3)})
                if latched:
                    t.cli("estop", "release", "--reason", "проверка: штатная остановка", answer="release")
            out[mode] = {"runs": rows, "peak_mps2": m.quantiles([r["peak_mps2"] for r in rows]),
                         "latches": sum(1 for r in rows if r["latch"])}
        t.safety_args("")
        t.node.destroy_subscription(sub)
        # всплеск двойника двухуровневый (≈2,2 и ≈3,2 м/с² — контакт роликов, калибровка 18.10): сравниваются медианы
        limit = Params().shock_mps2
        done = [k for k in ("hard", "soft", "agent") if k in out]
        out["ok"] = (all(out[k]["latches"] == 0 for k in done)
                     and max(out[k]["peak_mps2"]["max"] or 0 for k in done) < limit
                     and ("soft" not in out or "hard" not in out
                          or (out["soft"]["peak_mps2"]["p50"] or 0) <= (out["hard"]["peak_mps2"]["p50"] or 0))
                     and ("agent" not in out or all(r["cmd_zero_s"] is not None and r["cmd_zero_s"] <= 0.1
                                                    and r["moved_after_m"] < 0.005 for r in out["agent"]["runs"])))
        return out

    def k6_latency(self, runs: int = 10) -> dict:
        """K6 (PLAN §1): stop через MCP → ноль в /cmd_vel ≤ 100 мс (p95) — от вызова robot_stop (HTTP) до начала
        торможения и до нуля (стоп агента — сразу, решение 1а 19.10; до него — плавно, ноль через ≈0,5 с);
        препятствие → ноль ≤ 150 мс после скана — столб переставлен вплотную перед едущим роботом в видимой
        лидару части коридора (+13°), от приёма первого скана со столбом до первого нулевого /cmd_vel после постановки
        (отрицательное — safety_gate остановил раньше, чем скан дошёл до этого узла). Метки — часы этого узла (приём),
        то есть с доставкой DDS."""
        t = self.t
        scans = []
        sub = t.node.create_subscription(LaserScan, "/scan", lambda msg: scans.append((time.monotonic(), msg)),
                                         qos_profile_sensor_data)
        mcp, scan = [], []
        for k in range(runs):
            if k % 4 == 0:
                t.relocate(*SOUTH)
            self.gesture_pilot(True)
            t.spin(2.0, drive={GESTURE: (DEMO_SPEED, 0.0)})
            v_ref = (t.last("cmd") or {}).get("v", 0.0)
            call = self.mcp_async("robot_stop", {"reason": "k6"})
            t_call = time.monotonic()
            t.spin(3.0, drive={GESTURE: (DEMO_SPEED, 0.0)})
            call["thread"].join(10.0)
            t.spin(1.0, drive={GESTURE: (0.0, 0.0)})
            self.gesture_pilot(False)
            cmds = t.since("cmd", t_call)
            brake = m.first_below(cmds, t_call, v_ref)
            zero = next((c["t"] for c in cmds if c["v"] == 0.0 and c["w"] == 0.0), None)
            mcp.append({"v_ref": round(v_ref, 3), "brake_s": brake and round(brake - t_call, 3),
                        "zero_s": zero and round(zero - t_call, 3), "took_s": call.get("took_s")})
        t.create_pole("k6_pole", 5.0, 5.0, 0.025, 1.0)
        t.known("k6_pole")
        for k in range(runs):
            t.relocate(*SOUTH)
            stop = t.pilot(AGENT, (0.08, 0.0))
            t.spin(2.0)
            r = t.last("truth")
            # база: столб в (0,24; 0,06) — пеленг +13° (вне слепого сектора), 2,7 см от края корпуса
            bx, by = 0.24, 0.06
            wx = r["x"] + bx * math.cos(r["yaw"]) - by * math.sin(r["yaw"])
            wy = r["y"] + bx * math.sin(r["yaw"]) + by * math.cos(r["yaw"])
            t_set = time.monotonic()
            t.set_pose("k6_pole", wx, wy, 0.5)
            t.spin(2.0)
            stop.set()
            t.spin(0.5)
            seen = next((ts for ts, msg in scans if ts >= t_set and self._pole_in_scan(msg, 13.0, 0.35)), None)
            # первый ноль после постановки столба; раньше скана у этого узла — safety_gate получил скан раньше
            zero = next((c["t"] for c in t.since("cmd", t_set) if c["v"] == 0.0 and c["w"] == 0.0), None)
            scan.append({"seen_after_set_s": seen and round(seen - t_set, 3),
                         "zero_s": None if zero is None or seen is None else round(zero - seen, 3),
                         "stop_reasons": m.stop_reason(t.since("state", t_set), zero),
                         "contacts": m.touched(t.since("truth", t_set))})
            t.set_pose("k6_pole", 5.0, 5.0, 0.5)
        t.remove("k6_pole")
        t.node.destroy_subscription(sub)
        doc = {"mcp_stop": {"runs": mcp,
                            "brake_s": m.quantiles([x["brake_s"] for x in mcp if x["brake_s"] is not None]),
                            "zero_s": m.quantiles([x["zero_s"] for x in mcp if x["zero_s"] is not None])},
               "scan_stop": {"runs": scan, "zero_s": m.quantiles([x["zero_s"] for x in scan
                                                                   if x["zero_s"] is not None])}}
        # K6: stop через MCP — ноль ≤ 100 мс (стоп агента, решение 1а); препятствие — ноль ≤ 150 мс после скана
        # (отрицательное — safety_gate раньше)
        doc["ok"] = (doc["mcp_stop"]["zero_s"]["n"] == runs and doc["mcp_stop"]["zero_s"]["p95"] <= 0.1
                     and doc["scan_stop"]["zero_s"]["n"] == runs and doc["scan_stop"]["zero_s"]["p95"] <= 0.15
                     and not any(x["contacts"] for x in scan))
        return doc

    @staticmethod
    def _pole_in_scan(msg, deg: float, max_m: float) -> bool:
        """В скане есть отражение ближе max_m в пределах ±3° от пеленга deg (кадр лидара ≈ кадр базы)."""
        for i, r in enumerate(msg.ranges):
            a = math.degrees(msg.angle_min + i * msg.angle_increment)
            if abs(a - deg) <= 3.0 and math.isfinite(r) and r < max_m:
                return True
        return False

    def chair_turn(self) -> dict:
        """Мир hall, находка 2 (main_a1, 1605–1629 с): у стула safety_gate считал до ножки 0,044 м, по истине — 0,009, и
        поворот на месте задел её. Случаи: replay — как в инциденте: робот стоит в 0,1 м от стула (поставлен, памяти
        глубины нет), поворачивает направо к стулу до курса −0,922 и едет вперёд 0,15 м/с до стопа; far — подъезд к
        стулу с 0,6 м (курс инцидента и ±0,08 рад). После стопа — поворот на месте влево 0,8 рад/с 4 с (как turn180).
        Дважды: no_err — depth_err_m:=0 (клетка глубины без поправки на ошибку, как до правки), after — по умолчанию.
        Ждём в after: касаний нет ни на подъезде, ни на повороте (поворот запрещается, если клетка глубины вместе с
        ошибкой ближе turn_margin)."""
        out = {}
        for mode, extra in (("no_err", "-p depth_err_m:=0.0"), ("after", "")):
            self.t.safety_args(extra, arm=False)
            out[mode] = [self._chair_case(case) for case in ("replay", 0.0, -0.08, 0.08)]
        self.t.safety_args("")
        out["ok"] = not any(r["approach_contacts"] or r["turn_contacts"] for r in out["after"])
        return out

    def _chair_case(self, case) -> dict:
        t = self.t
        if case == "replay":
            t.relocate(*CHAIR_REPLAY)
            t.spin(6.0, drive={AGENT: (0.0, -0.8)}, until=lambda: abs(math.remainder(
                (t.last("truth") or {}).get("yaw", 0.0) - CHAIR_STOP[2], 2 * math.pi)) < 0.04)
            t.spin(0.8, drive={AGENT: (0.0, 0.0)})
        else:
            yaw = CHAIR_STOP[2] + case
            t.relocate(CHAIR_STOP[0] - 0.6 * math.cos(yaw), CHAIR_STOP[1] - 0.6 * math.sin(yaw), 0.01, yaw)
        t0 = time.monotonic()
        t.spin(14.0, drive={AGENT: (0.15, 0.0)}, until=lambda: time.monotonic() - t0 > 2.0 and m.stopped_at(
            t.since("truth", t0), t0 + 1.0) is not None)
        t.spin(1.0, drive={AGENT: (0.0, 0.0)})
        before_turn = t.last("state") or {}
        t1 = time.monotonic()
        t.spin(4.0, drive={AGENT: (0.0, 0.8)})
        t.spin(1.0, drive={AGENT: (0.0, 0.0)})
        truth = t.since("truth", t1)
        turned = sum(abs(math.remainder(b["yaw"] - a["yaw"], 2 * math.pi)) for a, b in zip(truth, truth[1:]))
        doc = {"case": case, "approach_m": round(m.path_length(t.since("truth", t0, t1)), 3),
               "stop_reasons": [r.split(":", 1)[0] for r in before_turn.get("reasons") or []],
               "gate_clearance_m": before_turn.get("min_clearance_m"),
               "truth_clearance_m": m.min_clearance(t.since("truth", t1 - 0.5, t1)),
               "approach_contacts": m.touched(t.since("truth", t0, t1)), "turn_rad": round(turned, 2),
               "turn_reasons": sorted({r.split(":", 1)[0] for s_ in t.since("state", t1) for r in s_["reasons"]}),
               "turn_contacts": m.touched(truth), "turn_min_clearance_m": m.min_clearance(truth)}
        if (t.last("state") or {}).get("state") == "estop":
            t.cli("estop", "release", "--reason", "проверка: стул", answer="release")
        return doc

    def dead_end(self) -> dict:
        """Мир hall, повторная приёмка 18.10: тупик — вперёд нельзя, поворот на месте заденет (touch), назад можно, а
        ни один жест не ведёт назад (A1 18.10: 221 с у ножки стула, 9 жестов без реакции). Исполнитель через
        escape_after_s (3 с) без заявок сам отъезжает назад по своему следу (/odom), до 0,1 м (README исполнителя,
        правило 10). Случаи (подъезд — манёвр жеста 0,5 м/с, как «come»): chair_a1v2 — к ножке стула курсом тупика
        A1 18.10 с 0,6 м до места, где он стоял (вперёд нельзя по глубине, поворот заденет); chair_far — к стулу с
        0,6 м курсом инцидента main_a1 (как chair_turn, far); wall_oblique — к восточной стене под углом ≈34° (S1);
        wall_placed — робот поставлен в 8,5 см лицом к стене (S5): следа нет, исполнитель не отъезжает — выводит агент
        толчками robot_nudge backward (до трёх, RUNBOOK §4.10). После остановки — 8 с покоя (распознавание
        остановлено, жестов нет), затем поворот на месте 0,8 рад/с 3 с, как turn180. Ждём: касаний нет; где при
        подъезде был тупик — отъезд исполнителя (escape → done), после него поворот разрешён и выполнен; у
        поставленного отъезда нет, после толчков агента поворот выполнен; тупик с отъездом — хотя бы в одном случае."""
        self.gesture_pilot(True)
        try:
            cases = {case: self._dead_end_case(case)
                     for case in ("chair_a1v2", "chair_far", "wall_oblique", "wall_placed")}
        finally:
            self.gesture_pilot(False)
        escaped = [c for c, r in cases.items() if r["dead_end"] and r["escaped"]]
        return dict(cases, escaped_cases=escaped, ok=bool(escaped) and all(r["ok"] for r in cases.values()))

    @staticmethod
    def _dead_end_state(s: dict) -> bool:
        limits = s.get("limits") or {}
        return (s.get("state") == "blocked" and not limits.get("v_max") and not limits.get("w_max")
                and (limits.get("v_rev") or 0.0) > 0.0 and any(r.startswith("touch") for r in s.get("reasons") or []))

    def _dead_end_case(self, case: str) -> dict:
        t = self.t
        t0 = time.monotonic()
        if case == "wall_oblique":
            t.relocate(*WALL_OBLIQUE)
            self.drive_until_stop(GESTURE, (DEMO_SPEED, 0.0))
        elif case in ("chair_a1v2", "chair_far"):
            x, y, yaw = CHAIR_DEAD_END if case == "chair_a1v2" else CHAIR_STOP
            t.relocate(x - 0.6 * math.cos(yaw), y - 0.6 * math.sin(yaw), 0.01, yaw)
            self.drive_until_stop(GESTURE, (DEMO_SPEED, 0.0))
        else:
            t.relocate(*WALL_PLACED, arm=False)
            t.spin(1.0)
        t1 = time.monotonic()
        t.spin(8.0)
        stop = next((s for s in t.since("state", t1 - 1.0, t1 + 3.0) if self._dead_end_state(s)), t.last("state"))
        events = [[e.get("type"), e.get("status")] for e in t.since("events", t1)
                  if e.get("type") == "escape" or e.get("task") == "escape"]
        doc = {"dead_end": self._dead_end_state(stop or {}),
               "stop_reasons": sorted({r.split(":", 1)[0] for r in (stop or {}).get("reasons") or []}),
               "gate_clearance_m": (stop or {}).get("min_clearance_m"), "escape_events": events,
               "escaped": ["escape", "start"] in events and ["task_end", "done"] in events,
               "back_m": round(m.path_length(t.since("truth", t1)), 3)}
        if case == "wall_placed":
            nudges = []
            for _ in range(3):
                res = self.tool_json(t.mcp("robot_nudge", {"direction": "backward", "duration_ms": 1000,
                                                           "speed": 0.05}))
                t.spin(1.5)
                nudges.append(res.get("ok"))
                if ((t.last("state") or {}).get("limits") or {}).get("w_max"):
                    break
            doc["agent_nudges"] = {"ok": nudges, "back_m": round(m.path_length(t.since("truth", t1 + 8.0)), 3)}
        after = t.last("state") or {}
        t2 = time.monotonic()
        t.spin(3.0, drive={GESTURE: (0.0, 0.8)})
        t.spin(1.0, drive={GESTURE: (0.0, 0.0)})
        turn = t.since("truth", t2)
        turned = sum(abs(math.remainder(b["yaw"] - a["yaw"], 2 * math.pi)) for a, b in zip(turn, turn[1:]))
        doc.update(w_max_after=(after.get("limits") or {}).get("w_max"),
                   after_reasons=sorted({r.split(":", 1)[0] for r in after.get("reasons") or []}),
                   turn_rad=round(turned, 2), contacts=m.touched(t.since("truth", t0)),
                   min_clearance_m=m.min_clearance(t.since("truth", t0)))
        turn_ok = (doc["w_max_after"] or 0.0) > 0.0 and turned > 0.5
        if case == "wall_placed":
            doc["ok"] = not doc["contacts"] and not events and all(doc["agent_nudges"]["ok"]) and turn_ok
        else:
            doc["ok"] = not doc["contacts"] and (not doc["dead_end"] or (doc["escaped"] and turn_ok))
        if (t.last("state") or {}).get("state") == "estop":
            doc["human_release_rc"] = t.cli("estop", "release", "--reason", "проверка: тупик", answer="release")["rc"]
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
ACCEPT = ("driver_faults", "scan_dup", "stop_shock", "k6_latency", "scan_blind")
OTHER = ("step_edge", "ramp_drive", "slope_latch", "chair_turn", "dead_end")


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, help="каталог результатов (safety.json, logs/)")
    ap.add_argument("--label", default="", help="метка прогона в safety.json (before/after)")
    ap.add_argument("--runs", type=int, help="замеров в сценариях с параметром runs (stop_shock, k6_latency)")
    ap.add_argument("--modes", help="режимы stop_shock через запятую: hard, soft, agent (по умолчанию все)")
    ap.add_argument("scenarios", nargs="*", choices=LAB + ACCEPT + OTHER, metavar="сценарий")
    a = ap.parse_args()
    given = {k: v for k, v in (("runs", a.runs), ("modes", a.modes)) if v is not None}
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
                fn = getattr(s, name)
                kw = {k: v for k, v in given.items() if k in inspect.signature(fn).parameters}
                results[key] = dict(fn(**kw), started=round(wall, 3), **({"args": kw} if kw else {}))
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
