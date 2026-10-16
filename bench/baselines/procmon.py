"""Память и CPU дерева процессов аналога во время прогона.

* На Mac — psutil (есть в .venv стенда): RSS дерева (корень + потомки: MCP-серверы stdio, подпроцессы exec),
  CPU user+sys живых процессов и завершившихся потомков (cpu_times().children_*), число потоков.
* В ВМ — sysmon-linux-arm64 (наш статический сэмплер, sysmon/bin): RSS и PSS по smaps_rollup, CPU, IO.
  Запускается в ВМ рядом с агентом, файл забирается после прогона.

Строки обоих источников приводятся к одному виду (rows): ts_wall, rss_kib (дерево), root_rss_kib, pss_kib
(если есть), cpu_s (дерево, накопленное), procs, threads.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class PsutilSampler:
    def __init__(self, root_pids: list[int], out_path: Path, interval_s: float = 0.5):
        self.root_pids = list(root_pids)
        self.out_path = Path(out_path)
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="procmon", daemon=True)
        self.rows: list[dict] = []

    def start(self) -> "PsutilSampler":
        self._thread.start()
        return self

    def stop(self) -> list[dict]:
        self._stop.set()
        self._thread.join(timeout=5)
        return self.rows

    def sample(self) -> dict | None:
        import psutil  # noqa: PLC0415

        procs = []
        for pid in self.root_pids:
            try:
                p = psutil.Process(pid)
                procs.append((p, True))
                procs.extend((c, False) for c in p.children(recursive=True))
            except psutil.Error:
                continue
        if not procs:
            return None
        rss = root_rss = 0
        cpu = 0.0
        threads = 0
        names: dict[str, int] = {}
        for p, is_root in procs:
            try:
                with p.oneshot():
                    mi = p.memory_info()
                    ct = p.cpu_times()
                    c = ct.user + ct.system + (ct.children_user + ct.children_system if is_root else 0.0)
                    rss += mi.rss
                    if is_root:
                        root_rss += mi.rss
                    cpu += c
                    threads += p.num_threads()
                    nm = p.name()
                    names[nm] = names.get(nm, 0) + mi.rss // 1024
            except psutil.Error:
                continue
        # завершившиеся потомки входят в children_* корня после wait() (Go os/exec и Python subprocess ждут)
        return {"ts_wall": time.time(), "rss_kib": rss // 1024, "root_rss_kib": root_rss // 1024,
                "cpu_s": round(cpu, 4), "procs": len(procs), "threads": threads,
                "by_name_kib": names}

    def _loop(self) -> None:
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.out_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "header", "source": "psutil", "interval_s": self.interval_s,
                                "root_pids": self.root_pids}) + "\n")
            while not self._stop.is_set():
                row = self.sample()
                if row is not None:
                    self.rows.append(row)
                    f.write(json.dumps(dict(row, type="sample"), ensure_ascii=False) + "\n")
                    f.flush()
                self._stop.wait(self.interval_s)


def load_rows(path: Path) -> list[dict]:
    """Строки psutil-сэмплера или sysmon.v1 → общий вид."""
    rows = []
    if not Path(path).exists():
        return rows
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "sample":
            continue
        if "tree" in d:      # sysmon.v1
            tree, root = d.get("tree") or {}, d.get("root") or {}
            rows.append({"ts_wall": d.get("ts_wall"), "rss_kib": tree.get("rss_kib"),
                         "root_rss_kib": root.get("rss_kib"), "pss_kib": tree.get("pss_kib"),
                         "cpu_s": tree.get("cpu_s"), "procs": tree.get("procs"), "threads": tree.get("threads")})
        else:
            rows.append(d)
    return rows


def summarize_rows(rows: list[dict], wall_from: float | None = None, wall_to: float | None = None) -> dict:
    sel = [r for r in rows if r.get("rss_kib") is not None
           and (wall_from is None or r["ts_wall"] >= wall_from) and (wall_to is None or r["ts_wall"] <= wall_to)]
    if not sel:
        return {"samples": 0}

    def stats(key: str) -> dict | None:
        vals = [r[key] for r in sel if r.get(key) is not None]
        if not vals:
            return None
        vals_s = sorted(vals)
        return {"min": vals_s[0], "median": vals_s[len(vals_s) // 2], "max": vals_s[-1], "last": vals[-1]}

    cpu = [r["cpu_s"] for r in sel if r.get("cpu_s") is not None]
    return {"samples": len(sel), "rss_kib": stats("rss_kib"), "root_rss_kib": stats("root_rss_kib"),
            "pss_kib": stats("pss_kib"), "threads": stats("threads"), "procs": stats("procs"),
            "cpu_s_wall_window": round(cpu[-1] - cpu[0], 3) if len(cpu) >= 2 else None,
            "wall_s": round(sel[-1]["ts_wall"] - sel[0]["ts_wall"], 3)}
