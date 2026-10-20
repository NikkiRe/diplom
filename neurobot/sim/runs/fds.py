"""(ВМ sim, root) Сэмплер числа открытых файловых дескрипторов процессов юнитов → JSONL (H14: индикатор старения рядом
с памятью — утечка сокетов rosbridge, MCP, PicoClaw видна по fd раньше, чем по PSS).

  python3 -m neurobot.sim.runs.fds --units units.json --out fds.jsonl [--interval 10] [--duration S]

units.json — тот же, что у сэмплера памяти (stacks.write_units_json, перечитывается на каждом сэмпле): процессы юнита —
cgroup.procs его группы и вложенных. Строка — {type: sample, t_wall, t_mono, units: {имя: {procs: [{pid, start, fds}]}}}
(start — время старта процесса в тиках, как у collect.py: пара pid + start отличает жизни процесса). Первая строка —
header. Только stdlib, Python 3.8+. Выход — по SIGTERM/SIGINT или --duration; ошибки — в stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from typing import Dict, List, Optional


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def pids(cgroup: str) -> List[int]:
    """Процессы группы и её вложенных групп."""
    out: List[int] = []
    for d, _, files in os.walk(cgroup):
        if "cgroup.procs" in files:
            out += [int(x) for x in (_read(os.path.join(d, "cgroup.procs")) or "").split()]
    return sorted(set(out))


def proc_fds(pid: int, proc_root: str = "/proc") -> Optional[dict]:
    """{pid, start, fds} процесса; None — процесс вышел между чтениями или fd не видны (не root)."""
    stat = _read(os.path.join(proc_root, str(pid), "stat"))
    if stat is None:
        return None
    try:
        n = len(os.listdir(os.path.join(proc_root, str(pid), "fd")))
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    # поле 22 — starttime; имя в скобках может содержать пробелы — режем по последней «)»
    start = int(stat.rpartition(")")[2].split()[19])
    return {"pid": pid, "start": start, "fds": n}


def sample(units_doc: dict, proc_root: str = "/proc") -> dict:
    units: Dict[str, dict] = {}
    for name, u in units_doc.get("units", {}).items():
        procs = [proc_fds(pid, proc_root) for pid in pids(u["cgroup"])]
        units[name] = {"procs": [p for p in procs if p is not None]}
    return {"type": "sample", "t_wall": time.time(), "t_mono": time.monotonic(), "units": units}


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--units", required=True, help="units.json прогона (stacks.write_units_json)")
    p.add_argument("--out", required=True, help="JSONL")
    p.add_argument("--interval", type=float, default=10.0, help="период, с")
    p.add_argument("--duration", type=float, default=0.0, help="длительность, с (0 — до сигнала)")
    a = p.parse_args(argv)
    if not 0.5 <= a.interval <= 600 or a.duration < 0:
        p.error("--interval 0,5–600 с, --duration ≥ 0")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    t0 = time.monotonic()
    with open(a.out, "a", encoding="utf-8") as out:
        out.write(json.dumps({"type": "header", "interval_s": a.interval, "t_wall": time.time(), "t_mono": t0}) + "\n")
        out.flush()
        while not stop.is_set():
            try:
                with open(a.units, encoding="utf-8") as f:
                    doc = json.load(f)
                out.write(json.dumps(sample(doc)) + "\n")
                out.flush()
            except (OSError, ValueError) as e:
                print("fds: %s" % e, file=sys.stderr)
            if a.duration and time.monotonic() - t0 >= a.duration:
                break
            stop.wait(a.interval - (time.monotonic() - t0) % a.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
