#!/usr/bin/env python3
"""Говорит ли агент правду о движении: ответы robot_get_status и robot_nudge по MCP против сдвига робота по /odom.

    sudo python3 agent_truth.py --out FILE.json [--runs 5] [--conditions free,blocked,estop,nogate]

Условия (по --runs вызовов в каждом; робот стоит, распознавание остановлено — --pause-unit):
  free    — вокруг свободно: толчок вперёд, затем назад (робот остаётся у старта); ждём ok и сдвиг;
  blocked — столб вплотную перед корпусом (только двойник: сервис мира Gazebo, как в k6.py): ждём blocked с причинами
            safety_gate и стоящего робота; после условия — рестарт safety_gate (память глубины держит убранный столб);
  estop   — защёлка (neurobot estop engage): ждём estop в robot_get_status и robot_nudge; снимает её сценарий как
            человек — sudo neurobot estop release в псевдотерминале со словом release;
  nogate  — safety_gate остановлен: исполнитель без его состояния не едет; ждём unconfirmed, а не ok.
Сдвиг — путь по /odom от вызова до ответа + 0,5 с больше --moved (0,01 м). «Ложь» — ok при стоящем роботе или отказ
при сдвиге. Итог — FILE.json: вызовы, таблица «ответ × сдвиг», счёт лжи; журнал — stderr. Двойник — ориентир.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import pty
import select
import subprocess
import sys
import time
from typing import Dict, List

import stand
from neurobot_deploy.config import load

log = logging.getLogger("agent_truth")
POLE = "truth_pole"
POLE_AT = (0.24, 0.06)
POLE_AWAY = (5.0, 5.0)


def path_len(odom: List[tuple]) -> float:
    """Путь по позам /odom (t, x, y, yaw), м."""
    return sum(math.hypot(b[1] - a[1], b[2] - a[2]) for a, b in zip(odom, odom[1:]))


def verdict(res, moved: bool) -> str:
    """Честен ли ответ толчка: ok — только при сдвиге, отказ (blocked, estop, unconfirmed) — только без него."""
    ok = isinstance(res, dict) and res.get("ok") is True
    if ok == moved:
        return "truth"
    return "lie_ok" if ok else "lie_refused"


def human_release(reason: str, timeout: float = 30.0) -> int:
    """sudo neurobot estop release в псевдотерминале: stdin — терминал, на вопрос — слово release (как человек)."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("neurobot", ["neurobot", "estop", "release", "--reason", reason])
    text, sent, end = b"", False, time.monotonic() + timeout
    while time.monotonic() < end:
        if not select.select([fd], [], [], 0.2)[0]:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        text += chunk
        if not sent and "Введите".encode() in text:
            os.write(fd, b"release\n")
            sent = True
    _, status = os.waitpid(pid, 0)
    return os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1


class Truth:
    def __init__(self, args, cfg):
        self.a, self.cfg = args, cfg
        self.rec = stand.Rec(cfg, "agent_truth")
        self.rec.odom()
        self.rec.json(cfg.get("NEUROBOT_SAFETY_STATE_TOPIC"))
        if "blocked" in args.conditions:
            self.rec.json("/twin/truth")
            self.world = stand.World(self.rec.node)
        self.rec.start()
        self.mcp = stand.Mcp(cfg.get("NEUROBOT_MCP_URL"), 3600.0)

    def nudge(self, cond: str, direction: str) -> dict:
        t0, t1, res = self.mcp.call("robot_nudge", {"direction": direction, "duration_ms": 1000, "speed": 0.15})
        time.sleep(0.5)
        moved = path_len(self.rec.since("odom", t0)) > self.a.moved
        _, _, st = self.mcp.call("robot_get_status", {})
        safety = st.get("safety", {}) if isinstance(st, dict) else {}
        row = {"condition": cond, "direction": direction, "took_s": round(t1 - t0, 3), "moved": moved,
               "path_m": round(path_len(self.rec.since("odom", t0)), 3),
               "ok": res.get("ok") if isinstance(res, dict) else None,
               "status": res.get("status") if isinstance(res, dict) else str(res)[:160],
               "reasons": (res.get("reasons") or [])[:2] if isinstance(res, dict) else [],
               "get_status": {"state": safety.get("state"), "estop": safety.get("estop"),
                              "motion_allowed": st.get("motion_allowed") if isinstance(st, dict) else None},
               "verdict": verdict(res, moved)}
        log.info("%s %s: %s", cond, direction, json.dumps({k: row[k] for k in ("ok", "status", "moved", "verdict")},
                                                         ensure_ascii=False))
        time.sleep(1.0)
        return row

    def free(self) -> List[dict]:
        return [self.nudge("free", "forward" if k % 2 == 0 else "backward") for k in range(self.a.runs)]

    def blocked(self) -> List[dict]:
        self.world.create_pole(POLE, *POLE_AWAY)
        try:
            self.world.set_pose(POLE, *stand.ahead(self.rec, *POLE_AT))
            time.sleep(1.5)
            rows = [self.nudge("blocked", "forward") for _ in range(self.a.runs)]
        finally:
            self.world.remove(POLE)
        stand.restart_armed(self.rec, self.cfg, self.a.gate_unit)
        return rows

    def estop(self) -> List[dict]:
        subprocess.run(["neurobot", "estop", "engage", "--reason", "agent_truth: проверка ответов агента"],
                       capture_output=True, timeout=60)
        try:
            return [self.nudge("estop", "forward") for _ in range(self.a.runs)]
        finally:
            rc = human_release("agent_truth: проверка окончена")
            if rc:
                log.error("защёлка не снята (код %d): снять человеком — sudo neurobot estop release", rc)

    def nogate(self) -> List[dict]:
        stand.systemctl("stop", self.a.gate_unit)
        try:
            return [self.nudge("nogate", "forward") for _ in range(self.a.runs)]
        finally:
            stand.restart_armed(self.rec, self.cfg, self.a.gate_unit)

    def close(self) -> None:
        self.mcp.close()
        self.rec.close()


def table(rows: List[dict]) -> Dict[str, Dict[str, int]]:
    """{условие: {«статус/сдвиг»: число}} и общий счёт лжи."""
    out: Dict[str, Dict[str, int]] = {}
    for r in rows:
        key = "{}/{}".format(r["status"] if r["ok"] is not True else "ok", "moved" if r["moved"] else "still")
        out.setdefault(r["condition"], {})
        out[r["condition"]][key] = out[r["condition"]].get(key, 0) + 1
    return out


def parse(argv=None):
    p = argparse.ArgumentParser(description="ответы MCP robot_nudge / robot_get_status против сдвига по /odom")
    p.add_argument("--out", required=True, help="итог JSON (существующий не перезаписывается)")
    p.add_argument("--runs", type=int, default=5, help="вызовов на условие (по умолчанию 5)")
    p.add_argument("--conditions", type=lambda s: [c for c in s.split(",") if c], default="free,blocked,estop,nogate",
                   help="free, blocked (двойник), estop, nogate через запятую")
    p.add_argument("--moved", type=float, default=0.01, help="сдвиг больше этого — робот поехал, м")
    p.add_argument("--pause-unit", default="neurobot-perception", help="распознавание: остановлено на время проверки")
    p.add_argument("--gate-unit", default="neurobot-safety", help="юнит safety_gate (nogate, рестарт после blocked)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", stream=sys.stderr)
    a = parse(argv)
    try:
        open(a.out, "x").close()
    except FileExistsError:
        print("agent_truth: {} уже есть — не перезаписываю".format(a.out), file=sys.stderr)
        return 2
    t = Truth(a, load())
    rows: List[dict] = []
    doc = {"tool": "neurobot/deploy/verify/agent_truth.py", "args": vars(a), "t_start": time.time(),
           "label": "двойник: ответы агента против /odom — ориентир, не результат робота"}
    try:
        if a.pause_unit:
            stand.systemctl("stop", a.pause_unit)
        for cond in a.conditions:
            rows += getattr(t, cond)()
    finally:
        if a.pause_unit:
            stand.systemctl("start", "--no-block", a.pause_unit, check=False)
        t.close()
        doc.update(calls=rows, table=table(rows), lies=sum(1 for r in rows if r["verdict"] != "truth"),
                   t_end=time.time())
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
