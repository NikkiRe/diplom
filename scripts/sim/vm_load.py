#!/usr/bin/env python3
"""(в VM) Замер нагрузки симуляции за окно: CPU VM (/proc/stat), CPU и RSS/PSS процессов группы симуляции
(/proc/<pid>/stat, smaps_rollup), память VM (/proc/meminfo), RTF и частоты тем (topic_rates.py, параллельно).

  python3 vm_load.py --seconds 20 [--topics ...] [--pid-file ~/robot_sim/current/pid]
CPU в «ядрах» (1.0 = одно vCPU занято полностью). Собственная стоимость замерщика (topic_rates) — отдельно.
"""
import argparse
import json
import os
import resource
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HZ = os.sysconf("SC_CLK_TCK")


def cpu_total():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle = v[3] + v[4]
    return sum(v[:8]), idle


def proc_table(pgid):
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                s = f.read()
            rpar = s.rindex(")")
            comm = s[s.index("(") + 1:rpar]
            fields = s[rpar + 2:].split()
            if int(fields[2]) != pgid:  # pgrp
                continue
            ticks = int(fields[11]) + int(fields[12])  # utime + stime
            with open(f"/proc/{d}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
            out[int(d)] = {"comm": comm, "ticks": ticks, "cmd": cmd[:160]}
        except (FileNotFoundError, ProcessLookupError, ValueError, PermissionError):
            continue
    return out


def mem_of(pid):
    r = {}
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                k, _, rest = line.partition(":")
                if k in ("Rss", "Pss"):
                    r[k.lower() + "_mib"] = round(int(rest.split()[0]) / 1024, 1)
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        pass
    return r


def meminfo():
    m = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            m[k] = int(rest.split()[0])
    return {"total_mib": round(m["MemTotal"] / 1024), "used_mib": round((m["MemTotal"] - m["MemAvailable"]) / 1024),
            "cached_mib": round(m["Cached"] / 1024)}


def label(p):
    c = p["cmd"]
    for key, name in (("gz sim", "gz_sim_server"), ("component_container", "nav2_container"),
                      ("parameter_bridge", "ros_gz_bridge"), ("image_bridge", "image_bridge"),
                      ("rosbridge_websocket", "rosbridge"), ("republish", "camera_compressor"), ("rosapi", "rosapi"),
                      ("robot_state_publisher", "robot_state_publisher"), ("ros2 launch", "ros2_launch")):
        if key in c:
            return name
    return p["comm"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--topics", nargs="+", default=None)
    ap.add_argument("--pid-file", default=os.path.expanduser("~/robot_sim/current/pid"))
    a = ap.parse_args()
    with open(a.pid_file) as f:
        pgid = os.getpgid(int(f.read().strip()))
    cmd = [sys.executable, os.path.join(HERE, "topic_rates.py"), "--seconds", str(a.seconds), "--warmup", "1"]
    if a.topics:
        cmd += ["--topics"] + a.topics
    w0 = time.monotonic()
    tot0, idle0 = cpu_total()
    p0 = proc_table(pgid)
    child = subprocess.run(cmd, capture_output=True, text=True, timeout=a.seconds + 60)
    tot1, idle1 = cpu_total()
    p1 = proc_table(pgid)
    wall = time.monotonic() - w0
    ncpu = os.cpu_count()
    vm_busy = (tot1 - tot0 - (idle1 - idle0)) / max(1, tot1 - tot0) * ncpu
    procs = {}
    for pid, p in p1.items():
        d = (p["ticks"] - p0.get(pid, {"ticks": p["ticks"]})["ticks"]) / HZ / wall
        name = label(p)
        e = procs.setdefault(name, {"cpu_cores": 0.0, "rss_mib": 0.0, "pss_mib": 0.0, "n": 0})
        e["cpu_cores"] += d
        m = mem_of(pid)
        e["rss_mib"] += m.get("rss_mib", 0.0)
        e["pss_mib"] += m.get("pss_mib", 0.0)
        e["n"] += 1
    for e in procs.values():
        e["cpu_cores"] = round(e["cpu_cores"], 3)
        e["rss_mib"] = round(e["rss_mib"], 1)
        e["pss_mib"] = round(e["pss_mib"], 1)
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        rates = json.loads(child.stdout)
    except json.JSONDecodeError:
        rates = {"error": child.stderr[-2000:]}
    out = {
        "wall_s": round(wall, 2), "vm_cpus": ncpu, "vm_cpu_cores_busy": round(vm_busy, 3),
        "sim_cpu_cores": round(sum(e["cpu_cores"] for e in procs.values()), 3),
        "sim_pss_mib": round(sum(e["pss_mib"] for e in procs.values()), 1),
        "probe_cpu_cores": round((ru.ru_utime + ru.ru_stime) / wall, 3),
        "vm_mem": meminfo(), "processes": dict(sorted(procs.items(), key=lambda kv: -kv[1]["cpu_cores"])),
        "rates": rates,
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
