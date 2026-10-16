"""Балласт памяти и CPU за части Jetson, которых на стенде нет (model.json, раздел ballast).

  python3 -m neurobot.sim.budget.ballast --mib N [--net] [--cpu-pct P] [--period-ms 100]
        отдельный процесс: держит N МиБ резидентной анонимной памяти (--net — за вычетом собственного PSS
        интерпретатора) и, если задано, занимает P % одного ядра ВМ циклами «работа — сон»; выход по SIGTERM/SIGINT
  python3 -m neurobot.sim.budget.ballast --mib N --run script.py [-- аргументы скрипта]
        балласт внутри процесса: выделить N МиБ и выполнить script.py как __main__ в этом же процессе. Так память
        CUDA perception на плате принадлежит процессу perception, и OOM выбирает его, а не отдельный балласт.

Страницы записываются, а не только отображаются: иначе ядро не выделит их до первого обращения. Свопа на стенде
нет (MemorySwapMax=0), поэтому балласт остаётся резидентным, как память CUDA (nvmap) на плате. Строка о балласте —
в stderr.
"""
from __future__ import annotations

import argparse
import os
import runpy
import signal
import sys
import threading
import time
from typing import List, Optional, Tuple

MIB = 1 << 20
PAGE = 4096
_HELD: List[bytearray] = []       # балласт in-process живёт, пока жив процесс


def own_pss_kib(path: str = "/proc/self/smaps_rollup") -> Optional[int]:
    """PSS этого процесса, КиБ; None — нет /proc (не Linux)."""
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("Pss:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def alloc_mib(target_mib: float, own_kib: Optional[int], net: bool) -> float:
    """Сколько МиБ выделить: target, а с --net — target минус собственный PSS (не меньше нуля)."""
    if not net or own_kib is None:
        return target_mib
    return max(0.0, target_mib - own_kib / 1024)


def hold(mib: float) -> bytearray:
    """Резидентный буфер: по байту в каждую страницу."""
    n = int(mib * MIB)
    buf = bytearray(n)
    if n:
        buf[::PAGE] = b"\x01" * len(range(0, n, PAGE))
    return buf


def duty(cpu_pct: float, period_s: float) -> Tuple[float, float]:
    """(работа, сон) за период для доли одного ядра cpu_pct."""
    if not 0 < cpu_pct <= 100:
        raise ValueError("--cpu-pct: 0 < P ≤ 100")
    if period_s <= 0:
        raise ValueError("--period-ms > 0")
    busy = period_s * cpu_pct / 100
    return busy, period_s - busy


def burn(cpu_pct: float, period_s: float, stop: threading.Event) -> None:
    busy, idle = duty(cpu_pct, period_s)
    while not stop.is_set():
        end = time.monotonic() + busy
        while time.monotonic() < end:
            pass
        stop.wait(idle)


def run_script(script: str, args: List[str]) -> None:
    """Выполнить script как `python script args` в этом процессе."""
    script = os.path.abspath(script)
    sys.argv = [script] + list(args)
    sys.path.insert(0, os.path.dirname(script))
    runpy.run_path(script, run_name="__main__")


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    tail: List[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, tail = argv[:i], argv[i + 1:]
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--mib", type=float, required=True, help="МиБ балласта")
    p.add_argument("--net", action="store_true", help="за вычетом собственного PSS интерпретатора")
    p.add_argument("--cpu-pct", type=float, default=0.0, help="доля одного ядра ВМ, %% (0 — без CPU)")
    p.add_argument("--period-ms", type=float, default=100.0, help="период цикла CPU, мс")
    p.add_argument("--run", metavar="SCRIPT", help="выполнить скрипт в этом процессе после выделения")
    a = p.parse_args(argv)
    if not 0 <= a.mib < 1 << 20:
        p.error("--mib: 0 ≤ N < 2^20")
    if a.cpu_pct:
        try:
            duty(a.cpu_pct, a.period_ms / 1000)
        except ValueError as e:
            p.error(str(e))
    if a.run and (a.cpu_pct or a.net):
        p.error("--run — только с --mib")
    if tail and not a.run:
        p.error("аргументы после -- — только для --run")
    own = own_pss_kib()
    mib = alloc_mib(a.mib, own, a.net)
    _HELD.append(hold(mib))
    print("ballast: %.1f МиБ%s%s" % (mib, " (цель %.1f, свой PSS %.1f МиБ)" % (a.mib, own / 1024) if a.net and own
                                       else "", ", CPU %.1f %%" % a.cpu_pct if a.cpu_pct else ""), file=sys.stderr)
    if a.run:
        run_script(a.run, tail)
        return 0
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    if a.cpu_pct:
        burn(a.cpu_pct, a.period_ms / 1000, stop)
    else:
        while not stop.wait(3600):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
