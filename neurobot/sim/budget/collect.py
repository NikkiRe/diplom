"""(ВМ sim) Сэмплер cgroup jetson.slice: память и CPU каждого юнита и его процессов → JSONL.

  python3 -m neurobot.sim.budget.collect --units units.json --out collect.jsonl [--interval 5] [--duration S]

Раз в interval для slice и каждого юнита из units.json: memory.current, memory.peak, memory.max, memory.events,
memory.stat, cpu.stat, pids.current; у slice ещё memory.pressure и cpu.pressure. Для каждого процесса юнита —
smaps_rollup (Rss, Pss, Pss_Anon, Pss_File, Pss_Shmem, Swap) и /proc/<pid>/stat (utime, stime, majflt, minflt,
потоки, время старта). Плюс loadavg, MemAvailable и счётчики /proc/stat всей ВМ. units.json пишет stack.py
и меняет при переносе процессов двойника, поэтому файл перечитывается на каждом сэмпле.
Первая строка — header, затем sample. Выход — по SIGTERM/SIGINT или по --duration; ошибки — в stderr.

unique_snapshot() (зовёт stack.py от root раз за шаг) считает уникальные резидентные страницы по /proc/<pid>/pagemap:
каждая физическая страница — один раз на группу. PSS делит общие библиотеки и с процессами вне slice (Gazebo,
узлы двойника, соседние агенты), и сумма PSS slice занижает то, что эти процессы займут на плате, где библиотеки
делят только они; RSS завышает (общее считается в каждом процессе). Уникальные страницы группы — между ними.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from array import array
from typing import Dict, Iterable, List, Optional, Set

SMAPS_KEYS = {"Rss": "rss_kib", "Pss": "pss_kib", "Pss_Anon": "pss_anon_kib", "Pss_File": "pss_file_kib",
              "Pss_Shmem": "pss_shmem_kib", "Swap": "swap_kib"}
MEM_STAT_KEYS = ("anon", "file", "kernel", "kernel_stack", "pagetables", "sock", "shmem", "file_mapped",
                 "pgmajfault", "workingset_refault_file")


def parse_flat(text: str) -> Dict[str, int]:
    """«ключ число» построчно (cpu.stat, memory.stat, memory.events)."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("-").isdigit():
            out[parts[0]] = int(parts[1])
    return out


def parse_smaps_rollup(text: str) -> Dict[str, int]:
    out = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in SMAPS_KEYS and rest.split():
            out[SMAPS_KEYS[key]] = int(rest.split()[0])
    return out


def parse_proc_stat(text: str) -> Dict[str, object]:
    """/proc/<pid>/stat; имя в скобках может содержать пробелы и скобки — режем по последней «)»."""
    left, _, right = text.rpartition(")")
    f = right.split()
    # после «)»: f[0] — поле 3 (state); поле N — f[N - 3]
    return {"comm": left.partition("(")[2], "minflt": int(f[7]), "majflt": int(f[9]), "utime": int(f[11]),
            "stime": int(f[12]), "threads": int(f[17]), "start": int(f[19])}


def parse_pressure(text: str) -> Dict[str, Dict[str, float]]:
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] in ("some", "full"):
            out[parts[0]] = {k: float(v) for k, v in (p.split("=") for p in parts[1:])}
    return out


def parse_proc_cpu(text: str) -> Dict[str, int]:
    """Первая строка /proc/stat: всего тиков и простоя (idle + iowait) по всем CPU ВМ."""
    f = [int(x) for x in text.splitlines()[0].split()[1:]]
    return {"total": sum(f[:8]), "idle": f[3] + f[4]}


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def cgroup(path: str, pressure: bool = False) -> Optional[dict]:
    """Счётчики группы; None — группы нет (юнит остановлен или не запущен)."""
    cur = _read(os.path.join(path, "memory.current"))
    if cur is None:
        return None
    out = {"memory_current": int(cur)}
    peak = _read(os.path.join(path, "memory.peak"))
    if peak is not None:
        out["memory_peak"] = int(peak)
    mx = (_read(os.path.join(path, "memory.max")) or "").strip()
    out["memory_max"] = None if mx in ("", "max") else int(mx)
    out["events"] = parse_flat(_read(os.path.join(path, "memory.events")) or "")
    stat = parse_flat(_read(os.path.join(path, "memory.stat")) or "")
    out["stat"] = {k: stat[k] for k in MEM_STAT_KEYS if k in stat}
    out["cpu"] = parse_flat(_read(os.path.join(path, "cpu.stat")) or "")
    pids = _read(os.path.join(path, "pids.current"))
    if pids is not None:
        out["pids"] = int(pids)
    if pressure:
        out["psi_mem"] = parse_pressure(_read(os.path.join(path, "memory.pressure")) or "")
        out["psi_cpu"] = parse_pressure(_read(os.path.join(path, "cpu.pressure")) or "")
    return out


def procs(path: str, proc_root: str = "/proc") -> List[dict]:
    """Процессы группы и её вложенных групп."""
    pids: List[int] = []
    for d, _, files in os.walk(path):
        if "cgroup.procs" in files:
            pids += [int(x) for x in (_read(os.path.join(d, "cgroup.procs")) or "").split()]
    out = []
    for pid in sorted(set(pids)):
        stat = _read(os.path.join(proc_root, str(pid), "stat"))
        smaps = _read(os.path.join(proc_root, str(pid), "smaps_rollup"))
        if stat is None or smaps is None:
            continue                      # процесс вышел между чтениями
        rec = {"pid": pid}
        rec.update(parse_proc_stat(stat))
        rec.update(parse_smaps_rollup(smaps))
        out.append(rec)
    return out


def system(proc_root: str = "/proc") -> dict:
    out: Dict[str, object] = {}
    load = (_read(os.path.join(proc_root, "loadavg")) or "").split()
    if load:
        out["load1"], out["load5"] = float(load[0]), float(load[1])
    for line in (_read(os.path.join(proc_root, "meminfo")) or "").splitlines():
        if line.startswith("MemAvailable:"):
            out["mem_available_kib"] = int(line.split()[1])
    st = _read(os.path.join(proc_root, "stat"))
    if st:
        out["cpu_ticks"] = parse_proc_cpu(st)
    return out


PAGE = 4096
PFN_MASK = (1 << 55) - 1
MAX_VMA = 64 << 30                    # отображения больше — резервы адресного пространства, не память


def pfns(pid: int, proc_root: str = "/proc") -> Set[int]:
    """Номера физических страниц, резидентных у процесса (нужен root: без CAP_SYS_ADMIN ядро отдаёт PFN 0)."""
    out: Set[int] = set()
    maps = _read(os.path.join(proc_root, str(pid), "maps"))
    if maps is None:
        return out
    try:
        with open(os.path.join(proc_root, str(pid), "pagemap"), "rb") as pm:
            for line in maps.splitlines():
                lo, _, hi = line.split(" ", 1)[0].partition("-")
                lo_i, hi_i = int(lo, 16), int(hi, 16)
                if hi_i - lo_i > MAX_VMA or lo_i >= 1 << 48:
                    continue
                pm.seek(lo_i // PAGE * 8)
                buf = pm.read((hi_i - lo_i) // PAGE * 8)
                arr = array("Q")
                arr.frombytes(buf[:len(buf) // 8 * 8])
                out.update(e & PFN_MASK for e in arr if e >> 63)
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return out
    return out


def unique_kib(pages: Iterable[Set[int]]) -> Optional[int]:
    """КиБ уникальных страниц объединения; None — PFN не видны (не root)."""
    union: Set[int] = set()
    for p in pages:
        union |= p
    if union and union <= {0}:
        return None
    return len(union - {0}) * PAGE // 1024


def unique_snapshot(units_doc: dict, proc_root: str = "/proc") -> dict:
    """Уникальные страницы каждого юнита и всего slice, а также суммы PSS и RSS тех же процессов в тот же момент."""
    per_unit, all_pages, pss, rss = {}, [], 0, 0
    for name, u in units_doc.get("units", {}).items():
        ps = procs(u["cgroup"], proc_root)
        pages = [pfns(p["pid"], proc_root) for p in ps]
        all_pages += pages
        per_unit[name] = unique_kib(pages)
        pss += sum(p.get("pss_kib", 0) for p in ps)
        rss += sum(p.get("rss_kib", 0) for p in ps)
    return {"t_wall": time.time(), "units_kib": per_unit, "slice_unique_kib": unique_kib(all_pages),
            "slice_pss_kib": pss, "slice_rss_kib": rss}


def sample(units_doc: dict, proc_root: str = "/proc") -> dict:
    units = {}
    for name, u in units_doc.get("units", {}).items():
        cg = cgroup(u["cgroup"])
        units[name] = {"cg": cg, "procs": procs(u["cgroup"], proc_root) if cg is not None else []}
    return {"type": "sample", "t_wall": time.time(), "t_mono": time.monotonic(),
            "slice": cgroup(units_doc["slice"], pressure=True), "units": units, "sys": system(proc_root)}


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--units", required=True, help="units.json прогона (stack.py)")
    p.add_argument("--out", required=True, help="JSONL")
    p.add_argument("--interval", type=float, default=5.0, help="период, с")
    p.add_argument("--duration", type=float, default=0.0, help="длительность, с (0 — до сигнала)")
    a = p.parse_args(argv)
    if not 0.5 <= a.interval <= 60 or a.duration < 0:
        p.error("--interval 0,5–60 с, --duration ≥ 0")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    t0 = time.monotonic()
    with open(a.out, "a", encoding="utf-8") as out:
        out.write(json.dumps({"type": "header", "interval_s": a.interval, "clk_tck": os.sysconf("SC_CLK_TCK"),
                              "page_size": os.sysconf("SC_PAGE_SIZE"), "ncpu": os.cpu_count(),
                              "t_wall": time.time(), "t_mono": t0}) + "\n")
        out.flush()
        while not stop.is_set():
            try:
                with open(a.units, encoding="utf-8") as f:
                    doc = json.load(f)
                out.write(json.dumps(sample(doc)) + "\n")
                out.flush()
            except (OSError, ValueError) as e:
                print("collect: %s" % e, file=sys.stderr)
            if a.duration and time.monotonic() - t0 >= a.duration:
                break
            stop.wait(a.interval - (time.monotonic() - t0) % a.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
