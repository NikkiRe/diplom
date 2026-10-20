#!/usr/bin/env python3
"""Провокация OOM на двойнике (ВМ, root): петля рестартов распознавания и выбор жертвы по OOMScoreAdjust.

    sudo python3 oom.py loop --deploy DIR --label L --out F.json [--seconds 180] [--mem 400] [--python PY]
    sudo python3 oom.py victims --deploy DIR --label L --out F.json [--runs 3] [--profile equal|robot]

DIR — каталог пакета deploy (юниты и CLI той версии, что меряется: «до» — снимок из git, «после» — рабочее дерево).
Пакет на двойнике не трогается: всё — в своих юнитах и slice с префиксом nbopt-, в конце они снимаются.

loop. Юнит распознавания из шаблона DIR (ExecStartPre — precheck той же версии CLI, Restart=always, RestartSec=1) под
своим именем, в своём slice с MemoryMax. Команда — нагрузка как у perception на старте: импорт torch и snntorch из
окружения perception двойника, затем рост памяти до OOM. Env-файл — установленный пакета (пользователь, sysfs,
манифест весов) с подменой префикса и команды. За --seconds: число запусков, OOM-убийств (memory.events slice),
CPU slice в долях ядра ВМ, паузы precheck по журналу.

victims. Юниты ролей с OOMScoreAdjust из юнитов DIR в одном slice, MemoryMax — на 64 МиБ выше занятого; затем
«всплеск» (0) растёт без предела — утечка. Кого ядро убило и в каком порядке — по Result=oom-kill юнитов; --runs
повторов. Профиль --profile:
  equal — safety_gate, распознавание и rosbridge по --hold МиБ: проверка правила ядра. Без OOMScoreAdjust очки равны,
          и жертва произвольна (20.10: в первом прогоне safety_gate 3 из 3, при повторе rosbridge 3 из 3);
  robot — доли памяти робота (budget §6.1: из MemTotal 3956 МиБ распознавание 1305, rosbridge 111, safety_gate 38;
          пульт ≈43 — юнит ui) × --scale; остальное до MemTotal — «прочее» с −1000 (ОС, драйверы, память вне RSS:
          в замере не кандидат), чтобы предел slice, от которого ядро считает очки, стоял в той же пропорции.
В итоге — RSS ролей перед всплеском и очки ядра: RSS + OOMScoreAdjust × MemoryMax / 1000 [расчёт, mm/oom_kill.c].

Итог — F.json (существующий не перезаписывается). Числа ВМ — ориентир, не робот.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from neurobot_deploy import render  # noqa: E402
from neurobot_deploy.config import Config, parse_env, quote  # noqa: E402

RUN = "/run/systemd/system"
WORK = "/var/tmp/nbopt-oom"
TWIN_PY = "/var/lib/neurobot-twin/venv/bin/python"
# нагрузка «как perception на старте»: тяжёлые импорты, затем память кусками по 32 МиБ с касанием страниц
WORKLOAD = """import sys, time
import torch, snntorch  # noqa: F401
chunks = []
while True:
    chunks.append(bytearray(32 << 20))
    time.sleep(0.02)
"""
# доли памяти робота, МиБ: budget §6.1 (PSS по компонентам, MemTotal Nano), пульт — юнит ui (≈43 МиБ anon)
ROBOT_TOTAL = 3956
ROBOT = {"safety": 38, "perception": 1305, "rosbridge": 111, "ui": 43}
ROLE_UNIT = {"safety": "neurobot-safety.service", "perception": "neurobot-perception.service",
             "rosbridge": "neurobot-rosbridge.service", "ui": "neurobot-ui.service"}
HEADROOM = 64  # МиБ сверх занятого до всплеска
HOLD = """import sys, time
mib = int(sys.argv[1]); step = int(sys.argv[2]) if len(sys.argv) > 2 else 0
data = [bytearray(1 << 20) for _ in range(mib)]
while True:
    time.sleep(0.05)
    if step:
        data.append(bytearray(step << 20))
"""


def sh(*args: str, check: bool = True) -> str:
    r = subprocess.run(list(args), capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError("{}: {}".format(" ".join(args), r.stderr.strip()))
    return r.stdout.strip()


def prop(unit: str, name: str) -> str:
    return sh("systemctl", "show", "-p", name, "--value", unit, check=False)


def cgroup_file(unit: str, name: str) -> str:
    """Файл cgroup юнита или slice; нет cgroup (ещё или уже) — пусто."""
    cgroup = prop(unit, "ControlGroup")
    if not cgroup:
        return ""
    try:
        with open("/sys/fs/cgroup{}/{}".format(cgroup, name)) as f:
            return f.read()
    except OSError:
        return ""


def stat_value(text: str, key: str) -> int:
    """Число из файла «ключ значение» cgroup (cpu.stat, memory.events); нет — 0."""
    for line in text.splitlines():
        k, _, v = line.partition(" ")
        if k == key:
            return int(v)
    return 0


def adjust_of(unit_text: str) -> int:
    for line in unit_text.splitlines():
        if line.startswith("OOMScoreAdjust="):
            return int(line.split("=", 1)[1])
    return 0


def rendered(deploy: str, env: Dict[str, str], path: str) -> Dict[str, str]:
    return render.render(Config(env, path), os.path.join(deploy, "units"))


def loop_unit(text: str, name: str, slice_: str, mem: int) -> str:
    """Юнит распознавания под своим именем: свой StateDirectory и slice, MemoryMax; без PartOf и WantedBy цели."""
    out = []
    for line in text.splitlines():
        if line.startswith(("PartOf=", "WantedBy=", "After=")):
            continue
        if line.startswith("StateDirectory="):
            line = "StateDirectory=nbopt/" + name
        out.append(line)
        if line == "[Service]":
            out += ["Slice=" + slice_, "MemoryMax={}M".format(mem), "MemorySwapMax=0"]
    return "\n".join(out) + "\n"


def journal_lines(unit: str, since: float) -> List[str]:
    return sh("journalctl", "--no-pager", "-o", "cat", "--since", "@{:.3f}".format(since), "-u", unit,
              check=False).splitlines()


def pauses(lines: List[str]) -> List[float]:
    """Паузы precheck по строкам restart_loop журнала («… пауза 15 с»)."""
    out = []
    for ln in lines:
        if "restart_loop" in ln and "пауза " in ln:
            try:
                out.append(float(ln.rsplit("пауза ", 1)[1].split()[0]))
            except (IndexError, ValueError):
                pass
    return out


def run_loop(a) -> dict:
    name = "nbopt-oom-" + a.label
    unit, slice_ = name + ".service", "nbopt-oom.slice"
    prefix = os.path.join(WORK, a.label)
    shutil.rmtree(prefix, ignore_errors=True)
    os.makedirs(os.path.join(prefix, "bin"))
    for f in ("neurobot", "neurobot-run"):
        shutil.copy(os.path.join(a.deploy, "bin", f), os.path.join(prefix, "bin", f))
    shutil.copytree(os.path.join(a.deploy, "neurobot_deploy"), os.path.join(prefix, "lib", "neurobot_deploy"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    with open(os.path.join(prefix, "workload.py"), "w", encoding="utf-8") as f:
        f.write(WORKLOAD)
    with open("/etc/neurobot/neurobot.env", encoding="utf-8") as f:
        base = f.read()
    over = {"NEUROBOT_PREFIX": prefix, "NEUROBOT_DRIVERS": "", "NEUROBOT_LABWORKERS": "",
            "NEUROBOT_PERCEPTION_CMD": "{} {}/workload.py --ros-args -r /cmd_vel:=/cmd_vel_gesture".format(
                a.python, prefix),
            "NEUROBOT_PERCEPTION_MODULES": "numpy<2 torch snntorch"}
    env_path = os.path.join(prefix, "etc", "neurobot.env")
    os.makedirs(os.path.dirname(env_path))
    with open(env_path, "w", encoding="utf-8") as f:
        f.write(base + "\n# nbopt: стенд петли OOM\n" + "".join("{}={}\n".format(k, quote(v)) for k, v in over.items()))
    for root, _dirs, files in os.walk(prefix):
        os.chmod(root, 0o755)
        for fn in files:
            os.chmod(os.path.join(root, fn), 0o755)
    env = dict(parse_env(base), **over)
    text = loop_unit(rendered(a.deploy, env, env_path)["neurobot-perception.service"], name, slice_, a.mem)
    with open(os.path.join(RUN, unit), "w", encoding="utf-8") as f:
        f.write(text)
    sh("systemctl", "daemon-reload")
    wall0 = time.time()
    try:
        cpu0 = stat_value(cgroup_file(slice_, "cpu.stat"), "usage_usec")
        oom0 = stat_value(cgroup_file(slice_, "memory.events"), "oom_kill")
        t0 = time.monotonic()
        sh("systemctl", "start", "--no-block", unit)
        time.sleep(a.seconds)
        dt = time.monotonic() - t0
        cpu1 = stat_value(cgroup_file(slice_, "cpu.stat"), "usage_usec")
        oom1 = stat_value(cgroup_file(slice_, "memory.events"), "oom_kill")
        n = int(prop(unit, "NRestarts") or 0)
    finally:
        sh("systemctl", "stop", unit, check=False)
        lines = journal_lines(unit, wall0)
        os.remove(os.path.join(RUN, unit))
        sh("systemctl", "daemon-reload")
        sh("systemctl", "reset-failed", unit, check=False)
        sh("systemctl", "stop", "nbopt.slice", check=False)  # и родительские slice стенда
    p = pauses(lines)
    return {"label": a.label, "unit_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "backoff_py": os.path.exists(os.path.join(a.deploy, "neurobot_deploy", "backoff.py")),
            "seconds": round(dt, 1), "mem_max_mib": a.mem, "starts": n + 1, "oom_kills": oom1 - oom0,
            "cpu_core_pct": round(100.0 * (cpu1 - cpu0) / 1e6 / dt, 1),
            "cpu_s_per_start": round((cpu1 - cpu0) / 1e6 / (n + 1), 2),
            "pauses_s": p, "paused_s": round(sum(p), 1), "load1": os.getloadavg()[0]}


def victim_sizes(profile: str, hold: int, scale: float) -> Dict[str, int]:
    """Память ролей, МиБ, в порядке запуска: equal — поровну, robot — доли робота × scale."""
    if profile == "equal":
        return {r: hold for r in ("safety", "perception", "rosbridge")}
    return {r: max(1, round(m * scale)) for r, m in ROBOT.items()}


def scores(rss: Dict[str, float], adjust: Dict[str, int], limit_mib: float) -> Dict[str, int]:
    """Очки ядра, МиБ: RSS + OOMScoreAdjust × предел / 1000 (oom_badness; при OOM в cgroup предел — её MemoryMax)."""
    return {r: round(rss[r] + adjust[r] * limit_mib / 1000.0) for r in rss}


def rss_mib(unit: str) -> float:
    try:
        with open("/proc/{}/status".format(prop(unit, "MainPID"))) as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


def run_victims(a) -> dict:
    with open("/etc/neurobot/neurobot.env", encoding="utf-8") as f:
        env = parse_env(f.read())
    units = rendered(a.deploy, env, "/etc/neurobot/neurobot.env")
    sizes = victim_sizes(a.profile, a.hold, a.scale)
    adjust = {r: adjust_of(units[ROLE_UNIT[r]]) for r in sizes}
    total = round(ROBOT_TOTAL * a.scale) if a.profile == "robot" else 0
    hold = os.path.join(WORK, "hold.py")
    os.makedirs(WORK, exist_ok=True)
    with open(hold, "w", encoding="utf-8") as f:
        f.write(HOLD)
    runs = []
    for i in range(a.runs):
        slice_ = "nbopt-victims-{}-{}.slice".format(a.label, i)
        names = {r: "nbopt-v-{}-{}-{}.service".format(a.label, i, r)
                 for r in list(sizes) + (["rest"] if total else []) + ["spike"]}

        def start(role: str, mib: int, adj: int, *extra: str) -> None:
            sh("systemd-run", "--quiet", "--unit", names[role], "--slice", slice_, "-p", "MemorySwapMax=0",
               "-p", "OOMScoreAdjust={}".format(adj), sys.executable, hold, str(mib), *extra)

        def used() -> int:
            time.sleep(3.0)
            return int(cgroup_file(slice_, "memory.current") or 0)

        try:
            # MemoryMax прошлого прогона с той же меткой лежит в /run/systemd/system.control до перезагрузки
            sh("systemctl", "revert", slice_, check=False)
            for role, mib in sizes.items():
                start(role, mib, adjust[role])
            cur = used()
            if total:
                # «прочее» добирает slice до MemTotal × scale без запаса; накладные интерпретатора — средние по ролям
                overhead = ((cur >> 20) - sum(sizes.values())) / len(sizes)
                start("rest", max(1, round(total - HEADROOM - (cur >> 20) - overhead)), -1000)
                cur = used()
            limit = cur + (HEADROOM << 20)
            rss = {r: rss_mib(names[r]) for r in sizes}
            dead = [r for r, v in rss.items() if not v]
            if dead:
                raise RuntimeError("до всплеска нет процесса роли: " + ", ".join(dead))
            sh("systemctl", "set-property", "--runtime", slice_, "MemoryMax={}".format(limit))
            t0 = time.monotonic()
            start("spike", 8, 0, "8")
            # утечка растёт, пока ядро не убьёт её саму: до того гибнут все, у кого очки выше
            while time.monotonic() - t0 < a.window and prop(names["spike"], "ActiveState") == "active":
                time.sleep(0.2)
            killed = []
            for role, unit in names.items():
                if prop(unit, "Result") == "oom-kill" or prop(unit, "ActiveState") in ("inactive", "failed"):
                    killed.append((int(prop(unit, "InactiveEnterTimestampMonotonic") or 0), role))
            runs.append({"killed_in_order": [r for _, r in sorted(killed)],
                         "alive": [r for r in names if r not in {k for _, k in killed}],
                         "oom_kills": stat_value(cgroup_file(slice_, "memory.events"), "oom_kill"),
                         "after_s": round(time.monotonic() - t0, 1), "limit_mib": limit >> 20,
                         "rss_mib": {r: round(v) for r, v in rss.items()},
                         "score_mib": scores(rss, adjust, limit / 2.0 ** 20)})
        finally:
            for unit in names.values():
                sh("systemctl", "stop", unit, check=False)
                sh("systemctl", "reset-failed", unit, check=False)
            sh("systemctl", "stop", slice_, check=False)
            sh("systemctl", "revert", slice_, check=False)
    sh("systemctl", "stop", "nbopt.slice", check=False)  # и родительские slice стенда
    return {"label": a.label, "profile": a.profile, "adjust": adjust, "sizes_mib": sizes,
            "scale": a.scale if total else None, "runs": runs}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="провокация OOM на двойнике: петля рестартов и выбор жертвы")
    sub = p.add_subparsers(dest="cmd", required=True)
    for cmd in ("loop", "victims"):
        s = sub.add_parser(cmd)
        s.add_argument("--deploy", required=True, help="каталог пакета deploy меряемой версии")
        s.add_argument("--label", required=True, help="метка условия: before | after")
        s.add_argument("--out", required=True, help="итог JSON (существующий не перезаписывается)")
    s = sub.choices["loop"]
    s.add_argument("--seconds", type=float, default=180.0, help="окно, с (180, как шаг лестницы budget)")
    s.add_argument("--mem", type=int, default=400, help="MemoryMax юнита, МиБ (400: импорт torch ≈210 МиБ)")
    s.add_argument("--python", default=TWIN_PY, help="интерпретатор с torch (окружение perception двойника)")
    s = sub.choices["victims"]
    s.add_argument("--runs", type=int, default=3)
    s.add_argument("--profile", choices=("equal", "robot"), default="equal",
                   help="equal — роли поровну (--hold); robot — доли памяти робота (--scale)")
    s.add_argument("--hold", type=int, default=200, help="equal: память каждого из трёх юнитов ролей, МиБ")
    s.add_argument("--scale", type=float, default=0.5, help="robot: масштаб памяти робота (0,5: slice ≈ 2 ГиБ)")
    s.add_argument("--window", type=float, default=15.0, help="предел ожидания гибели всплеска, с")
    a = p.parse_args(argv)
    if os.geteuid() != 0:
        print("oom: нужен root", file=sys.stderr)
        return 2
    try:
        open(a.out, "x").close()
    except FileExistsError:
        print("oom: {} уже есть — не перезаписываю".format(a.out), file=sys.stderr)
        return 2
    res = run_loop(a) if a.cmd == "loop" else run_victims(a)
    doc = dict({"tool": "neurobot/deploy/verify/oom.py " + a.cmd, "deploy": os.path.abspath(a.deploy),
                "t_end": time.time(), "note": "ВМ sim, ядро 6.8, cgroup v2: ориентир, не робот (на Nano ядро 4.9, "
                                              "cgroup v1)"}, **res)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
