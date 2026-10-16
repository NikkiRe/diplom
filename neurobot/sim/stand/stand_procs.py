#!/usr/bin/env python3
"""(в ВМ sim) Процессы стенда по /proc: группа стенда (PGID — PID ros2 launch, $SIM_RUN_ROOT/current/pid) и серверы
`gz sim`. Сирота — `gz sim`, переживший свой ros2 launch: он продолжает старый мир, держит последнюю команду DiffDrive
и ест CPU. launch стенда не стартует, пока в ВМ есть чужой `gz sim`; при выходе stm32_sim или scan_mask он гасит
группу командой down, а check.sh командой crash проверяет, что стенд гаснет целиком. Тем же пользуется цифровой
двойник (neurobot/sim/twin): его launch гасит группу командой down, а проверка роняет узел --victim.
  python3 stand_procs.py down --pgid N [--int-s 10] [--term-s 5]
        погасить группу, как vm_stop.sh: SIGINT, через int-s — SIGTERM, через term-s — SIGKILL (сервер gz, зависший
        при завершении, не отвечает ни на SIGINT, ни на SIGTERM); строки журнала — в stderr
  python3 stand_procs.py crash [--run-dir ~/robot_sim/current] [--victim stm32_sim.py] [--hang-gz] [--wait-s 25]
        kill -9 узла группы из run-dir (имя файла скрипта, по умолчанию stm32_sim стенда) и ждать, пока погаснет вся
        группа; --hang-gz — перед этим SIGSTOP серверу gz (так он ведёт себя, когда зависает при завершении). JSON в
        stdout, код 0 — группа погасла и gz sim в ВМ не осталось
  python3 stand_procs.py gz                    серверы gz sim в ВМ (JSON)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from typing import List, NamedTuple, Optional, Sequence

RUN_ROOT = os.environ.get("SIM_RUN_ROOT", os.path.expanduser("~/robot_sim"))
GZ_SIM = re.compile(r"^(?:\S*/)?(?:sh -c )?(?:ruby \S*/)?gz sim(?:\s|$)")   # программа — gz sim, не упоминание


class Proc(NamedTuple):
    pid: int
    pgid: int
    argv: tuple


def snapshot(proc: str = "/proc") -> List[Proc]:
    out = []
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc, name, "stat"), "rb") as fh:
                stat = fh.read().decode(errors="replace")
            with open(os.path.join(proc, name, "cmdline"), "rb") as fh:
                argv = tuple(a.decode(errors="replace") for a in fh.read().split(b"\0") if a)
        except OSError:      # процесс завершился между listdir и open
            continue
        # После «(comm)»: state ppid pgrp …; comm может содержать пробелы и скобки — режем по последней «)».
        out.append(Proc(int(name), int(stat[stat.rindex(")") + 2:].split()[2]), argv))
    return out


def is_gz_sim(argv: Sequence[str]) -> bool:
    """`gz sim …`: обёртка `sh -c "ruby …/gz sim …"` из ros_gz_sim и сервер — ruby, переписавший свой cmdline
    одной строкой «gz sim …»."""
    return GZ_SIM.match(" ".join(argv)) is not None


def gz_sim(procs: List[Proc], exclude_pgid: Optional[int] = None) -> List[Proc]:
    return [p for p in procs if is_gz_sim(p.argv) and p.pgid != exclude_pgid]


def group(procs: List[Proc], pgid: int) -> List[Proc]:
    return [p for p in procs if p.pgid == pgid]


def brief(procs: List[Proc]) -> list:
    return [{"pid": p.pid, "pgid": p.pgid, "cmd": " ".join(p.argv)[:160]} for p in procs]


def alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def down(pgid: int, int_s: float, term_s: float) -> Optional[str]:
    """Погасить группу процессов: последний посланный сигнал («-» — группы уже нет; None — пережила и SIGKILL)."""
    sent = "-"
    for sig, wait_s in ((signal.SIGINT, int_s), (signal.SIGTERM, term_s), (signal.SIGKILL, 5.0)):
        if not alive(pgid):
            return sent
        print("[stand_procs] группа %d: %s" % (pgid, sig.name), file=sys.stderr, flush=True)
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return sent
        sent = sig.name
        t_end = time.monotonic() + wait_s
        while alive(pgid) and time.monotonic() < t_end:
            time.sleep(0.2)
    return None if alive(pgid) else sent


def crash(run_dir: str, wait_s: float, hang_gz: bool = False, victim: str = "stm32_sim.py") -> dict:
    with open(os.path.join(run_dir, "pid")) as fh:
        pgid = int(fh.read())
    procs = group(snapshot(), pgid)
    victims = [p for p in procs if any(os.path.basename(a) == victim for a in p.argv)]
    if not victims:
        raise SystemExit("stand_procs: в группе %d нет %s" % (pgid, victim))
    hung = gz_sim(procs) if hang_gz else []
    for p in hung:
        os.kill(p.pid, signal.SIGSTOP)
    for p in victims:
        os.kill(p.pid, signal.SIGKILL)
    t0 = time.monotonic()
    while group(snapshot(), pgid) and time.monotonic() - t0 < wait_s:
        time.sleep(0.2)
    procs = snapshot()
    left = group(procs, pgid)
    return {"pgid": pgid, "killed": [p.pid for p in victims], "hung_gz": [p.pid for p in hung],
            "group_gone_after_s": None if left else round(time.monotonic() - t0, 2),
            "group_left": brief(left), "gz_sim_left": brief(gz_sim(procs)), "wait_s": wait_s}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("down")
    d.add_argument("--pgid", type=int, required=True)
    d.add_argument("--int-s", type=float, default=10.0)
    d.add_argument("--term-s", type=float, default=5.0)
    c = sub.add_parser("crash")
    c.add_argument("--run-dir", default=os.path.join(RUN_ROOT, "current"))
    c.add_argument("--victim", default="stm32_sim.py", help="имя файла скрипта узла, которому kill -9")
    c.add_argument("--hang-gz", action="store_true")
    c.add_argument("--wait-s", type=float, default=25.0)
    sub.add_parser("gz")
    a = ap.parse_args()
    if a.cmd == "down":
        return 0 if down(a.pgid, a.int_s, a.term_s) else 1
    if a.cmd == "gz":
        print(json.dumps(brief(gz_sim(snapshot())), ensure_ascii=False, indent=1))
        return 0
    out = crash(a.run_dir, a.wait_s, a.hang_gz, a.victim)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out["group_gone_after_s"] is not None and not out["gz_sim_left"] else 1


if __name__ == "__main__":
    sys.exit(main())
