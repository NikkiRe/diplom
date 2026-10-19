"""(ВМ sim, root) Очередь прогонов в ВМ (README §4.1): шаги идут один за другим юнитом runs-queue и от сессии на Mac
не зависят — её можно останавливать и продолжать. Готовый шаг (итог в каталоге есть) пропускается; начатый и
недоделанный (ВМ остановили посреди шага, прогон прерван) — его каталог уходит в BASE/interrupted/, шаг идёт заново.

  sudo python3 -m neurobot.sim.runs.queue --mockllm URL [--base DIR] [--log FILE] ШАГ…
  ШАГ: run:a0|a1:МЕТКА:РАСПИСАНИЕ[:world=hall|lab,no_return=nan|inf] | budget:МЕТКА:МиБ:РАСПИСАНИЕ |
       start:МЕТКА:N[:СЦЕНАРИИ|all]

run — runner.py (опции — его --world и --no-return, только A1), budget — budget_a1.py, start — start_test пакета с
жестом start_gesture.py (СЦЕНАРИИ «сломанного старта» через «,», all — все по составу пакета; без них — только
холодные старты). Расписание — каталог BASE/sched_РАСПИСАНИЕ (run.sh sched ПРОФИЛЬ [ИМЯ]), вывод шага —
BASE/МЕТКА.log, ход очереди — строки JSON в --log. Неудачный шаг очередь не останавливает. Логи — stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request
from typing import Callable, List, NamedTuple

BASE = "/var/tmp/nb-runs"
START_DEVIATION = "двойник (мир hall), perception — FakeYolo по таймлайну start, кадры через relay.py"


class Step(NamedTuple):
    kind: str
    label: str
    args: tuple


RUN_OPTIONS = {"world": ("hall", "lab"), "no_return": ("nan", "inf")}


def run_options(text: str) -> dict:
    """«world=lab,no_return=inf» → {"world": "lab", "no_return": "inf"}; ValueError — неизвестное."""
    out = {}
    for item in filter(None, text.split(",")):
        key, _, value = item.partition("=")
        if value not in RUN_OPTIONS.get(key, ()):
            raise ValueError("опция %r: %s" % (item, ", ".join("%s=%s" % (k, "|".join(v))
                                                              for k, v in RUN_OPTIONS.items())))
        out[key] = value
    return out


def parse_step(text: str) -> Step:
    parts = text.split(":")
    kind = parts[0]
    run_ok = len(parts) == 4 and parts[1:2] in (["a0"], ["a1"]) or len(parts) == 5 and parts[1] == "a1"
    ok = (kind == "run" and run_ok) or (kind == "budget" and len(parts) == 4 and parts[2].isdigit()) \
        or (kind == "start" and len(parts) in (3, 4) and parts[2].isdigit())
    if not ok or not all(parts[1:3]):
        raise ValueError("шаг %r: run:a0|a1:МЕТКА:РАСПИСАНИЕ[:ОПЦИИ A1] | budget:МЕТКА:МиБ:РАСПИСАНИЕ | "
                         "start:МЕТКА:N[:СЦЕНАРИИ]" % text)
    if kind == "run":
        opts = run_options(parts[4]) if len(parts) == 5 else {}
        return Step(kind, parts[2], (parts[1], parts[3]) + tuple("--%s=%s" % (k.replace("_", "-"), v)
                                                                 for k, v in sorted(opts.items())))
    return Step(kind, parts[1], tuple(parts[2:]))


def done(step: Step, base: str) -> bool:
    """Итог шага на месте: run.json без ошибки прерывания (run, budget) или summary.json (start)."""
    out = os.path.join(base, step.label)
    if step.kind == "start":
        return os.path.exists(os.path.join(out, "summary.json"))
    try:
        with open(os.path.join(out, "run.json"), encoding="utf-8") as f:
            return "error" not in json.load(f)
    except (OSError, ValueError):
        return False


def argv(step: Step, base: str, mock: str) -> List[str]:
    out = os.path.join(base, step.label)
    py = [sys.executable, "-m"]
    if step.kind == "run":
        system, profile = step.args[:2]
        return py + ["neurobot.sim.runs.runner", "run", "--system", system, "--label", step.label, "--sched",
                     os.path.join(base, "sched_" + profile), "--out", out, "--mockllm", mock] + list(step.args[2:])
    if step.kind == "budget":
        mem, profile = step.args
        return py + ["neurobot.sim.runs.budget_a1", "--out", out, "--mem", mem, "--sched",
                     os.path.join(base, "sched_" + profile), "--label", step.label, "--mockllm", mock]
    sched = os.path.join(base, "sched_start")
    gesture = " ".join(shlex.quote(a) for a in py + ["neurobot.sim.runs.start_gesture", "check", "--sched", sched,
                                                      "--log", os.path.join(out, "gestures.jsonl")])
    broken = [] if step.args[1:] == ("all",) else ["--broken=" + "".join(step.args[1:])]
    test = ["neurobot", "start-test", "--cycles", step.args[0]] + broken + [
        "--out", out, "--gesture-cmd", gesture, "--deviation", START_DEVIATION]
    prepare = py + ["neurobot.sim.runs.start_gesture", "prepare", "--sched", sched, "--mockllm", mock]
    return ["/bin/sh", "-c", "%s && exec %s" % (" ".join(shlex.quote(a) for a in prepare),
                                                " ".join(shlex.quote(a) for a in test))]


def mock_alive(mock: str, timeout: float = 60.0) -> bool:
    end = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(mock + "/healthz", timeout=3.0):
                return True
        except OSError:
            if time.monotonic() >= end:
                return False
            time.sleep(2.0)


def log(path: str, **rec) -> None:
    rec["t"] = round(time.time(), 3)
    line = json.dumps(rec, ensure_ascii=False)
    print("[queue] " + line, file=sys.stderr, flush=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_queue(steps: List[Step], base: str, mock: str, log_path: str,
              call: Callable[[List[str], str], int], alive: Callable[[str], bool] = mock_alive) -> int:
    """Шаги по порядку; код — число неудачных шагов."""
    bad = 0
    for st in steps:
        out = os.path.join(base, st.label)
        if done(st, base):
            log(log_path, step=st.label, status="skip", why="итог уже есть")
            continue
        if os.path.exists(out):
            dest = os.path.join(base, "interrupted", "%s-%s" % (st.label, time.strftime("%Y%m%d-%H%M%S")))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.rename(out, dest)
            log(log_path, step=st.label, status="moved", to=dest)
        if not alive(mock):
            bad += 1
            log(log_path, step=st.label, status="fail", why="mockllm не отвечает: %s" % mock)
            continue
        log(log_path, step=st.label, status="start", kind=st.kind, args=list(st.args))
        rc = call(argv(st, base, mock), os.path.join(base, st.label + ".log"))
        ok = rc == 0 and done(st, base)
        bad += not ok
        log(log_path, step=st.label, status="end", rc=rc, ok=ok)
    return bad


def call_logged(cmd: List[str], log_path: str) -> int:
    with open(log_path, "a", encoding="utf-8") as f:
        return subprocess.call(cmd, stdout=f, stderr=f, stdin=subprocess.DEVNULL)


def main(argv_=None) -> int:
    p = argparse.ArgumentParser(prog="python -m neurobot.sim.runs.queue", description=__doc__.split("\n\n")[0])
    p.add_argument("steps", nargs="+", help="шаги очереди (см. выше)")
    p.add_argument("--mockllm", required=True, help="bench/mockllm в ВМ, напр. http://127.0.0.1:18150")
    p.add_argument("--base", default=BASE, help="каталог прогонов в ВМ")
    p.add_argument("--log", default="", help="ход очереди, строки JSON (по умолчанию BASE/queue.jsonl)")
    a = p.parse_args(argv_)
    try:
        steps = [parse_step(s) for s in a.steps]
    except ValueError as e:
        print("queue: %s" % e, file=sys.stderr)
        return 2
    if len({s.label for s in steps}) != len(steps):
        print("queue: метки шагов повторяются", file=sys.stderr)
        return 2
    return min(run_queue(steps, a.base, a.mockllm, a.log or os.path.join(a.base, "queue.jsonl"), call_logged), 1)


if __name__ == "__main__":
    sys.exit(main())
