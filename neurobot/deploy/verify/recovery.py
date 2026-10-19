#!/usr/bin/env python3
"""Смерть и зависание узла внутри юнита ros2 launch (драйверы, rosbridge) — поднимается ли система сама и за сколько.

    sudo python3 recovery.py --out FILE.json [--runs 5] [--signals KILL,STOP] [--settle 10] [--timeout 60] [--mcp]
                             [случай …]
    случай: driver@<имя>[:шаблон] | rosbridge[:шаблон]      по умолчанию — все драйверы env-файла и rosbridge

Узел — процесс юнита, кроме главного (ros2 launch), командная строка которого подходит под шаблон — регулярное
выражение, re.search (по умолчанию: у rosbridge — rosbridge_websocket, у драйвера — первый по PID; у базы двойника
сервер Gazebo — «^gz sim»: первый по PID там — обёртка /bin/sh, её SIGSTOP ничего не останавливает). KILL — kill -9
(его ловит обёртка launchfiles/supervised.launch.py: launch гаснет, юнит перезапускает systemd), STOP — SIGSTOP (жив,
но молчит: его ловит сторож по свежести тем и ping rosbridge). Метки от сигнала: новый запуск юнита (InvocationID),
юнит снова active, «вернулся» — у драйвера все его темы свежести (NEUROBOT_DRIVER_<имя>_FRESH, как у сторожа) прислали
сообщение после нового запуска, у rosbridge — WebSocket отвечает; с --mcp ещё и robot_get_status по MCP видит
rosbridge (агентский путь целиком). Перед каждым замером юнит active не меньше --settle с и его темы свежие. После
STOP старый процесс получает SIGCONT (если жив). Кто перезапустил — строки журнала сторожа и обёртки. Итог —
FILE.json; в конце — doctor (код и корневая причина). Числа двойника — ориентир.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

import stand
from neurobot_deploy import watchdog
from neurobot_deploy.config import Config, load
from neurobot_deploy.probes import ProbeError, ws_handshake
from neurobot_deploy.rosgraph import TopicWatch

log = logging.getLogger("recovery")
SIGNALS = {"KILL": signal.SIGKILL, "STOP": signal.SIGSTOP}
ROSBRIDGE_PATTERN = "rosbridge_websocket"


def cases(cfg: Config, names: List[str]) -> List[Tuple[str, str, str]]:
    """[(компонент, юнит, шаблон)] из аргументов или env-файла."""
    names = names or ["driver@" + d for d in cfg.drivers()] + (["rosbridge"] if "rosbridge" in cfg.components()
                                                               else [])
    out = []
    for name in names:
        comp, _, pattern = name.partition(":")
        if comp == "rosbridge":
            out.append((comp, watchdog.ROSBRIDGE_UNIT, pattern or ROSBRIDGE_PATTERN))
        elif comp.startswith("driver@") and comp[len("driver@"):] in cfg.drivers():
            out.append((comp, "neurobot-{}.service".format(comp), pattern))
        else:
            raise SystemExit("recovery: неизвестный случай {!r}: driver@<имя из NEUROBOT_DRIVERS> или rosbridge".format(
                name))
    return out


def victim(pids: List[int], main_pid: int, pattern: str, cmdline) -> Optional[int]:
    """Узел юнита: не главный процесс; командная строка подходит под шаблон (re.search), без шаблона — первый по PID."""
    for pid in sorted(pids):
        if pid != main_pid and (not pattern or re.search(pattern, cmdline(pid))):
            return pid
    return None


def who(journal: str) -> List[str]:
    """Кто перезапустил: строки сторожа (restart …) и обёртки launch (гашу launch)."""
    keep = ("restart neurobot-", "гашу launch")
    return [ln.strip()[-200:] for ln in journal.splitlines() if any(k in ln for k in keep)][:4]


class Recovery:
    def __init__(self, args, cfg: Config):
        self.a, self.cfg = args, cfg
        self.watch = watchdog.driver_watch(cfg)
        topics = sorted({t for ts in self.watch.values() for t, _ in ts})
        self.topics = TopicWatch(cfg, topics) if topics else None
        self.port = cfg.port("NEUROBOT_ROSBRIDGE_PORT")

    def close(self) -> None:
        if self.topics:
            self.topics.close()

    def wait(self, seconds: float) -> None:
        if self.topics:
            self.topics.spin(seconds)
        else:
            time.sleep(seconds)

    def fresh(self, unit: str, after: float) -> bool:
        """Все темы свежести юнита прислали сообщение позже after (монотонные с); у rosbridge — WebSocket отвечает."""
        if unit == watchdog.ROSBRIDGE_UNIT:
            try:
                ws_handshake("127.0.0.1", self.port, 1.0)
                return True
            except ProbeError:
                return False
        last = self.topics.last if self.topics else {}
        return all((last.get(t) or 0.0) > after for t, _ in self.watch.get(unit, []))

    def mcp_ok(self) -> bool:
        try:
            m = stand.Mcp(self.cfg.get("NEUROBOT_MCP_URL"), 5.0)
        except Exception:  # noqa: BLE001 — сервер недоступен: не вернулся
            return False
        try:
            _, _, res = m.call("robot_get_status", {})
            return isinstance(res, dict) and bool(res.get("connected"))
        finally:
            m.close()

    def steady(self, unit: str, timeout: float = 120.0) -> bool:
        """Юнит active не меньше settle с и его темы свежие (сообщение моложе 1 с)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            p = stand.unit_props(unit, "ActiveState", "ActiveEnterTimestampMonotonic")
            since = time.monotonic() - int(p.get("ActiveEnterTimestampMonotonic") or 0) / 1e6
            if p.get("ActiveState") == "active" and since >= self.a.settle and self.fresh(unit, time.monotonic() - 1.0):
                return True
            self.wait(0.5)
        return False

    def run(self, comp: str, unit: str, pattern: str, sig: str) -> dict:
        row: Dict[str, object] = {"case": comp, "signal": sig}
        if not self.steady(unit):
            row["error"] = "юнит не устоялся перед замером"
            return row
        p = stand.unit_props(unit, "MainPID", "InvocationID")
        pid = victim(stand.unit_pids(unit), int(p.get("MainPID") or 0), pattern, stand.cmdline)
        if pid is None:
            row["error"] = "нет узла по шаблону {!r}".format(pattern)
            return row
        row.update(pid=pid, node=stand.cmdline(pid)[:160])
        wall0, t0, inv0 = time.time(), time.monotonic(), p.get("InvocationID")
        os.kill(pid, SIGNALS[sig])
        marks: Dict[str, Optional[float]] = {"new_s": None, "active_s": None, "back_s": None, "mcp_s": None}
        t_new = None
        end = t0 + self.a.timeout
        while time.monotonic() < end and (marks["back_s"] is None or (self.a.mcp and comp == "rosbridge"
                                                                       and marks["mcp_s"] is None)):
            now = time.monotonic()
            q = stand.unit_props(unit, "InvocationID", "ActiveState")
            if t_new is None and q.get("InvocationID") and q.get("InvocationID") != inv0:
                t_new = now
                marks["new_s"] = round(now - t0, 3)
            if t_new is not None and marks["active_s"] is None and q.get("ActiveState") == "active":
                marks["active_s"] = round(now - t0, 3)
            if marks["active_s"] is not None and marks["back_s"] is None and self.fresh(unit, t_new):
                marks["back_s"] = round(time.monotonic() - t0, 3)
            if self.a.mcp and comp == "rosbridge" and marks["back_s"] is not None and marks["mcp_s"] is None \
                    and self.mcp_ok():
                marks["mcp_s"] = round(time.monotonic() - t0, 3)
            self.wait(0.1)
        if sig == "STOP":
            try:
                os.kill(pid, signal.SIGCONT)
                row["old_alive"] = True
            except ProcessLookupError:
                row["old_alive"] = False
        row.update(marks, recovered=marks["back_s"] is not None)
        journal = subprocess.run(["journalctl", "--no-pager", "-o", "cat", "--since", "@{:.3f}".format(wall0 - 0.5),
                                  "-u", unit, "-u", "neurobot-watchdog"], capture_output=True, text=True).stdout
        row["by"] = who(journal)
        log.info("%s %s: %s", comp, sig, json.dumps(marks))
        return row


def parse(argv=None):
    p = argparse.ArgumentParser(description="kill -9 и SIGSTOP узлов внутри юнитов ros2 launch: время восстановления")
    p.add_argument("--out", required=True, help="итог JSON (существующий не перезаписывается)")
    p.add_argument("--runs", type=int, default=5, help="замеров на случай и сигнал (по умолчанию 5)")
    p.add_argument("--signals", default="KILL,STOP", help="KILL, STOP или оба через запятую")
    p.add_argument("--settle", type=float, default=10.0, help="юнит active хотя бы столько перед замером, с")
    p.add_argument("--timeout", type=float, default=60.0, help="ждать восстановления не дольше, с")
    p.add_argument("--mcp", action="store_true", help="у rosbridge ждать и robot_get_status по MCP")
    p.add_argument("case", nargs="*",
                   help="driver@<имя>[:шаблон] | rosbridge[:шаблон]; шаблон — регулярное выражение командной строки")
    return p.parse_args(argv)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", stream=sys.stderr)
    a = parse(argv)
    try:
        open(a.out, "x").close()
    except FileExistsError:
        print("recovery: {} уже есть — не перезаписываю".format(a.out), file=sys.stderr)
        return 2
    cfg = load()
    todo = cases(cfg, a.case)
    sigs = [s for s in a.signals.split(",") if s]
    if not set(sigs) <= set(SIGNALS):
        raise SystemExit("recovery: --signals — из {}".format(", ".join(SIGNALS)))
    r = Recovery(a, cfg)
    rows = []
    doc = {"tool": "neurobot/deploy/verify/recovery.py", "args": vars(a), "t_start": time.time(),
           "label": "двойник: времена — ориентир, не результат робота"}
    try:
        for comp, unit, pattern in todo:
            for sig in sigs:
                for _ in range(a.runs):
                    rows.append(r.run(comp, unit, pattern, sig))
    finally:
        r.close()
        doc["runs"] = rows
        doc["summary"] = {"{} {}".format(c, s): dict(stand.quantiles([x.get("back_s") for x in rows
                                                                      if x["case"] == c and x["signal"] == s]),
                                                     recovered=sum(1 for x in rows if x["case"] == c and
                                                                   x["signal"] == s and x.get("recovered")),
                                                     total=sum(1 for x in rows if x["case"] == c and x["signal"] == s))
                          for c, _, _ in todo for s in sigs}
        doctor = subprocess.run(["neurobot", "doctor", "--json"], capture_output=True, text=True, timeout=180)
        doc["doctor_rc"] = doctor.returncode
        try:
            doc["doctor_root"] = json.loads(doctor.stdout).get("root")
        except ValueError:
            doc["doctor_root"] = doctor.stdout[-300:]
        doc["t_end"] = time.time()
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
